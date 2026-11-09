"""Desktop tests for the tone test's analysis.

    python3 -m unittest discover -s tools -p 'test_*.py'

The point of these: the analysis has to be trustworthy BEFORE it is pointed at
a radio, because on the day there is no independent way to check it. So it is
fed synthetic recordings with VOX behaviour chosen in advance and required to
recover the numbers it was given.
"""

import json
import math
import os
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tone_test as tt

FS = 48000


def fake_recording(attack_s, hang_s, tone_hz=tt.DEFAULT_TONE_HZ,
                   noise=0.002, carrier_noise=0.05, seed=1, fs=FS):
    """What a receiving radio would produce for a given VOX behaviour.

    Played: 0.5 s silence, 1.0 s tone, 3.0 s silence. The transmitter is off
    until attack_s into the tone and stays on hang_s past the end of it, so
    the carrier -- audible as a raised noise floor -- brackets a tone that is
    attack_s shorter than the one played.
    """
    rng = random.Random(seed)
    n = int((tt.LEAD_SILENCE_S + tt.TONE_S + tt.TAIL_SILENCE_S) * fs)
    tone_start = tt.LEAD_SILENCE_S
    tone_end = tone_start + tt.TONE_S
    key_on = tone_start + attack_s
    key_off = tone_end + hang_s

    out = []
    for i in range(n):
        t = i / float(fs)
        x = rng.gauss(0.0, noise)
        if key_on <= t < key_off:
            x += rng.gauss(0.0, carrier_noise)          # open squelch
            if key_on <= t < tone_end:
                x += 0.4 * math.sin(2.0 * math.pi * tone_hz * i / fs)
        out.append(x)
    return out


class TestGoertzel(unittest.TestCase):

    def test_it_finds_the_tone_it_is_looking_for(self):
        n = 4800
        sig = [0.5 * math.sin(2 * math.pi * 1500 * i / FS) for i in range(n)]
        self.assertGreater(tt.goertzel(sig, FS, 1500), 0.1)

    def test_it_ignores_a_tone_it_is_not(self):
        n = 4800
        sig = [0.5 * math.sin(2 * math.pi * 700 * i / FS) for i in range(n)]
        self.assertLess(tt.goertzel(sig, FS, 1500), 0.02)

    def test_it_finds_a_tone_buried_in_noise(self):
        rng = random.Random(2)
        n = 4800
        sig = [0.2 * math.sin(2 * math.pi * 1500 * i / FS) + rng.gauss(0, 0.3)
               for i in range(n)]
        self.assertGreater(tt.goertzel(sig, FS, 1500),
                           tt.goertzel(sig, FS, 900) * 2)


class TestSpanDetection(unittest.TestCase):

    def test_a_single_click_does_not_start_a_transmission(self):
        # An FM link below threshold produces impulses constantly. One must
        # not be read as the carrier opening, or every measurement taken in
        # marginal conditions is wrong.
        levels = [0.01] * 50
        levels[20] = 5.0
        start, end, _ = tt.find_span(levels, 0.01)
        self.assertIsNone(start)

    def test_a_sustained_rise_does(self):
        levels = [0.01] * 20 + [5.0] * 20 + [0.01] * 20
        start, end, _ = tt.find_span(levels, 0.01)
        self.assertIsNotNone(start)
        self.assertTrue(18 <= start <= 22, start)
        self.assertTrue(38 <= end <= 42, end)


class TestVoxRecovery(unittest.TestCase):
    """Feed it known VOX behaviour; require the numbers back."""

    def _check(self, attack, hang, tol=0.03):
        r = tt.analyze(fake_recording(attack, hang), FS)
        self.assertIsNone(r["problem"], r["problem"])
        self.assertAlmostEqual(r["attack_s"], attack, delta=tol,
                               msg="attack: got %.3f want %.3f"
                                   % (r["attack_s"], attack))
        self.assertAlmostEqual(r["hang_s"], hang, delta=tol,
                               msg="hang: got %.3f want %.3f"
                                   % (r["hang_s"], hang))

    def test_typical_vox(self):
        self._check(0.10, 1.00)

    def test_fast_vox(self):
        self._check(0.05, 0.30)

    def test_slow_vox(self):
        self._check(0.25, 2.00)

    def test_loopback_has_neither(self):
        # The control: adapter wired to itself, no radio. Both must be ~0, and
        # if they are not the audio path is at fault and no radio measurement
        # afterwards means anything.
        self._check(0.0, 0.0)

    def test_a_long_hang_is_reported_as_larger_than_a_short_one(self):
        short = tt.analyze(fake_recording(0.1, 0.4), FS)["hang_s"]
        long_ = tt.analyze(fake_recording(0.1, 1.8), FS)["hang_s"]
        self.assertGreater(long_ - short, 1.2)


class TestFailureModes(unittest.TestCase):
    """On the day, a failure has to say what is wrong rather than print a
    plausible number."""

    def test_silence_is_reported_as_a_broken_path_not_as_zero_vox(self):
        rng = random.Random(3)
        r = tt.analyze([rng.gauss(0, 0.001) for _ in range(FS * 4)], FS)
        self.assertIsNotNone(r["problem"])
        self.assertIn("no tone", r["problem"])
        self.assertIsNone(r["attack_s"])

    def test_the_wrong_tone_frequency_is_reported_as_a_broken_path(self):
        r = tt.analyze(fake_recording(0.1, 1.0), FS, tone_hz=2400.0)
        self.assertIsNotNone(r["problem"])

    def test_report_never_prints_a_vox_figure_it_does_not_have(self):
        rng = random.Random(4)
        text = tt.report(tt.analyze([rng.gauss(0, 0.001)
                                     for _ in range(FS * 4)], FS))
        self.assertIn("FAILED", text)
        self.assertNotIn("VOX attack", text)


class TestWavRoundTrip(unittest.TestCase):

    def test_a_written_signal_reads_back_the_same(self):
        import tempfile
        sig = tt.make_test_signal(8000)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            tt.write_wav(path, sig, 8000)
            back, fs = tt.read_wav(path)
            self.assertEqual(fs, 8000)
            self.assertEqual(len(back), len(sig))
            worst = max(abs(a - b) for a, b in zip(sig, back))
            self.assertLess(worst, 1e-3)
        finally:
            os.unlink(path)

    def test_the_generated_signal_has_the_shape_the_analysis_expects(self):
        sig = tt.make_test_signal(8000)
        self.assertAlmostEqual(len(sig) / 8000.0,
                               tt.LEAD_SILENCE_S + tt.TONE_S
                               + tt.TAIL_SILENCE_S, places=2)
        # tail must outlast any plausible VOX hang or it cannot be measured
        self.assertGreater(tt.TAIL_SILENCE_S, 2.0)


class TestResolvePwTarget(unittest.TestCase):
    """--target matches only a numeric object.serial, never a node name --
    this is what stands between a bench run and routing audio to whatever
    device happened to be default."""

    def _dump(self, nodes):
        return [
            {"type": "PipeWire:Interface:Node",
             "info": {"props": {"node.name": name, "object.serial": serial}}}
            for name, serial in nodes
        ]

    def test_a_number_passes_through_unresolved(self):
        with mock.patch.object(tt.subprocess, "check_output") as co:
            self.assertEqual(tt.resolve_pw_target("42"), "42")
            co.assert_not_called()

    def test_none_passes_through_unresolved(self):
        with mock.patch.object(tt.subprocess, "check_output") as co:
            self.assertIsNone(tt.resolve_pw_target(None))
            co.assert_not_called()

    def test_a_name_resolves_to_its_serial(self):
        dump = self._dump([("alsa_input.usb-Digirig", "37")])
        with mock.patch.object(tt.subprocess, "check_output",
                               return_value=json.dumps(dump)):
            self.assertEqual(tt.resolve_pw_target("alsa_input.usb-Digirig"),
                             "37")

    def test_the_most_recently_created_duplicate_wins(self):
        # Stale nodes from earlier reconnects share a name; the live one is
        # whichever has the highest serial.
        dump = self._dump([("alsa_input.usb-Digirig", "12"),
                           ("alsa_input.usb-Digirig", "58"),
                           ("alsa_input.usb-Digirig", "40")])
        with mock.patch.object(tt.subprocess, "check_output",
                               return_value=json.dumps(dump)):
            self.assertEqual(tt.resolve_pw_target("alsa_input.usb-Digirig"),
                             "58")

    def test_an_unmatched_name_passes_through_unresolved(self):
        dump = self._dump([("some_other_device", "9")])
        with mock.patch.object(tt.subprocess, "check_output",
                               return_value=json.dumps(dump)):
            self.assertEqual(tt.resolve_pw_target("alsa_input.usb-Digirig"),
                             "alsa_input.usb-Digirig")

    def test_pw_dump_failing_passes_the_name_through_rather_than_raising(self):
        with mock.patch.object(tt.subprocess, "check_output",
                               side_effect=OSError("no pw-dump")):
            self.assertEqual(tt.resolve_pw_target("alsa_input.usb-Digirig"),
                             "alsa_input.usb-Digirig")


if __name__ == "__main__":
    unittest.main()


class TestStartupClickAndUnmeasurableHang(unittest.TestCase):
    """Both of these got through the first time and produced a confident
    wrong answer on real hardware."""

    def test_a_startup_click_does_not_break_the_measurement(self):
        # Recording devices click as they open. It clips, it sits inside the
        # noise-floor window, and it was dragging the threshold above every
        # real signal in the file.
        rec = fake_recording(0.10, 1.00)
        for i in range(int(0.02 * FS)):
            rec[i] = 1.0 if i % 2 else -1.0
        r = tt.analyze(rec, FS)
        self.assertIsNone(r["problem"], r["problem"])
        self.assertAlmostEqual(r["attack_s"], 0.10, delta=0.03)
        self.assertAlmostEqual(r["hang_s"], 1.00, delta=0.03)

    def _no_distinct_carrier(self, seed=9):
        """A tone that is clearly there, in background loud enough that no
        carrier can be picked out of it.

        The honest shape of "hang cannot be measured". An audible tone is
        itself broadband energy, so a carrier cannot end BEFORE the tone does
        -- that scenario is physically impossible and testing it was testing
        nothing. What does happen is this: the background never steps up
        distinctly enough to time, so there is no carrier edge to subtract
        from and any hang figure would be invented.
        """
        rng = random.Random(seed)
        n = int(4.5 * FS)
        rec = []
        for i in range(n):
            t = i / float(FS)
            x = rng.gauss(0.0, 0.10)
            if tt.LEAD_SILENCE_S <= t < tt.LEAD_SILENCE_S + tt.TONE_S:
                x += 0.30 * math.sin(2 * math.pi * tt.DEFAULT_TONE_HZ * i / FS)
            rec.append(x)
        return rec

    def test_hang_is_refused_rather_than_clamped_when_unmeasurable(self):
        r = tt.analyze(self._no_distinct_carrier(), FS)
        self.assertIsNotNone(r["tone"], "tone should still be found")
        self.assertIsNotNone(r["attack_s"], "attack is still measurable")
        self.assertIsNone(r["hang_s"])
        self.assertIn("HANG WAS NOT MEASURED", r["problem"])

    def test_report_prints_no_hang_figure_when_it_has_none(self):
        text = tt.report(tt.analyze(self._no_distinct_carrier(seed=10), FS))
        self.assertIn("NOT MEASURED", text)
        self.assertNotIn("per-transmission overhead", text)


class TestSpuriousToneRejection(unittest.TestCase):
    """The bug the first radio run hit: a 1.0 s burst measured as 2.1 s."""

    def test_a_quiet_blip_long_after_the_tone_does_not_extend_it(self):
        # In near-silence the tone frequency and its neighbours are both
        # minute, so their quotient is noise and isolated windows score well
        # above the ratio threshold on energy a hundred times weaker than any
        # real tone. A span runs first-hit to last-hit, so one blip seconds
        # later swallowed the whole recording.
        rng = random.Random(11)
        n = int(4.5 * FS)
        rec = []
        for i in range(n):
            t = i / float(FS)
            x = rng.gauss(0.0, 0.002)
            if tt.LEAD_SILENCE_S <= t < tt.LEAD_SILENCE_S + tt.TONE_S:
                x += rng.gauss(0.0, 0.05)
                x += 0.4 * math.sin(2 * math.pi * tt.DEFAULT_TONE_HZ * i / FS)
            # a faint blip at the tone frequency, 2 s after everything ended
            if 3.6 <= t < 3.64:
                x += 0.004 * math.sin(2 * math.pi * tt.DEFAULT_TONE_HZ
                                      * i / FS)
            rec.append(x)
        r = tt.analyze(rec, FS)
        self.assertIsNotNone(r["tone"])
        self.assertLess(r["heard_tone_s"], tt.TONE_S * 1.2,
                        "the blip stretched the measured tone")

    def test_a_tone_heard_far_longer_than_played_is_flagged(self):
        # Belt and braces: if something does stretch it, say so rather than
        # publish the numbers.
        r = tt.analyze(fake_recording(0.1, 1.0), FS, played_tone_s=0.4)
        self.assertIsNotNone(r["problem"])
        self.assertIn("latching onto", r["problem"])
