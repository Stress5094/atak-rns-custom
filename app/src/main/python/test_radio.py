import unittest

import radio


class TestPresets(unittest.TestCase):

    def test_the_meshtastic_table_is_reproduced_exactly(self):
        """Pinned because the point of using their names is that the numbers
        behind them are theirs too. A preset that drifts is worse than a
        preset that was never claimed to match."""
        self.assertEqual(radio.preset_params("Long Fast"), (11, 250000, 5))
        self.assertEqual(radio.preset_params("Medium Fast"), (9, 250000, 5))
        self.assertEqual(radio.preset_params("Short Turbo"), (7, 500000, 5))
        self.assertEqual(radio.preset_params("Long Slow"), (12, 125000, 8))

    def test_presets_run_fastest_to_slowest(self):
        """The list is an axis, and the UI presents it as one."""
        rates = [radio.bitrate_bps(name) for name in radio.preset_names()]
        self.assertEqual(rates, sorted(rates, reverse=True))

    def test_bitrate_matches_what_the_radio_will_report(self):
        # SF11/250k/4:5 -> 1074 bps, the figure the README's comparison uses.
        self.assertAlmostEqual(radio.bitrate_bps("Long Fast"), 1074.2, places=0)
        # The pre-preset setting this app shipped with, for the upgrade note.
        self.assertAlmostEqual(radio.bitrate_bps("Medium Fast"), 3515.6, places=0)

    def test_the_default_is_the_field_tested_preset(self):
        # Short Slow since 2026-10-15: the only preset measured at distance,
        # and the one the relay bucket fits. See radio.DEFAULT_PRESET.
        self.assertEqual(radio.DEFAULT_PRESET, "Short Slow")

    def test_an_unknown_preset_has_no_params_rather_than_raising(self):
        self.assertIsNone(radio.preset_params("Extra Long Turbo"))
        self.assertIsNone(radio.bitrate_bps("Extra Long Turbo"))


class TestBands(unittest.TestCase):

    def test_known_bands_are_recognised(self):
        self.assertEqual(radio.band_name(915000000), "915 MHz US")
        self.assertEqual(radio.band_name(868000000), "868 MHz EU")
        self.assertEqual(radio.band_name(433500000), "433 MHz ISM")

    def test_a_gap_between_bands_is_not_a_band(self):
        self.assertIsNone(radio.band_name(880000000))


class TestValidation(unittest.TestCase):
    """Every rejection here is a mistake that otherwise shows up as "the radio
    does not work", which looks identical to a dead board."""

    def test_a_good_setting_passes(self):
        self.assertIsNone(radio.validate("Long Fast", 915000000, 22))

    def test_an_unknown_preset_is_refused(self):
        self.assertIsNotNone(radio.validate("Turbo Encabulator", 915000000, 22))

    def test_power_above_rns_own_limit_is_refused(self):
        """Pinned to RNS's limit rather than a number of our choosing. Above
        37, RNodeInterface.__init__ fails its config check and the interface
        never comes up -- so a value we accepted but RNS did not would work
        until the next app start and then present as a dead radio."""
        self.assertEqual(radio.TXPOWER_MAX_DBM, 37)
        self.assertIsNotNone(radio.validate("Long Fast", 915000000, 38))
        self.assertIsNone(radio.validate("Long Fast", 915000000, 37))

    def test_power_above_the_boards_own_ceiling_is_allowed(self):
        """Deliberately not refused. The SX1262 clamps above ~22 dBm and
        reports the clamped value back, which the readback check surfaces --
        and what reaches the antenna through an external amplifier is not
        something this app can see, so it does not get to veto it."""
        self.assertIsNone(radio.validate("Long Fast", 915000000, 30))
        self.assertGreater(radio.TXPOWER_MAX_DBM, radio.TYPICAL_BOARD_MAX_DBM)

    def test_negative_power_is_refused(self):
        self.assertIsNotNone(radio.validate("Long Fast", 915000000, -1))

    def test_frequency_outside_the_chip_range_is_refused(self):
        self.assertIsNotNone(radio.validate("Long Fast", 2400000000, 22))
        self.assertIsNotNone(radio.validate("Long Fast", 1000000, 22))

    def test_non_numeric_input_is_refused_rather_than_crashing(self):
        """The fields are free text, so this is what a fat-fingered entry
        actually looks like coming out of the UI."""
        self.assertIsNotNone(radio.validate("Long Fast", "9one5", 22))
        self.assertIsNotNone(radio.validate("Long Fast", 915000000, ""))

    def test_a_channel_hanging_over_the_band_edge_is_refused(self):
        """915 MHz US runs to 928.0. A 250 kHz channel centred at 927.95 puts
        half its energy outside the band."""
        self.assertIsNotNone(radio.validate("Long Fast", 927950000, 22))
        self.assertIsNone(radio.validate("Long Fast", 927800000, 22))

    def test_the_edge_check_scales_with_the_presets_bandwidth(self):
        """Short Turbo is 500 kHz wide, so it runs out of band sooner than a
        125 kHz preset at the same centre."""
        centre = 927800000
        self.assertIsNotNone(radio.validate("Short Turbo", centre, 22))
        self.assertIsNone(radio.validate("Long Slow", centre, 22))

    def test_a_frequency_outside_every_known_band_is_allowed(self):
        """Deliberate. Narrowing to one region's ISM band would just be a US
        assumption with better manners, and this has to work wherever it is
        carried. Legality is the operator's, and the UI says so."""
        self.assertIsNone(radio.validate("Long Fast", 880000000, 22))


class TestDescribe(unittest.TestCase):

    def test_the_line_carries_the_numbers_not_just_the_name(self):
        """Two radios that disagree are compared at a glance, and the preset
        name alone hides a bandwidth difference."""
        line = radio.describe("Long Fast", 915000000, 22)
        self.assertIn("Long Fast", line)
        self.assertIn("915.000", line)
        self.assertIn("SF11", line)
        self.assertIn("250k", line)
        self.assertIn("22 dBm", line)

    def test_an_unknown_preset_still_produces_a_line(self):
        self.assertIn("unknown", radio.describe("Nope", 915000000, 22))


if __name__ == "__main__":
    unittest.main()
