"""Tests for measuring VOX with the receiving radio's squelch open.

    python3 -m unittest discover -s tools -p 'test_*.py'

Three earlier methods each returned a believable VOX hang that was an
artefact -- 10 ms, then "longer than 2 s", then "longer than 6 s" -- and two
of those got as far as real hardware before anyone knew. So this one is
required to recover known values from a simulated receiver first, and the
simulator models the exact thing the others could not see: a transmitter that
is keyed but unmodulated, which is SILENT.
"""

import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tone_test as tt

FS = 48000


def open_squelch_recording(attack_s, hang_s, tone_hz=tt.DEFAULT_TONE_HZ,
                           hiss=0.30, quiet=0.01, seed=1, fs=FS):
    """What an open-squelch receiver outputs.

    The whole point is the third state. A closed squelch gives silence when
    there is no carrier, which is indistinguishable from the silence of a
    keyed-but-unmodulated transmitter -- that ambiguity is what defeated every
    previous method. Open, the un-keyed state is LOUD, so the quiet stretch
    after the tone can only be the transmitter still holding the channel.
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
        if key_on <= t < key_off:
            if key_on <= t < tone_end:
                out.append(0.4 * math.sin(2.0 * math.pi * tone_hz * i / fs)
                           + rng.gauss(0.0, quiet))
            else:
                out.append(rng.gauss(0.0, quiet))      # keyed, unmodulated
        else:
            out.append(rng.gauss(0.0, hiss))           # squelch open, no carrier
    return out


class TestRecovery(unittest.TestCase):

    def _check(self, attack, hang, tol=0.04):
        r = tt.analyze_open_squelch(open_squelch_recording(attack, hang), FS)
        self.assertIsNone(r["problem"], r["problem"])
        self.assertAlmostEqual(r["attack_s"], attack, delta=tol,
                               msg="attack %.3f want %.3f"
                                   % (r["attack_s"], attack))
        self.assertAlmostEqual(r["hang_s"], hang, delta=tol,
                               msg="hang %.3f want %.3f"
                                   % (r["hang_s"], hang))

    def test_short_hang(self):
        self._check(0.06, 0.20)

    def test_typical_hang(self):
        self._check(0.06, 0.80)

    def test_long_hang(self):
        self._check(0.10, 1.80)

    def test_near_zero_hang_is_reported_as_ambiguous(self):
        # A transmitter that drops at once leaves no quiet period -- which
        # looks exactly like levels too close to show one. This method cannot
        # separate those, and says so rather than picking one. Every earlier
        # method picked one, and picked wrong.
        r = tt.analyze_open_squelch(open_squelch_recording(0.06, 0.01), FS)
        self.assertIsNotNone(r["problem"])
        self.assertIn("no quiet state", r["problem"])
        self.assertIsNone(r["hang_s"])
        self.assertIsNotNone(r["attack_s"], "attack is still measurable")

    def test_hang_is_not_confused_by_the_attack(self):
        r = tt.analyze_open_squelch(open_squelch_recording(0.30, 0.30), FS)
        self.assertAlmostEqual(r["hang_s"], 0.30, delta=0.04)

    def test_two_different_hangs_are_told_apart(self):
        # The failure of the two-burst method was that a 50 ms effect competed
        # with 85 ms of scatter. Here the difference must be unmistakable.
        a = tt.analyze_open_squelch(open_squelch_recording(0.06, 0.20), FS)
        b = tt.analyze_open_squelch(open_squelch_recording(0.06, 1.20), FS)
        self.assertGreater(b["hang_s"] - a["hang_s"], 0.9)


class TestFailureModes(unittest.TestCase):

    def test_a_closed_squelch_is_reported_not_measured(self):
        # Without hiss there is no way to see the transmitter drop, and the
        # tool must say so rather than report the quiet as an enormous hang.
        rec = open_squelch_recording(0.06, 0.5, hiss=0.0005)
        r = tt.analyze_open_squelch(rec, FS)
        self.assertIsNotNone(r["problem"])
        self.assertIn("squelch is not open", r["problem"])
        self.assertIsNone(r["hang_s"])

    def test_no_tone_is_reported_as_a_broken_path(self):
        rng = random.Random(5)
        rec = [rng.gauss(0.0, 0.3) for _ in range(int(4.5 * FS))]
        r = tt.analyze_open_squelch(rec, FS)
        self.assertIsNotNone(r["problem"])
        self.assertIn("no tone", r["problem"])

    def test_hiss_that_never_returns_is_flagged(self):
        # Hang longer than the recording, or a receiver that stopped hissing.
        # Either way the number is a floor, not a measurement.
        rec = open_squelch_recording(0.06, tt.TAIL_SILENCE_S + 1.0)
        r = tt.analyze_open_squelch(rec, FS)
        self.assertIsNotNone(r["problem"])
        self.assertIn("never came back", r["problem"])

    def test_report_prints_no_numbers_without_a_tone(self):
        rng = random.Random(6)
        rec = [rng.gauss(0.0, 0.3) for _ in range(int(4.5 * FS))]
        text = tt.report_open_squelch(tt.analyze_open_squelch(rec, FS))
        self.assertIn("FAILED", text)
        self.assertNotIn("VOX hang", text)


if __name__ == "__main__":
    unittest.main()


class TestLevelsTooLow(unittest.TestCase):

    def test_a_tone_barely_above_the_hiss_is_refused(self):
        # The first real open-squelch run looked like this: hiss 0.053, the
        # loudest thing in the file 0.125, everything after the tone 0.033.
        # Three states that must be told apart, all within a whisker. It
        # reported "hang 0 ms" -- which is a number, not a measurement.
        rec = open_squelch_recording(0.06, 0.5, hiss=0.15, quiet=0.10)
        r = tt.analyze_open_squelch(rec, FS)
        self.assertIsNotNone(r["problem"])
        self.assertIn("no quiet state", r["problem"])
        self.assertIsNone(r["hang_s"])

    def test_report_prints_no_numbers_when_levels_are_too_low(self):
        rec = open_squelch_recording(0.06, 0.5, hiss=0.15, quiet=0.10)
        text = tt.report_open_squelch(tt.analyze_open_squelch(rec, FS))
        self.assertNotIn("per-transmission overhead", text)

    def test_good_levels_still_measure(self):
        # The guard must not reject a healthy recording.
        r = tt.analyze_open_squelch(open_squelch_recording(0.06, 0.5), FS)
        self.assertIsNone(r["problem"], r["problem"])
        self.assertAlmostEqual(r["hang_s"], 0.5, delta=0.04)


class TestToneSpanIsLevelIndependent(unittest.TestCase):
    """The measured tone length must not move with the recording gain.

    Two runs of the same 1.000 s tone through the same radio reported 440 ms
    and 0 ms of VOX attack. Nothing about the radio changed between them; the
    recording conditions did. The span was thresholded against a fixed fraction
    of the whole file's peak, so it walked -- one run measured the tone at
    1.010 s, longer than the tone that was played, which is the tell that the
    span rather than the radio was being measured.

    Be clear about what these tests do and do not cover. The two
    `_refine_tone_span` cases below are real: they fail against the old
    behaviour. `test_same_attack_across_tone_to_hiss_ratios` does NOT -- the
    simulator switches the tone on and off as a clean rectangle, so every
    sane threshold agrees on where the edges are, and the old code passes it
    too. It is kept as a property that must hold, not as a reproduction of the
    original fault. Reproducing that needs the ramps, de-emphasis ringing and
    squelch tail of a real FM receiver, which this simulator does not model;
    the standing check on it is the hardware recording itself, which the fix
    takes from 0 ms to 80 ms and which agrees with a hand measurement of the
    same file.
    """

    def test_same_attack_across_tone_to_hiss_ratios(self):
        # Overall gain is the wrong knob: the old threshold was a fraction of
        # the file's peak, so scaling everything scaled it too and nothing
        # moved. What actually varied between the two hardware runs was how
        # loud the tone sat against the hiss -- FM capture sets the recovered
        # audio level independently of the open-squelch noise -- and that is
        # what walks the span.
        attack, hang = 0.08, 3.2
        seen = {}
        for hiss in (0.06, 0.15, 0.34):
            rec = open_squelch_recording(attack, hang, hiss=hiss)
            r = tt.analyze_open_squelch(rec, FS)
            self.assertIsNone(r["problem"], "hiss %.2f: %s" % (hiss, r["problem"]))
            self.assertIsNotNone(r["attack_s"], "hiss %.2f: attack refused" % hiss)
            seen[hiss] = r["attack_s"]
        for hiss, got in seen.items():
            self.assertAlmostEqual(got, attack, delta=0.04,
                                   msg="hiss %.2f: attack %.3f want %.3f"
                                       % (hiss, got, attack))
        spread = max(seen.values()) - min(seen.values())
        self.assertLess(spread, 0.02,
                        "attack moved with tone-to-hiss ratio: %r" % (seen,))

    def test_widens_a_span_the_ratio_test_broke_in_half(self):
        absolute = [0.001] * 10 + [1.0] * 20 + [0.001] * 10
        self.assertEqual(tt._refine_tone_span(absolute, 10, 14), (10, 29))

    def test_contracts_a_span_that_ran_out_into_the_noise(self):
        absolute = [0.001] * 10 + [1.0] * 20 + [0.001] * 10
        self.assertEqual(tt._refine_tone_span(absolute, 5, 35), (10, 29))


class TestToneLongerThanPlayed(unittest.TestCase):
    """A tone cannot come back longer than it went out.

    When it appears to, the honest answer is that attack is unavailable --
    clamping the subtraction at zero is how a broken span reports itself as a
    perfect radio.
    """

    def _overrun(self):
        return tt.analyze_open_squelch(open_squelch_recording(0.05, 1.0), FS,
                                       played_tone_s=0.5)

    def test_attack_is_refused_not_zeroed(self):
        r = self._overrun()
        self.assertIsNone(r["attack_s"])
        self.assertIn("cannot be derived", r["attack_problem"])

    def test_hang_still_measured(self):
        # Hang comes from the hiss returning and owes nothing to the tone
        # length, so a bad attack must not cost us a good hang.
        r = self._overrun()
        self.assertAlmostEqual(r["hang_s"], 1.0, delta=0.04)

    def test_report_says_not_measured(self):
        text = tt.report_open_squelch(self._overrun())
        self.assertIn("NOT MEASURED", text)
        self.assertIn("at least", text)


class TestDeadCaptureIsRefused(unittest.TestCase):
    """A file of one repeated sample is not a radio that stayed silent.

    pw-record given a --target that no longer exists writes exactly that. On
    this bench the built-in mic's name lost a ".2" suffix when another sound
    card was plugged in, and the resulting dead file was read downstream as a
    transmitter that never keyed -- blaming the squelch, the frequency and the
    wiring, all three of which were fine.
    """

    def test_a_constant_recording_is_named_as_a_dead_device(self):
        r = tt.analyze_open_squelch([15124] * (5 * FS), FS)
        self.assertIsNone(r["tone"])
        self.assertIn("dead", r["problem"])

    def test_digital_silence_is_also_refused(self):
        r = tt.analyze_open_squelch([0] * (5 * FS), FS)
        self.assertIsNone(r["tone"])
        self.assertIn("dead", r["problem"])

    def test_it_sends_you_to_the_device_list_not_the_radio(self):
        r = tt.analyze_open_squelch([15124] * (5 * FS), FS)
        self.assertIn("pactl", r["problem"])

    def test_a_real_recording_still_passes(self):
        r = tt.analyze_open_squelch(open_squelch_recording(0.06, 0.5), FS)
        self.assertIsNone(r["problem"], r["problem"])


class TestDevicePreflight(unittest.TestCase):

    SOURCES = ["alsa_input.pci-0000_03_00.6.HiFi__Mic1__source",
               "alsa_input.usb-046d_Brio_105_2439APSK3PF8-02.mono-fallback"]
    SINKS = ["alsa_output.usb-C-Media_Electronics_Inc._USB_Audio_Device-00"
             ".analog-stereo"]

    def test_a_known_source_passes(self):
        tt.check_device(self.SOURCES[0], "sources", available=self.SOURCES)

    def test_a_known_sink_passes(self):
        tt.check_device(self.SINKS[0], "sinks", available=self.SINKS)

    def test_the_stale_name_that_actually_bit_us_is_refused(self):
        with self.assertRaises(RuntimeError) as cm:
            tt.check_device("alsa_input.pci-0000_03_00.6.HiFi__Mic1__source.2",
                            "sources", available=self.SOURCES)
        self.assertIn("does not exist", str(cm.exception))

    def test_the_error_lists_what_is_there(self):
        with self.assertRaises(RuntimeError) as cm:
            tt.check_device("nope", "sources", available=self.SOURCES)
        for name in self.SOURCES:
            self.assertIn(name, str(cm.exception))

    def test_an_empty_listing_does_not_block_a_working_setup(self):
        tt.check_device("anything", "sources", available=[])
