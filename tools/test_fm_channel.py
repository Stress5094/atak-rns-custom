"""Desktop tests for the FM channel model.

    python3 -m unittest discover -s tools -p 'test_*.py' -v

No numpy, no radio, no Android -- these run anywhere python3 does.

Every test here asserts a property of a REAL FM link, not of the code: the
emphasis pair cancels, the passband is where the passband is, the limiter
limits, and the cliff is a cliff. If a change makes one of these fail, the
model has stopped describing a radio.
"""

import math
import random
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fm_channel as fc

FS = 48000


def tone(hz, n=4800, amp=0.5, fs=FS):
    return [amp * math.sin(2.0 * math.pi * hz * i / fs) for i in range(n)]


def settled(samples, skip=2000):
    """Drop the filter start-up transient. Every filter here begins with zero
    history, so the first few hundred samples are ramping and say nothing
    about steady-state response."""
    return samples[skip:]


def goertzel(samples, hz, fs):
    """Energy at one frequency. Used to read two tones out of ONE burst,
    which is the only way to see a tilt through stage_drive's normalisation."""
    n = len(samples)
    k = int(0.5 + n * hz / float(fs))
    w = 2.0 * math.pi * k / n
    coeff = 2.0 * math.cos(w)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s2, s1 = s1, s0
    return s1 * s1 + s2 * s2 - coeff * s1 * s2


def two_tone(a_hz, b_hz, n=4000, amp=0.35, fs=FS):
    return [amp * (math.sin(2.0 * math.pi * a_hz * i / fs)
                   + math.sin(2.0 * math.pi * b_hz * i / fs))
            for i in range(n)]


def ratio_db(samples, a_hz, b_hz, fs):
    """How much louder a_hz is than b_hz, in dB.

    THE WINDOW IS TRIMMED TO A WHOLE NUMBER OF CYCLES OF BOTH TONES, and that
    is not tidiness. A Goertzel bin sits at k*fs/n; a tone landing halfway
    between two bins loses up to 3.9 dB to scalloping. The first version of
    this helper analysed whatever length it was handed -- 3600 samples, which
    puts 1500 Hz exactly on a bin and 2250 Hz exactly between two -- and
    reported 3.9 dB of tilt on a signal that had been through nothing at all.
    It was within a hair of being recorded as a bug in the channel model.
    """
    step = _bin_aligned_step(a_hz, b_hz, fs)
    n = (len(samples) // step) * step
    if n < step:
        raise ValueError("need at least %d samples to align both tones" % step)
    window = samples[:n]
    return 10.0 * math.log10(goertzel(window, a_hz, fs)
                             / goertzel(window, b_hz, fs))


def _bin_aligned_step(a_hz, b_hz, fs):
    """Smallest sample count on which both tones land on an exact bin."""
    def period(hz):
        return int(fs) // math.gcd(int(fs), int(hz))
    pa, pb = period(a_hz), period(b_hz)
    return pa * pb // math.gcd(pa, pb)


class TestEmphasis(unittest.TestCase):
    """Pre- and de-emphasis must be inverses, or the model invents a tilt the
    modem then gets blamed for."""

    def test_the_pair_cancels(self):
        sig = tone(1000)
        back = fc.de_emphasis(fc.pre_emphasis(sig, FS), FS)
        a, b = settled(sig), settled(back)
        self.assertAlmostEqual(fc.rms(a), fc.rms(b), places=3)

    def test_it_cancels_across_the_whole_passband(self):
        # The tilt this guards against is frequency-dependent, so one tone in
        # the middle proves nothing.
        for hz in (300, 800, 1500, 2200, 3000):
            sig = tone(hz)
            back = fc.de_emphasis(fc.pre_emphasis(sig, FS), FS)
            self.assertAlmostEqual(fc.rms(settled(sig)), fc.rms(settled(back)),
                                   places=3, msg="%d Hz" % hz)

    def test_pre_emphasis_actually_boosts_treble(self):
        # ~6 dB/octave. 3 kHz must come out well above 300 Hz, or the tau is
        # wrong -- the 75 us broadcast constant would nearly flatten this.
        low = fc.rms(settled(fc.pre_emphasis(tone(300), FS)))
        high = fc.rms(settled(fc.pre_emphasis(tone(3000), FS)))
        self.assertGreater(high / low, 5.0)

    def test_pre_emphasis_is_unity_at_dc(self):
        flat = [0.5] * 4800
        self.assertAlmostEqual(fc.rms(settled(fc.pre_emphasis(flat, FS))), 0.5,
                               places=3)


class TestPassband(unittest.TestCase):
    """300-3000 Hz gets through. This is the constraint that rules out modems
    designed for wider channels, so it has to be enforced honestly."""

    def test_midband_survives(self):
        sig = tone(1000)
        out = fc.bandpass(sig, FS)
        self.assertGreater(fc.rms(settled(out)) / fc.rms(settled(sig)), 0.8)

    def test_below_the_passband_is_gone(self):
        sig = tone(100)
        out = fc.bandpass(sig, FS)
        self.assertLess(fc.rms(settled(out)) / fc.rms(settled(sig)), 0.15)

    def test_above_the_passband_is_gone(self):
        sig = tone(6000)
        out = fc.bandpass(sig, FS)
        self.assertLess(fc.rms(settled(out)) / fc.rms(settled(sig)), 0.15)

    def test_the_edges_are_down_but_not_dead(self):
        # A modem may legitimately place a tone near an edge; the model must
        # not pretend the edge is a brick wall.
        for hz in (PASS_LOW, PASS_HIGH) if False else (300, 3000):
            r = (fc.rms(settled(fc.bandpass(tone(hz), FS)))
                 / fc.rms(settled(tone(hz))))
            self.assertTrue(0.2 < r < 0.95, "%d Hz ratio %.2f" % (hz, r))


class TestClipping(unittest.TestCase):

    def test_it_limits(self):
        self.assertLessEqual(max(abs(x) for x in fc.clip(tone(1000, amp=3.0))),
                             fc.DEFAULT_CLIP + 1e-9)

    def test_it_leaves_a_quiet_signal_alone(self):
        sig = tone(1000, amp=0.5)
        self.assertEqual(fc.clip(sig), sig)

    def test_clipping_generates_harmonics_in_band(self):
        # The reason over-driving hurts: a clipped 1 kHz tone puts energy at
        # 3 kHz, which is still inside the passband and lands on top of
        # whatever the modem is doing there.
        clipped = fc.clip(tone(1000, amp=3.0))
        # third harmonic present => bandpassing around it keeps real energy
        h3 = fc._highpass(fc._lowpass(clipped, FS, 3400), FS, 2600)
        self.assertGreater(fc.rms(settled(h3)), 0.05)


class TestDrive(unittest.TestCase):

    def test_correct_drive_reaches_the_limit_without_clipping(self):
        staged = fc.stage_drive(tone(1000, amp=0.01), drive=1.0)
        self.assertAlmostEqual(max(abs(x) for x in staged), fc.DEFAULT_CLIP,
                               places=6)
        self.assertEqual(fc.clip(staged), staged)

    def test_overdrive_clips(self):
        staged = fc.stage_drive(tone(1000, amp=0.01), drive=2.0)
        self.assertNotEqual(fc.clip(staged), staged)

    def test_silence_is_not_amplified_into_noise(self):
        # Dividing by a zero peak is the obvious bug here.
        self.assertEqual(fc.stage_drive([0.0] * 100), [0.0] * 100)


class TestNoiseAndTheCliff(unittest.TestCase):

    def test_high_snr_barely_touches_the_signal(self):
        sig = tone(1000)
        out = fc.add_noise(sig, 40.0, rng=random.Random(1))
        err = math.sqrt(sum((a - b) ** 2 for a, b in zip(sig, out)) / len(sig))
        self.assertLess(err / fc.rms(sig), 0.05)

    def test_low_snr_buries_it(self):
        sig = tone(1000)
        out = fc.add_noise(sig, 0.0, rng=random.Random(1))
        err = math.sqrt(sum((a - b) ** 2 for a, b in zip(sig, out)) / len(sig))
        self.assertGreater(err / fc.rms(sig), 0.5)

    def test_above_threshold_there_are_no_clicks(self):
        # The whole point of the threshold: above it, FM is quiet. If clicks
        # leak into the clean region every modem looks worse than it is.
        sig = tone(1000)
        out = fc.add_noise(sig, 25.0, rng=random.Random(7))
        excursion = max(abs(a - b) for a, b in zip(sig, out))
        self.assertLess(excursion, fc.rms(sig) * 2.0)

    def test_below_threshold_produces_impulses(self):
        sig = tone(1000)
        out = fc.add_noise(sig, 2.0, rng=random.Random(7))
        excursion = max(abs(a - b) for a, b in zip(sig, out))
        self.assertGreater(excursion, fc.rms(sig) * 2.0)

    def test_the_cliff_is_steeper_than_the_slope_above_it(self):
        # This is the property that makes the model worth having over plain
        # AWGN: degradation must ACCELERATE below the knee, not continue
        # linearly. Measured as error growth per dB on each side.
        sig = tone(1000)

        def err(snr):
            out = fc.add_noise(sig, snr, rng=random.Random(3))
            return math.sqrt(sum((a - b) ** 2
                                 for a, b in zip(sig, out)) / len(sig))

        above = err(14.0) - err(18.0)     # 4 dB above the knee
        below = err(2.0) - err(6.0)       # 4 dB below it
        self.assertGreater(below, above * 2.0)


class TestEndToEnd(unittest.TestCase):

    def test_a_clean_passband_tone_survives_the_whole_path(self):
        sig = tone(1500)
        out = fc.through(sig, FS, snr_db=40.0, rng=random.Random(1))
        self.assertGreater(fc.rms(settled(out)), 0.01)

    def test_out_of_band_does_not_survive_it(self):
        inband = fc.rms(settled(fc.through(tone(1500), FS, 40.0,
                                           rng=random.Random(1))))
        outband = fc.rms(settled(fc.through(tone(6000), FS, 40.0,
                                            rng=random.Random(1))))
        self.assertLess(outband, inband * 0.2)

    def test_overdrive_costs_something_measurable(self):
        # If this ever passes trivially, stage_drive has stopped mattering and
        # the model has quietly become forgiving.
        sig = [0.4 * math.sin(2 * math.pi * 800 * i / FS)
               + 0.4 * math.sin(2 * math.pi * 2400 * i / FS)
               for i in range(4800)]
        clean = fc.through(sig, FS, 40.0, drive=1.0, rng=random.Random(1))
        hot = fc.through(sig, FS, 40.0, drive=4.0, rng=random.Random(1))
        # Compare shape, not level: overdrive distorts, it does not just
        # change volume.
        def norm(s):
            r = fc.rms(settled(s))
            return [x / r for x in settled(s)] if r else settled(s)
        a, b = norm(clean), norm(hot)
        diff = math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)) / len(a))
        self.assertGreater(diff, 0.05)

    def test_it_is_deterministic_for_a_seeded_rng(self):
        # A sweep that cannot be repeated cannot be bisected when it changes.
        a = fc.through(tone(1500), FS, 8.0, rng=random.Random(42))
        b = fc.through(tone(1500), FS, 8.0, rng=random.Random(42))
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()


class TestVox(unittest.TestCase):
    """VOX keying. The two failures are independent: losing the front of a
    frame, and holding the channel after it."""

    def test_the_front_of_a_burst_is_lost(self):
        v = fc.Vox(attack_s=0.1, hang_s=0.5)
        heard, _ = v.transmit(tone(1500, n=FS), FS)
        self.assertEqual(heard[:int(0.1 * FS)], [0.0] * int(0.1 * FS))
        self.assertNotEqual(heard[int(0.15 * FS)], 0.0)

    def test_a_lead_in_tone_protects_the_data(self):
        # 0.1 s of lead-in, then the payload. The payload must survive intact.
        v = fc.Vox(attack_s=0.1, hang_s=0.5)
        lead = tone(1500, n=int(0.1 * FS))
        payload = tone(1500, n=int(0.4 * FS))
        heard, _ = v.transmit(lead + payload, FS)
        surviving = heard[len(lead):]
        self.assertAlmostEqual(fc.rms(surviving), fc.rms(payload), places=3)

    def test_hang_time_is_charged_as_airtime(self):
        v = fc.Vox(attack_s=0.1, hang_s=1.0)
        _, held = v.transmit(tone(1500, n=int(0.4 * FS)), FS)
        # 0.4 s of audio, keyed 0.1 s in, held 1.0 s past the end.
        self.assertAlmostEqual(held, 0.4 - 0.1 + 1.0, places=2)

    def test_hang_dominates_a_short_burst(self):
        # The whole reason bundling matters here: a small frame is almost
        # entirely overhead.
        v = fc.Vox(attack_s=0.1, hang_s=1.0)
        _, held = v.transmit(tone(1500, n=int(0.05 * FS)), FS)
        self.assertGreater(held, 0.9)

    def test_audio_too_quiet_never_keys_and_costs_nothing(self):
        v = fc.Vox(threshold=0.5)
        heard, held = v.transmit(tone(1500, amp=0.01, n=FS), FS)
        self.assertEqual(held, 0.0)
        self.assertEqual(set(heard), {0.0})

    def test_longer_hang_costs_strictly_more(self):
        burst = tone(1500, n=int(0.3 * FS))
        short = fc.Vox(hang_s=0.5).transmit(burst, FS)[1]
        long_ = fc.Vox(hang_s=2.0).transmit(burst, FS)[1]
        self.assertAlmostEqual(long_ - short, 1.5, places=2)


class TestMeasuredResponse(unittest.TestCase):
    """A Response is a MEASUREMENT, so these assert that it reproduces what
    was measured and refuses to speak about what was not."""

    FS8 = 8000                 # the rate the phone chains were measured at

    def test_it_reproduces_its_own_measured_points(self):
        """The calibration the ground rules demand: drive a clean synthetic
        tone through the filter and check it comes out at the dB the
        measurement recorded. A response that cannot reproduce its own inputs
        is a filter with an opinion."""
        r = fc.PIXEL_RX_20260930
        for hz, want_db in r.points:
            sig = tone(hz, n=4000, fs=self.FS8)
            out = r.apply(sig, self.FS8)
            got = 20.0 * math.log10(
                fc.rms(settled(out, 300)) / fc.rms(settled(sig, 300)))
            self.assertAlmostEqual(got, want_db, delta=0.5,
                                   msg="%.0f Hz wanted %+.2f dB, got %+.2f"
                                       % (hz, want_db, got))

    def test_the_tilt_is_a_real_tilt(self):
        """14 dB across the measured span is the finding. If a change flattens
        it, the model is back to flattering every candidate."""
        r = fc.PIXEL_RX_20260930
        self.assertGreater(r.gain_db(1500) - r.gain_db(3000), 12.0)

    def test_interpolation_is_logarithmic_not_linear_in_hz(self):
        """A roll-off is straight against log frequency. Interpolating
        linearly in Hz understates the middle of an octave, which is where
        tones actually sit.

        Asserted on a PROBE spanning two octaves, not on the Pixel curve: its
        measured points are 0.3 octaves apart, where log and linear
        interpolation differ by 0.15 dB and no honest tolerance can tell them
        apart. A test has to be given a span on which it could fail.
        """
        wide = fc.Response("octave-probe", ((1000.0, 0.0), (4000.0, -24.0)),
                           source="synthetic, for this test only")
        # Two octaves at 12 dB each puts the halfway point at 2000 Hz...
        self.assertAlmostEqual(wide.gain_db(2000.0), -12.0, places=6)
        # ...where interpolating linearly in Hz would put it at 2500.
        self.assertNotAlmostEqual(wide.gain_db(2500.0), -12.0, places=1)

    def test_an_unmeasured_frequency_is_refused_not_guessed(self):
        r = fc.PIXEL_RX_20260930
        with self.assertRaises(fc.OutOfBand):
            r.gain_db(1000.0)
        with self.assertRaises(fc.OutOfBand):
            r.gain_db(3500.0)

    def test_covers_answers_before_a_run_rather_than_during_it(self):
        r = fc.PIXEL_RX_20260930
        self.assertTrue(r.covers(1500, 1750, 2000, 2250))
        # today's desktop operating point, which this chain never measured
        self.assertFalse(r.covers(1000, 1400, 1800, 2200))

    def test_one_point_is_not_a_curve(self):
        with self.assertRaises(ValueError):
            fc.Response("one", ((2000.0, -3.0),), source="a reading")

    def test_a_response_must_carry_its_provenance(self):
        with self.assertRaises(ValueError):
            fc.Response("anon", ((1500.0, 0.0), (3000.0, -10.0)), source="")

    def test_the_oneplus_single_reading_is_not_dressed_up_as_a_curve(self):
        """7.3 dB apart from the Pixel at one tone on one day is the evidence
        that a response is per-chain. It is one point, so there must be no
        Response object for it to be mistaken for."""
        self.assertNotIn("oneplus", " ".join(fc.MEASURED_RESPONSES))
        self.assertLess(fc.ONEPLUS_RX_3000HZ_DB,
                        fc.PIXEL_RX_20260930.gain_db(3000.0))

    def test_a_rate_it_cannot_afford_is_refused_with_the_reason(self):
        """48000 Hz needs ~481 taps for the same resolution and costs minutes
        a burst. Refusing beats quietly running a sweep for hours."""
        with self.assertRaises(ValueError) as caught:
            fc.PIXEL_RX_20260930.taps(48000)
        self.assertIn("8000", str(caught.exception))


class TestThroughWithAMeasuredResponse(unittest.TestCase):
    """The measured path must not also run the modelled stages the
    measurement already contains."""

    FS8 = 8000

    def test_it_tilts_the_band_the_way_the_measurement_did(self):
        """Both tones in ONE burst, and the ratio between them read out.

        Two separate bursts cannot show a tilt at all: stage_drive normalises
        each one to the limiter, so it rescales away exactly the difference
        being looked for. The first version of this test did that and passed
        against a model that had the tilt applied twice.
        """
        r = fc.PIXEL_RX_20260930
        sig = two_tone(1500, 2250, n=4000, fs=self.FS8)
        out = fc.through(sig, self.FS8, 40.0, rng=random.Random(7),
                         response=r)
        got = ratio_db(settled(out, 400), 1500, 2250, self.FS8)
        want = r.gain_db(1500) - r.gain_db(2250)
        self.assertAlmostEqual(got, want, delta=1.5,
                               msg="measured tilt %+.2f dB, model gave %+.2f"
                                   % (want, got))

    def test_the_modelled_passband_is_not_applied_on_top_of_it(self):
        """A measured chain ALREADY contains its own passband. Running the
        modelled one as well would bend the band a second time, and a
        candidate would be condemned for a chain that does not exist.

        A flat measured response must therefore come out flat.
        """
        flat = fc.Response("flat-probe",
                           ((1400.0, 0.0), (2400.0, 0.0)),
                           source="synthetic, for this test only",
                           usable_hz=(1250.0, 2500.0))
        sig = two_tone(1500, 2250, n=4000, fs=self.FS8)
        out = fc.through(sig, self.FS8, 40.0, rng=random.Random(3),
                         response=flat)
        tilt = abs(ratio_db(settled(out, 400), 1500, 2250, self.FS8))
        self.assertLess(tilt, 1.5,
                        "a FLAT measured response produced %.2f dB of tilt, "
                        "so a modelled stage is running on top of it" % tilt)

    def test_without_a_response_the_old_path_is_untouched(self):
        """Default behaviour must not move: every figure in the record came
        from it."""
        rng = random.Random(11)
        was = fc.through(tone(1500), FS, 20.0, rng=rng)
        rng = random.Random(11)
        now = fc.through(tone(1500), FS, 20.0, rng=rng, response=None)
        self.assertEqual(was, now)
