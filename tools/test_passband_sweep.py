"""Calibration for tools/passband_sweep.py against clean synthetic signals.

The measurement discipline on this bench says to calibrate every probe against
a synthetic signal before trusting it against a radio -- this module is that
calibration, kept as a permanent test so the probe cannot silently drift.

Two shapes are checked, both built from the tool's OWN sweep generator and
analysed through the tool's OWN main() path (never a re-implementation):

  * a flat recording (the sweep recorded back perfectly) must come back flat
    to within a small tolerance, and no tone may read harmonic-dominated;
  * a recording with a KNOWN per-tone gain applied must come back with those
    gains, which is the probe's whole job -- the shape across frequency is
    the result, not the absolute level.

Also pinned: `align()` must find the played-file origin even when the front of
the recording is missing (a VOX cable keys late, and anchoring to first-audio
instead of searching once cost a whole measurement -- see align()'s docstring).
"""

import io
import math
import os
import sys
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fm_bench_audio as audio
import passband_sweep as ps


def _gain_for(hz, slope_db_per_oct=0.0, ref_hz=1000.0):
    """A known gain in dB at `hz`: 0 at/below ref, slope above it."""
    if hz <= ref_hz or slope_db_per_oct == 0.0:
        return 0.0
    return slope_db_per_oct * math.log2(hz / ref_hz)


def _sweep_wav(tmpdir, slope_db_per_oct=0.0):
    """The tool's own sweep, with a known per-tone gain, written to a WAV."""
    tones = list(ps.DEFAULT_TONES)
    samples = ps.build_sweep(tones, ps.FS)
    shaped = []
    lead_n = int(ps.LEAD_S * ps.FS)
    seg_n = int(ps.SEG_S * ps.FS)
    for i, x in enumerate(samples):
        if i < lead_n:
            shaped.append(x)
            continue
        tone_idx = (i - lead_n) // seg_n
        if tone_idx >= len(tones):
            shaped.append(x)
            continue
        g = _gain_for(tones[tone_idx], slope_db_per_oct)
        shaped.append(x * (10.0 ** (g / 20.0)))
    path = os.path.join(tmpdir, "sweep_%.0f.wav" % slope_db_per_oct)
    audio.write_wav(path, shaped, ps.FS, channels=2)
    return path, tones


def _analyse(path):
    """Run main()'s --analyze path on `path`; return (rows, report text).

    `rows` is what measure() returned: (hz, fundamental, rms, harmonics).
    """
    raw, fs = audio.read_wav(path)
    first = ps.find_start(raw, fs)
    off = ps.align(raw, ps.DEFAULT_TONES, fs)
    rows = ps.measure(raw, ps.DEFAULT_TONES, off, fs)
    buf = io.StringIO()
    with redirect_stdout(buf):
        ps.report(rows, out=buf)
    return rows, buf.getvalue()


class SweepBuildTest(unittest.TestCase):

    def test_build_sweep_has_the_expected_shape(self):
        samples = ps.build_sweep(ps.DEFAULT_TONES)
        total = (ps.LEAD_S + len(ps.DEFAULT_TONES) * ps.SEG_S) * ps.FS
        self.assertEqual(len(samples), total)
        # Every tone at the SAME amplitude -- the point is the difference in
        # what comes back, so the sent file must not carry a shape of its own.
        data = samples[int(ps.LEAD_S * ps.FS):]
        seg = int(ps.SEG_S * ps.FS)
        rms_per_tone = []
        for i in range(len(ps.DEFAULT_TONES)):
            block = data[i * seg:(i + 1) * seg]
            rms_per_tone.append(math.sqrt(
                sum(x * x for x in block) / len(block)))
        self.assertAlmostEqual(min(rms_per_tone), max(rms_per_tone), delta=1e-6)


class FlatCalibrationTest(unittest.TestCase):
    """A perfect loopback of the sweep must read flat."""

    @classmethod
    def setUpClass(cls):
        cls.path, cls.tones = _sweep_wav(os.environ.get("TMPDIR") or "/tmp")
        cls.rows, cls.report = _analyse(cls.path)

    def test_alignment_lands_on_the_origin(self):
        raw, fs = audio.read_wav(self.path)
        off = ps.align(raw, ps.DEFAULT_TONES, fs)
        # Perfect loopback: the played file's origin is where audio starts.
        first = ps.find_start(raw, fs)
        self.assertLess(abs(off - first), int(0.05 * fs))

    def test_every_tone_reads_within_a_db_of_flat(self):
        peak = max(g for _, g, _, _ in self.rows)
        for hz, g, _, _ in self.rows:
            db = 20 * math.log10(g / peak) if g > 0 else float("-inf")
            self.assertGreaterEqual(db, -1.0,
                                    "%d Hz read %.1f dB down on a flat "
                                    "recording" % (hz, db))

    def test_nothing_reads_harmonic_dominated(self):
        for hz, g, _, harm in self.rows:
            if harm:
                worst = max(harm.values())
                self.assertLessEqual(worst, g,
                                     "%d Hz read harmonic-dominated on a flat "
                                     "recording" % hz)

    def test_report_marks_the_modem_tones(self):
        for hz in ps._current_modem_tones():
            self.assertIn("%d " % hz, self.report)


class TiltCalibrationTest(unittest.TestCase):
    """A known -6 dB/octave tilt above 1000 Hz must be recovered."""

    SLOPE = -6.0

    @classmethod
    def setUpClass(cls):
        cls.path, cls.tones = _sweep_wav(os.environ.get("TMPDIR") or "/tmp",
                                         slope_db_per_oct=cls.SLOPE)
        cls.rows, cls.report = _analyse(cls.path)

    def test_the_tilt_is_recovered_at_checkpoints(self):
        peak = max(g for _, g, _, _ in self.rows)
        checkpoints = (300, 800, 1000, 1400, 1800, 2200, 2800, 3000)
        for hz, g, _, _ in self.rows:
            if hz not in checkpoints:
                continue
            measured = 20 * math.log10(g / peak) if g > 0 else float("-inf")
            expected = _gain_for(hz, self.SLOPE)  # peak is at/below 1000 Hz
            self.assertAlmostEqual(measured, expected, delta=1.5,
                                   msg="%d Hz read %+.1f dB, applied %+.1f"
                                       % (hz, measured, expected))

    def test_low_side_stays_flat(self):
        peak = max(g for _, g, _, _ in self.rows)
        for hz, g, _, _ in self.rows:
            if hz <= 1000.0:
                db = 20 * math.log10(g / peak) if g > 0 else float("-inf")
                self.assertGreaterEqual(db, -1.5,
                                        "%d Hz read %.1f dB down with no "
                                        "tilt applied there" % (hz, db))


class LateKeyingAlignmentTest(unittest.TestCase):
    """align() must survive a missing front -- the VOX-keys-late case.

    The real geometry (see play_and_record): the recorder starts 0.4 s before
    the play, and VOX keys the radio 0.3 s into the lead tone, so the
    recording is (pre-roll silence) + (key delay silence) + (signal with its
    first 0.3 s missing) + (tail silence). The played-file origin therefore
    sits 0.3 s BEFORE the first audio that arrives, and only a search finds
    it -- anchoring to first-audio is the bug that once cost a whole
    measurement. align() has a ~0.2 s plateau (its window is shorter than a
    tone), so the assertion is "within the plateau around the true origin",
    not an exact sample.
    """

    @classmethod
    def setUpClass(cls):
        path, _ = _sweep_wav(os.environ.get("TMPDIR") or "/tmp")
        raw, fs = audio.read_wav(path)
        chop = int(0.3 * fs)                 # the lead the VOX delay ate
        preroll = int(0.4 * fs)              # recorder started 0.4 s early
        key = int(0.3 * fs)                  # VOX key delay
        tail = int(0.4 * fs)                 # recorder runs past the play
        cls.recording = ([0] * preroll + [0] * key + raw[chop:]
                         + [0] * tail)
        cls.tones = ps.DEFAULT_TONES

    def test_origin_is_searched_not_anchored(self):
        off = ps.align(self.recording, ps.DEFAULT_TONES, ps.FS)
        first = ps.find_start(self.recording, ps.FS)
        # True origin = 0.4 s (the pre-roll). First audio = 0.7 s. Anchoring
        # to first audio returns 0.7; the search must land on the origin,
        # within its plateau.
        self.assertAlmostEqual(first / float(ps.FS), 0.7, delta=0.02)
        self.assertGreaterEqual(off / float(ps.FS), 0.25,
                                msg="align() landed before the origin")
        self.assertLessEqual(off / float(ps.FS), 0.55,
                             msg="align() landed after the origin's plateau")
        self.assertLess(off, first,
                        "align() returned first-audio, not the origin "
                        "behind the late key")

    def test_measurement_is_not_shifted_by_the_late_key(self):
        rows = ps.measure(self.recording, ps.DEFAULT_TONES,
                          ps.align(self.recording, ps.DEFAULT_TONES, ps.FS),
                          ps.FS)
        peak = max(g for _, g, _, _ in rows)
        for hz, g, _, _ in rows:
            db = 20 * math.log10(g / peak) if g > 0 else float("-inf")
            self.assertGreaterEqual(db, -1.0,
                                    "%d Hz mis-measured after a late key "
                                    "(%.1f dB)" % (hz, db))


class MainAnalyzeTest(unittest.TestCase):
    """main() --analyze runs end to end on a clean recording and exits 0."""

    def test_main_analyze_exits_zero_and_reaches_the_report(self):
        path, _ = _sweep_wav(os.environ.get("TMPDIR") or "/tmp")
        seen = []
        real_report = ps.report
        ps.report = lambda rows, modem_tones=None, out=None: seen.extend(rows)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = ps.main(["--analyze", path])
        finally:
            ps.report = real_report
        self.assertEqual(code, 0)
        self.assertEqual(len(seen), len(ps.DEFAULT_TONES))
        self.assertIn("aligned", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
