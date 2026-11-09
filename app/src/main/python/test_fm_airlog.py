"""Tests for fm_airlog: one verdict per burst heard, and the reason it was lost.

Pure -- a fake clock and hand-made observations, no modem. The scenarios are the
ones the 2026-10-09 baseline could not tell apart: 5 of 26 bursts missing, with
"the receiver was transmitting" and "the channel lost it" looking identical.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fm_airlog
import fm_health

BURST_S = 2.95
IN_BAND_RMS = (fm_health.TARGET_RMS_LOW + fm_health.TARGET_RMS_HIGH) / 2.0


class _Clock(object):
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class Harness(object):
    def __init__(self):
        self.clock = _Clock()
        self.ledger = fm_airlog.BurstLedger(BURST_S, clock=self.clock)

    def carrier(self, seconds, level=0.97, peak=0.4, rms=IN_BAND_RMS,
                decoded_at=None, rejected_at=None, reason="", step=0.1):
        """Carrier for `seconds`; optionally a decode landing at `decoded_at`."""
        n = int(round(seconds / step))
        for i in range(n):
            self.clock.advance(step)
            t = (i + 1) * step
            self.ledger.observe(
                True, level, peak, rms,
                decoded=1 if decoded_at is not None and abs(t - decoded_at) < step / 2 else 0,
                rejected=1 if rejected_at is not None and abs(t - rejected_at) < step / 2 else 0,
                reason=reason)

    def quiet(self, seconds, step=0.1, decoded_first=False):
        n = int(round(seconds / step))
        for i in range(n):
            self.clock.advance(step)
            self.ledger.observe(False, 0.55, 0.05, 0.05,
                                decoded=1 if decoded_first and i == 0 else 0)

    def lines(self):
        return self.ledger.pop_events()


class TestWhyABurstWasLost(unittest.TestCase):

    def test_a_decoded_burst_is_ok(self):
        h = Harness()
        h.carrier(3.0, decoded_at=2.9)
        h.quiet(3.0)
        (line,) = h.lines()
        self.assertIn("burst OK", line)
        self.assertIn("1 frame(s) decoded", line)

    def test_a_decode_landing_after_the_carrier_fell_still_counts(self):
        # codec2 decodes at the END of the frame and the carrier statistic lags,
        # so the decode can arrive after the carrier has already dropped.
        h = Harness()
        h.carrier(3.0)
        h.quiet(0.4, decoded_first=True)
        h.quiet(3.0)
        (line,) = h.lines()
        self.assertIn("burst OK", line)

    def test_a_full_carrier_with_nothing_decoded_is_lost_and_hedged(self):
        h = Harness()
        h.carrier(3.0)
        h.quiet(3.0)
        (line,) = h.lines()
        self.assertIn("burst LOST", line)
        self.assertIn("in band", line)
        # The hint is a guess from the level and must say so.
        self.assertIn("a guess", line)

    def test_a_low_level_is_named_as_a_possible_weak_signal(self):
        h = Harness()
        h.carrier(3.0, rms=0.03)
        h.quiet(3.0)
        (line,) = h.lines()
        self.assertIn("burst LOST", line)
        self.assertIn("LOW", line)

    def test_a_clipped_level_says_turn_the_volume_down(self):
        h = Harness()
        h.carrier(3.0, peak=0.99, rms=0.4)
        h.quiet(3.0)
        (line,) = h.lines()
        self.assertIn("full scale", line)
        self.assertIn("volume down", line)

    def test_a_short_carrier_is_cut_short_not_lost(self):
        h = Harness()
        h.carrier(0.6)
        h.quiet(8.0)            # grace, then the MERGE_S hold, then it is logged
        (line,) = h.lines()
        self.assertIn("burst CUT SHORT", line)

    def test_a_crc_valid_but_unusable_frame_is_rejected_with_its_reason(self):
        h = Harness()
        h.carrier(3.0, rejected_at=2.9, reason="length prefix 9999 > 125")
        h.quiet(3.0)
        (line,) = h.lines()
        self.assertIn("burst REJECTED", line)
        self.assertIn("length prefix 9999", line)

    def test_a_decode_with_no_carrier_is_a_detector_miss_not_a_channel_event(self):
        h = Harness()
        h.quiet(1.0)
        h.clock.advance(0.1)
        h.ledger.observe(False, 0.55, 0.3, 0.2, decoded=1)
        (line,) = h.lines()
        self.assertIn("detector", line)
        self.assertNotIn("LOST", line)


class TestTheDetectorsHoldDoesNotPadTheVerdict(unittest.TestCase):
    """fm_carrier keeps `busy` true for a second after a burst ends. That is for
    listen-before-transmit; counted here it would make a 0.6 s impulse read as a
    1.6 s carrier (not cut short) and dilute a burst's level with idle noise."""

    def _with_hold(self, h, carrier_s, hold_s=1.0, step=0.1, **kw):
        h.carrier(carrier_s, **kw)
        for _ in range(int(round(hold_s / step))):
            h.clock.advance(step)
            # busy (the hold) but not strong, and idle-loud audio
            h.ledger.observe(True, 0.55, 0.9, 0.7, strong=False)

    def test_an_impulse_is_still_cut_short_through_the_hold(self):
        h = Harness()
        self._with_hold(h, 0.6)
        h.quiet(8.0)
        (line,) = h.lines()
        self.assertIn("burst CUT SHORT", line)

    def test_idle_noise_in_the_hold_does_not_hide_a_weak_burst(self):
        h = Harness()
        self._with_hold(h, 3.0, rms=0.03)
        h.quiet(3.0)
        (line,) = h.lines()
        self.assertIn("LOW", line)

    def test_the_reported_carrier_length_excludes_the_hold(self):
        h = Harness()
        self._with_hold(h, 3.0)
        h.quiet(3.0)
        (line,) = h.lines()
        self.assertIn("carrier 2.9 s", line)   # 3.0 s of strong chunks, less the first step


class TestOverOurOwnTransmit(unittest.TestCase):

    def test_a_carrier_inside_our_transmit_window_is_over_our_tx(self):
        h = Harness()
        h.ledger.note_tx(True)
        h.clock.advance(1.0)
        h.carrier(2.0)          # heard while the flag is up
        h.ledger.note_tx(False)
        h.quiet(3.0)
        (line,) = h.lines()
        self.assertIn("burst OVER OUR TX", line)

    def test_a_burst_that_began_under_our_transmit_is_over_our_tx(self):
        # The receive loop is not fed while we transmit, so the carrier first
        # appears AFTER the transmit ended -- within the lead window.
        h = Harness()
        h.ledger.note_tx(True)
        h.clock.advance(3.0)
        h.ledger.note_tx(False)
        h.clock.advance(0.3)
        h.carrier(1.5)
        h.quiet(3.0)
        (line,) = h.lines()
        self.assertIn("burst OVER OUR TX", line)

    def test_a_transmit_long_before_does_not_claim_a_clean_loss(self):
        h = Harness()
        h.ledger.note_tx(True)
        h.clock.advance(3.0)
        h.ledger.note_tx(False)
        h.quiet(20.0)
        h.carrier(3.0)
        h.quiet(3.0)
        (line,) = h.lines()
        self.assertIn("burst LOST", line)
        self.assertNotIn("OVER OUR TX", line)

    def test_a_decoded_burst_is_ok_even_if_we_transmitted_nearby(self):
        h = Harness()
        h.ledger.note_tx(True)
        h.clock.advance(1.0)
        h.ledger.note_tx(False)
        h.carrier(3.0, decoded_at=2.9)
        h.quiet(3.0)
        (line,) = h.lines()
        self.assertIn("burst OK", line)

    def test_unkey_without_key_is_harmless(self):
        h = Harness()
        h.ledger.note_tx(False)
        h.carrier(3.0)
        h.quiet(3.0)
        self.assertEqual(len(h.lines()), 1)


class TestABlipThatThenDecodedIsOneBurst(unittest.TestCase):
    """The detector flickers, so one burst can read as a blip, a gap and a
    decode. 2026-10-09: both "CUT SHORT, carrier 0.0 s" lines of a 26-burst run
    were bursts that went on to be delivered, the decode ~1.2 s after the
    blip's verdict. Reported as an impulse AND a detector miss, one burst was
    being counted twice and mislabelled once."""

    def test_a_blip_then_a_decode_with_no_carrier_is_one_ok(self):
        h = Harness()
        h.carrier(0.3)
        h.quiet(2.5)                       # the blip settles and is HELD
        self.assertEqual(h.lines(), [])
        h.clock.advance(0.5)
        h.ledger.observe(False, 0.55, 0.3, 0.2, decoded=1)
        (line,) = h.lines()
        self.assertIn("burst OK", line)
        self.assertIn("flickered", line)
        h.quiet(8.0)
        self.assertEqual(h.lines(), [])    # and no CUT SHORT arrives later
        self.assertEqual(h.ledger.summary(), "OK 1")

    def test_a_blip_then_a_short_second_carrier_that_decodes_is_one_ok(self):
        h = Harness()
        h.carrier(0.3)
        h.quiet(2.5)
        h.carrier(0.8, decoded_at=0.7)
        h.quiet(4.0)
        (line,) = h.lines()
        self.assertIn("burst OK", line)
        self.assertIn("flickered", line)
        self.assertEqual(h.ledger.summary(), "OK 1")

    def test_a_blip_with_no_decode_is_still_reported_once_the_hold_ends(self):
        h = Harness()
        h.carrier(0.3)
        h.quiet(2.5)
        self.assertEqual(h.lines(), [])    # held, not yet reported
        h.quiet(6.0)
        (line,) = h.lines()
        self.assertIn("burst CUT SHORT", line)

    def test_a_decode_long_after_a_blip_does_not_absorb_it(self):
        h = Harness()
        h.carrier(0.3)
        h.quiet(9.0)                       # blip reported as CUT SHORT
        a, = h.lines()
        self.assertIn("CUT SHORT", a)
        h.carrier(3.0, decoded_at=2.9)
        h.quiet(4.0)
        (b,) = h.lines()
        self.assertIn("burst OK", b)
        self.assertNotIn("flickered", b)

    def test_a_second_blip_flushes_the_first(self):
        h = Harness()
        h.carrier(0.3)
        h.quiet(2.2)
        h.carrier(0.3)
        h.quiet(9.0)
        lines = h.lines()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(all("CUT SHORT" in l for l in lines))


class TestHowLongWeTransmitted(unittest.TestCase):
    """For the sequence-gap line: a numbered frame that never arrived while we
    were transmitting was probably hidden by it, and the receive path has no
    episode to say so because it is not fed audio while the radio keys up."""

    def test_nothing_transmitted_reads_zero(self):
        h = Harness()
        h.clock.advance(100)
        self.assertEqual(h.ledger.tx_within(30), (0, 0.0))

    def test_a_finished_transmit_inside_the_interval_is_counted(self):
        h = Harness()
        h.ledger.note_tx(True)
        h.clock.advance(3.5)
        h.ledger.note_tx(False)
        h.clock.advance(5.0)
        n, secs = h.ledger.tx_within(30)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(secs, 3.5)

    def test_a_transmit_before_the_interval_is_not(self):
        h = Harness()
        h.ledger.note_tx(True)
        h.clock.advance(3.5)
        h.ledger.note_tx(False)
        h.clock.advance(60.0)
        self.assertEqual(h.ledger.tx_within(20), (0, 0.0))

    def test_a_transmit_straddling_the_edge_counts_only_its_inside_part(self):
        h = Harness()
        h.ledger.note_tx(True)
        h.clock.advance(4.0)
        h.ledger.note_tx(False)
        h.clock.advance(10.0)
        n, secs = h.ledger.tx_within(12.0)      # reaches back 2 s into the 4 s
        self.assertEqual(n, 1)
        self.assertAlmostEqual(secs, 2.0)

    def test_a_transmit_still_in_progress_counts(self):
        h = Harness()
        h.clock.advance(10.0)
        h.ledger.note_tx(True)
        h.clock.advance(2.0)
        n, secs = h.ledger.tx_within(30)
        self.assertEqual(n, 1)
        self.assertAlmostEqual(secs, 2.0)


class TestSeveralBursts(unittest.TestCase):

    def test_two_bursts_a_few_seconds_apart_are_two_verdicts(self):
        h = Harness()
        h.carrier(3.0, decoded_at=2.9)
        h.quiet(2.0)
        h.carrier(3.0)
        h.quiet(3.0)
        a, b = h.lines()
        self.assertIn("burst OK", a)
        self.assertIn("burst LOST", b)

    def test_a_decode_is_credited_to_the_burst_it_belongs_to(self):
        # A new carrier opening while the last is still waiting out its grace
        # must settle the old one first, not inherit its decode.
        h = Harness()
        h.carrier(3.0)
        h.quiet(0.4)
        h.carrier(3.0, decoded_at=2.9)
        h.quiet(3.0)
        a, b = h.lines()
        self.assertIn("burst LOST", a)
        self.assertIn("burst OK", b)

    def test_counts_and_summary(self):
        h = Harness()
        self.assertEqual(h.ledger.summary(), "no bursts heard yet")
        h.carrier(3.0, decoded_at=2.9)
        h.quiet(3.0)
        h.carrier(3.0)
        h.quiet(3.0)
        h.lines()
        self.assertEqual(h.ledger.summary(), "OK 1, LOST 1")

    def test_pop_events_drains(self):
        h = Harness()
        h.carrier(3.0)
        h.quiet(3.0)
        self.assertEqual(len(h.lines()), 1)
        self.assertEqual(h.lines(), [])

    def test_history_is_bounded(self):
        h = Harness()
        for _ in range(fm_airlog.MAX_EVENTS + 10):
            h.carrier(3.0)
            h.quiet(2.0)
        h.quiet(3.0)
        self.assertEqual(len(h.ledger.history), fm_airlog.MAX_EVENTS)


if __name__ == "__main__":
    unittest.main()
