"""Tests for the two-burst VOX hang measurement.

    python3 -m unittest discover -s tools -p 'test_*.py'

The single-burst method could not measure hang at all, and only a run on real
hardware revealed it -- it reported 10 ms, which was the width of the tone's
own decay. So this method gets validated against simulated radios with known
VOX behaviour BEFORE it is pointed at anything, and the simulator models the
thing that broke the last one: during hang the transmitter is keyed but
SILENT, because nothing is modulating it.
"""

import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tone_test as tt

FS = 48000


def through_vox(sig, fs, attack_s, hang_s, threshold=0.05,
                room_noise=0.02, seed=1):
    """What a receiving radio outputs, given a transmitting radio's VOX.

    The important detail, and the one the single-burst method missed: while
    the transmitter is keyed but the audio has stopped, the receiver hears
    SILENCE, not carrier. There is nothing modulating it. That is why hang is
    invisible to anything watching received level, and why it has to be
    inferred from whether a later burst had to be keyed up again.
    """
    rng = random.Random(seed)
    n = len(sig)
    out = []
    tx_on = False
    key_at = None
    last_audio = None

    for i in range(n):
        t = i / float(fs)
        loud = abs(sig[i]) >= threshold
        if loud:
            last_audio = t
            if not tx_on and key_at is None:
                key_at = t + attack_s
        if key_at is not None and t >= key_at:
            tx_on, key_at = True, None
        if tx_on and last_audio is not None and t > last_audio + hang_s:
            tx_on = False
            last_audio = None

        if tx_on and loud:
            out.append(sig[i] + rng.gauss(0.0, 0.01))   # tone gets through
        elif tx_on:
            out.append(rng.gauss(0.0, 0.005))           # keyed, unmodulated
        else:
            out.append(rng.gauss(0.0, room_noise))      # squelched
    return out


class TestBurstFinding(unittest.TestCase):

    def test_it_finds_two_clean_bursts(self):
        sig = tt.make_two_burst_signal(0.4, FS)
        heard = through_vox(sig, FS, attack_s=0.0, hang_s=0.0)
        self.assertEqual(len(tt.find_bursts(heard, FS)), 2)

    def test_a_momentary_dropout_does_not_split_a_burst(self):
        sig = tt.make_two_burst_signal(0.4, FS)
        heard = through_vox(sig, FS, attack_s=0.0, hang_s=0.0)
        for i in range(int(0.8 * FS), int(0.81 * FS)):   # 10 ms hole
            heard[i] = 0.0
        self.assertEqual(len(tt.find_bursts(heard, FS)), 2)


class TestHangDetection(unittest.TestCase):
    """The whole point: does the gap get bridged or not?"""

    def _bridged(self, gap, attack, hang):
        sig = tt.make_two_burst_signal(gap, FS)
        heard = through_vox(sig, FS, attack, hang)
        return tt.analyze_two_burst(heard, FS, gap)

    def test_a_gap_shorter_than_hang_is_bridged(self):
        r = self._bridged(gap=0.2, attack=0.13, hang=0.8)
        self.assertIsNone(r["problem"], r["problem"])
        self.assertTrue(r["bridged"],
                        "hang 0.8 s should carry across a 0.2 s gap")

    def test_a_gap_longer_than_hang_is_not(self):
        r = self._bridged(gap=1.2, attack=0.13, hang=0.3)
        self.assertIsNone(r["problem"], r["problem"])
        self.assertFalse(r["bridged"],
                         "hang 0.3 s cannot carry across a 1.2 s gap")

    def test_the_crossover_lands_near_the_real_hang(self):
        # Sweeping the gap and taking the first unbridged one IS the
        # measurement, so it has to land close to the truth.
        hang = 0.5
        first_unbridged = None
        for gap in (0.1, 0.2, 0.3, 0.4, 0.6, 0.8, 1.2):
            r = self._bridged(gap=gap, attack=0.13, hang=hang)
            if r["problem"]:
                continue
            if not r["bridged"]:
                first_unbridged = gap
                break
        self.assertIsNotNone(first_unbridged, "never stopped bridging")
        self.assertTrue(0.4 <= first_unbridged <= 0.8,
                        "crossover at %s for a %.1f s hang"
                        % (first_unbridged, hang))

    def test_a_near_zero_hang_bridges_nothing(self):
        r = self._bridged(gap=0.1, attack=0.13, hang=0.01)
        self.assertFalse(r["bridged"])

    def test_attack_is_measured_from_the_burst_that_was_keyed(self):
        r = self._bridged(gap=1.2, attack=0.13, hang=0.2)
        self.assertAlmostEqual(r["attack_s"], 0.13, delta=0.04)


if __name__ == "__main__":
    unittest.main()
