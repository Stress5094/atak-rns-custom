"""passphrase_gen: the Generate button's six words."""

import unittest
from unittest import mock

import passphrase_gen as pg


class PassphraseGenTest(unittest.TestCase):
    def test_the_list_is_the_eff_short_list(self):
        # The EFF's 1296 less "yo-yo". A word lost or doubled when the list
        # was pasted in would quietly weaken every passphrase, so pin it.
        self.assertEqual(len(pg.WORDS), 1295)
        self.assertEqual(len(set(pg.WORDS)), 1295)
        self.assertNotIn("yo-yo", pg.WORDS)
        self.assertEqual((pg.WORDS[0], pg.WORDS[-1]), ("acid", "zoom"))
        for w in pg.WORDS:
            self.assertRegex(w, r"^[a-z]+$")   # no dash: dashes separate words
            self.assertLessEqual(len(w), 5)

    def test_six_words_from_the_list_with_single_dashes(self):
        p = pg.generate()
        self.assertRegex(p, r"^[a-z]+(-[a-z]+){5}$")
        words = p.split("-")
        self.assertEqual(len(words), pg.DEFAULT_WORDS)
        self.assertTrue(all(w in pg.WORDS for w in words))

    def test_strength(self):
        self.assertAlmostEqual(pg.bits(), 62.03, places=2)
        self.assertGreaterEqual(pg.bits(), 60)

    def test_it_uses_the_system_random_source(self):
        with mock.patch.object(pg.secrets, "choice", return_value="acorn") as c:
            self.assertEqual(pg.generate(3), "acorn-acorn-acorn")
        self.assertEqual(c.call_count, 3)

    def test_draws_differ(self):
        self.assertEqual(len({pg.generate() for _ in range(50)}), 50)

    def test_zero_words_is_refused(self):
        with self.assertRaises(ValueError):
            pg.generate(0)

    def test_generated_phrases_are_never_weak(self):
        for _ in range(200):
            p = pg.generate()
            self.assertFalse(pg.is_weak(p), p)
            self.assertAlmostEqual(pg.estimate_bits(p), 62.03, places=2)

    def test_a_short_word_and_year_phrase_is_weak(self):
        self.assertTrue(pg.is_weak("alpha squad 2026"))
        self.assertTrue(pg.is_weak("password"))
        self.assertTrue(pg.is_weak("bravo-team-7"))
        self.assertTrue(pg.is_weak(""))
        self.assertTrue(pg.is_weak(None))

    def test_two_generated_words_are_weak_and_four_are_not(self):
        two = "-".join(pg.WORDS[:2])
        four = "-".join(pg.WORDS[:4])
        self.assertAlmostEqual(pg.estimate_bits(two), 2 * 10.34, places=1)
        self.assertTrue(pg.is_weak(two))
        self.assertFalse(pg.is_weak(four))

    def test_the_62_bit_figure_is_accepted_and_unknown_words_are_not_counted_as_words(self):
        six = "-".join(pg.WORDS[100:106])
        self.assertAlmostEqual(pg.estimate_bits(six), pg.bits(), places=6)
        self.assertLess(pg.estimate_bits("-".join(["zzzz"] * 6)), pg.bits())

    def test_a_long_mixed_passphrase_is_not_flagged(self):
        self.assertFalse(pg.is_weak("Tr0ub4dor&3-horse!Staple"))

    def test_non_ascii_does_not_crash(self):
        for p in ("pass\u00e9\u00e8\u2603", "\u30d1\u30b9\u30ef\u30fc\u30c9", "\U0001f600" * 3,
                  "a\x00b", "-".join(["\u00e9"] * 3)):
            bits = pg.estimate_bits(p)
            self.assertIsInstance(bits, float)
            self.assertIsInstance(pg.is_weak(p), bool)


if __name__ == "__main__":
    unittest.main()
