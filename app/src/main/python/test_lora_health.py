"""That LoRa's status reading combines its hard attachment signal with a
soft escalation-rate signal honestly -- never calling LoRa degraded from a
single unlucky message, and never calling it up while the radio is not
even attached."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lora_health


class _Clock(object):
    def __init__(self):
        self.t = 500.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _health(clock=None, **kw):
    return lora_health.LoraHealth(clock=clock or _Clock(), **kw)


class TestHardSignal(unittest.TestCase):

    def test_not_online_is_down_regardless_of_delivery_history(self):
        h = _health()
        for _ in range(10):
            h.note_delivered()
        self.assertEqual(h.state(online=False), lora_health.DOWN)

    def test_online_with_no_history_is_up(self):
        self.assertEqual(_health().state(online=True), lora_health.UP)


class TestSoftSignal(unittest.TestCase):
    """The opposite caution from fm_health.py's: LoRa's problem is too FEW
    samples, not a short burst overestimating a swingy link."""

    def test_a_single_escalation_is_not_enough_to_call_it_degraded(self):
        h = _health(min_samples=3)
        h.note_escalated()
        self.assertEqual(h.state(online=True), lora_health.UP)
        self.assertIsNone(h.escalation_rate())

    def test_enough_escalations_reads_degraded(self):
        h = _health(min_samples=3, degraded_rate=0.5)
        h.note_escalated()
        h.note_escalated()
        h.note_delivered()
        self.assertEqual(h.state(online=True), lora_health.DEGRADED)

    def test_mostly_delivered_stays_up_even_with_enough_samples(self):
        h = _health(min_samples=3, degraded_rate=0.5)
        h.note_delivered()
        h.note_delivered()
        h.note_escalated()
        self.assertEqual(h.state(online=True), lora_health.UP)

    def test_congestion_never_feeds_this_signal(self):
        """DISPLACED/CONGESTED mean the channel is already busy, not
        broken -- rns_bridge.py must only call note_escalated() for a true
        reliable.NO_ACK exhaustion, never for those. Nothing in this module
        can enforce that by itself; this test documents the contract by
        confirming delivered-only history never degrades."""
        h = _health(min_samples=1, degraded_rate=0.5)
        h.note_delivered()
        self.assertEqual(h.state(online=True), lora_health.UP)

    def test_old_outcomes_age_out_of_the_window(self):
        clock = _Clock()
        h = _health(clock, window_s=300.0, min_samples=2, degraded_rate=0.5)
        h.note_escalated()
        h.note_escalated()
        self.assertEqual(h.state(online=True), lora_health.DEGRADED)
        clock.advance(301.0)
        h.note_delivered()
        # Only the fresh delivery is left in the window -- one sample,
        # below min_samples, so it goes back to having no opinion.
        self.assertIsNone(h.escalation_rate())
        self.assertEqual(h.state(online=True), lora_health.UP)


class TestSummary(unittest.TestCase):

    def test_down_names_the_radio_not_the_channel(self):
        h = _health()
        self.assertIn("not attached", h.summary(online=False))

    def test_degraded_states_the_rate(self):
        h = _health(min_samples=2, degraded_rate=0.5)
        h.note_escalated()
        h.note_escalated()
        self.assertIn("100%", h.summary(online=True))

    def test_up_does_not_overclaim(self):
        text = _health().summary(online=True).lower()
        for banned in ("reliable", "healthy", "guaranteed"):
            self.assertNotIn(banned, text)


class TestSnapshot(unittest.TestCase):

    def test_it_is_json_ready(self):
        import json
        h = _health(min_samples=2, degraded_rate=0.5)
        h.note_escalated()
        h.note_escalated()
        snap = h.snapshot(online=True)
        json.dumps(snap)  # must not raise
        self.assertEqual(snap["state"], lora_health.DEGRADED)
        self.assertTrue(snap["online"])
        self.assertEqual(snap["samples_in_window"], 2)
        self.assertAlmostEqual(snap["escalation_rate"], 1.0)

    def test_rate_is_none_rather_than_zero_before_enough_samples(self):
        # None reads as "no opinion yet"; zero would read as "confirmed
        # healthy", which nothing here has earned from one sample.
        h = _health(min_samples=3)
        h.note_delivered()
        self.assertIsNone(h.snapshot(online=True)["escalation_rate"])


if __name__ == "__main__":
    unittest.main()
