"""Tests for fm_framing: the two bugs that broke the FM link invisibly.

Both of the failures this module exists to prevent (see its docstring) were
found on real hardware after costing real bench time, and both were invisible
to the obvious test. These are the regression tests that make them desktop
failures instead.
"""

import unittest

import fm_framing


# A realistic frame payload: the 128 B code this link shipped on, less the
# 2 B CRC the physical layer appends for itself.
FRAME = 126


class TestCapacity(unittest.TestCase):

    def test_capacity_is_the_frame_less_the_length_prefix(self):
        self.assertEqual(fm_framing.capacity_for(FRAME),
                         FRAME - fm_framing.LEN_PREFIX_BYTES)

    def test_a_frame_too_small_to_hold_the_prefix_is_refused(self):
        for tiny in (0, 1):
            with self.subTest(frame=tiny):
                with self.assertRaises(fm_framing.FramingError):
                    fm_framing.capacity_for(tiny)

    def test_a_code_too_large_for_a_one_byte_prefix_is_refused(self):
        """The 512 B code is real (it was tried, and failed for duration
        reasons) -- if it is ever picked, this must be a caught error and not
        a silent truncation."""
        with self.assertRaises(fm_framing.FramingError) as caught:
            fm_framing.capacity_for(510)
        self.assertIn("length prefix", str(caught.exception))

    def test_the_largest_frame_a_one_byte_prefix_can_describe_is_accepted(self):
        boundary = fm_framing.MAX_ADDRESSABLE + fm_framing.LEN_PREFIX_BYTES
        self.assertEqual(fm_framing.capacity_for(boundary),
                         fm_framing.MAX_ADDRESSABLE)
        with self.assertRaises(fm_framing.FramingError):
            fm_framing.capacity_for(boundary + 1)


class TestPackedFrameIsAlwaysFullSize(unittest.TestCase):
    """BUG ONE. The physical layer has no length field and hands back a
    fixed-size frame every time, so the framing has to supply the length
    itself -- and the frame it builds has to be exactly the size the modem
    will transmit, or the modem's own padding reintroduces the problem."""

    def test_every_payload_size_produces_a_full_frame(self):
        for n in (0, 1, 2, 22, 60, FRAME - 1):
            with self.subTest(payload=n):
                self.assertEqual(
                    len(fm_framing.pack(b"x" * n, FRAME)), FRAME)

    def test_a_payload_at_exactly_capacity_fits(self):
        payload = b"y" * fm_framing.capacity_for(FRAME)
        self.assertEqual(len(fm_framing.pack(payload, FRAME)), FRAME)

    def test_one_byte_over_capacity_is_refused_with_a_useful_message(self):
        payload = b"y" * (fm_framing.capacity_for(FRAME) + 1)
        with self.assertRaises(fm_framing.FramingError) as caught:
            fm_framing.pack(payload, FRAME)
        msg = str(caught.exception)
        # The message must not suggest raising the frame size, because burst
        # duration is itself a hard constraint.
        self.assertIn("Fragment it upstream", msg)


class TestRoundTrip(unittest.TestCase):

    def test_every_payload_size_round_trips_exactly(self):
        for n in (0, 1, 2, 22, 60, fm_framing.capacity_for(FRAME)):
            with self.subTest(payload=n):
                payload = bytes(bytearray(range(256))[:1] * n) if n else b""
                framed = fm_framing.pack(payload, FRAME)
                self.assertEqual(fm_framing.unpack(framed, FRAME), payload)

    def test_high_bytes_survive(self):
        """Every byte value, including >= 0x80. Signed/unsigned confusion at
        this boundary is a real failure this project has hit before -- a raw
        Java byte array reaching Python produced negative ints that only
        surfaced much deeper as "byte must be in range(0, 256)"."""
        payload = bytes(bytearray(range(256))[:100])
        framed = fm_framing.pack(payload, FRAME)
        self.assertEqual(fm_framing.unpack(framed, FRAME), payload)

    def test_the_physical_crc_trailer_is_stripped(self):
        """A decoded frame arrives with the physical layer's own CRC appended.
        Trusting the frame without stripping it is bug one: the mesh layer
        authenticates by exact length, so the trailer made every real frame
        fail regardless of RF quality."""
        payload = b"a real beacon's worth of bytes"
        framed = fm_framing.pack(payload, FRAME)
        with_crc = framed + b"\xAB\xCD"
        self.assertEqual(fm_framing.unpack(with_crc, FRAME), payload)

    def test_padding_is_not_returned_as_content(self):
        payload = b"short"
        framed = fm_framing.pack(payload, FRAME)
        self.assertEqual(fm_framing.unpack(framed, FRAME), payload)
        self.assertNotIn(b"\x00" * 10, fm_framing.unpack(framed, FRAME))


class TestScrambling(unittest.TestCase):
    """BUG TWO, and the one invisible to a throughput test.

    A ~22 B payload in a 126 B frame is ~83% padding, which is the shape of
    almost all of this link's real traffic. Filled with zeros it produced
    modulation that parked on one tone for ~1 s stretches and decoded zero
    frames; whitened, the same link decoded cleanly. A sweep using near-full
    payloads never generates the long run and so never sees this.
    """

    def test_a_mostly_empty_frame_has_no_long_zero_run(self):
        framed = fm_framing.pack(b"x" * 22, FRAME)
        self.assertNotIn(b"\x00" * 2, framed,
                         "a long zero run produces low-diversity modulation "
                         "the demodulator cannot recover timing from")

    def test_an_all_zero_payload_is_whitened_too(self):
        """The worst realistic case is not an EMPTY payload, it is a full one
        that happens to be zeros -- a zeroed field inside a real message, which
        random padding would have left exactly as it found it."""
        framed = fm_framing.pack(bytes(fm_framing.capacity_for(FRAME)), FRAME)
        self.assertNotIn(b"\x00" * 2, framed)
        self.assertEqual(fm_framing.unpack(framed, FRAME),
                         bytes(fm_framing.capacity_for(FRAME)))

    def test_an_empty_payload_is_still_whitened(self):
        """Nothing but a length byte and padding."""
        framed = fm_framing.pack(b"", FRAME)
        self.assertEqual(len(framed), FRAME)
        self.assertNotIn(b"\x00" * 2, framed)
        self.assertEqual(fm_framing.unpack(framed, FRAME), b"")

    def test_the_frame_is_deterministic(self):
        """Unlike the random padding this replaced. A reproducible frame means
        a bench trial can be repeated and a tx/rx comparison is an equality --
        on a link where every measurement is a counting experiment, that is
        worth more than the unpredictability it gives up, which nothing needed.
        """
        self.assertEqual(fm_framing.pack(b"same", FRAME),
                         fm_framing.pack(b"same", FRAME))

    def test_a_full_payload_needs_no_padding_and_still_round_trips(self):
        payload = b"z" * fm_framing.capacity_for(FRAME)
        self.assertEqual(
            fm_framing.unpack(fm_framing.pack(payload, FRAME), FRAME), payload)

    def test_two_payloads_differing_in_one_byte_differ_in_one_byte(self):
        """An ADDITIVE scrambler, not a self-synchronising one. This is the
        property IL2P's ordering rule is about: the keystream depends on
        position only, so one bit flipped on the air stays one bit flipped
        going into the LDPC decoder. A multiplicative scrambler would feed
        received bits back into its own state and smear each error across its
        taps -- destroying the error correction the whole waveform is chosen
        for.
        """
        a = fm_framing.pack(b"A" + b"x" * 20, FRAME)
        b = fm_framing.pack(b"B" + b"x" * 20, FRAME)
        differing = sum(1 for x, y in zip(bytearray(a), bytearray(b)) if x != y)
        self.assertEqual(differing, 1)


class TestScrambler(unittest.TestCase):
    """The scrambler itself. x^9 + x^4 + 1 seeded to all ones, which is what
    IL2P specifies for exactly this job."""

    def test_it_is_its_own_inverse(self):
        for data in (b"", b"\x00", b"hello", bytes(bytearray(range(256)))):
            with self.subTest(n=len(data)):
                self.assertEqual(fm_framing.scramble(fm_framing.scramble(data)),
                                 data)

    def test_the_keystream_is_a_prefix_of_itself_at_every_length(self):
        """Position-synchronous: the keystream for n bytes must be the first n
        bytes of the keystream for any longer frame, or a 30 B code and a 126 B
        code would disagree about byte 0 and nothing would ever decode across a
        configuration change."""
        long = fm_framing.keystream(130)
        for n in (0, 1, 29, 126):
            with self.subTest(n=n):
                self.assertEqual(fm_framing.keystream(n), long[:n])

    def test_the_register_is_maximal_length(self):
        """511 states. A tap set with a short cycle gives a repeating byte
        pattern, which is the exact failure this is here to prevent -- so the
        cycle length is asserted rather than assumed from the polynomial."""
        ks = fm_framing.keystream(511 + 8)
        self.assertEqual(ks[:8], ks[511:519])
        # And it does NOT repeat sooner than that, at any period which would
        # fit inside a frame. 511 is not a multiple of 8, so the byte stream
        # runs 511 bytes before wrapping even though the bit period is 511 bits.
        for period in range(1, 128):
            self.assertNotEqual(ks[:period], ks[period:2 * period],
                                "keystream repeats every %d bytes" % period)

    def test_the_keystream_is_balanced(self):
        """Roughly half ones, which is what keeps a 4FSK modulator visiting all
        four tones. A biased keystream would whiten the bytes and still park the
        modulation."""
        ks = fm_framing.keystream(256)
        ones = sum(bin(b).count("1") for b in bytearray(ks))
        self.assertAlmostEqual(ones / float(256 * 8), 0.5, delta=0.05)

    def test_scrambling_happens_inside_the_frame_the_fec_will_encode(self):
        """The ordering rule, asserted at the boundary that enforces it: what
        pack() returns is already scrambled, so whatever hands it to the
        encoder cannot get the order wrong. See this module's docstring."""
        payload = b"ordering matters"
        framed = fm_framing.pack(payload, FRAME)
        self.assertNotIn(payload, framed)
        self.assertIn(payload, fm_framing.scramble(framed))


def corrupt_frame(claimed_len, present):
    """A frame as it ARRIVES when the length field is corrupt: scrambled, so
    unpack() unscrambles it back to the claimed length.

    Built this way rather than from a raw b"\\xFF..." literal because the
    length prefix is inside the scrambled region now -- a literal 0xFF on the
    wire unscrambles to something else, and a test asserting on the wrong
    number would pass while describing a different failure than the one the
    log message has to explain.
    """
    return fm_framing.scramble(bytes(bytearray([claimed_len])) + b"x" * present)


class TestRejection(unittest.TestCase):
    """A frame that reaches unpack() has already passed the physical CRC, so a
    rejection here is evidence about the LEVEL, not about noise. It has to be
    reported, not silently dropped -- a bare `continue` here is what let a
    link report "Hearing" while delivering nothing for an evening."""

    def test_a_length_longer_than_the_frame_is_rejected(self):
        self.assertIsNone(fm_framing.unpack(corrupt_frame(255, 10), FRAME))

    def test_an_empty_frame_is_rejected(self):
        self.assertIsNone(fm_framing.unpack(b"", FRAME))

    def test_rejection_is_explained_in_terms_of_the_receive_level(self):
        why = fm_framing.describe_rejection(corrupt_frame(255, 10), FRAME)
        self.assertIn("receive level", why)
        self.assertIn("255", why)

    def test_a_short_frame_rejection_says_so(self):
        why = fm_framing.describe_rejection(b"", FRAME)
        self.assertIn("too short", why)

    def test_a_truncated_frame_is_rejected_rather_than_half_delivered(self):
        """Claiming 60 B of content but carrying 20 must not deliver 20 B of
        a message upstream -- the mesh layer would fail to authenticate it
        anyway, but a partial frame delivered as whole is the worse failure
        because it looks like a decode."""
        framed = fm_framing.pack(b"x" * 60, FRAME)
        self.assertIsNone(fm_framing.unpack(framed[:30], FRAME))


if __name__ == "__main__":
    unittest.main()
