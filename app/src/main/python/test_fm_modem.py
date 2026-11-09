"""Tests for fm_modem: the bytes-on-air path, exercised against real libcodec2.

The framing and the operating point are already pinned by test_fm_framing and
test_fm_config, but those are arithmetic. This module is the first thing in the
chain that touches the actual waveform: everything above it (fm_burst, the
receive path, the interface) assumes its round-trip is byte-exact and its
duration model is honest. It shipped "essentially untested" and that is the
gap these tests close.

Every test here runs the REAL libcodec2 through ctypes, not a mock. A mock
would verify the wrapper's Python and nothing else; the wrapper's Python is
the boring half. The valuable half is the contract with codec2 itself -- the
frame size vs the CRC, the sample counts, the sync bitmask -- and only the
real library can break those.

WHY THESE CAN SKIP. libcodec2 is not installed everywhere. On a machine
without it these skip (run_tests.sh fails loudly on any skip, so a weak run is
never mistaken for a clean one); on this machine they run, because the whole
point is to catch a regression before it reaches a radio.

WHY THERE IS A RETRY IN THE ROUND-TRIP TESTS. codec2's FSK-LDPC receiver can
occasionally (~0.4%, measured over 500 loopback trials) fail to decode the
FIRST frame after the preamble even on a perfectly clean loopback -- the sync
state machine has not fully settled. Later frames are unaffected. This is not
a wrapper bug; it is a property of the modem, and it is precisely why the
transport sends every frame more than once and never treats a single copy as
reliable. The tests mirror the transport: a bounded retry, with the miss
documented here rather than silently absorbed.
"""

import random
import unittest

import fm_config
import fm_framing
import fm_modem

_HAVE_CODEC2 = fm_modem.available()
requires_codec2 = unittest.skipUnless(
    _HAVE_CODEC2, "libcodec2 is not installed -- these tests need the real "
                  "library, because a mock would only test the wrapper")

# The codes the rebuild actually cares about. H_4096_8192_3d is deliberately
# absent from the round-trip loop: it is documented as measured BADLY (a ~34 s
# burst) and its frame is 512 B, which makes a single round-trip take ~16 s of
# modulation for a code nobody is going to start from. Its CRC math is still
# covered cheaply in test_payload_bytes_for, which needs no library.
CODES = ("H_128_256_5", "H_256_512_4", "H_256_768_22", "H_1024_2048_4f")


def _frame_payload(codename):
    """The usable payload size of a code, without constructing a modem."""
    return fm_config.code_bytes(codename) - fm_modem.CRC_BYTES


def _nin(modem):
    """How many samples codec2 wants per rawdatarx call, for incremental feed."""
    return fm_modem._lib().freedv_nin(modem._f)


def _pad_to_chunk(samples, chunk):
    """Zero-pad to a whole number of `chunk`-sized blocks, so the final feed
    is never a ragged partial that drops a frame."""
    rem = len(samples) % chunk
    if rem:
        samples = list(samples) + [0.0] * (chunk - rem)
    return samples


def _demodulate_burst(codename, payloads, lead_in_s=0.0):
    """One modulate->demodulate round trip. Returns the recovered payloads.

    Does NOT retry the first-frame miss -- see the module docstring. Callers
    that need a clean decode go through _assert_recovers.
    """
    frame_payload = _frame_payload(codename)
    with fm_modem.Codec2Fsk(fm_config.NARROW, codename) as tx:
        frames = [fm_framing.pack(p, frame_payload) for p in payloads]
        burst = tx.modulate_burst(frames, lead_in_s=lead_in_s)
        with fm_modem.Codec2Fsk(fm_config.NARROW, codename) as rx:
            got = rx.demodulate(burst)
    return [fm_framing.unpack(g, frame_payload) for g in got]


def _assert_recovers(codename, payloads, lead_in_s=0.0, attempts=4):
    """Assert the burst decodes to exactly `payloads`, retrying the rare
    first-frame miss. Four attempts at ~0.4% makes a false failure ~1e-10."""
    last = None
    for _ in range(attempts):
        last = _demodulate_burst(codename, payloads, lead_in_s)
        if sorted(last) == sorted(payloads):
            return
    raise AssertionError(
        "burst did not decode to %r after %d attempts; last recovered %r"
        % (payloads, attempts, last))


def _feed_and_sample(modem, samples, chunk):
    """Feed `samples` in `chunk`-sized pieces, recording, for each piece,
    (did_a_frame_decode, was_has_sync_true_right_after).

    This is the only way to observe sync, because has_sync() is transient:
    it is true only at the instant a frame decodes and decays over ~0.5-0.6 s.
    Sampling it after a full demodulate() has run is always False -- the flush
    silence has already decayed it -- which is precisely why this helper feeds
    incrementally instead.
    """
    events = []
    for i in range(0, len(samples), chunk):
        piece = samples[i:i + chunk]
        decoded = bool(modem.feed(piece))
        events.append((decoded, modem.has_sync()))
    return events


@requires_codec2
class TestConstruction(unittest.TestCase):
    """The frame size a code promises in the table is the frame size the
    library actually hands back, with the CRC already accounted for."""

    def test_frame_payload_bytes_matches_the_code_table(self):
        # Constructing a freedv instance is cheap; only modulation is slow, and
        # none of these codes is the 512 B one.
        for codename in CODES:
            with self.subTest(code=codename):
                with fm_modem.Codec2Fsk(fm_config.NARROW, codename) as m:
                    self.assertEqual(m.frame_payload_bytes,
                                     _frame_payload(codename))

    def test_payload_bytes_for_knows_every_code_without_the_library(self):
        # The bridge's MTU budget calls this before any modem exists, so it
        # must not need the native library -- and it must agree with the table
        # for ALL five codes, including the 512 B one this file otherwise
        # skips for speed.
        for codename in fm_modem.CODES:
            with self.subTest(code=codename):
                self.assertEqual(fm_modem.payload_bytes_for(codename),
                                 fm_config.code_bytes(codename)
                                 - fm_modem.CRC_BYTES)

    def test_an_unknown_code_is_refused_not_guessed(self):
        with self.assertRaises(fm_modem.ModemError):
            fm_modem.payload_bytes_for("H_not_a_real_code")
        with self.assertRaises(fm_modem.ModemError):
            fm_modem.Codec2Fsk(fm_config.NARROW, "H_not_a_real_code")

    def test_the_sample_rate_comes_from_the_profile(self):
        with fm_modem.Codec2Fsk(fm_config.NARROW, "H_256_768_22") as m:
            self.assertEqual(m.sample_rate_hz, fm_config.SAMPLE_RATE_HZ)


@requires_codec2
class TestRoundTrip(unittest.TestCase):
    """modulate_burst -> demodulate returns exactly the bytes that went in.

    This is the whole contract. If it breaks, everything above this file --
    framing, burst, the receive path -- breaks silently at the same time, and
    the symptom is a link that reports frames arriving while delivering
    nothing, which is the single most expensive failure in this project's
    history.
    """

    def test_every_code_round_trips_byte_exact(self):
        for codename in CODES:
            capacity = fm_framing.capacity_for(_frame_payload(codename))
            sizes = (0, 1, 2, capacity // 2, capacity)
            for n in sizes:
                with self.subTest(code=codename, payload=n):
                    payload = bytes((i % 256 for i in range(n)))
                    _assert_recovers(codename, [payload])

    def test_high_bytes_survive_the_round_trip(self):
        # Signed/unsigned confusion at 0x80 has bitten this project before; a
        # byte arriving as a negative int only surfaces far downstream.
        payload = bytes(bytearray(range(256))[:29])
        _assert_recovers("H_256_768_22", [payload])


@requires_codec2
class TestBurstDuration(unittest.TestCase):
    """burst_duration_s() must match the samples modulate_burst() actually
    produces, because the duration ceiling is checked against it before a
    burst is ever built. If the two disagree, a configuration the model calls
    legal can overshoot the ceiling on the air."""

    def test_one_frame_at_zero_lead_in_matches_exactly(self):
        with fm_modem.Codec2Fsk(fm_config.NARROW, "H_256_768_22") as m:
            frame = fm_framing.pack(b"x", m.frame_payload_bytes)
            samples = m.modulate_burst([frame], lead_in_s=0.0)
            self.assertEqual(
                m.burst_duration_s(1, lead_in_s=0.0),
                len(samples) / float(m.sample_rate_hz))

    def test_a_fractional_lead_in_matches_within_one_sample(self):
        # The lead-in is quantised to whole samples (int(round(...))), the
        # reported duration is not, so a fractional lead-in agrees only to
        # within one sample. The test pins that quantization rather than
        # pretending it does not exist.
        with fm_modem.Codec2Fsk(fm_config.NARROW, "H_256_768_22") as m:
            frame = fm_framing.pack(b"x", m.frame_payload_bytes)
            for lead in (0.1, 0.5, 1.6):
                with self.subTest(lead_in=lead):
                    samples = m.modulate_burst([frame], lead_in_s=lead)
                    actual = len(samples) / float(m.sample_rate_hz)
                    self.assertAlmostEqual(
                        m.burst_duration_s(1, lead_in_s=lead), actual,
                        delta=1.0 / m.sample_rate_hz)

    def test_multiple_frames_match(self):
        with fm_modem.Codec2Fsk(fm_config.NARROW, "H_256_768_22") as m:
            frame = fm_framing.pack(b"x", m.frame_payload_bytes)
            for n in (1, 2, 3):
                with self.subTest(frames=n):
                    samples = m.modulate_burst([frame] * n, lead_in_s=0.0)
                    self.assertEqual(
                        m.burst_duration_s(n, lead_in_s=0.0),
                        len(samples) / float(m.sample_rate_hz))


@requires_codec2
class TestHasSync(unittest.TestCase):
    """has_sync() is the listen-before-transmit signal, and it must
    discriminate real traffic from the always-loud idle noise this audio path
    sits in (RMS ~0.65-0.75 with zero traffic). It is also transient, so the
    way to observe it matters as much as the value."""

    def _sync_events(self, codename="H_256_768_22"):
        """Modulate one frame, feed it (plus flush) incrementally, and return
        the (decoded, has_sync) history."""
        with fm_modem.Codec2Fsk(fm_config.NARROW, codename) as tx:
            frame = fm_framing.pack(b"a real burst", tx.frame_payload_bytes)
            burst = tx.modulate_burst([frame])
            with fm_modem.Codec2Fsk(fm_config.NARROW, codename) as rx:
                chunk = _nin(rx)
                signal = self._flush_after(rx, burst)
                events = _feed_and_sample(rx, signal, chunk)
        return events

    def _flush_after(self, modem, burst):
        # Enough silence to force the last frame out AND let the sync status
        # decay back to zero, as a whole number of nin-sized chunks. The frame
        # surfaces roughly a burst-length after the burst ends and the status
        # decays over ~0.5-0.6 s after that, so three seconds of silence is
        # comfortably past both; the test measures the modem, not the flush.
        silence = [0.0] * (3 * modem.sample_rate_hz)
        return _pad_to_chunk(list(burst) + silence, _nin(modem))

    def test_noise_never_reports_sync(self):
        with fm_modem.Codec2Fsk(fm_config.NARROW, "H_256_768_22") as m:
            chunk = _nin(m)
            rng = random.Random(0)
            noise = [rng.uniform(-1.0, 1.0) for _ in range(chunk * 40)]
            events = _feed_and_sample(m, noise, chunk)
        self.assertFalse(any(decoded for decoded, _sync in events))
        self.assertFalse(any(sync for _decoded, sync in events),
                         "idle noise must never move the sync indicator, "
                         "because an energy threshold cannot do this job on "
                         "this hardware")

    def test_a_real_burst_syncs_at_the_moment_it_decodes(self):
        # Retried because the first-frame miss (see module docstring) would
        # otherwise read as "no decode, no sync" and flake the test.
        for _ in range(4):
            events = self._sync_events()
            if any(decoded and sync for decoded, sync in events):
                return
        self.fail("sync was never observed alongside a decode")

    def test_sync_is_transient_and_decays_back_to_zero(self):
        # The whole reason listen-before-transmit must sample live rather than
        # after the fact: by the time a burst is fully demodulated, the status
        # has decayed. A stale reading means "unknown", not "idle". The retry
        # guarantees a decode actually happened, so the decay assertion is not
        # vacuous.
        events = self._sync_events()
        for _ in range(4):
            if any(decoded or sync for decoded, sync in events):
                break
            events = self._sync_events()
        self.assertTrue(any(decoded for decoded, _sync in events),
                        "no frame ever decoded, so decay was never exercised")
        self.assertTrue(any(sync for _decoded, sync in events),
                        "sync was never observed, so there was nothing to decay")
        self.assertFalse(events[-1][1],
                         "after the flush, sync must have decayed back to zero")


@requires_codec2
class TestResettingTheReceiveState(unittest.TestCase):
    """reset_rx_state() exists because FreeDV's own GUI resets its receivers
    at the end of every burst -- "a demodulator left 'in sync' from before
    keying would miss the next burst" -- and this transport has that gap (the
    audio service stops feeding the decoder while its own burst plays) and
    never reset anything.

    It reopens the native handle rather than calling freedv_set_sync(), which
    is documented as driving the OFDM state machine; this mode is FSK-LDPC,
    where freedv_get_sync() was already measured returning 0 throughout. An
    API that may silently no-op is no use for a reset whose entire purpose is
    to change hidden state. What these tests protect is that reopening does
    not change anything ELSE.
    """

    def _modem(self):
        return fm_modem.Codec2Fsk(fm_config.OPERATING_POINT.profile,
                                  fm_config.DEFAULT_CODE)

    @requires_codec2
    def test_the_frame_size_survives_a_reset(self):
        """A reopen that quietly produced a different frame size would corrupt
        every length calculation above it -- which is why reset_rx_state
        asserts this itself and does not merely hope."""
        with self._modem() as m:
            before = m.frame_payload_bytes
            m.reset_rx_state()
            self.assertEqual(m.frame_payload_bytes, before)

    @requires_codec2
    def test_a_burst_still_decodes_after_a_reset(self):
        with self._modem() as m:
            payload = b"after the reset"
            frame = fm_framing.pack(payload, m.frame_payload_bytes)
            burst = m.modulate_burst([frame], lead_in_s=0.1)
            self.assertEqual(len(m.demodulate(burst)), 1)
            m.reset_rx_state()
            got = m.demodulate(burst)
            self.assertEqual(len(got), 1)
            self.assertEqual(
                fm_framing.unpack(got[0], m.frame_payload_bytes), payload)

    @requires_codec2
    def test_repeated_resets_are_safe(self):
        """It is called once per burst on a link that runs for days, so a
        handle leaked or double-closed per reset would surface as a process
        that dies after some unremarkable number of transmissions."""
        with self._modem() as m:
            frame = fm_framing.pack(b"x", m.frame_payload_bytes)
            burst = m.modulate_burst([frame], lead_in_s=0.1)
            for _ in range(30):
                m.reset_rx_state()
            self.assertEqual(len(m.demodulate(burst)), 1)

    @requires_codec2
    def test_modulation_still_works_after_a_reset(self):
        """The handle is shared between directions, so a reset taken for the
        receiver must not leave the modulator unusable."""
        with self._modem() as m:
            m.reset_rx_state()
            frame = fm_framing.pack(b"still transmits", m.frame_payload_bytes)
            burst = m.modulate_burst([frame], lead_in_s=0.1)
            self.assertGreater(len(burst), 0)
            self.assertEqual(len(m.demodulate(burst)), 1)

    @requires_codec2
    def test_closing_after_a_reset_is_safe(self):
        with self._modem() as m:
            m.reset_rx_state()
        # the context manager's close() ran on the REOPENED handle


class TestMultiFrame(unittest.TestCase):
    """Frames sharing one preamble decode independently -- the property the
    in-burst-repeat design rests on."""

    def test_every_frame_in_a_burst_is_recovered(self):
        _assert_recovers("H_256_768_22", [b"first", b"second", b"third"])

    def test_a_destroyed_copy_is_silently_absent_and_neighbours_survive(self):
        # Corrupt exactly the middle frame's sample region and the outer two
        # must still decode while the middle one is absent. This is the
        # property that lets a noise burst kill one copy without touching the
        # redundancy around it -- the opposite of concatenated whole bursts,
        # which were measured to lose the middle frame and its neighbours.
        payloads = [b"AAA", b"BBB", b"CCC"]
        frame_payload = _frame_payload("H_256_768_22")
        with fm_modem.Codec2Fsk(fm_config.NARROW, "H_256_768_22") as tx:
            lib = fm_modem._lib()
            frames = [fm_framing.pack(p, frame_payload) for p in payloads]
            burst = tx.modulate_burst(frames, lead_in_s=0.0)
            pre = lib.freedv_get_n_tx_preamble_modem_samples(tx._f)
            n_tx = lib.freedv_get_n_tx_modem_samples(tx._f)
            # The middle frame occupies [pre + n_tx, pre + 2*n_tx).
            damaged = list(burst)
            for i in range(pre + n_tx, pre + 2 * n_tx):
                damaged[i] = 0.0
            with fm_modem.Codec2Fsk(fm_config.NARROW, "H_256_768_22") as rx:
                got = rx.demodulate(damaged)
        recovered = sorted(fm_framing.unpack(g, frame_payload) for g in got)
        self.assertEqual(recovered, [b"AAA", b"CCC"],
                         "the middle copy must vanish, not take its "
                         "neighbours with it")


@requires_codec2
class TestTheDurationModelMatchesTheLibrary(unittest.TestCase):
    """THE CROSS-CHECK PHASE 1 EXISTS FOR.

    fm_config.LinkConfig computes burst duration from the code table in pure
    arithmetic, so the ceiling can be enforced when a configuration is CHOSEN
    -- on a desktop, with no libcodec2, no audio device and no radio. That is
    only safe while the model is true of the modem that will actually transmit,
    and this is what says so.

    The model is three claims about codec2's FSK-LDPC framing:

        preamble  = 50 * (M // 2) symbols
        frame     = (codeword bits + 32) / log2(M) symbols
        postamble = 0

    All three were measured rather than read out of the source. Because codec2
    supports M = 2 and 4 only, testing both tone counts enumerates the whole
    domain of the first formula rather than sampling it. If a codec2 build ever
    changes a frame size, a unique word or the preamble length, this fails here
    -- loudly, in milliseconds, instead of as an over-long burst nobody
    notices until delivery drops on a hillside.
    """

    # Every code, including the 512 B one: constructing a modem is cheap, only
    # modulation is slow, and nothing here modulates.
    ALL_CODES = sorted(fm_config.CODES)

    def _profile(self, tones, rs, fs):
        """A throwaway profile at an arbitrary legal point. The chain carries a
        raised ceiling and no band, because this test is about codec2's
        arithmetic and not about what any cable can pass."""
        return fm_config.Profile(
            name="model-check",
            first_tone_hz=1000,
            tone_spacing_hz=rs,
            tones=tones,
            symbol_rate_bd=rs,
            chain=fm_config.Chain(
                name="none", radios="none", cables="none",
                measured_on="2026-10-02", delivered="nothing, this is a test"),
            sample_rate_hz=fs,
            symbol_rate_ceiling_bd=rs)

    def test_the_arithmetic_agrees_with_the_library_everywhere(self):
        for tones in (2, 4):
            for rs, fs in ((250, 8000), (400, 8000), (250, 48000),
                           (400, 48000), (500, 48000)):
                prof = self._profile(tones, rs, fs)
                for codename in self.ALL_CODES:
                    with self.subTest(tones=tones, rs=rs, fs=fs,
                                      code=codename):
                        # The free function, not a LinkConfig: every code has to
                        # be covered, and the 512 B one cannot BE a LinkConfig
                        # (its 509 B of capacity overflows the length prefix).
                        # Pricing a configuration that is illegal is exactly
                        # what the free function is for.
                        with fm_modem.Codec2Fsk(prof, codename) as m:
                            self.assertAlmostEqual(
                                fm_config.burst_duration_s(
                                    codename, tones, rs, 1, 0.0),
                                m.burst_duration_s(1, 0.0),
                                places=9)

    def test_it_agrees_for_multiple_copies_and_a_lead_in(self):
        """Repeats and the lead-in enter both models the same way, but they are
        where the previous transport's figures diverged from reality, so they
        are checked rather than assumed to cancel."""
        prof = self._profile(4, 400, 48000)
        for repeats in (1, 2, 3):
            for lead_in in (0.0, 0.10, 1.6):
                with self.subTest(repeats=repeats, lead_in=lead_in):
                    cfg = fm_config.LinkConfig(
                        name="x", profile=prof, codename="H_256_768_22",
                        repeats=repeats, lead_in_s=lead_in)
                    with fm_modem.Codec2Fsk(prof, "H_256_768_22") as m:
                        self.assertAlmostEqual(
                            cfg.burst_duration_s(),
                            m.burst_duration_s(repeats, lead_in), places=9)

    def test_the_unique_word_is_thirty_two_bits(self):
        """Asserted against the library directly, because it is the one term in
        the model that is not visible in a code's name."""
        for tones in (2, 4):
            prof = self._profile(tones, 400, 48000)
            for codename in self.ALL_CODES:
                with self.subTest(tones=tones, code=codename):
                    with fm_modem.Codec2Fsk(prof, codename) as m:
                        lib = fm_modem._lib()
                        sps = prof.samples_per_symbol()
                        frame_symbols = (
                            lib.freedv_get_n_tx_modem_samples(m._f) // sps)
                        codeword = fm_config.CODES[codename][1]
                        extra = (frame_symbols * prof.bits_per_symbol()
                                 - codeword)
                        self.assertEqual(extra, fm_config.UNIQUE_WORD_BITS)

    def test_the_preamble_is_fifty_symbols_per_tone_pair(self):
        for tones in (2, 4):
            for rs, fs in ((250, 8000), (400, 48000)):
                prof = self._profile(tones, rs, fs)
                with self.subTest(tones=tones, rs=rs, fs=fs):
                    with fm_modem.Codec2Fsk(prof, "H_256_768_22") as m:
                        lib = fm_modem._lib()
                        got = (lib.freedv_get_n_tx_preamble_modem_samples(m._f)
                               // prof.samples_per_symbol())
                        self.assertEqual(
                            got,
                            fm_config.PREAMBLE_SYMBOLS_PER_PAIR * (tones // 2))

    def test_there_is_no_postamble(self):
        """Zero for FSK-LDPC at every combination measured. The modulator still
        asks for one and appends what it gets, so a build that grows one is
        carried rather than truncated -- but the duration model would then be
        short, which is why this is asserted and not assumed."""
        prof = self._profile(4, 400, 48000)
        with fm_modem.Codec2Fsk(prof, "H_256_768_22") as m:
            self.assertEqual(
                fm_modem._lib().freedv_get_n_tx_postamble_modem_samples(m._f),
                0)

    def test_the_operating_point_measures_2_95_seconds_on_the_real_modem(self):
        """Phase 0's headline figure, end to end: the shipped operating point,
        against the library, at the bench's own 48000 Hz."""
        op = fm_config.OPERATING_POINT
        bench = fm_config.Profile(
            name=op.profile.name, first_tone_hz=op.profile.first_tone_hz,
            tone_spacing_hz=op.profile.tone_spacing_hz,
            tones=op.profile.tones, symbol_rate_bd=op.profile.symbol_rate_bd,
            chain=op.profile.chain, sample_rate_hz=48000)
        with fm_modem.Codec2Fsk(bench, op.codename) as m:
            self.assertAlmostEqual(
                m.burst_duration_s(op.repeats, op.lead_in_s), 2.95, places=6)


if __name__ == "__main__":
    unittest.main()
