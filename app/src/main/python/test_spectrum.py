import unittest

import spectrum


class TestChannelPlan(unittest.TestCase):
    """The planner's only real job is not recommending an illegal channel."""

    def test_channels_tile_the_band(self):
        channels = spectrum.plan_channels(902000000, 928000000, 125000)
        self.assertEqual(channels[0], 902000000 + 62500)
        # Consecutive centres are one bandwidth apart, so nothing is measured
        # twice and nothing is skipped.
        gaps = {b - a for a, b in zip(channels, channels[1:])}
        self.assertEqual(gaps, {125000})

    def test_no_channel_hangs_over_either_band_edge(self):
        """The edge is exactly where a naive range() puts a channel half
        outside the band, which would be recommending an illegal transmission."""
        low, high, bw = 902000000, 928000000, 125000
        for centre in spectrum.plan_channels(low, high, bw):
            self.assertGreaterEqual(centre - bw // 2, low)
            self.assertLessEqual(centre + bw // 2, high)

    def test_a_band_too_narrow_for_one_channel_yields_none(self):
        self.assertEqual(spectrum.plan_channels(902000000, 902050000, 125000), [])

    def test_a_wider_step_still_never_overhangs(self):
        low, high, bw = 863000000, 870000000, 125000
        for centre in spectrum.plan_channels(low, high, bw, step_hz=500000):
            self.assertLessEqual(centre + bw // 2, high)

    def test_nonsense_arguments_are_refused(self):
        with self.assertRaises(ValueError):
            spectrum.plan_channels(902000000, 928000000, 0)
        with self.assertRaises(ValueError):
            spectrum.plan_channels(928000000, 902000000, 125000)


class TestRanking(unittest.TestCase):

    def setUp(self):
        self.sweep = spectrum.Sweep(bandwidth_hz=125000)

    def observe_passes(self, freq, *floors):
        for floor in floors:
            self.sweep.observe(freq, floor)

    def test_the_worst_reading_ranks_not_the_mean(self):
        """A channel silent on most passes and unusable on one is not a good
        channel, but its mean says it is. This is the whole reason for
        sweeping more than once."""
        quiet_but_bursty = 902000000
        steady = 903000000
        self.observe_passes(quiet_but_bursty, -120, -120, -70)   # mean -103
        self.observe_passes(steady, -100, -100, -100)            # mean -100

        ranked = self.sweep.ranked()
        self.assertEqual(ranked[0][0], steady)

    def test_a_single_observation_is_not_enough_to_rank(self):
        self.sweep.observe(902000000, -130)
        self.assertEqual(self.sweep.ranked(), [])

    def test_missing_readings_are_skipped_not_fatal(self):
        self.sweep.observe(902000000, None)
        self.sweep.observe(902000000, -100)
        self.sweep.observe(902000000, -100)
        self.assertEqual(len(self.sweep.ranked()), 1)

    def test_spread_reports_burstiness(self):
        self.observe_passes(902000000, -120, -70)
        self.assertAlmostEqual(self.sweep.spread_db(902000000), 50.0)

    def test_ranking_is_stable_rather_than_dict_ordered(self):
        for freq in (905000000, 903000000, 904000000):
            self.observe_passes(freq, -100, -100)
        self.assertEqual([f for f, _ in self.sweep.ranked()],
                         [903000000, 904000000, 905000000])


class TestRecommendation(unittest.TestCase):
    """The verdict is the product. Getting it wrong sends someone to
    reconfigure every node in the network for nothing."""

    def setUp(self):
        self.sweep = spectrum.Sweep(bandwidth_hz=125000)

    def observe_passes(self, freq, *floors):
        for floor in floors:
            self.sweep.observe(freq, floor)

    def test_no_data_when_nothing_was_observed(self):
        self.assertEqual(self.sweep.recommendation()["verdict"], "no_data")

    def test_a_flat_band_says_so_instead_of_naming_a_winner(self):
        """The case that matters most. Broadband noise makes every channel
        equally bad; naming the marginally-least-bad one implies a fix that
        does not exist."""
        for i, freq in enumerate((902000000, 903000000, 904000000)):
            self.observe_passes(freq, -100 - i, -100 - i)
        result = self.sweep.recommendation(current_hz=902000000)
        self.assertEqual(result["verdict"], "uniform")
        self.assertLess(result["span_db"], spectrum.MEANINGFUL_DB)

    def test_a_genuinely_quieter_channel_says_move(self):
        self.observe_passes(902000000, -90, -90)      # current, loud
        self.observe_passes(915000000, -120, -120)    # much quieter
        result = self.sweep.recommendation(current_hz=902000000)
        self.assertEqual(result["verdict"], "move")
        self.assertEqual(result["best_hz"], 915000000)

    def test_already_on_a_good_channel_says_stay(self):
        """Being on the quietest channel and being told to move to one 1 dB
        better is a reconfiguration of every node for nothing."""
        self.observe_passes(915000000, -119, -119)    # current
        self.observe_passes(902000000, -120, -120)    # nominally best
        self.observe_passes(903000000, -80, -80)      # makes the span wide
        result = self.sweep.recommendation(current_hz=915000000)
        self.assertEqual(result["verdict"], "stay")

    def test_the_uniform_verdict_wins_over_move(self):
        """A flat band must not produce "move" just because the current
        channel happens to sit at the bottom of the noise."""
        self.observe_passes(902000000, -100, -100)
        self.observe_passes(903000000, -102, -102)
        result = self.sweep.recommendation(current_hz=902000000)
        self.assertEqual(result["verdict"], "uniform")


class TestFormatting(unittest.TestCase):

    def test_every_verdict_formats_without_raising(self):
        sweep = spectrum.Sweep(bandwidth_hz=125000)
        self.assertTrue(spectrum.format_recommendation(
            sweep.recommendation()))

        for freq, floor in ((902000000, -90), (915000000, -120)):
            for _ in range(2):
                sweep.observe(freq, floor)
        sweep.note_pass_complete()

        for current in (902000000, 915000000, None):
            lines = spectrum.format_recommendation(
                sweep.recommendation(current_hz=current), top=sweep.ranked()[:3])
            self.assertTrue(all(isinstance(line, str) for line in lines))

    def test_a_flat_band_message_names_the_things_that_would_help(self):
        sweep = spectrum.Sweep(bandwidth_hz=125000)
        for freq in (902000000, 903000000):
            for _ in range(2):
                sweep.observe(freq, -100)
        sweep.note_pass_complete()
        text = " ".join(spectrum.format_recommendation(
            sweep.recommendation(current_hz=902000000)))
        self.assertIn("will not help", text)


if __name__ == "__main__":
    unittest.main()
