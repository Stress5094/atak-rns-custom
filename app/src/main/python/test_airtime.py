"""Tests for per-link airtime.

    python3 -m unittest discover -s app/src/main/python -p 'test_*.py'

The bridge ran on one airtime function that divided bytes by a bitrate. That
was right while there was one radio and its cost was proportional to its bytes.
FM is a second radio, and it charges 55 ms to key up before a bit moves.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import airtime


class TestFrameCost(unittest.TestCase):

    def test_matches_the_old_formula_when_there_is_no_key_up(self):
        # The LoRa path must not shift by a microsecond: the retry schedule and
        # the TX pacing are both built on this number.
        link = airtime.Link("lora", 3125, per_tx_s=0.0)
        for n in (0, 1, 60, 500):
            self.assertAlmostEqual(link.frame_s(n), ((n + 19) * 8.0) / 3125,
                                   places=12)

    def test_key_up_is_added_once_per_frame_not_per_byte(self):
        slow = airtime.Link("fm", 442, per_tx_s=0.055)
        free = airtime.Link("fm", 442, per_tx_s=0.0)
        for n in (0, 6, 60, 600):
            self.assertAlmostEqual(slow.frame_s(n) - free.frame_s(n), 0.055,
                                   places=12)

    def test_a_negative_payload_is_refused(self):
        with self.assertRaises(ValueError):
            airtime.Link("fm", 442).frame_s(-1)

    def test_header_is_charged(self):
        link = airtime.Link("x", 8.0, per_tx_s=0.0, header_bytes=19)
        self.assertAlmostEqual(link.frame_s(0), 19.0)


class TestLiveBitrate(unittest.TestCase):
    """The RNode reports its bitrate; a downed interface reports 0."""

    def test_a_callable_is_read_each_time(self):
        rate = [1074]
        link = airtime.Link("lora", lambda: rate[0], fallback_bps=3125)
        first = link.frame_s(60)
        rate[0] = 537
        self.assertAlmostEqual(link.frame_s(60), first * 2, places=9)

    def test_zero_falls_back_rather_than_dividing_by_it(self):
        # An infinity here propagates into a sleep() and hangs the sender.
        link = airtime.Link("lora", lambda: 0, fallback_bps=3125)
        self.assertAlmostEqual(link.frame_s(60), ((60 + 19) * 8.0) / 3125)

    def test_none_falls_back(self):
        link = airtime.Link("lora", lambda: None, fallback_bps=3125)
        self.assertAlmostEqual(link.frame_s(60), ((60 + 19) * 8.0) / 3125)

    def test_negative_falls_back(self):
        link = airtime.Link("lora", lambda: -1, fallback_bps=3125)
        self.assertAlmostEqual(link.frame_s(60), ((60 + 19) * 8.0) / 3125)

    def test_a_live_bitrate_without_a_fallback_is_refused_at_construction(self):
        # Better to fail where the link is built than to divide by zero later,
        # in a sleep, on the radio thread.
        with self.assertRaises(ValueError):
            airtime.Link("lora", lambda: 1074)

    def test_a_fixed_bitrate_is_its_own_fallback(self):
        airtime.Link("fm", 442)  # must not raise

    def test_a_nonpositive_fallback_is_refused(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                airtime.Link("fm", lambda: 442, fallback_bps=bad)


class TestOverhead(unittest.TestCase):
    """Header and key-up together, because both are per-frame and neither
    carries a byte anyone asked for."""

    def test_overhead_counts_the_header_not_just_the_key_up(self):
        link = airtime.Link("fm", 442, per_tx_s=0.055)
        header_s = (19 * 8.0) / 442
        self.assertAlmostEqual(link.fixed_s(), 0.055 + header_s, places=12)

    def test_the_header_dominates_the_key_up_at_fm_bitrates(self):
        # The result this module was written not expecting, and the reason
        # small frames are bad on LoRa too.
        link = airtime.Link("fm", 442, per_tx_s=0.055)
        header_s = (19 * 8.0) / 442
        self.assertGreater(header_s, 5 * link.per_tx_s)

    def test_small_frames_are_mostly_overhead_on_both_links(self):
        lora = airtime.Link("lora", 1074, per_tx_s=0.0)
        fm = airtime.Link("fm", 442, per_tx_s=0.055)
        self.assertGreater(lora.overhead_fraction(6), 0.70)
        self.assertGreater(fm.overhead_fraction(6), 0.70)

    def test_overhead_falls_as_the_payload_grows(self):
        fm = airtime.Link("fm", 442, per_tx_s=0.055)
        fractions = [fm.overhead_fraction(n) for n in (6, 60, 200, 600)]
        self.assertEqual(fractions, sorted(fractions, reverse=True))

    def test_a_link_with_no_fixed_cost_at_all_has_no_overhead(self):
        link = airtime.Link("ideal", 1000, per_tx_s=0.0, header_bytes=0)
        self.assertEqual(link.overhead_fraction(60), 0.0)


class TestCapacity(unittest.TestCase):

    def test_frames_per_minute_at_full_duty(self):
        link = airtime.Link("x", 8.0, per_tx_s=0.0, header_bytes=0)
        self.assertAlmostEqual(link.frames_per_minute(1), 60.0)

    def test_duty_scales_it(self):
        link = airtime.Link("x", 8.0, per_tx_s=0.0, header_bytes=0)
        self.assertAlmostEqual(link.frames_per_minute(1, duty=0.2), 12.0)

    def test_fm_carries_far_less_than_lora(self):
        lora = airtime.Link("lora", 1074, per_tx_s=0.0)
        fm = airtime.Link("fm", 442, per_tx_s=0.055)
        self.assertGreater(lora.frames_per_minute(60, 0.2),
                           2 * fm.frames_per_minute(60, 0.2))

    def test_an_impossible_duty_is_refused(self):
        link = airtime.Link("fm", 442)
        for bad in (0.0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                link.frames_per_minute(60, duty=bad)


class TestBundling(unittest.TestCase):
    """What merging frames buys, which is the case for holding them."""

    def test_nothing_to_save_on_a_single_frame(self):
        self.assertEqual(airtime.Link("fm", 442).bundle_saving_s([60]), 0.0)
        self.assertEqual(airtime.Link("fm", 442).bundle_saving_s([]), 0.0)

    def test_saving_is_the_avoided_key_ups_and_headers(self):
        link = airtime.Link("fm", 442, per_tx_s=0.055)
        saved = link.bundle_saving_s([6] * 10)
        self.assertAlmostEqual(saved, 9 * link.fixed_s(), places=9)

    def test_bundling_pays_on_lora_too(self):
        # It is the header doing the work, and LoRa carries the same header.
        lora = airtime.Link("lora", 1074, per_tx_s=0.0)
        self.assertGreater(lora.bundle_saving_s([6] * 10), 1.0)

    def test_bundling_never_costs_airtime(self):
        for link in (airtime.Link("lora", 1074), airtime.Link("fm", 442, per_tx_s=0.055)):
            for lens in ([1, 1], [6] * 4, [200, 1], [0, 0, 0]):
                self.assertGreaterEqual(link.bundle_saving_s(lens), 0.0)


class TestMeasuredConstants(unittest.TestCase):
    """These came off the bench; a change to one should be deliberate."""

    def test_ptt_keyup_is_the_measured_figure(self):
        self.assertAlmostEqual(airtime.FM_PTT_KEYUP_S, 0.055)

    def test_vox_is_recorded_as_worse_than_ptt(self):
        self.assertGreater(airtime.FM_VOX_KEYUP_S, airtime.FM_PTT_KEYUP_S)

    def test_the_aprs_k1_disaster_is_recorded(self):
        # 3.2 s per transmission, measured through two different sound cards.
        self.assertGreater(airtime.FM_VOX_APRS_K1_KEYUP_S,
                           10 * airtime.FM_VOX_KEYUP_S)

    def test_fm_defaults_to_ptt(self):
        self.assertEqual(airtime.fm(442).per_tx_s, airtime.FM_PTT_KEYUP_S)

    def test_lora_charges_no_key_up(self):
        self.assertEqual(airtime.lora(lambda: 1074, 3125).per_tx_s, 0.0)
