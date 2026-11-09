"""Tests for fm_rx: the receive path, in the order the stages have to run.

Two kinds of test here, and the split is deliberate.

The de-emphasis filter and the unframing are pure arithmetic over floats and
bytes, so they are tested exactly -- including the one property the bench's own
version of this filter does NOT have, namely that its state survives a chunk
boundary. That is the bug a live receive path would have inherited by copying
the bench function, and it is invisible in anything but a chunked test.

The rest runs the REAL libcodec2, because the thing worth checking is that a
burst built by fm_burst survives being fed back in arbitrarily-sized pieces --
which is how audio actually arrives from a capture device, and which exercises
codec2's sync state across calls rather than within one.
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fm_burst
import fm_config
import fm_framing
import fm_health
import fm_modem
import fm_rx

_HAVE_CODEC2 = fm_modem.available()
requires_codec2 = unittest.skipUnless(
    _HAVE_CODEC2, "libcodec2 is not installed -- these tests need the real "
                  "library, because a mock would only test the wrapper")


class _Clock(object):
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def bench_profile(chain=None):
    """The operating point at the sample rate the bench measured it at."""
    op = fm_config.OPERATING_POINT.profile
    return fm_config.Profile(
        name=op.name, first_tone_hz=op.first_tone_hz,
        tone_spacing_hz=op.tone_spacing_hz, tones=op.tones,
        symbol_rate_bd=op.symbol_rate_bd, chain=chain or op.chain,
        sample_rate_hz=48000)


def tilted_chain(us):
    """A chain that needs de-emphasis, like the K1 PRO as a receiver."""
    return fm_config.Chain(
        name="needs de-emphasis", radios="r", cables="K1 PRO receiving",
        measured_on="2026-10-02", delivered="34 of 40 with de-emphasis",
        sample_rate_hz=48000,
        symbol_rate_ceiling_bd=fm_config.DIGIRIG_RX_CEILING_BD,
        usable_band_hz=fm_config.MEASURED_USABLE_BAND_HZ,
        deemphasis_us=us)


class _StubModem(fm_modem.Modem):
    """Hands back whatever frames a test wants, so the unframing and the health
    accounting can be checked without modulating anything."""

    def __init__(self, frame_payload_bytes=30, sample_rate_hz=48000):
        self.frame_payload_bytes = frame_payload_bytes
        self.sample_rate_hz = sample_rate_hz
        self.queued = []
        self.fed = []
        self.sync = False

    def feed(self, samples):
        self.fed.append(list(samples))
        out, self.queued = self.queued, []
        return out

    def has_sync(self):
        return self.sync


def rx_for(modem, config=None, chain=None, clock=None):
    config = config or fm_config.LinkConfig(
        name="t", profile=bench_profile(chain))
    return fm_rx.FmRx(modem, config,
                      health=fm_health.LinkHealth(clock=clock or _Clock()))


class TestDeemphasisCarriesStateAcrossChunks(unittest.TestCase):
    """THE BUG A COPY OF THE BENCH FUNCTION WOULD HAVE HAD.

    tools/fm_bench_audio.deemphasis() resets its accumulator on every call,
    which is right there -- it is handed one complete captured burst. A live
    path is handed a continuous stream in arbitrary chunks, and resetting per
    chunk puts a step discontinuity at every boundary: an impulse straight into
    the filter the demodulator is trying to track, several times a second.
    """

    def test_chunked_filtering_equals_whole_stream_filtering(self):
        signal = [0.5] * 500
        whole = fm_rx.Deemphasis(250.0, 48000)(signal)

        chunked = []
        f = fm_rx.Deemphasis(250.0, 48000)
        for i in range(0, len(signal), 37):      # deliberately ragged
            chunked.extend(f(signal[i:i + 37]))

        self.assertEqual(len(chunked), len(whole))
        for a, b in zip(whole, chunked):
            self.assertAlmostEqual(a, b, places=12)

    def test_a_stateless_filter_would_fail_that(self):
        """Guards the test above against being vacuous: if the filter were
        stateless the two would visibly diverge, so this asserts the comparison
        can actually tell them apart."""
        signal = [0.5] * 500
        whole = fm_rx.Deemphasis(250.0, 48000)(signal)
        stateless = []
        for i in range(0, len(signal), 37):
            stateless.extend(fm_rx.Deemphasis(250.0, 48000)(signal[i:i + 37]))
        self.assertNotAlmostEqual(whole[40], stateless[40], places=6)

    def test_it_settles_towards_the_input(self):
        out = fm_rx.Deemphasis(250.0, 48000)([1.0] * 2000)
        self.assertLess(out[0], 0.5)
        self.assertAlmostEqual(out[-1], 1.0, delta=0.01)

    def test_zero_is_a_bypass_not_a_degenerate_filter(self):
        f = fm_rx.Deemphasis(0.0, 48000)
        self.assertFalse(f.active)
        signal = [0.1, -0.4, 0.9]
        self.assertEqual(f(signal), signal)

    def test_reset_clears_the_accumulator(self):
        f = fm_rx.Deemphasis(250.0, 48000)
        f([1.0] * 2000)
        first = f([1.0])[0]
        f.reset()
        self.assertLess(f([1.0])[0], first)

    def test_the_time_constant_comes_from_the_chain(self):
        """It is a property of the CABLE, so it travels with the Chain rather
        than being a knob on the receive path."""
        rx = rx_for(_StubModem(), chain=tilted_chain(250.0))
        self.assertTrue(rx.deemphasis.active)
        self.assertEqual(rx.deemphasis.tau_us, 250.0)

    def test_a_chain_that_needs_none_gets_none(self):
        rx = rx_for(_StubModem())
        self.assertEqual(fm_config.OPERATING_POINT.deemphasis_us(), 0.0)
        self.assertFalse(rx.deemphasis.active)


class TestTheFilterIsPointedTheRightWay(unittest.TestCase):
    """CALIBRATED AGAINST A CLEAN SYNTHETIC SIGNAL, which is a ground rule here
    and is load-bearing for this filter in particular: a de-emphasis with the
    wrong shape makes the fault WORSE, and every other test in this file would
    still pass. Nothing else pins the orientation.

    The measured fault is the top tone arriving ~70% stronger than the bottom
    (+4.6 dB), so the correction has to be a gain that FALLS with frequency.
    """

    def tone_gains_db(self, tau_us):
        import math
        fs = 48000
        out = []
        for f in fm_config.FAST.tone_list():
            n = int(fs * 0.2)
            sig = [math.sin(2 * math.pi * f * i / fs) for i in range(n)]
            y = fm_rx.Deemphasis(tau_us, fs)(sig)
            tail = y[n // 2:]            # past the filter's own settling
            rms = (sum(v * v for v in tail) / len(tail)) ** 0.5
            out.append(20 * math.log10(rms * (2 ** 0.5)))
        return out

    def test_gain_falls_monotonically_across_the_tone_set(self):
        db = self.tone_gains_db(250.0)
        for lower, higher in zip(db, db[1:]):
            self.assertLess(higher, lower,
                            "de-emphasis must attenuate the HIGH tones more, "
                            "or it amplifies the very tilt it exists to cancel")

    def test_the_correction_is_the_right_size_for_the_measured_fault(self):
        """+4.6 dB of fault wants roughly -4.6 dB of correction. 250 us gives
        -5.8 dB across the set -- slightly over, same order."""
        db = self.tone_gains_db(250.0)
        tilt = db[-1] - db[0]
        self.assertLess(tilt, -4.0)
        self.assertGreater(tilt, -8.0)

    def test_why_the_value_is_insensitive_over_50_to_900_us(self):
        """The record measured this filter working anywhere from 50 to 900 us
        and left that unexplained. It falls out of what the estimator actually
        compares: tones against EACH OTHER, not against an absolute level.

        Across 50-900 us the TILT only moves from about -1.7 dB to -6.7 dB,
        while the absolute attenuation moves from -0.6 dB to -22 dB. So the
        quantity that matters barely changes over a range where the one that
        does not changes by 20 dB. Consistent with the measurement rather than
        a proof of it -- but it does mean a wrong value here fails gently,
        which is worth knowing before anyone tries to tune it.
        """
        tilts = []
        for us in (50.0, 250.0, 900.0):
            db = self.tone_gains_db(us)
            tilts.append(db[-1] - db[0])
            # The absolute attenuation, by contrast, runs away.
            if us == 50.0:
                self.assertGreater(db[0], -2.0)
            if us == 900.0:
                self.assertLess(db[0], -10.0)
        self.assertLess(max(tilts) - min(tilts), 6.0,
                        "the tilt should be far more stable than the "
                        "absolute attenuation across this range")
        for t in tilts:
            self.assertLess(t, 0.0)


class TestLevelsAreMeasuredBeforeFiltering(unittest.TestCase):
    """The level readout tells an operator what their RADIO is delivering, and
    the volume knob is the gain stage on this link. De-emphasis changes
    amplitude, so measuring after it would report a level the knob cannot be
    set against."""

    def test_the_reported_level_is_the_unfiltered_one(self):
        modem = _StubModem()
        rx = rx_for(modem, chain=tilted_chain(900.0))
        flat = [0.4] * 2000
        rx.feed(flat)
        # A 900 us filter attenuates this heavily; the readout must not show
        # the attenuated figure.
        self.assertAlmostEqual(rx.health.recent_level(), 0.4, delta=0.01)
        filtered = fm_rx.Deemphasis(900.0, 48000)(flat)
        self.assertLess(max(filtered), 0.4)

    def test_the_demodulator_gets_the_filtered_samples(self):
        modem = _StubModem()
        rx = rx_for(modem, chain=tilted_chain(900.0))
        rx.feed([0.4] * 500)
        self.assertEqual(len(modem.fed), 1)
        self.assertLess(max(modem.fed[0]), 0.4)


class TestRejectionsAreCountedAndExplained(unittest.TestCase):
    """A bare `continue` here is what let a bench spend an evening watching a
    link report "Hearing" while delivering precisely nothing. A frame reaching
    the unframing has already passed codec2's CRC, so a rejection is evidence
    about the receive LEVEL rather than about noise."""

    def corrupt(self, claimed_len, present):
        return fm_framing.scramble(
            bytes(bytearray([claimed_len])) + b"x" * present)

    def test_a_corrupt_frame_is_rejected_not_delivered(self):
        modem = _StubModem()
        rx = rx_for(modem)
        modem.queued = [self.corrupt(255, 10)]
        self.assertEqual(rx.feed([0.3] * 100), [])
        self.assertEqual(rx.health.snapshot()["rejects_in_window"], 1)
        self.assertEqual(rx.health.snapshot()["decodes_in_window"], 0)

    def test_the_reason_names_the_receive_level(self):
        modem = _StubModem()
        rx = rx_for(modem)
        modem.queued = [self.corrupt(255, 10)]
        rx.feed([0.3] * 100)
        self.assertIn("receive level", rx.rejections[-1])
        self.assertIn("255", rx.rejections[-1])

    def test_all_frames_rejected_shows_garbled_not_hearing(self):
        """The distinction the GARBLED state exists for: the physical layer is
        provably working and nothing reached the mesh."""
        modem = _StubModem()
        rx = rx_for(modem)
        modem.queued = [self.corrupt(255, 10), self.corrupt(250, 4)]
        rx.feed([0.3] * 100)
        self.assertEqual(rx.health.state(), fm_health.GARBLED)

    def test_a_usable_frame_shows_hearing(self):
        modem = _StubModem()
        rx = rx_for(modem)
        modem.queued = [fm_framing.pack(b"real", 30)]
        self.assertEqual(rx.feed([0.3] * 100), [b"real"])
        self.assertEqual(rx.health.state(), fm_health.HEARING)

    def test_a_mixed_burst_counts_both_separately(self):
        """"Something arrived" and "something arrived that we could use" are
        different claims, and folding them together is the original bug."""
        modem = _StubModem()
        rx = rx_for(modem)
        modem.queued = [fm_framing.pack(b"good", 30), self.corrupt(255, 10)]
        self.assertEqual(rx.feed([0.3] * 100), [b"good"])
        snap = rx.health.snapshot()
        self.assertEqual(snap["decodes_in_window"], 1)
        self.assertEqual(snap["rejects_in_window"], 1)
        self.assertEqual(rx.health.state(), fm_health.HEARING)

    def test_the_rejection_log_is_bounded(self):
        modem = _StubModem()
        rx = rx_for(modem)
        for _ in range(rx.max_rejections * 3):
            modem.queued = [self.corrupt(255, 10)]
            rx.feed([0.3] * 100)
        self.assertEqual(len(rx.rejections), rx.max_rejections)


class TestChannelBusy(unittest.TestCase):
    """Listen-before-transmit asks fm_carrier, never an energy threshold and
    never the modem's sync bitmask: this project's receive audio sits at RMS
    0.65-0.75 with zero real traffic, and the bitmask is idle for the whole of a
    burst. test_fm_carrier owns the detector; this owns the wiring."""

    def test_it_ignores_the_modems_sync_reading(self):
        modem = _StubModem()
        rx = rx_for(modem)
        modem.sync = True
        self.assertFalse(rx.channel_busy())

    def test_it_reads_the_audio_the_path_was_fed(self):
        modem = _StubModem()
        rx = rx_for(modem)
        fs = modem.sample_rate_hz
        profile = rx.config.profile
        self.assertFalse(rx.channel_busy())
        n = fs // profile.symbol_rate_bd
        tones = profile.tone_list()
        burst = []
        phase = 0.0
        for k in range(profile.symbol_rate_bd):          # one second, 4 tones
            step = 2 * math.pi * tones[k % len(tones)] / fs
            for _ in range(n):
                burst.append(0.5 * math.sin(phase))
                phase += step
        rx.feed(burst)
        self.assertTrue(rx.channel_busy())

    def test_the_detector_sees_the_deemphasised_audio_the_modem_sees(self):
        modem = _StubModem()
        rx = rx_for(modem)
        seen = []
        rx.carrier.feed = lambda samples: seen.append(list(samples))
        rx.feed([0.3] * 100)
        self.assertEqual(seen, modem.fed)


class TestReporting(unittest.TestCase):

    def test_the_level_line_comes_first(self):
        """It is the one thing on the screen an operator can act on."""
        rx = rx_for(_StubModem())
        rx.feed([0.3] * 100)
        self.assertIn("rx level", rx.summary_lines()[0])

    def test_a_tilted_chain_says_de_emphasis_is_on(self):
        rx = rx_for(_StubModem(), chain=tilted_chain(250.0))
        rx.feed([0.3] * 100)
        self.assertTrue(any("de-emphasis" in l for l in rx.summary_lines()))

    def test_an_untilted_chain_does_not_mention_it(self):
        rx = rx_for(_StubModem())
        rx.feed([0.3] * 100)
        self.assertFalse(any("de-emphasis" in l for l in rx.summary_lines()))

    def test_the_snapshot_is_json_ready_and_carries_the_filter(self):
        rx = rx_for(_StubModem(), chain=tilted_chain(250.0))
        rx.feed([0.3] * 100)
        snap = rx.snapshot()
        self.assertEqual(snap["deemphasis_us"], 250.0)
        self.assertIn("state", snap)
        self.assertIn("level_line", snap)


@requires_codec2
class TestTheWholePathAgainstRealCodec2(unittest.TestCase):
    """A burst built by fm_burst, fed back the way audio actually arrives: in
    arbitrarily-sized chunks, through a persistent modem."""

    def config(self):
        return fm_config.LinkConfig(name="t", profile=bench_profile())

    def test_a_burst_fed_in_chunks_decodes_byte_exact(self):
        cfg = self.config()
        payload = b"a whole receive path"
        with fm_modem.Codec2Fsk(bench_profile(), cfg.codename) as tx:
            samples = fm_burst.build(tx, payload, cfg)
        with fm_modem.Codec2Fsk(bench_profile(), cfg.codename) as rx_modem:
            rx = rx_for(rx_modem, config=cfg)
            got = []
            # Ragged chunks, and a tail of silence: the demodulator only emits
            # a frame once fed past the end of it (see fm_modem.demodulate).
            stream = list(samples) + [0.0] * (len(samples) // 2)
            for i in range(0, len(stream), 1021):
                got.extend(rx.feed(stream[i:i + 1021]))
        self.assertIn(payload, got)
        self.assertEqual(rx.health.state(), fm_health.HEARING)
        self.assertEqual(rx.rejections, [])

    def test_pcm_bytes_are_accepted_directly(self):
        """What Kotlin's AudioRecord loop actually hands over."""
        cfg = self.config()
        payload = b"pcm in"
        with fm_modem.Codec2Fsk(bench_profile(), cfg.codename) as tx:
            samples = fm_burst.build(tx, payload, cfg)
        pcm = fm_burst.to_pcm16(list(samples) + [0.0] * (len(samples) // 2))
        with fm_modem.Codec2Fsk(bench_profile(), cfg.codename) as rx_modem:
            rx = rx_for(rx_modem, config=cfg)
            got = []
            for i in range(0, len(pcm), 4096):
                # Chunks are whole samples: an odd byte split would desync the
                # int16 stream, which is the caller's contract to honour.
                got.extend(rx.feed_pcm(pcm[i:i + 4096]))
        self.assertIn(payload, got)

    def test_silence_alone_decodes_nothing_and_reads_deaf(self):
        cfg = self.config()
        clock = _Clock()
        with fm_modem.Codec2Fsk(bench_profile(), cfg.codename) as rx_modem:
            rx = rx_for(rx_modem, config=cfg, clock=clock)
            for _ in range(20):
                self.assertEqual(rx.feed([0.0] * 2048), [])
        self.assertEqual(rx.health.state(), fm_health.DEAF)
        self.assertEqual(rx.rejections, [])

    def test_an_empty_payload_still_round_trips(self):
        """The worst case for the scrambler: nothing but a length byte and
        padding, which unwhitened was the shape that decoded zero frames."""
        cfg = self.config()
        with fm_modem.Codec2Fsk(bench_profile(), cfg.codename) as tx:
            samples = fm_burst.build(tx, b"", cfg)
        with fm_modem.Codec2Fsk(bench_profile(), cfg.codename) as rx_modem:
            rx = rx_for(rx_modem, config=cfg)
            got = []
            stream = list(samples) + [0.0] * (len(samples) // 2)
            for i in range(0, len(stream), 2048):
                got.extend(rx.feed(stream[i:i + 2048]))
        self.assertIn(b"", got)

    def test_de_emphasis_in_the_path_does_not_break_a_clean_burst(self):
        """The chain that needs the filter still has to decode its own traffic
        through it -- the filter is a correction, not a gate."""
        chain = tilted_chain(250.0)
        prof = bench_profile(chain)
        cfg = fm_config.LinkConfig(name="t", profile=prof)
        payload = b"through the filter"
        with fm_modem.Codec2Fsk(prof, cfg.codename) as tx:
            samples = fm_burst.build(tx, payload, cfg)
        with fm_modem.Codec2Fsk(prof, cfg.codename) as rx_modem:
            rx = rx_for(rx_modem, config=cfg)
            self.assertTrue(rx.deemphasis.active)
            got = []
            stream = list(samples) + [0.0] * (len(samples) // 2)
            for i in range(0, len(stream), 2048):
                got.extend(rx.feed(stream[i:i + 2048]))
        self.assertIn(payload, got)


@requires_codec2
class TestEachBurstGetsAVerdictAgainstRealCodec2(unittest.TestCase):
    """fm_airlog fed by the real detector and the real modem, not hand-made
    observations. The verdict's clock is audio time: it advances by the length
    of each chunk fed, so a test runs in milliseconds and still sees a burst
    last as long as it does on the air."""

    CHUNK = 2048

    def config(self):
        # THE OPERATING POINT (one copy), not LinkConfig's defaults, which are a
        # 32 B code at two copies: with two copies in a burst, cutting the
        # front off still leaves a whole second copy to decode, so "a burst
        # that lost its start" would correctly read OK and the test would be
        # asserting about a different link.
        op = fm_config.OPERATING_POINT
        return fm_config.LinkConfig(
            name="t", profile=bench_profile(), codename=op.codename,
            repeats=op.repeats, keying=op.keying, lead_in_s=op.lead_in_s)

    def burst(self, cfg, payload=b"verdict"):
        with fm_modem.Codec2Fsk(bench_profile(), cfg.codename) as tx:
            return list(fm_burst.build(tx, payload, cfg))

    def run_stream(self, stream, tx_windows=()):
        """Feed `stream` and return (the lines logged, the rx).

        `tx_windows` are (start_s, end_s) in audio time during which this node
        is "transmitting"; the audio inside them is DROPPED, exactly as the
        Kotlin receive loop drops it."""
        cfg = self.config()
        clock = _Clock()
        fs = 48000
        with fm_modem.Codec2Fsk(bench_profile(), cfg.codename) as modem:
            rx = rx_for(modem, config=cfg)
            rx.airlog._clock = clock
            lines = []
            pos = 0
            active = False
            while pos < len(stream):
                chunk = stream[pos:pos + self.CHUNK]
                t0 = pos / float(fs)
                now_tx = any(a <= t0 < b for a, b in tx_windows)
                if now_tx != active:
                    active = now_tx
                    rx.note_tx(active)
                clock.advance(len(chunk) / float(fs))
                pos += len(chunk)
                if now_tx:
                    continue
                rx.feed(chunk)
                lines.extend(rx.pop_events())
        return lines, rx

    def test_a_clean_burst_is_logged_ok(self):
        cfg = self.config()
        stream = [0.0] * 24000 + self.burst(cfg) + [0.0] * (48000 * 5)
        lines, rx = self.run_stream(stream)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("burst OK", lines[0])
        self.assertEqual(rx.snapshot()["bursts"], "OK 1")

    def test_a_burst_cut_off_part_way_is_not_logged_ok(self):
        cfg = self.config()
        full = self.burst(cfg)
        stream = [0.0] * 24000 + full[:len(full) // 3] + [0.0] * (48000 * 9)
        lines, _ = self.run_stream(stream)
        self.assertEqual(len(lines), 1, lines)
        self.assertNotIn("burst OK", lines[0])

    def test_a_burst_that_arrives_while_we_transmit_is_not_blamed_on_the_channel(self):
        """The 2026-10-09 question: 5 bursts missing, and no way to say whether
        the receiver was keyed up. Our transmit covers the START of the burst
        (audio dropped, as on the phone); what is heard after it cannot decode."""
        cfg = self.config()
        full = self.burst(cfg)
        lead = 24000
        stream = [0.0] * lead + full + [0.0] * (48000 * 5)
        tx = [(0.0 + lead / 48000.0, lead / 48000.0 + 1.5)]
        lines, _ = self.run_stream(stream, tx_windows=tx)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("OVER OUR TX", lines[0])

    def test_a_short_blip_reads_cut_short_through_the_detectors_hold(self):
        """The real detector keeps `busy` true for a second after the energy is
        gone. Verdicts taken from `busy` would measure a 0.6 s blip as ~1.6 s,
        longer than the half-burst cut-off, and call it a lost burst. This is
        the only test where that difference exists, because only the real
        detector has a hold."""
        tone = [0.4 * math.sin(2 * math.pi * 1400.0 * n / 48000.0)
                for n in range(int(0.6 * 48000))]
        stream = [0.0] * 24000 + tone + [0.0] * (48000 * 9)
        lines, _ = self.run_stream(stream)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("CUT SHORT", lines[0])

    def test_two_bursts_are_two_verdicts(self):
        cfg = self.config()
        b = self.burst(cfg)
        stream = [0.0] * 24000 + b + [0.0] * (48000 * 4) + b + [0.0] * (48000 * 5)
        lines, rx = self.run_stream(stream)
        self.assertEqual(len(lines), 2, lines)
        self.assertEqual(rx.snapshot()["bursts"], "OK 2")


if __name__ == "__main__":
    unittest.main()
