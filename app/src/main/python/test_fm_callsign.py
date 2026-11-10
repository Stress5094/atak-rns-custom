"""Tests for fm_callsign: the clear-text Morse station ID.

The audio is checked by DECODING it back -- an envelope detector and a
dit-length slicer written here, independent of the generator's own schedule --
so a timing or table mistake shows up as the wrong callsign, not just as a
number that changed.
"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rns_stub

rns_stub.install()   # fm_link imports RNS

import fm_callsign
import fm_config
import fm_link

FS = fm_config.SAMPLE_RATE_HZ
REVERSE = {v: k for k, v in fm_callsign.MORSE.items()}


def decode(samples, fs=FS, wpm=fm_callsign.WPM):
    """Morse text from float samples: rectify, smooth over ~1/4 dit, slice
    on/off runs and classify them by length in dits."""
    win = max(1, int(fs * fm_callsign.dit_s(wpm) / 4))
    acc, env = 0.0, []
    buf = [abs(v) for v in samples]
    for i, v in enumerate(buf):
        acc += v
        if i >= win:
            acc -= buf[i - win]
        env.append(acc / win)
    thr = max(env) / 3.0
    runs = []
    cur, n = env[0] > thr, 0
    for e in env:
        on = e > thr
        if on == cur:
            n += 1
        else:
            runs.append((cur, n))
            cur, n = on, 1
    runs.append((cur, n))
    dit = fs * fm_callsign.dit_s(wpm)
    text, letter = "", ""
    for on, n in runs:
        units = n / dit
        if on:
            letter += "." if units < 2 else "-"
        elif units >= 2:
            if letter:
                text += REVERSE[letter]
                letter = ""
            if units >= 5:
                text += " "
    if letter:
        text += REVERSE[letter]
    return text.strip()


class TestNormalize(unittest.TestCase):

    def test_accepts_and_cleans(self):
        self.assertEqual(fm_callsign.normalize(" kx1abc "), "KX1ABC")
        self.assertEqual(fm_callsign.normalize("WRAB123"), "WRAB123")
        self.assertEqual(fm_callsign.normalize("kx1abc/p"), "KX1ABC/P")

    def test_rejects(self):
        for bad in (None, "", "  ", "AB", "KX1-ABC", "KX1 ABC", "BOBBY",
                    "1234", "KX1ABCDEFGHIJ"):
            self.assertIsNone(fm_callsign.normalize(bad), bad)


class TestKeying(unittest.TestCase):

    def test_paris_is_fifty_dits(self):
        # The definition of WPM: "PARIS " is exactly 50 dit units.
        sched = fm_callsign.keying("PARIS")
        units = sum(s for _, s in sched) / fm_callsign.dit_s()
        self.assertAlmostEqual(units + 7, 50.0, places=6)

    def test_starts_and_ends_keyed(self):
        sched = fm_callsign.keying("KX1ABC")
        self.assertTrue(sched[0][0])
        self.assertTrue(sched[-1][0])


class TestAudio(unittest.TestCase):

    def test_decodes_back(self):
        for call in ("KX1ABC", "WRAB123", "KX1ABC/M", "N0Q"):
            s = fm_callsign.samples(call, FS)
            self.assertEqual(decode(s), call)

    def test_decodes_back_with_vox_lead(self):
        s = fm_callsign.samples("KX1ABC", FS, lead_s=1.0)
        # The lead is one long "dah" in front: drop it, the rest is the call.
        text = decode(s)
        self.assertTrue(text.endswith("KX1ABC"), text)

    def test_level_and_edges(self):
        s = fm_callsign.samples("KX1ABC", FS)
        self.assertLessEqual(max(abs(v) for v in s), fm_callsign.AMP + 1e-9)
        self.assertGreater(max(abs(v) for v in s), fm_callsign.AMP * 0.95)
        # Shaped, not hard-keyed: the very first sample and the last of the
        # tail are silent.
        self.assertAlmostEqual(s[0], 0.0, places=6)
        self.assertEqual(s[-1], 0.0)

    def test_duration_matches(self):
        s = fm_callsign.samples("KX1ABC", FS)
        self.assertAlmostEqual(len(s) / float(FS),
                               fm_callsign.duration_s("KX1ABC"), delta=0.01)
        # A typical 6-character call stays around 5 s of airtime.
        self.assertLess(fm_callsign.duration_s("KX1ABC"), 5.5)

    def test_pcm16(self):
        b = fm_callsign.pcm16("KX1ABC", FS)
        self.assertEqual(len(b) % 2, 0)
        peak = max(abs(v) for v in struct.unpack("<%dh" % (len(b) // 2), b))
        self.assertLessEqual(peak, 32767)
        self.assertGreater(peak, 15000)


class TestFmLinkEntry(unittest.TestCase):

    def setUp(self):
        self._saved = fm_link._CONFIG[0]

    def tearDown(self):
        fm_link._CONFIG[0] = self._saved

    def test_invalid_is_none(self):
        self.assertIsNone(fm_link.callsign_id_pcm(""))
        self.assertIsNone(fm_link.callsign_id_pcm("BOB"))
        self.assertFalse(fm_link.callsign_valid("BOB"))
        self.assertTrue(fm_link.callsign_valid("kx1abc"))

    def test_cm108_has_no_lead(self):
        fm_link.set_profile(fm_config.DEFAULT_PROFILE, vox=False)
        b = fm_link.callsign_id_pcm("kx1abc")
        self.assertEqual(b, fm_callsign.pcm16("KX1ABC", fm_link.config().profile.sample_rate_hz))

    def test_vox_adds_lead(self):
        fm_link.set_profile(fm_config.DEFAULT_PROFILE, vox=True)
        b = fm_link.callsign_id_pcm("KX1ABC")
        plain = fm_callsign.pcm16("KX1ABC", fm_link.config().profile.sample_rate_hz)
        extra_s = (len(b) - len(plain)) / 2.0 / fm_link.config().profile.sample_rate_hz
        self.assertGreater(extra_s, fm_link.CALLSIGN_VOX_LEAD_S)
        s = struct.unpack("<%dh" % (len(b) // 2), b)
        self.assertEqual(decode([v / 32767.0 for v in s]).split()[-1][-6:], "KX1ABC")


if __name__ == "__main__":
    unittest.main()
