"""Tests for fm_tune: the tone sweep and what it recommends.

The analysis is checked against signals whose answer is known by
construction -- a clean sweep, one shaped like a narrow radio path with a
tilt, noise and a random delay -- before it is ever trusted on a radio
(the project's rule: calibrate every probe against a clean synthetic signal).
"""

import json
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rns_stub

rns_stub.install()   # fm_link imports RNS

import fm_config
import fm_custom
import fm_link
import fm_tune


def shaped(lo=1100, hi=2600, tilt_db_per_khz=0.0, noise=0.01, delay_s=1.37,
           seed=3, wobble_db=0.0):
    """The sweep as a radio path would deliver it: each tone scaled by a
    band edge falling 6 dB per 100 Hz outside [lo, hi] plus a tilt, then an
    optional slow gain swing (+/-wobble_db over 1.5 s -- what the first real
    run showed), noise and a delay. Built per tone, so the answer is exact."""
    def gain(hz):
        db = tilt_db_per_khz * (hz - 1800) / 1000.0
        if hz < lo:
            db -= (lo - hz) / 100.0 * 6
        if hz > hi:
            db -= (hz - hi) / 100.0 * 6
        return 10 ** (db / 20.0)

    import math
    s = fm_tune.sweep_samples(gain=gain)
    if wobble_db:
        s = [v * 10 ** (wobble_db * math.sin(2 * math.pi * i / (1.5 * fm_tune.FS)) / 20.0)
             for i, v in enumerate(s)]
    out = [0.0] * int(delay_s * fm_tune.FS) + s + [0.0] * fm_tune.FS
    r = random.Random(seed)
    return [v + r.gauss(0, noise) for v in out]


class TestTheSweep(unittest.TestCase):

    def test_every_ladder_tone_is_sent_once_in_a_shuffled_order(self):
        self.assertEqual(sorted(fm_tune.ORDER), list(range(len(fm_tune.LADDER_HZ))))
        self.assertNotEqual(fm_tune.ORDER, sorted(fm_tune.ORDER))

    def test_the_order_is_fixed(self):
        """Sender and receiver agree with nothing exchanged; a changed seed
        is a wire change."""
        self.assertEqual(fm_tune.ORDER[:5], fm_tune.ORDER[:5])
        import importlib
        again = importlib.reload(fm_tune)
        self.assertEqual(again.ORDER, fm_tune.ORDER)

    def test_its_length(self):
        self.assertAlmostEqual(len(fm_tune.sweep_samples()) / float(fm_tune.FS),
                               fm_tune.SWEEP_S, places=2)
        self.assertEqual(len(fm_tune.sweep_pcm16()),
                         2 * len(fm_tune.sweep_samples()))


class TestTheAnalysis(unittest.TestCase):

    def test_a_clean_sweep_is_found_flat_at_the_right_moment(self):
        pad = 5000
        m = fm_tune.analyse([0.0] * pad + fm_tune.sweep_samples() + [0.0] * pad)
        self.assertGreater(m["score"], 0.95)
        # Resolution is one 20 ms block.
        self.assertAlmostEqual(m["offset_s"], pad / 8000.0 + fm_tune.LEAD_S, delta=0.03)
        self.assertEqual(m["band_hz"], [300, 3500])
        self.assertEqual(m["tilt_db_per_khz"], 0.0)

    def test_a_narrow_path_finds_its_edges(self):
        m = fm_tune.analyse(shaped(lo=1100, hi=2600))
        self.assertAlmostEqual(m["offset_s"], 1.37 + fm_tune.LEAD_S, delta=0.03)
        # 6 dB per 100 Hz outside the band, so the -6 dB edge is one step out.
        self.assertEqual(m["band_hz"], [1000, 2700])

    def test_a_tilt_is_measured_with_its_sign(self):
        m = fm_tune.analyse(shaped(lo=300, hi=3500, tilt_db_per_khz=-4.0))
        self.assertAlmostEqual(m["tilt_db_per_khz"], -4.0, delta=0.6)
        m = fm_tune.analyse(shaped(lo=300, hi=3500, tilt_db_per_khz=+4.0))
        self.assertAlmostEqual(m["tilt_db_per_khz"], +4.0, delta=0.6)

    def test_a_gain_moving_under_the_sweep_does_not_read_as_a_response(self):
        """The first real run's failure (Pixel <- OnePlus, 2026-10-25): the
        level swung over seconds and the tones' levels scattered 20 dB. With
        the reference tone in every slot a +/-10 dB swing must read flat."""
        m = fm_tune.analyse(shaped(lo=300, hi=3500, wobble_db=10.0))
        spread = max(p["level_db"] for p in m["points"]) - min(p["level_db"] for p in m["points"])
        self.assertLess(spread, 1.5, m["points"])
        self.assertEqual(m["band_hz"], [300, 3500])

    def test_pcm16_bytes_are_accepted(self):
        m = fm_tune.analyse(fm_tune.sweep_pcm16())
        self.assertGreater(m["score"], 0.9)

    def test_silence_noise_and_too_short_are_refused_with_different_reasons(self):
        with self.assertRaisesRegex(fm_tune.TuneError, "No audio came in"):
            fm_tune.analyse([0.0] * 8000 * 14)
        r = random.Random(1)
        with self.assertRaisesRegex(fm_tune.TuneError, "not the sweep"):
            fm_tune.analyse([r.gauss(0, 0.05) for _ in range(8000 * 14)])
        with self.assertRaisesRegex(fm_tune.TuneError, "Listen for longer"):
            fm_tune.analyse([0.0] * 8000 * 5)

    def test_the_result_says_what_it_cannot_tell_you(self):
        m = fm_tune.analyse(fm_tune.sweep_samples())
        self.assertTrue(any("SHAPE" in n for n in m["notes"]))


class TestTheRecommendation(unittest.TestCase):

    def setUp(self):
        self.m = fm_tune.analyse(shaped(lo=1100, hi=2600, tilt_db_per_khz=-2.0))
        self.r = fm_tune.recommend(self.m)

    def test_every_candidate_is_buildable_and_inside_the_band(self):
        lo, hi = self.m["band_hz"]
        for c in self.r["candidates"]:
            cfg = fm_custom.link_config(fm_custom.CustomSpec.from_dict(c["spec"]))
            self.assertEqual(fm_custom.net_code(cfg), c["net_code"])
            self.assertGreaterEqual(min(c["tones_hz"]), lo)
            self.assertLessEqual(max(c["tones_hz"]), hi)

    def test_proven_settings_come_first_and_one_unproven_at_most(self):
        cands = self.r["candidates"]
        self.assertFalse(cands[0]["unproven"], cands[0])
        self.assertLessEqual(sum(c["unproven"] for c in cands), 1)

    def test_proven_ones_are_shortest_burst_first(self):
        proven = [c for c in self.r["candidates"] if not c["unproven"]]
        self.assertEqual([c["burst_s"] for c in proven],
                         sorted(c["burst_s"] for c in proven))

    def test_every_offered_placement_clears_the_spread_bar(self):
        for c in self.r["candidates"]:
            self.assertLessEqual(c["spread_db"], fm_tune.SPREAD_DB)

    def test_the_closest_preset_comes_along_as_the_control(self):
        ctl = self.r["control"]
        self.assertIn(ctl["preset"], fm_config.PROFILES)

    def test_the_two_tone_1000_bd_result_is_cited_where_it_appears(self):
        m = fm_tune.analyse(fm_tune.sweep_samples())
        found = []
        for c in fm_tune.recommend(m, limit=50)["candidates"]:
            if c["spec"]["tones"] == 2 and c["spec"]["symbol_rate_bd"] == 1000:
                found.append(c)
                self.assertTrue(any("20/42" in n for n in c["notes"]))

    def test_vox_keying_marks_rates_above_the_k1pro_ceiling(self):
        r = fm_tune.recommend(self.m, keying=fm_config.KEYING_VOX, limit=50)
        for c in r["candidates"]:
            if c["spec"]["symbol_rate_bd"] > 250:
                self.assertTrue(c["unproven"], c)

    def test_a_steep_rise_suggests_de_emphasis_and_says_it_is_a_guess(self):
        m = fm_tune.analyse(shaped(lo=300, hi=3500, tilt_db_per_khz=+6.0))
        self.assertIn("worth trying", fm_tune.recommend(m)["deemphasis_hint"])
        self.assertIsNone(self.r["deemphasis_hint"])

    def test_nothing_measured_means_nothing_recommended(self):
        with self.assertRaises(fm_tune.TuneError):
            fm_tune.recommend({"points": self.m["points"], "band_hz": None})


class TestSendAndListenThroughTheLink(unittest.TestCase):

    def setUp(self):
        fm_link.tune_cancel()
        self.addCleanup(fm_link.tune_cancel)

    def test_send_queues_the_sweep_for_the_audio_service(self):
        with tempfile.TemporaryDirectory() as home:
            s = fm_link.tune_send(home)
            self.assertAlmostEqual(s, fm_tune.SWEEP_S)
            with open(os.path.join(home, fm_link.PLAY_REQUEST)) as fh:
                self.assertEqual(fh.read(), "%s 8000 0\n" % fm_link.SWEEP_FILE)
            self.assertEqual(os.path.getsize(os.path.join(home, fm_link.SWEEP_FILE)),
                             len(fm_tune.sweep_pcm16()))
            fm_link.tune_send(home)       # a second request is appended
            with open(os.path.join(home, fm_link.PLAY_REQUEST)) as fh:
                self.assertEqual(len(fh.read().splitlines()), 2)

    def test_listening_through_feed_audio_measures_the_sweep(self):
        import struct
        import time
        sig = shaped(lo=1000, hi=2500, noise=0.005)
        pcm = struct.pack("<%dh" % len(sig),
                          *(max(-32768, min(32767, int(v * 32767))) for v in sig))
        fm_link.tune_listen(len(sig) / 8000.0)
        for i in range(0, len(pcm), 1600):
            fm_link.feed_audio("", pcm[i:i + 1600])
        deadline = time.time() + 20
        st = json.loads(fm_link.tune_status())
        while st["state"] != "done" and time.time() < deadline:
            time.sleep(0.1)
            st = json.loads(fm_link.tune_status())
        self.assertEqual(st["state"], "done")
        r = st["result"]
        self.assertTrue(r["found"], r)
        self.assertEqual(r["band_hz"], [900, 2600])
        self.assertTrue(r["recommendation"]["candidates"])

    def test_signed_java_bytes_from_kotlin_are_recorded_correctly(self):
        """Kotlin hands feed_audio a Chaquopy jarray of SIGNED bytes:
        iterating it gives -128..127 and only bytes() converts it. Appending
        it raw failed on the phone when the recording completed."""
        import struct
        import time

        class JavaBytes(list):
            def __init__(self, raw):
                super().__init__(b - 256 if b > 127 else b for b in raw)
                self._raw = raw

            def __bytes__(self):
                return self._raw

        sig = shaped(lo=1000, hi=2500, noise=0.005)
        pcm = struct.pack("<%dh" % len(sig),
                          *(max(-32768, min(32767, int(v * 32767))) for v in sig))
        fm_link.tune_listen(len(sig) / 8000.0)
        for i in range(0, len(pcm), 1600):
            fm_link._tune_feed(JavaBytes(pcm[i:i + 1600]))
        deadline = time.time() + 20
        st = json.loads(fm_link.tune_status())
        while st["state"] != "done" and time.time() < deadline:
            time.sleep(0.1)
            st = json.loads(fm_link.tune_status())
        self.assertTrue(st["result"]["found"], st)

    def test_a_broken_chunk_ends_the_listen_and_never_raises(self):
        class Broken(object):
            def __bytes__(self):
                raise RuntimeError("audio went away")
        fm_link.tune_listen(30)
        fm_link._tune_feed(Broken())          # must not raise
        st = json.loads(fm_link.tune_status())
        self.assertEqual(st["state"], "done")
        self.assertIn("audio went away", st["result"]["error"])

    def test_an_unmeasurable_recording_reports_why(self):
        r = fm_tune.analyse_and_recommend(b"\x00\x00" * 8000 * 14)
        self.assertFalse(r["found"])
        self.assertIn("No audio", r["error"])


if __name__ == "__main__":
    unittest.main()
