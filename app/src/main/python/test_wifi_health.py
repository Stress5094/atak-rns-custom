"""That WiFi's status reading combines its hard attachment signal with a
soft escalation-rate signal honestly -- same contract as
test_lora_health.py, since wifi_health.py deliberately shares LoRa's
template rather than FM's."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wifi_health


class _Clock(object):
    def __init__(self):
        self.t = 500.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _health(clock=None, **kw):
    return wifi_health.WifiHealth(clock=clock or _Clock(), **kw)


class TestHardSignal(unittest.TestCase):

    def test_not_online_is_down_regardless_of_delivery_history(self):
        h = _health()
        for _ in range(10):
            h.note_delivered()
        self.assertEqual(h.state(online=False), wifi_health.DOWN)

    def test_online_with_no_history_is_up(self):
        self.assertEqual(_health().state(online=True), wifi_health.UP)


class TestSoftSignal(unittest.TestCase):

    def test_a_single_escalation_is_not_enough_to_call_it_degraded(self):
        h = _health(min_samples=3)
        h.note_escalated()
        self.assertEqual(h.state(online=True), wifi_health.UP)
        self.assertIsNone(h.escalation_rate())

    def test_enough_escalations_reads_degraded(self):
        h = _health(min_samples=3, degraded_rate=0.5)
        h.note_escalated()
        h.note_escalated()
        h.note_delivered()
        self.assertEqual(h.state(online=True), wifi_health.DEGRADED)

    def test_mostly_delivered_stays_up_even_with_enough_samples(self):
        h = _health(min_samples=3, degraded_rate=0.5)
        h.note_delivered()
        h.note_delivered()
        h.note_escalated()
        self.assertEqual(h.state(online=True), wifi_health.UP)

    def test_old_outcomes_age_out_of_the_window(self):
        clock = _Clock()
        h = _health(clock, window_s=300.0, min_samples=2, degraded_rate=0.5)
        h.note_escalated()
        h.note_escalated()
        self.assertEqual(h.state(online=True), wifi_health.DEGRADED)
        clock.advance(301.0)
        h.note_delivered()
        self.assertIsNone(h.escalation_rate())
        self.assertEqual(h.state(online=True), wifi_health.UP)


class TestSnapshot(unittest.TestCase):

    def test_it_is_json_ready(self):
        import json
        h = _health(min_samples=2, degraded_rate=0.5)
        h.note_escalated()
        h.note_escalated()
        snap = h.snapshot(online=True)
        json.dumps(snap)  # must not raise
        self.assertEqual(snap["state"], wifi_health.DEGRADED)
        self.assertTrue(snap["online"])
        self.assertEqual(snap["samples_in_window"], 2)
        self.assertAlmostEqual(snap["escalation_rate"], 1.0)

    def test_rate_is_none_rather_than_zero_before_enough_samples(self):
        h = _health(min_samples=3)
        h.note_delivered()
        self.assertIsNone(h.snapshot(online=True)["escalation_rate"])


if __name__ == "__main__":
    unittest.main()
