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


if __name__ == "__main__":
    unittest.main()
