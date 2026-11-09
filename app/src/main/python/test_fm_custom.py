"""Tests for fm_custom: operator-entered FM settings.

Three promises, each tested here:
  1. The presets are untouched -- a spec copied from a preset builds the
     same burst, and nothing in PROFILES changed.
  2. Nothing codec2 would abort on, and nothing over the burst ceiling, can be
     built -- with a message that says why.
  3. The net code holds exactly what must match, and a custom spec equal to a
     preset is on the same net as that preset.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rns_stub

rns_stub.install()   # fm_link imports RNS

import fm_config
import fm_custom
import fm_link
from fm_custom import CustomSpec


def spec(**over):
    d = CustomSpec.from_preset("fast").to_dict()
    d.update(over)
    return CustomSpec.from_dict(d)


def build(**over):
    return fm_custom.link_config(spec(**over))


class TestThePresetsAreUntouched(unittest.TestCase):

    def test_a_preset_copied_into_a_spec_builds_the_same_burst(self):
        for name in fm_config.PROFILES:
            want = fm_config.link_config(name)
            got = fm_custom.link_config(CustomSpec.from_preset(name))
            self.assertEqual(got.profile.tone_list(), want.profile.tone_list(), name)
            self.assertEqual(got.profile.symbol_rate_bd, want.profile.symbol_rate_bd, name)
            self.assertEqual(got.codename, want.codename, name)
            self.assertEqual(got.repeats, want.repeats, name)
            self.assertEqual(got.keying, want.keying, name)
            self.assertEqual(got.lead_in_s, want.lead_in_s, name)
            self.assertEqual(got.deemphasis_us(), want.deemphasis_us(), name)
            self.assertAlmostEqual(got.burst_duration_s(), want.burst_duration_s(), 9)
            self.assertEqual(got.capacity_bytes(), want.capacity_bytes(), name)
            self.assertEqual(fm_custom.net_code(got), fm_custom.net_code(want), name)

    def test_the_vox_option_builds_what_the_preset_path_builds(self):
        """Ticking "no PTT pin" on a custom spec must give the same lead-in and
        filter as ticking it on a preset, or the two paths disagree about the
        same cable.

        Not `narrow`: its spec carries the chain's measured 1.6 s explicitly,
        and an explicit lead-in is the operator's and wins over the VOX
        default (the preset path replaces it with 2.2 s)."""
        for name in fm_config.PROFILES:
            if CustomSpec.from_preset(name).lead_in_s is not None:
                continue
            fm_link.set_profile(name, vox=True)
            want = fm_link.config()
            got = fm_custom.link_config(CustomSpec.from_preset(name), vox=True)
            self.assertEqual(got.keying, want.keying, name)
            self.assertEqual(got.lead_in_s, want.lead_in_s, name)
            self.assertEqual(got.deemphasis_us(), want.deemphasis_us(), name)
        fm_link.set_profile(fm_config.DEFAULT_PROFILE)

    def test_narrows_measured_lead_in_is_copied(self):
        self.assertEqual(CustomSpec.from_preset("narrow").lead_in_s, 1.6)
        self.assertIsNone(CustomSpec.from_preset("fast").lead_in_s)

    def test_custom_is_not_offered_as_a_measured_preset(self):
        self.assertNotIn(fm_custom.CUSTOM_NAME, fm_config.PROFILES)
        self.assertEqual(fm_config.profile_names(), ["fast", "narrow", "wide"])

    def test_a_spec_equal_to_a_preset_says_so(self):
        for name in fm_config.PROFILES:
            cfg = fm_custom.link_config(CustomSpec.from_preset(name))
            self.assertEqual(fm_custom.matching_preset(cfg), name)
        self.assertIsNone(fm_custom.matching_preset(build(first_tone_hz=1100)))

    def test_the_default_preset_raises_no_warnings(self):
        """A warning on the default would train operators to ignore them."""
        self.assertEqual(fm_custom.warnings_for(fm_config.OPERATING_POINT), [])


class TestNothingIllegalCanBeBuilt(unittest.TestCase):
    """Every refusal here is either a codec2 assertion (which aborts the
    process, not raises) or the burst ceiling."""

    def refused(self, fragment, **over):
        with self.assertRaises(fm_config.ProfileError) as caught:
            build(**over)
        self.assertIn(fragment, str(caught.exception))

    def test_only_two_or_four_tones(self):
        self.refused("2 or 4 ONLY", tones=8, tone_spacing_hz=200,
                     symbol_rate_bd=200, first_tone_hz=1000)
        self.refused("power of two", tones=3)

    def test_the_rate_must_divide_the_sample_rate(self):
        self.refused("divide the sample rate", symbol_rate_bd=300,
                     tone_spacing_hz=300)

    def test_too_few_samples_per_symbol(self):
        self.refused("samples per symbol", symbol_rate_bd=2000,
                     tone_spacing_hz=2000, first_tone_hz=300, tones=2)

    def test_tones_closer_than_the_rate(self):
        self.refused("not orthogonal", tone_spacing_hz=300)

    def test_above_nyquist(self):
        self.refused("Nyquist", first_tone_hz=3000, tone_spacing_hz=400)

    def test_below_the_radios_audio_band(self):
        self.refused("sub-audible", first_tone_hz=200)

    def test_over_the_burst_ceiling(self):
        self.refused("ceiling", symbol_rate_bd=100, tone_spacing_hz=100,
                     repeats=3)

    def test_a_code_the_framing_cannot_describe(self):
        self.refused("length prefix", codename="H_4096_8192_3d")

    def test_ranges_on_the_per_node_fields(self):
        self.refused("copies", repeats=0)
        self.refused("copies", repeats=4)
        self.refused("lead-in", lead_in_s=3.5)
        self.refused("lead-in", lead_in_s=-0.1)
        self.refused("de-emphasis", deemphasis_us=5000)
        self.refused("keying", keying="carrier")
        self.refused("unknown error-correction code", codename="H_1_2")

    def test_measured_evidence_is_reported_as_measured(self):
        cfg = build(evidence={"measured_on": "2026-10-25",
                              "delivered": "29 of 30 beacons"})
        self.assertIn("29 of 30 beacons", fm_custom.evidence_text(cfg))
        self.assertIn("not measured", fm_custom.evidence_text(build()))

    def test_a_measured_band_is_enforced_and_an_unmeasured_one_is_not(self):
        build(first_tone_hz=600)        # legal: no band claimed
        with self.assertRaises(fm_config.ProfileError) as caught:
            build(first_tone_hz=600,
                  evidence={"measured_on": "2026-10-25",
                            "usable_band_hz": [1000, 3000]})
        self.assertIn("outside", str(caught.exception))


class TestTheSpecRoundTrips(unittest.TestCase):

    def test_json_round_trip(self):
        s = spec(tones=2, first_tone_hz=1200, tone_spacing_hz=1000,
                 symbol_rate_bd=500, repeats=2, keying="vox", lead_in_s=1.2,
                 deemphasis_us=250.0,
                 evidence={"measured_on": "2026-10-25", "delivered": "9/10"})
        self.assertEqual(CustomSpec.from_json(s.to_json()), s)

    def test_defaults_follow_the_keying_rather_than_being_frozen(self):
        """None means "this keying's default", so switching VOX -> PTT does not
        keep VOX's 2.2 s lead-in."""
        vox = spec(keying="vox")
        self.assertEqual(vox.resolved_lead_in_s(), fm_config.VOX_CABLE_LEAD_IN_S)
        ptt = spec(keying="cm108")
        self.assertEqual(ptt.resolved_lead_in_s(),
                         fm_config.KEYING_LEAD_IN_S[fm_config.KEYING_CM108])

    def test_bad_input_is_a_message_not_a_crash(self):
        for text in ("", "not json", "[]", "null",
                     '{"tones": "four"}', '{"tones": true}',
                     '{"first_tone_hz": 1000.5}', '{"tone": 4}',
                     '{"evidence": {"colour": "red"}}'):
            with self.assertRaises(fm_custom.SpecError, msg=text):
                CustomSpec.from_json(text)

    def test_a_misspelt_field_is_refused_not_defaulted(self):
        with self.assertRaises(fm_custom.SpecError) as caught:
            CustomSpec.from_dict({"symbol_rate": 250})
        self.assertIn("symbol_rate", str(caught.exception))


class TestTheNetCode(unittest.TestCase):

    def test_the_default_reads_as_expected(self):
        self.assertEqual(fm_custom.net_code(fm_config.OPERATING_POINT),
                         "4T-1000/400-400Bd-H1024.2048")

    def test_code_tokens_are_unique(self):
        tokens = [fm_custom._code_token(c) for c in fm_config.CODES]
        self.assertEqual(len(tokens), len(set(tokens)))

    def test_it_round_trips_for_every_preset_and_some_customs(self):
        cfgs = [fm_config.link_config(n) for n in fm_config.PROFILES] + [
            build(tones=2, first_tone_hz=1000, tone_spacing_hz=1000,
                  symbol_rate_bd=500),
            build(codename="H_256_768_22", symbol_rate_bd=250,
                  tone_spacing_hz=300, first_tone_hz=1350)]
        for cfg in cfgs:
            code = fm_custom.net_code(cfg)
            back = fm_custom.link_config(fm_custom.parse_net_code(code))
            self.assertEqual(fm_custom.net_code(back), code)

    def test_it_holds_only_what_must_match(self):
        """Copies, keying, lead-in and filter are per node: two nodes differing
        only in those hear each other, so the code must not differ either."""
        base = fm_custom.net_code(build())
        for over in (dict(repeats=2), dict(keying="vox"), dict(lead_in_s=0.5),
                     dict(deemphasis_us=250.0)):
            self.assertEqual(fm_custom.net_code(build(**over)), base, over)
        for over in (dict(first_tone_hz=1100), dict(tone_spacing_hz=500),
                     dict(symbol_rate_bd=250, tone_spacing_hz=400),
                     dict(codename="H_256_768_22"),
                     dict(tones=2)):
            self.assertNotEqual(fm_custom.net_code(build(**over)), base, over)

    def test_parsing_is_forgiving_of_case_and_strict_of_shape(self):
        s = fm_custom.parse_net_code(" 4t-1500/250-250bd-h1024.2048 ")
        self.assertEqual((s.first_tone_hz, s.symbol_rate_bd), (1500, 250))
        for bad in ("", "fast", "4T-1000-400-400Bd-H1024.2048",
                    "4T-1000/400-400Bd-H9.9"):
            with self.assertRaises(fm_custom.SpecError, msg=bad):
                fm_custom.parse_net_code(bad)

    def test_the_mesh_envelope_matches_mesh(self):
        import mesh
        self.assertEqual(fm_custom.MESH_ENVELOPE_B, mesh.OVERHEAD)


class TestTheEditorsChoices(unittest.TestCase):

    def test_every_offered_rate_can_be_built(self):
        for tones in (2, 4):
            rates = fm_custom.legal_rates(tones)
            self.assertIn(400, rates)
            for r in rates:
                fm_config.Profile(
                    name="probe", first_tone_hz=fm_custom.MIN_FIRST_TONE_HZ,
                    tone_spacing_hz=r, tones=tones, symbol_rate_bd=r,
                    chain=fm_custom._chain_for(spec(symbol_rate_bd=r)))

    def test_every_rate_that_divides_is_offered(self):
        want = [r for r in range(1000, 99, -1) if 8000 % r == 0]
        self.assertEqual(fm_custom.legal_rates(4), want)

    def test_every_offered_code_can_carry_a_frame(self):
        names = [row["codename"] for row in fm_custom.codes()]
        self.assertIn(fm_config.DEFAULT_CODE, names)
        self.assertNotIn("H_4096_8192_3d", names)
        for name in names:
            build(codename=name)

    def test_choices_json_is_complete(self):
        c = json.loads(fm_custom.choices_json(4))
        self.assertEqual(c["keyings"], ["cm108", "vox"])
        self.assertEqual(CustomSpec.from_dict(c["default_spec"]),
                         CustomSpec.from_preset(fm_config.DEFAULT_PROFILE))


class TestWarnings(unittest.TestCase):

    def warned(self, fragment, **over):
        text = " | ".join(fm_custom.warnings_for(build(**over)))
        self.assertIn(fragment, text, over)

    def test_each_warning_fires(self):
        self.warned("faster than any cable", symbol_rate_bd=800,
                    tone_spacing_hz=800, first_tone_hz=300)
        self.warned("K1 PRO", keying="vox")
        self.warned("band measured", first_tone_hz=600)
        self.warned("position report needs", codename="H_256_768_22")
        self.warned("Longer bursts", symbol_rate_bd=250, tone_spacing_hz=250,
                    first_tone_hz=1500, repeats=1, keying="vox")
        self.warned("fail together", repeats=2)
        self.warned("0 of 3 bursts", keying="vox", lead_in_s=0.3)

    def test_the_k1pro_warning_is_only_for_vox(self):
        self.assertFalse(any("K1 PRO" in w for w in fm_custom.warnings_for(build())))


class TestTheJsonEntryPointsNeverRaise(unittest.TestCase):

    def test_describe(self):
        for text in (None, "", "{", "[]", '{"tones": 3}', '{"tones": "x"}'):
            d = json.loads(fm_custom.describe_json(text))
            self.assertFalse(d["ok"], text)
            self.assertTrue(d["error"], text)

    def test_describe_the_default(self):
        d = json.loads(fm_custom.describe_json(fm_custom.preset_spec_json("fast")))
        self.assertTrue(d["ok"])
        self.assertEqual(d["net_code"], "4T-1000/400-400Bd-H1024.2048")
        self.assertEqual(d["matches_preset"], "fast")
        self.assertEqual(d["burst_s"], round(fm_config.OPERATING_POINT.burst_duration_s(), 3))
        self.assertEqual(d["content_bytes"], 113)
        self.assertIn("not measured", d["evidence"])

    def test_an_illegal_spec_still_shows_its_net_code(self):
        d = json.loads(fm_custom.describe_json(spec(tone_spacing_hz=300).to_json()))
        self.assertFalse(d["ok"])
        self.assertEqual(d["net_code"], "4T-1000/300-400Bd-H1024.2048")

    def test_net_code_entry(self):
        self.assertIn("spec", json.loads(
            fm_custom.spec_from_net_code_json("4T-1000/400-400Bd-H1024.2048")))
        self.assertIn("error", json.loads(fm_custom.spec_from_net_code_json("x")))
        self.assertEqual(fm_custom.preset_spec_json("nope"), "")


class TestFmLinkRunsACustomSpec(unittest.TestCase):

    def setUp(self):
        self.addCleanup(fm_link.set_profile, fm_config.DEFAULT_PROFILE)

    def test_it_applies_and_resets_the_receiver(self):
        fm_link._RX[0] = object()
        name, error = fm_link.set_custom(
            spec(first_tone_hz=1200, symbol_rate_bd=250,
                 tone_spacing_hz=300).to_json())
        self.assertEqual((name, error), (fm_custom.CUSTOM_NAME, ""))
        self.assertIsNone(fm_link._RX[0])
        self.assertEqual(fm_link.net_code(), "4T-1200/300-250Bd-H1024.2048")
        self.assertFalse(fm_link.profile_is_default())

    def test_a_bad_spec_falls_back_to_the_default_and_says_why(self):
        fm_link.set_custom(spec(first_tone_hz=1200).to_json())
        name, error = fm_link.set_custom('{"tones": 3}')
        self.assertEqual(name, fm_config.DEFAULT_PROFILE)
        self.assertIn("power of two", error)
        self.assertEqual(fm_link.net_code(),
                         fm_custom.net_code(fm_config.OPERATING_POINT))

    def test_the_fallback_keeps_the_vox_choice(self):
        fm_link.set_custom("garbage", vox=True)
        self.assertEqual(fm_link.config().keying, fm_config.KEYING_VOX)

    def test_derived_values_follow_the_custom_burst(self):
        """Eligibility, the beacon grid and the busy wait all ask the live
        config -- the reason they were derived rather than constant. The
        longest legal custom burst is the case that would expose a stale
        constant."""
        fm_link.set_custom(spec(symbol_rate_bd=125, tone_spacing_hz=125,
                                first_tone_hz=1000).to_json())
        burst = fm_link.burst_duration_s()
        self.assertGreater(burst, 8.0)
        self.assertLessEqual(burst, fm_config.MAX_BURST_S)
        self.assertGreater(fm_link.channel_busy_wait_s(), burst)
        plan = json.loads(fm_link.audio_plan())
        self.assertAlmostEqual(plan["burst_s"], burst)
        self.assertEqual(plan["net_code"], fm_link.net_code())
        # bitrate_bps is solved so that a full frame costs exactly one burst.
        import airtime
        link = airtime.fm(fm_link.bitrate_bps())
        self.assertAlmostEqual(link.frame_s(fm_link.hw_mtu()), burst, places=6)


class TestCustomSpecsRoundTripThroughTheRealModem(unittest.TestCase):
    """build_tx_burst -> feed_audio against real libcodec2 at 8000 Hz, for
    placements no preset uses: the fields reach the modem, not only the
    arithmetic."""

    SPECS = (
        dict(tones=2, first_tone_hz=1000, tone_spacing_hz=1000, symbol_rate_bd=500),
        dict(tones=4, first_tone_hz=1200, tone_spacing_hz=350, symbol_rate_bd=320),
        dict(tones=4, first_tone_hz=1000, tone_spacing_hz=500, symbol_rate_bd=500,
             codename="H_256_768_22"),
        dict(tones=4, first_tone_hz=800, tone_spacing_hz=200, symbol_rate_bd=200,
             codename="H_256_512_4", repeats=2),
    )

    def setUp(self):
        self.addCleanup(fm_link.set_profile, fm_config.DEFAULT_PROFILE)

    def _round_trip(self, payload):
        fm_link.reset_rx()
        pcm = fm_link.build_tx_burst("", payload)
        stream = pcm + bytes(len(pcm))     # flush: a full burst of silence
        got = b""
        for i in range(0, len(stream), 4096):
            got += fm_link.feed_audio("", stream[i:i + 4096])
        return fm_link.FrameParser().feed(got)

    def test_each_comes_back_byte_exact(self):
        for over in self.SPECS:
            _name, error = fm_link.set_custom(spec(**over).to_json())
            self.assertEqual(error, "", over)
            payload = b"custom".ljust(fm_link.hw_mtu(), b".")
            # Retried: codec2 drops the first frame after a preamble ~0.4% of
            # the time on clean loopback (see test_fm_modem).
            for _ in range(4):
                if payload in self._round_trip(payload):
                    break
            else:
                self.fail("no frame back for %r" % (over,))


class TestWhatAMismatchActuallyDoes(unittest.TestCase):
    """Pins what the net code's docstring claims, measured 2026-10-25 on clean
    synthetic loopback. If codec2 or the wrapper starts constraining its
    frequency estimator, the tone-placement half of this changes and the
    net code's meaning with it. NOT a statement about the air."""

    def setUp(self):
        self.addCleanup(fm_link.set_profile, fm_config.DEFAULT_PROFILE)

    def decodes(self, tx, rx):
        fm_link.set_custom(spec(**tx).to_json())
        payload = b"mismatch".ljust(fm_link.hw_mtu(), b".")[:fm_link.hw_mtu()]
        pcm = fm_link.build_tx_burst("", payload)
        fm_link.set_custom(spec(**rx).to_json())
        for _ in range(3):      # first-frame-after-preamble loss, see above
            fm_link.reset_rx()
            stream = pcm + bytes(len(pcm))
            got = b"".join(fm_link.feed_audio("", stream[i:i + 4096])
                           for i in range(0, len(stream), 4096))
            if payload in fm_link.FrameParser().feed(got):
                return True
        return False

    BASE = dict(first_tone_hz=1000, tone_spacing_hz=400, symbol_rate_bd=400)

    def test_a_tone_placement_mismatch_still_decodes(self):
        self.assertTrue(self.decodes(self.BASE, dict(self.BASE, first_tone_hz=2200)))
        self.assertTrue(self.decodes(self.BASE, dict(self.BASE, tone_spacing_hz=800)))
        narrow = CustomSpec.from_preset("narrow").to_dict()
        wide = CustomSpec.from_preset("wide").to_dict()
        self.assertTrue(self.decodes(narrow, wide))

    def test_rate_count_and_code_mismatches_are_silence(self):
        self.assertFalse(self.decodes(self.BASE, dict(self.BASE, symbol_rate_bd=250)))
        self.assertFalse(self.decodes(self.BASE, dict(self.BASE, tones=2,
                                                      tone_spacing_hz=800)))
        self.assertFalse(self.decodes(self.BASE, dict(self.BASE,
                                                      codename="H_256_512_4")))


if __name__ == "__main__":
    unittest.main()
