import unittest

import cot_codec
import loadtest
from test_cot_codec import compact_path


class TestBody(unittest.TestCase):
    def test_same_seq_gives_the_same_text(self):
        # Two runs at different presets must offer byte-identical traffic, or
        # the comparison is measuring two things at once.
        self.assertEqual(loadtest.body(7, 60), loadtest.body(7, 60))

    def test_different_seq_gives_different_text(self):
        self.assertNotEqual(loadtest.body(7, 60), loadtest.body(8, 60))

    def test_length_is_what_was_asked_for(self):
        for size in (1, 10, 40, 200):
            self.assertEqual(len(loadtest.body(1, size)), size)

    def test_text_is_words_not_filler(self):
        # Filler would compress against the trained dictionary to almost
        # nothing and understate every frame size in the run.
        text = loadtest.body(3, 80)
        self.assertIn(" ", text)
        for word in text.split()[:-1]:      # last one may be cut mid-word
            self.assertIn(word, loadtest.WORDS)


class TestGeochat(unittest.TestCase):
    def chat(self, seq=1, size=40):
        return loadtest.geochat("ANDROID-BENCH-abc", "ALPHA", seq, size)

    def test_is_parseable_cot(self):
        summary = cot_codec.summarize(self.chat())
        self.assertEqual(summary.type, "b-t-f")

    def test_takes_the_compact_path(self):
        # The whole point of shaping it like ATAK's own: a fixture that fell
        # back to zlib would measure a path real traffic never uses.
        self.assertTrue(compact_path(cot_codec.encode(self.chat())))

    def test_bigger_body_makes_a_bigger_frame(self):
        small = len(cot_codec.encode(self.chat(size=20)))
        large = len(cot_codec.encode(self.chat(size=200)))
        self.assertGreater(large, small)

    def test_sequence_appears_in_the_message_id(self):
        # So a frame seen on the air can be tied back to the message that
        # offered it, which is how delivery is counted.
        self.assertIn(b"00000009", self.chat(seq=9))


class TestSchedule(unittest.TestCase):
    def test_interval_is_the_inverse_of_the_rate(self):
        self.assertEqual(loadtest.interval_s(60), 1.0)
        self.assertEqual(loadtest.interval_s(30), 2.0)
        self.assertAlmostEqual(loadtest.interval_s(8), 7.5)

    def test_no_rate_means_no_sending(self):
        self.assertEqual(loadtest.interval_s(0), 0.0)
        self.assertEqual(loadtest.interval_s(-5), 0.0)

    def test_jitter_stays_within_a_quarter(self):
        import random
        rng = random.Random(1)
        for _ in range(200):
            self.assertTrue(1.5 <= loadtest.jittered(2.0, rng) <= 2.5)


if __name__ == "__main__":
    unittest.main()
