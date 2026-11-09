"""Desktop tests for the frame-position probe.

    python3 -m unittest discover -s tools -p 'test_*.py' -v

No radio and no libcodec2: the modem is stubbed, because what is at risk here
is not the waveform but the BOOKKEEPING -- which position a recovered frame is
credited to, and whether the payload rotation really balances.

The probe answered a live question (the first frame after the preamble is the
one that fails, p = 0.00098) before it had any tests, on the strength of a
loopback run. Loopback proves it decodes; it does not prove the accounting,
because on loopback every frame arrives and every way of counting agrees.
These are the cases where they stop agreeing.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "src", "main", "python"))

import fm_frame_position as fp
import fm_framing


FRAME_PAYLOAD = 126


class StubModem(object):
    """Hands back exactly the frames it was told to, in the order given.

    A stub rather than the real modem on purpose: the question is what
    decode_positions does with a given set of recovered frames, and the real
    modem cannot be made to drop a chosen one.
    """

    frame_payload_bytes = FRAME_PAYLOAD

    def __init__(self, frames):
        self._frames = list(frames)

    def demodulate(self, samples):
        return self._frames


def packed(ident, size):
    return fm_framing.pack(fp.payload_for_id(ident, size), FRAME_PAYLOAD)


class TestPayloads(unittest.TestCase):

    def setUp(self):
        self.size = fm_framing.capacity_for(FRAME_PAYLOAD)

    def test_a_payload_names_itself_in_its_first_byte(self):
        for ident in range(4):
            self.assertEqual(fp.payload_for_id(ident, self.size)[0], ident)

    def test_a_payload_fills_the_frame(self):
        self.assertEqual(len(fp.payload_for_id(0, self.size)), self.size)

    def test_payloads_differ_along_their_whole_length(self):
        """Two payloads differing in ONE byte decode almost identically, which
        would leave a content effect indistinguishable from a position effect
        -- the very thing the rotation exists to separate."""
        a = fp.payload_for_id(0, self.size)
        b = fp.payload_for_id(1, self.size)
        differing = sum(1 for x, y in zip(a, b) if x != y)
        self.assertGreater(differing, self.size // 2,
                           "payloads differ in only %d of %d bytes"
                           % (differing, self.size))

    def test_payloads_are_deterministic(self):
        self.assertEqual(fp.payload_for_id(3, self.size),
                         fp.payload_for_id(3, self.size))


class TestRotation(unittest.TestCase):
    """Balanced by construction rather than by luck: a tally by position and a
    tally by payload are only separable when every payload has spent the same
    number of trials in every position."""

    def test_every_payload_visits_every_position_once_per_cycle(self):
        for frames in (2, 3, 4):
            seen = {(pos, ident): 0
                    for pos in range(frames) for ident in range(frames)}
            for trial in range(frames):
                for pos, ident in enumerate(fp.rotation(trial, frames)):
                    seen[(pos, ident)] += 1
            self.assertEqual(set(seen.values()), {1},
                             "frames=%d is not a balanced rotation" % frames)

    def test_a_rotation_is_a_permutation(self):
        for frames in (2, 3, 5):
            for trial in range(2 * frames):
                self.assertEqual(sorted(fp.rotation(trial, frames)),
                                 list(range(frames)))

    def test_positions_are_not_all_the_same_payload(self):
        self.assertNotEqual(len(set(fp.rotation(0, 3))), 1)


class TestDecodePositionsCreditsTheRightPosition(unittest.TestCase):
    """THE TRAP THIS PROBE EXISTS AROUND. The demodulator returns what it
    recovered, not a slot per frame -- so when the first frame is missing, the
    second one arrives at index 0 of the list. Reading position off the list
    index would report "the first frame is fine and the last is missing",
    which is the exact opposite of the finding."""

    def setUp(self):
        self.size = fm_framing.capacity_for(FRAME_PAYLOAD)

    def test_both_frames_arriving(self):
        ids = [0, 1]
        modem = StubModem([packed(i, self.size) for i in ids])
        got, n, rejected = fp.decode_positions(modem, [], set(ids), self.size)
        self.assertEqual(got, {0, 1})
        self.assertEqual((n, rejected), (2, 0))

    def test_only_the_second_frame_arriving_is_credited_to_the_second(self):
        """The returned list has ONE entry, at index 0. It must still be
        credited to payload 1, not to position 0."""
        ids = [0, 1]
        modem = StubModem([packed(1, self.size)])
        got, n, rejected = fp.decode_positions(modem, [], set(ids), self.size)
        self.assertEqual(got, {1})
        self.assertNotIn(0, got)
        self.assertEqual((n, rejected), (1, 0))

    def test_only_the_first_frame_arriving(self):
        ids = [0, 1]
        modem = StubModem([packed(0, self.size)])
        got, _n, _r = fp.decode_positions(modem, [], set(ids), self.size)
        self.assertEqual(got, {0})

    def test_nothing_arriving(self):
        got, n, rejected = fp.decode_positions(
            StubModem([]), [], {0, 1}, self.size)
        self.assertEqual(got, set())
        self.assertEqual((n, rejected), (0, 0))

    def test_a_frame_with_the_right_id_but_corrupt_filler_is_rejected(self):
        """A correct first byte over wrong content is not an arrival. Counting
        it would credit a position for a frame that did not survive."""
        good = packed(0, self.size)
        corrupt = fm_framing.pack(
            bytes([0]) + bytes(self.size - 1), FRAME_PAYLOAD)
        self.assertNotEqual(good, corrupt)
        got, _n, rejected = fp.decode_positions(
            StubModem([corrupt]), [], {0, 1}, self.size)
        self.assertEqual(got, set())
        self.assertEqual(rejected, 1)

    def test_a_frame_from_outside_this_burst_is_rejected(self):
        """An id nobody sent -- a stray decode from another trial's capture --
        must not be credited to any position here."""
        got, _n, rejected = fp.decode_positions(
            StubModem([packed(7, self.size)]), [], {0, 1}, self.size)
        self.assertEqual(got, set())
        self.assertEqual(rejected, 1)

    def test_an_unframeable_frame_counts_as_rejected(self):
        got, _n, rejected = fp.decode_positions(
            StubModem([bytes(FRAME_PAYLOAD)]), [], {0, 1}, self.size)
        self.assertEqual(got, set())
        self.assertEqual(rejected, 1)

    def test_a_duplicate_of_one_position_is_not_two_arrivals(self):
        """Counted as a SET of ids: the same frame recovered twice is one
        position arriving, not two."""
        got, n, _r = fp.decode_positions(
            StubModem([packed(1, self.size)] * 2), [], {0, 1}, self.size)
        self.assertEqual(got, {1})
        self.assertEqual(n, 2)


class TestTheOverTheAirGuards(unittest.TestCase):
    """Same discipline as fm_bench: nothing keys a radio without a recorded
    note and explicit cards."""

    def _error(self, argv):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                fp.main(argv)
        return err.getvalue()

    def test_a_note_is_required_over_the_air(self):
        self.assertIn("--note is required",
                      self._error(["--tx", "0", "--rx", "1"]))

    def test_cards_are_required_over_the_air(self):
        self.assertIn("--tx and --rx are required",
                      self._error(["--note", "knobs at 11"]))

    def test_one_frame_is_refused_because_position_is_the_question(self):
        self.assertIn("at least 2", self._error(["--loopback", "--frames", "1"]))

    def test_both_directions_under_vox_is_refused(self):
        self.assertIn("deterministic PTT",
                      self._error(["--tx", "0", "--rx", "1", "--vox",
                                   "--both-directions", "--note", "n"]))


if __name__ == "__main__":
    unittest.main()
