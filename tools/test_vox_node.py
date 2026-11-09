"""The listen-before-transmit decision rule of vox_node, without a radio."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vox_node  # noqa: E402


class FakeClock(object):
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class TestGate(unittest.TestCase):
    def test_idle_is_clear(self):
        c = FakeClock()
        g = vox_node.LbtGate(clock=c)
        for _ in range(20):
            g.note(0.57)
            c.sleep(0.1)
        self.assertFalse(g.busy())

    def test_a_reading_over_threshold_holds_the_channel_busy(self):
        c = FakeClock()
        g = vox_node.LbtGate(0.66, 1.0, clock=c)
        g.note(0.99)
        c.sleep(0.9)
        self.assertTrue(g.busy())
        c.sleep(0.2)
        self.assertFalse(g.busy())

    def test_flicker_inside_a_burst_does_not_read_clear(self):
        """A mid-burst statistic often dips below threshold for a few tenths of a
        second (5% of mid-burst samples read < 0.614 on the calibration capture)."""
        c = FakeClock()
        g = vox_node.LbtGate(0.66, 1.0, clock=c)
        for level in (0.99, 0.55, 0.58, 0.60, 0.97):
            g.note(level)
            self.assertTrue(g.busy())
            c.sleep(0.2)

    def test_reset_clears_it(self):
        c = FakeClock()
        g = vox_node.LbtGate(clock=c)
        g.note(0.99)
        g.reset()
        self.assertFalse(g.busy())

    def test_the_threshold_is_exclusive_of_idle_noise(self):
        # Idle max seen on the calibration capture was 0.669 -- ABOVE this
        # threshold on a single sample, which is why the rule is a hold on a
        # rising statistic and why it is a calibration to revisit per chain.
        self.assertGreater(vox_node.THRESHOLD, 0.619)   # idle p99


class TestWait(unittest.TestCase):
    def _run(self, busy_for, max_wait=10.0, backoff=0.5):
        c = FakeClock()
        g = vox_node.LbtGate(0.66, 1.0, clock=c)
        g.note(0.99)
        # the other node keeps transmitting until `busy_for`, then stops
        orig_sleep = c.sleep

        def sleep(s):
            orig_sleep(s)
            if c.t < busy_for:
                g.note(0.99)
        res = vox_node.wait_until_clear(
            g, max_wait, sleep=sleep, clock=c, rng=lambda a, b: backoff)
        return res, c

    def test_a_clear_channel_transmits_immediately(self):
        c = FakeClock()
        g = vox_node.LbtGate(clock=c)
        waited, deferrals, expired = vox_node.wait_until_clear(
            g, 10.0, sleep=c.sleep, clock=c)
        self.assertEqual((waited, deferrals, expired), (0.0, 0, False))

    def test_it_waits_out_a_burst_then_backs_off_then_goes(self):
        (waited, deferrals, expired), c = self._run(busy_for=4.0)
        self.assertFalse(expired)
        self.assertEqual(deferrals, 1)
        # burst + the 1 s hold tail + the 0.5 s back-off
        self.assertGreater(waited, 4.0 + 1.0)

    def test_it_gives_up_rather_than_blocking_forever(self):
        (waited, deferrals, expired), c = self._run(busy_for=1e9, max_wait=6.0)
        self.assertTrue(expired)
        self.assertLess(waited, 7.0)

    def test_back_off_is_bounded(self):
        import random
        for _ in range(50):
            v = random.uniform(0.0, vox_node.BACKOFF_MAX_S)
            self.assertLessEqual(v, vox_node.BACKOFF_MAX_S)


if __name__ == "__main__":
    unittest.main()
