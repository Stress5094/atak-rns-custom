"""Tests for fm_carrier: the listen-before-transmit detector that works DURING a
burst.

THE BUG THIS FILE EXISTS FOR. listen-before-transmit asked codec2's rx-status
bitmask, which is idle for the whole of a burst and goes busy only after it. A
node that listened while another was mid-burst heard a clear channel and keyed
up over it -- 15 of 20 overlaps in the 2026-10-03 two-phone run began 1.1-3.4 s
into a burst already in progress. `TestTheOriginalBug` reproduces that with a
real burst and asserts both halves: the old reading IS blind, the new one is not.
Without the first half, the second proves nothing.

The pure-signal tests need no libcodec2.
"""

import math
import random
import unittest

import fm_burst
import fm_carrier
import fm_config
import fm_modem
import fm_rx

requires_codec2 = unittest.skipUnless(
    fm_modem.available(), "libcodec2 is not installed")

FS = 8000
RS = 400
TONES = [1000, 1400, 1800, 2200]


def noise(n, tilt=False, seed=1):
    """Unit-RMS white noise, or noise rising with frequency (a discriminator's
    shape: the first difference of white noise has power proportional to f^2)."""
    rng = random.Random(seed)
    w = [rng.gauss(0, 1) for _ in range(n + 1)]
    x = [w[i + 1] - w[i] for i in range(n)] if tilt else w[:n]
    rms = math.sqrt(sum(v * v for v in x) / n)
    return [v / rms for v in x]


def fsk(symbols, tones=TONES, fs=FS, rs=RS, amp=1.0):
    """Phase-continuous M-FSK of the given tone indices, like the real thing."""
    n = fs // rs
    out, phase = [], 0.0
    for k in symbols:
        step = 2 * math.pi * tones[k] / fs
        for _ in range(n):
            out.append(amp * math.sin(phase))
            phase += step
    return out


def random_fsk(seconds, seed=2, tones=TONES, amp=1.0):
    rng = random.Random(seed)
    return fsk([rng.randrange(len(tones)) for _ in range(int(seconds * RS))],
               tones=tones, amp=amp)


# Audio before the tracked idle floor is trusted: it must first have enough
# readings, then refresh, and the window must have filled.
WARMUP_S = (fm_carrier.FLOOR_MIN_S + fm_carrier.FLOOR_UPDATE_S
            + fm_carrier.WINDOW_S + 1.0)


def detector(**kw):
    return fm_carrier.CarrierDetector(TONES, RS, FS, **kw)


def busy_trace(det, signal, chunk=int(FS * 0.2)):
    """busy after each chunk of `signal`, as (seconds, busy) pairs."""
    out = []
    for i in range(0, len(signal) - chunk + 1, chunk):
        det.feed(signal[i:i + chunk])
        out.append(((i + chunk) / float(FS), det.busy))
    return out


class TestIdle(unittest.TestCase):
    """A false busy costs a wait; but a detector that is always busy is a
    transmitter that never transmits, so idle must read idle."""

    def test_white_noise_is_never_busy(self):
        trace = busy_trace(detector(), noise(FS * 60))
        self.assertFalse(any(b for _, b in trace))

    def test_noise_tilted_like_a_discriminator_is_never_busy(self):
        """Once the floor is learned. Tilted noise idles at 0.553, not the
        white-noise 0.52, which is ABOVE the threshold a white-noise floor gives
        (0.569 - 3 sd); a fixed threshold would flicker, the tracked floor
        does not. The warm-up is the price of not assuming a chain."""
        trace = busy_trace(detector(), noise(FS * 60, tilt=True))
        self.assertFalse(any(b for t, b in trace if t >= WARMUP_S))

    def test_loud_noise_is_not_busy(self):
        """The reason this is not a level threshold: the receive audio sits at
        RMS 0.65-0.75 with no traffic. Level must not matter."""
        loud = [0.7 * v for v in noise(FS * 30)]
        self.assertFalse(any(b for _, b in busy_trace(detector(), loud)))

    def test_digital_silence_is_not_busy(self):
        """A closed squelch is exact zeros, which carry no information."""
        self.assertFalse(any(b for _, b in
                             busy_trace(detector(), [0.0] * (FS * 5))))

    def test_nothing_is_busy_before_there_is_anything_to_average(self):
        d = detector()
        d.feed(random_fsk(0.02))      # 8 symbols of a perfect burst
        self.assertFalse(d.busy)


class TestSignal(unittest.TestCase):

    def test_a_burst_reads_busy_while_it_is_still_arriving(self):
        idle = noise(FS * 3)
        burst = random_fsk(3.0)
        trace = busy_trace(detector(), idle + burst + noise(FS * 3, seed=9))
        busy_at = [t for t, b in trace if b]
        self.assertTrue(busy_at)
        # Onset is at 3.0 s. Within one chunk of the window filling, ~0.3 s.
        self.assertLessEqual(min(busy_at), 3.0 + 0.45)
        # ...and stays busy to the end of the burst, not just once.
        mid = [b for t, b in trace if 3.5 <= t <= 5.9]
        self.assertTrue(all(mid))

    def test_it_releases_within_one_window_of_the_burst_ending(self):
        trace = busy_trace(detector(), noise(FS * 2) + random_fsk(3.0)
                           + noise(FS * 3, seed=9))
        after = [b for t, b in trace if t >= 5.0 + fm_carrier.RELEASE_S + 0.2]
        self.assertFalse(any(after))

    def test_a_burst_in_the_noise_floor_a_phone_hears(self):
        """Signal buried in loud noise at 0 dB SNR, the weakest codec2 still
        decodes at this code. If this stopped reading busy the detector would
        be deafer than the modem."""
        sig = random_fsk(3.0)
        n = noise(len(sig), tilt=True)
        # sig rms is 0.707, so noise at 0.707 rms is 0 dB.
        mixed = [s + 0.707 * v for s, v in zip(sig, n)]
        trace = busy_trace(detector(), noise(FS * 2, tilt=True, seed=5) + mixed)
        self.assertTrue(all(b for t, b in trace if 2.5 <= t <= 4.9))

    def test_it_is_the_tone_shape_not_the_level(self):
        """A burst 20 dB quieter than the idle noise next to it still reads
        busy when it is all there is; the same level of pure noise does not."""
        quiet = random_fsk(2.0, amp=0.01)
        self.assertTrue(detector_after(quiet).busy)
        self.assertFalse(detector_after([0.01 * v for v in noise(FS * 2)]).busy)

    def test_one_loud_click_is_not_a_burst(self):
        """The case an energy detector fails: an impulse is all level and no
        tone structure."""
        sig = noise(FS * 3)
        sig[FS + 10] = 40.0
        self.assertFalse(any(b for _, b in busy_trace(detector(), sig)))

    def test_a_single_steady_tone_reads_busy(self):
        """A carrier with no modulation, such as a radio's test tone, is
        something to wait for as well."""
        self.assertTrue(detector_after(fsk([2] * 800)).busy)


def detector_after(signal):
    d = detector()
    d.feed(signal)
    return d


class TestChunking(unittest.TestCase):

    def test_the_answer_does_not_depend_on_how_the_audio_was_chunked(self):
        sig = noise(FS) + random_fsk(1.5) + noise(FS, seed=4)
        whole = detector()
        ragged = detector()
        whole.feed(sig)
        i = 0
        sizes = [1, 7, 19, 20, 21, 333, 1600, 5]
        k = 0
        while i < len(sig):
            n = sizes[k % len(sizes)]
            ragged.feed(sig[i:i + n])
            i += n
            k += 1
        self.assertAlmostEqual(whole.level, ragged.level, places=9)

    def test_reset_forgets_a_burst(self):
        d = detector_after(random_fsk(2.0))
        self.assertTrue(d.busy)
        d.reset()
        self.assertFalse(d.busy)


class TestHold(unittest.TestCase):
    """The statistic flickers mid-burst on the VOX chain (5% of mid-burst samples
    read under 0.614 on the 2026-10-08 capture); a bare comparison called those
    moments clear, so a node that checked then keyed up into a live burst."""

    def _det(self):
        return fm_carrier.CarrierDetector(TONES, RS, FS)

    def test_a_dip_inside_a_burst_still_reads_busy(self):
        d = self._det()
        d.feed(random_fsk(2.0, seed=3))
        self.assertTrue(d.busy)
        # a window's worth of noise: the windowed statistic falls below the
        # threshold well inside the hold, but the channel must not read clear
        d.feed(noise(int(FS * fm_carrier.WINDOW_S), seed=4))
        self.assertLess(d.level, d.threshold)
        self.assertTrue(d.busy)

    def test_it_goes_idle_once_the_hold_has_passed(self):
        d = self._det()
        d.feed(random_fsk(2.0, seed=3))
        d.feed(noise(int(FS * (fm_carrier.HOLD_S + fm_carrier.WINDOW_S + 0.5)), seed=4))
        self.assertFalse(d.busy)

    def test_reset_clears_the_hold(self):
        d = self._det()
        d.feed(random_fsk(2.0, seed=3))
        d.reset()
        self.assertFalse(d.busy)

    def test_idle_noise_never_arms_the_hold(self):
        d = self._det()
        for i in range(10):
            d.feed(noise(FS * 3, seed=10 + i))
            self.assertFalse(d.busy)

    def test_release_bound_covers_window_and_hold(self):
        self.assertAlmostEqual(fm_carrier.RELEASE_S,
                               fm_carrier.WINDOW_S + fm_carrier.HOLD_S)


class TestThresholds(unittest.TestCase):

    def test_baselines_are_the_closed_form(self):
        self.assertAlmostEqual(fm_carrier.noise_baseline(4), 25.0 / 48.0)
        self.assertAlmostEqual(fm_carrier.noise_baseline(2), 0.75)

    def test_the_four_tone_threshold_sits_just_over_the_white_noise_floor(self):
        """0.569 on the white-noise floor, with the 1 s window. It is ABOVE
        white-noise idle at 1 s (max 0.541 in 60 s runs) and far below the
        weakest burst the modem decodes (0.77 at 0 dB), so it can sit this low
        only because the window is long."""
        self.assertAlmostEqual(fm_carrier.default_threshold(4), 0.569, places=3)
        self.assertGreater(fm_carrier.default_threshold(4), 0.541)
        self.assertLess(fm_carrier.default_threshold(4), 0.77)

    def test_the_strong_mark_is_the_old_busy_threshold(self):
        """What fm_airlog times a carrier by. Unchanged at 0.66 so a carrier
        episode is still about the burst and not about the window's memory."""
        self.assertAlmostEqual(fm_carrier.strong_threshold(4), 0.66, places=2)

    def test_the_margin_is_never_under_six_idle_spreads(self):
        """A fraction of the RANGE is 0.025 with two tones: 3.5 spreads, and
        white noise crossed it in 15-22 s. The floor on the margin is why."""
        for tones in (2, 4):
            margin = (fm_carrier.default_threshold(tones)
                      - fm_carrier.noise_baseline(tones))
            self.assertGreaterEqual(
                margin, fm_carrier.MARGIN_SPREADS * fm_carrier.idle_spread(400) - 1e-9)

    def test_two_tone_idle_and_burst_still_separate(self):
        tones = [1000, 1400]
        d = fm_carrier.CarrierDetector(tones, RS, FS)
        self.assertFalse(any(b for t, b in busy_trace(d, noise(FS * 120))
                             if t >= WARMUP_S))
        d = fm_carrier.CarrierDetector(tones, RS, FS)
        d.feed(random_fsk(2.0, tones=tones))
        self.assertTrue(d.busy)

    def test_it_refuses_a_rate_that_is_not_a_whole_number_of_samples(self):
        with self.assertRaises(ValueError):
            fm_carrier.CarrierDetector(TONES, 375, 8000)

    def test_it_refuses_one_tone(self):
        with self.assertRaises(ValueError):
            fm_carrier.CarrierDetector([1000], RS, FS)


@requires_codec2
class TestTheLongWindow(unittest.TestCase):
    """The 2026-10-11 change: a 1.0 s window, a threshold 0.10 of the way up,
    a tracked floor. Each test pins something the 0.2 s / 0.66 detector could
    not do or something the change could have broken."""

    def _burst_in_noise(self, snr_db, seed=1):
        sig = random_fsk(3.0, seed=seed)
        mix = [10 ** (-snr_db / 20.0) * 0.707 * v for v in noise(FS * 8, seed=seed + 10)]
        for i, s in enumerate(sig):
            mix[FS * 3 + i] += s
        return mix

    def _old_detector(self):
        base = fm_carrier.noise_baseline(4)
        return fm_carrier.CarrierDetector(
            TONES, RS, FS, window_s=0.2, threshold=base + 0.29 * (1 - base),
            hold_s=1.0)

    def test_a_burst_at_minus_six_db_reads_busy_where_the_old_detector_was_deaf(self):
        """Noise at twice the signal's rms. Measured on 3 seeds: new 83-92% of
        the burst body, old 0%. Over the air (40 recorded bursts, noise added
        afterwards) the same step was 0/40 -> 16/40 flagged."""
        mix = self._burst_in_noise(-6.0)
        new = [b for t, b in busy_trace(detector(), mix) if 3.6 <= t <= 5.9]
        old = [b for t, b in busy_trace(self._old_detector(), mix) if 3.6 <= t <= 5.9]
        self.assertGreater(sum(new) / float(len(new)), 0.75)
        self.assertEqual(sum(old), 0)

    def test_it_does_not_cry_wolf_in_the_noise_next_to_a_weak_burst(self):
        mix = self._burst_in_noise(-6.0)
        trace = busy_trace(detector(), mix)
        self.assertFalse(any(b for t, b in trace if 1.5 <= t <= 2.9))

    def test_it_rises_within_a_window_of_a_clean_burst_starting(self):
        trace = busy_trace(detector(), noise(FS * 3, seed=7) + random_fsk(3.0))
        first = min(t for t, b in trace if b)
        self.assertLessEqual(first, 3.0 + 0.45)

    def test_it_falls_within_the_release_bound(self):
        d = detector()
        d.feed(noise(FS * 3, seed=7) + random_fsk(3.0))
        gone = None
        for i in range(60):
            d.feed(noise(FS // 5, seed=100 + i))
            if not d.busy:
                gone = (i + 1) * 0.2
                break
        self.assertIsNotNone(gone)
        self.assertLessEqual(gone, fm_carrier.RELEASE_S + 0.2)

    def test_the_floor_follows_a_chain_whose_idle_is_higher(self):
        """Tilted noise idles at 0.553. Starting from the white-noise 0.52 the
        floor moves up and the threshold with it; idle then stays idle and a
        burst on that chain is still found."""
        d = detector()
        busy_trace(d, noise(FS * 30, tilt=True))
        self.assertGreater(d.floor, 0.53)
        self.assertLess(d.floor, 0.56)
        self.assertGreater(d.threshold, 0.575)
        self.assertFalse(any(b for _, b in busy_trace(d, noise(FS * 30, tilt=True, seed=3))))
        d.feed(random_fsk(2.0))
        self.assertTrue(d.busy)

    def test_traffic_does_not_drag_the_floor_up(self):
        """17% of the time on the air (the two-phone run's occupancy). The floor
        is a low percentile, so it should stay at the idle level."""
        audio = []
        for k in range(10):
            audio += noise(int(FS * 10), seed=20 + k)
            audio += random_fsk(2.0, seed=30 + k)       # 2 s of 12 = 17%
        d = detector()
        busy_trace(d, audio)
        self.assertAlmostEqual(d.floor, fm_carrier.noise_baseline(4), delta=0.012)

    def test_a_floor_that_starts_too_low_cannot_lock_the_detector_busy(self):
        """The deadlock a 'floor from non-busy readings' rule has: set too low,
        idle reads busy, busy is never learned from. Percentile-of-everything
        does not have it."""
        d = detector()
        d._idle_floor = 0.44            # as if learned from a quiet chain
        busy_trace(d, noise(FS * 40, tilt=True))
        self.assertFalse(any(b for _, b in busy_trace(d, noise(FS * 20, tilt=True, seed=2))))

    def test_the_floor_is_clamped_so_a_busy_channel_cannot_deafen_it(self):
        d = detector()
        busy_trace(d, random_fsk(60.0))          # a channel that is never quiet
        # ABSOLUTE, not built from the constant under test: 0.52 + 0.10.
        self.assertLessEqual(d.floor, 0.63)
        self.assertGreater(d.floor, 0.60)       # it did rise, to the clamp

    def test_reset_keeps_the_floor(self):
        """A burst can be forgotten; what the chain's idle looks like cannot."""
        d = detector()
        busy_trace(d, noise(FS * 30, tilt=True))
        floor = d.floor
        d.reset()
        self.assertEqual(d.floor, floor)

    def test_strong_is_about_the_burst_not_the_windows_memory_of_it(self):
        d = detector()
        d.feed(noise(FS * 2, seed=7))
        self.assertFalse(d.strong)
        d.feed(random_fsk(1.5))
        self.assertTrue(d.strong)
        d.feed(noise(FS, seed=8))        # a window after the burst ended
        self.assertFalse(d.strong)

    def test_a_fixed_threshold_is_honoured(self):
        d = fm_carrier.CarrierDetector(TONES, RS, FS, threshold=0.9)
        self.assertEqual(d.threshold, 0.9)
        busy_trace(d, noise(FS * 30, tilt=True))
        self.assertEqual(d.threshold, 0.9)


class TestTheOriginalBug(unittest.TestCase):
    """The reading listen-before-transmit USED to take, against a real burst."""

    @classmethod
    def setUpClass(cls):
        cfg = fm_config.OPERATING_POINT
        cls.cfg = cfg
        with fm_modem.Codec2Fsk(cfg.profile, codename=cfg.codename) as tx:
            cls.burst = list(fm_burst.build(tx, b"x" * 100, cfg))
        cls.fs = cfg.profile.sample_rate_hz

    def readings(self):
        """(seconds into burst, codec2's has_sync, fm_carrier's busy), per 0.2 s
        chunk, after 2 s of idle."""
        fs, cfg = self.fs, self.cfg
        rx_modem = fm_modem.Codec2Fsk(cfg.profile, codename=cfg.codename)
        self.addCleanup(rx_modem.close)
        det = fm_carrier.CarrierDetector(
            cfg.profile.tone_list(), cfg.profile.symbol_rate_bd, fs)
        sig = noise(fs * 2) + self.burst + [0.0] * (fs * 6)
        chunk = int(fs * 0.2)
        out = []
        for i in range(0, len(sig) - chunk + 1, chunk):
            part = sig[i:i + chunk]
            rx_modem.feed(part)
            det.feed(part)
            out.append((i / float(fs) - 2.0, rx_modem.has_sync(), det.busy))
        return out

    def test_codec2s_sync_is_blind_for_the_whole_burst(self):
        """THE BUG. If this ever fails the old reading got better and this file
        should be re-read before anything is concluded from it."""
        burst_s = len(self.burst) / float(self.fs)
        during = [s for t, s, _ in self.readings() if 0.0 <= t <= burst_s - 0.3]
        self.assertTrue(during)
        self.assertFalse(any(during),
                         "codec2 sync read busy mid-burst; the premise of "
                         "fm_carrier no longer holds")

    def test_the_carrier_detector_sees_it_throughout(self):
        burst_s = len(self.burst) / float(self.fs)
        rows = self.readings()
        mid = [b for t, _, b in rows if 0.5 <= t <= burst_s - 0.3]
        self.assertTrue(mid)
        self.assertTrue(all(mid), "carrier detector missed part of a burst")
        first = min(t for t, _, b in rows if b)
        self.assertLessEqual(first, 0.6, "slow to notice a burst starting")

    def test_and_it_is_idle_again_soon_after(self):
        """Not held for ~2.6 s like the sync reading: a node should not wait
        out a quiet channel."""
        burst_s = len(self.burst) / float(self.fs)
        late = [b for t, _, b in self.readings()
                if t >= burst_s + fm_carrier.RELEASE_S + 0.2]
        self.assertFalse(any(late))


@requires_codec2
class TestFmRxUsesIt(unittest.TestCase):

    def test_channel_busy_is_the_carrier_reading_not_the_modems(self):
        cfg = fm_config.OPERATING_POINT
        with fm_modem.Codec2Fsk(cfg.profile, codename=cfg.codename) as modem:
            modem.has_sync = lambda: True      # the OLD signal, screaming busy
            rx = fm_rx.FmRx(modem, cfg)
            self.assertFalse(rx.channel_busy())
            fs = cfg.profile.sample_rate_hz
            rx.feed(noise(fs * 2))
            self.assertFalse(rx.channel_busy())
            rx.feed(random_fsk(1.0))
            self.assertTrue(rx.channel_busy())


if __name__ == "__main__":
    unittest.main()
