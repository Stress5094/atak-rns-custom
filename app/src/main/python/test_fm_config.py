"""Tests for fm_config: the hard constraints are enforced, not documented.

The transport this replaces carried every one of these as a comment asking
the next reader not to break it. The point of this suite is that an illegal
operating point now fails on a desktop, in milliseconds, with a message
naming the measurement it violated -- instead of on a hillside, as silence.
"""

import unittest

import fm_config


def chain():
    """A throwaway Chain, for tests about the other constraints."""
    return fm_config.Chain(
        name="unit test", radios="none", cables="none",
        measured_on="2026-10-01", delivered="nothing, this is a test")


def build(**over):
    kw = dict(name="t", first_tone_hz=1500, tone_spacing_hz=250, tones=4,
              symbol_rate_bd=250, chain=chain())
    kw.update(over)
    return fm_config.Profile(**kw)


class TestOrthogonality(unittest.TestCase):
    """Tones closer together than the symbol rate are not orthogonal and the
    demodulator cannot separate them at all. This is the constraint that
    actually binds on this link."""

    def test_spacing_below_the_symbol_rate_is_refused(self):
        with self.assertRaises(fm_config.ProfileError) as caught:
            build(tone_spacing_hz=200, symbol_rate_bd=250)
        self.assertIn("not orthogonal", str(caught.exception))

    def test_spacing_equal_to_the_symbol_rate_is_the_tightest_legal(self):
        p = build(tone_spacing_hz=250, symbol_rate_bd=250)
        self.assertEqual(p.tone_spacing_hz, p.symbol_rate_bd)

    def test_wider_spacing_is_fine(self):
        self.assertEqual(build(tone_spacing_hz=500).tone_spacing_hz, 500)

    def test_the_shipped_narrow_profile_sits_exactly_on_this_boundary(self):
        """Worth asserting rather than just noting: the measured passband
        wants 250 Hz spacing and the cable's analog limit wants 250 Bd, and
        those two independent constraints meet with no slack at all. If
        either moves, this profile is no longer legal."""
        p = fm_config.NARROW
        self.assertEqual(p.tone_spacing_hz, p.symbol_rate_bd)


class TestSymbolRateCeiling(unittest.TestCase):
    """An ANALOG limit in ONE RECEIVING CABLE, not in FM and not in software.

    The 250 Bd figure was recorded as FM's ceiling -- 375, 480 and 500 Bd all
    measured 0-1/4 delivered at a healthy signal level, with an LM358 envelope
    follower identified from PCB photographs as the cause. 2026-10-02 relocated
    it: with a Digirig Lite RECEIVING, 400 Bd ran 178 of 180 copies on the same
    radios. So the ceiling is a property of the chain, and these tests are
    about it being attached to one.
    """

    def test_above_an_unqualified_chains_ceiling_is_refused(self):
        for rate in (375, 400, 500):
            with self.subTest(rate=rate):
                with self.assertRaises(fm_config.ProfileError):
                    build(symbol_rate_bd=rate, tone_spacing_hz=rate)

    def test_the_refusal_explains_that_gain_will_not_fix_it(self):
        with self.assertRaises(fm_config.ProfileError) as caught:
            build(symbol_rate_bd=500, tone_spacing_hz=500)
        msg = str(caught.exception)
        self.assertIn("ANALOG limit", msg)
        self.assertIn("no gain adjustment rescues it", msg)

    def test_a_profile_inherits_its_chains_ceiling(self):
        """The whole point of the relocation: the figure comes from the cable,
        and the Chain is what names the cable. Nobody has to remember to pass
        it."""
        fast_chain = fm_config.Chain(
            name="digirig-ish", radios="r", cables="Digirig Lite receiving",
            measured_on="2026-10-02", delivered="178/180 copies",
            symbol_rate_ceiling_bd=fm_config.DIGIRIG_RX_CEILING_BD)
        p = build(symbol_rate_bd=400, tone_spacing_hz=400, chain=fast_chain)
        self.assertEqual(p.symbol_rate_ceiling_bd,
                         fm_config.DIGIRIG_RX_CEILING_BD)
        self.assertEqual(p.symbol_rate_bd, 400)

    def test_a_chain_is_still_held_to_its_own_ceiling(self):
        """Inheriting a faster cable's figure must not become a way to raise
        the ceiling generally -- 600 Bd is above the Digirig's measured 500."""
        fast_chain = fm_config.Chain(
            name="digirig-ish", radios="r", cables="c",
            measured_on="2026-10-02", delivered="x",
            symbol_rate_ceiling_bd=fm_config.DIGIRIG_RX_CEILING_BD)
        with self.assertRaises(fm_config.ProfileError):
            build(symbol_rate_bd=600, tone_spacing_hz=600, chain=fast_chain,
                  first_tone_hz=1000)

    def test_an_explicit_ceiling_still_overrides_for_the_bench(self):
        """tools/fm_bench.py's whole job is to run above a ceiling and find out
        whether it holds, so the override has to stay."""
        p = build(symbol_rate_bd=500, tone_spacing_hz=500,
                  symbol_rate_ceiling_bd=500)
        self.assertEqual(p.symbol_rate_bd, 500)

    def test_an_unqualified_chain_inherits_the_slowest_cable_not_the_fastest(self):
        """A new cable is assumed slow until measured. The other way round, an
        unmeasured chain would be configured at a rate nothing supports and
        fail as silence."""
        self.assertEqual(fm_config.MAX_SYMBOL_RATE_BD,
                         fm_config.K1PRO_RX_CEILING_BD)
        self.assertEqual(fm_config.MAX_SYMBOL_RATE_BD, 250)
        self.assertEqual(chain().symbol_rate_ceiling_bd, 250)
        self.assertGreater(fm_config.DIGIRIG_RX_CEILING_BD,
                           fm_config.MAX_SYMBOL_RATE_BD)


class TestBandCeiling(unittest.TestCase):
    """The ceiling that survives the relocation, and it is arithmetic: 4FSK
    needs (M-1) spacings of at least the symbol rate, so the measured
    1000-3000 Hz band caps 4FSK at 666 Bd however good the cable is."""

    def test_four_tones_in_the_measured_band(self):
        self.assertEqual(fm_config.band_symbol_rate_ceiling_bd(4), 666)

    def test_more_tones_is_slower_not_faster(self):
        """The counterintuitive one, which is why it is asserted rather than
        argued: bandwidth grows with (M-1) while bits grow with log2(M)."""
        rates = {m: (fm_config.band_symbol_rate_ceiling_bd(m)
                     * (m - 1).bit_length())
                 for m in (2, 4, 8, 16)}
        self.assertGreater(rates[4], rates[8])
        self.assertGreater(rates[8], rates[16])

    def test_1200_baud_does_not_fit_at_any_power(self):
        """It would need 3600 Hz of audio band against a measured 2000."""
        self.assertLess(fm_config.band_symbol_rate_ceiling_bd(4), 1200)

    def test_tones_outside_a_swept_chains_band_are_refused(self):
        swept = fm_config.Chain(
            name="swept", radios="r", cables="c", measured_on="2026-10-02",
            delivered="x", usable_band_hz=(1000, 3000),
            symbol_rate_ceiling_bd=500)
        # The low edge is sharp and real: 400 Hz measured 0% delivered at a
        # rate that worked at 1000 Hz.
        with self.assertRaises(fm_config.ProfileError) as caught:
            build(first_tone_hz=400, tone_spacing_hz=500, symbol_rate_bd=500,
                  chain=swept)
        self.assertIn("is absent", str(caught.exception))

    def test_the_refusal_states_the_fastest_rate_that_would_fit(self):
        swept = fm_config.Chain(
            name="swept", radios="r", cables="c", measured_on="2026-10-02",
            delivered="x", usable_band_hz=(1000, 3000),
            symbol_rate_ceiling_bd=1000)
        with self.assertRaises(fm_config.ProfileError) as caught:
            build(first_tone_hz=1000, tone_spacing_hz=800, symbol_rate_bd=800,
                  chain=swept)
        self.assertIn("666 Bd", str(caught.exception))

    def test_an_unswept_chain_gets_no_band_check_rather_than_a_borrowed_one(self):
        """Applying one chain's band to another is the mistake the whole module
        is built against: a placement that measured 91% on one bench measured
        0 of 9 on a second."""
        self.assertIsNone(chain().usable_band_hz)
        p = build(first_tone_hz=500, tone_spacing_hz=250, symbol_rate_bd=250)
        self.assertEqual(p.first_tone_hz, 500)


class TestDecoderRequirements(unittest.TestCase):
    """codec2 asserts both of these internally and ABORTS THE PROCESS -- a
    core dump where an error message belongs."""

    def test_a_symbol_rate_that_does_not_divide_the_sample_rate_is_refused(self):
        with self.assertRaises(fm_config.ProfileError) as caught:
            build(symbol_rate_bd=240, tone_spacing_hz=250)
        self.assertIn("divide the sample rate", str(caught.exception))

    def test_the_refusal_suggests_legal_rates_within_the_ceiling(self):
        with self.assertRaises(fm_config.ProfileError) as caught:
            build(symbol_rate_bd=240, tone_spacing_hz=250)
        msg = str(caught.exception)
        self.assertIn("250", msg)
        # Must not suggest a rate the analog ceiling forbids.
        self.assertNotIn("500", msg)

    def test_too_few_samples_per_symbol_is_refused(self):
        # 1600 Bd divides 8000 Hz exactly, so it clears the check above and
        # reaches this one: 8000/1600 is 5 samples per symbol against the 8
        # the demodulator needs. Two tones rather than four, and a low first
        # tone, so the wide spacing orthogonality demands does not trip
        # Nyquist first -- this test is about samples per symbol alone.
        with self.assertRaises(fm_config.ProfileError) as caught:
            build(tones=2, first_tone_hz=500, symbol_rate_bd=1600,
                  tone_spacing_hz=1600, symbol_rate_ceiling_bd=2000)
        self.assertIn("samples per symbol", str(caught.exception))

    def test_the_shipped_profile_has_ample_samples_per_symbol(self):
        self.assertEqual(fm_config.NARROW.samples_per_symbol(), 32)
        self.assertGreaterEqual(fm_config.NARROW.samples_per_symbol(),
                                fm_config.MIN_SAMPLES_PER_SYMBOL)


class TestToneAlphabet(unittest.TestCase):

    def test_a_non_power_of_two_alphabet_is_refused(self):
        for n in (3, 5, 6, 7):
            with self.subTest(tones=n):
                with self.assertRaises(fm_config.ProfileError):
                    build(tones=n)

    def test_one_tone_is_refused(self):
        with self.assertRaises(fm_config.ProfileError):
            build(tones=1)

    def test_bits_per_symbol_is_exact(self):
        self.assertEqual(build(tones=2).bits_per_symbol(), 1)
        self.assertEqual(build(tones=4).bits_per_symbol(), 2)

    def test_eight_tones_is_refused_because_codec2_aborts_on_it(self):
        """A power of two is NOT sufficient: codec2 supports M = 2 and 4 only.

        This case used to be asserted the other way round -- the test checked
        that bits_per_symbol() returned 3 for an 8-tone profile, which is
        correct arithmetic for a profile that cannot exist. Building one on
        2026-10-02 core dumped the interpreter from inside fsk.c's
        `assert(M == 2 || M == 4)`.

        The limit costs nothing. Orthogonal M-FSK needs (M-1) spacings at
        >= the symbol rate, so in a fixed bandwidth the raw rate goes as
        log2(M)/(M-1) -- falling with M. 8FSK is about a third slower than
        4FSK in the same span, not faster. See fm_config's own comment.

        That is the exact failure fm_config exists to convert into a message,
        and the power-of-two check looked like it already covered it.
        """
        with self.assertRaises(fm_config.ProfileError) as caught:
            build(tones=8, tone_spacing_hz=250)
        self.assertIn("2 or 4", str(caught.exception))

    def test_raw_bitrate_follows_the_alphabet(self):
        self.assertEqual(build(tones=4, symbol_rate_bd=250).raw_bps(), 500)
        self.assertEqual(build(tones=2, symbol_rate_bd=250).raw_bps(), 250)


class TestNyquist(unittest.TestCase):
    """Separate from the passband question: a tone above Nyquist aliases
    rather than transmits, and would do so on a radio with a perfectly flat
    response out to any frequency."""

    def test_a_top_tone_above_nyquist_is_refused(self):
        with self.assertRaises(fm_config.ProfileError) as caught:
            build(first_tone_hz=3500, tone_spacing_hz=400)
        self.assertIn("Nyquist", str(caught.exception))

    def test_a_tone_at_exactly_nyquist_is_refused(self):
        with self.assertRaises(fm_config.ProfileError):
            build(first_tone_hz=4000 - 3 * 250, tone_spacing_hz=250, tones=4)

    def test_a_zero_or_negative_first_tone_is_refused(self):
        for f in (0, -100):
            with self.subTest(first=f):
                with self.assertRaises(fm_config.ProfileError):
                    build(first_tone_hz=f)


class TestEvidenceIsMandatory(unittest.TestCase):
    """A tone placement is a property of a specific radio+cable+radio path.
    The same placement measured 91% delivered on one bench and 0 of 9 on
    another, on the same software."""

    def test_a_profile_without_a_chain_is_refused(self):
        with self.assertRaises(fm_config.ProfileError) as caught:
            build(chain=None)
        msg = str(caught.exception)
        self.assertIn("passband_sweep", msg)
        self.assertIn("0 of", msg)

    def test_every_shipped_profile_names_the_hardware_it_was_measured_on(self):
        for name, p in fm_config.PROFILES.items():
            with self.subTest(profile=name):
                self.assertIsNotNone(p.chain)
                self.assertTrue(p.chain.radios)
                self.assertTrue(p.chain.cables)
                self.assertTrue(p.chain.measured_on)
                self.assertTrue(p.chain.delivered)

    def test_the_evidence_line_states_what_was_measured_and_where(self):
        line = fm_config.NARROW.evidence_line()
        self.assertIn("7 of 9", line)
        self.assertIn("2026-09-30", line)
        self.assertIn("4.7 dB", line)


class TestShippedProfiles(unittest.TestCase):

    def test_narrow_is_the_measured_placement(self):
        self.assertEqual(fm_config.NARROW.tone_list(),
                         [1500, 1750, 2000, 2250])

    def test_narrow_sits_inside_the_measured_passband(self):
        """The 2026-09-30 captures put the usable region at roughly
        1250-2500 Hz, best around 1500-2000."""
        for t in fm_config.NARROW.tone_list():
            self.assertGreaterEqual(t, 1250)
            self.assertLessEqual(t, 2500)

    def test_wide_is_the_older_placement(self):
        self.assertEqual(fm_config.WIDE.tone_list(),
                         [1500, 2000, 2500, 3000])

    def test_wide_is_documented_as_not_transferring(self):
        """It measured 0 of 9 on the phone pair. The chain note is what stops
        it being read as a general-purpose alternative."""
        self.assertIn("DOES NOT TRANSFER", fm_config.WIDE.chain.notes)

    def test_the_default_is_the_most_recently_measured_placement(self):
        """`fast`, from 2026-10-02, and it is worth being precise about what
        that claims. It is the placement with the best-taken evidence -- swept
        band, interleaved rounds, 63 minutes, per-copy reported alongside
        delivery -- and the operating point the rebuild is built around. It is
        NOT qualified on the handheld chain, which is what evidence_line() is
        for."""
        self.assertEqual(fm_config.DEFAULT_PROFILE, "fast")
        self.assertIs(fm_config.profile(), fm_config.FAST)

    def test_occupied_bandwidth_is_reported(self):
        self.assertEqual(fm_config.NARROW.occupied_bandwidth_hz(), 750)
        self.assertEqual(fm_config.WIDE.occupied_bandwidth_hz(), 1500)


class TestLookup(unittest.TestCase):

    def test_a_known_name_resolves(self):
        self.assertIs(fm_config.profile("wide"), fm_config.WIDE)

    def test_lookup_is_case_and_whitespace_insensitive(self):
        """The name arrives from Kotlin as a bare preference string."""
        self.assertIs(fm_config.profile("  WIDE "), fm_config.WIDE)

    def test_an_unknown_name_falls_back_rather_than_raising(self):
        """Raising would take the whole bridge's startup down over a stale
        preference; falling back leaves a working link."""
        self.assertIs(fm_config.profile("nosuchprofile"), fm_config.FAST)

    def test_none_and_empty_fall_back(self):
        for name in (None, "", "   "):
            with self.subTest(name=name):
                self.assertIs(fm_config.profile(name), fm_config.FAST)

    def test_resolve_reports_whether_it_substituted(self):
        """A silent substitution is a link that works but not with the peers
        the operator expected -- the startup path needs to be able to say so."""
        p, substituted = fm_config.resolve("narrow")
        self.assertIs(p, fm_config.NARROW)
        self.assertFalse(substituted)

        p, substituted = fm_config.resolve("gone")
        self.assertIs(p, fm_config.FAST)
        self.assertTrue(substituted)

    def test_an_absent_name_is_not_reported_as_a_substitution(self):
        """No preference stored yet is the ordinary first-run case, not a
        withdrawn profile."""
        _p, substituted = fm_config.resolve(None)
        self.assertFalse(substituted)

    def test_profile_names_lists_every_profile(self):
        self.assertEqual(sorted(fm_config.profile_names()),
                         ["fast", "narrow", "wide"])


class TestSummaryLine(unittest.TestCase):
    """Two operators comparing this line by eye was the ONLY mismatch check
    that ever existed, because a disagreeing receiver recovers no frames and
    so cannot be told it disagrees."""

    def test_it_names_the_profile_and_spells_out_the_frequencies(self):
        line = fm_config.NARROW.summary_line()
        self.assertIn("narrow", line)
        for t in (1500, 1750, 2000, 2250):
            self.assertIn(str(t), line)

    def test_it_states_that_both_ends_must_match(self):
        self.assertIn("both ends must match", fm_config.NARROW.summary_line())

    def test_it_carries_the_symbol_rate_too(self):
        """A placement match with a rate mismatch is the same total silence."""
        self.assertIn("250 Bd", fm_config.NARROW.summary_line())


class TestCodeTable(unittest.TestCase):
    """The code table moved here from fm_modem so that burst duration can be
    computed without the native library. test_fm_modem asserts these figures
    against real libcodec2; these assert the arithmetic on top of them."""

    def test_the_table_is_data_bits_and_codeword_bits(self):
        """Both numbers are in the code's own NAME, which is why the table is
        shaped this way: a float rate makes the codeword length inexact, and
        the codeword length is what duration is computed from."""
        for name, (data, codeword) in fm_config.CODES.items():
            with self.subTest(code=name):
                parts = name.split("_")
                self.assertEqual(int(parts[1]), data)
                self.assertEqual(int(parts[2]), codeword)

    def test_code_bytes_excludes_nothing_and_includes_the_crc(self):
        self.assertEqual(fm_config.code_bytes("H_256_768_22"), 32)
        self.assertEqual(fm_config.code_bytes("H_1024_2048_4f"), 128)

    def test_code_rate_is_reported_not_computed_from(self):
        self.assertAlmostEqual(fm_config.code_rate("H_256_768_22"), 1 / 3.0)
        self.assertAlmostEqual(fm_config.code_rate("H_256_512_4"), 0.5)

    def test_an_unknown_code_is_refused_not_guessed(self):
        with self.assertRaises(fm_config.ProfileError):
            fm_config.code_bytes("H_not_a_real_code")

    def test_the_default_code_is_the_smallest_that_holds_A_POSITION(self):
        """CHANGED 2026-10-02 from "the smallest that holds a BEACON", and the
        difference between those two sentences is what Phase 4 cost.

        The 32 B H_256_768_22 held a beacon exactly -- 17 B plaintext plus a
        12 B envelope, zero spare -- and that was the whole argument for it. But
        the codec sees capacity MINUS the envelope, and at 17 B it can carry the
        difference between two positions (16 B) and not a position (26 B floor,
        even with identity stripped). So self-position reports fell back to
        zlib'd XML at 12 bursts each, and the difference-coding that would have
        fitted could never start.

        The next code up is the 128 B one, because CODES has nothing between 32
        and 128. That is not a coincidence the design chose; it is the shape of
        codec2's table.
        """
        self.assertEqual(fm_config.DEFAULT_CODE, "H_1024_2048_4f")
        op = fm_config.OPERATING_POINT
        self.assertEqual(op.capacity_bytes(), 125)
        # What the codec actually gets, which is the number that mattered.
        self.assertEqual(op.capacity_bytes() - 12, 113)

    def test_there_is_no_code_between_the_two(self):
        """So this was never a dial, only a step. Asserted because the obvious
        reaction to "32 B is too small and 128 B is too long" is to look for
        something in between, and there is nothing to find."""
        sizes = sorted(fm_config.code_bytes(c) for c in fm_config.CODES)
        self.assertEqual([n for n in sizes if 32 < n < 128], [])

    def test_the_smaller_code_could_not_carry_a_position(self):
        """The measurement that moved the default, as arithmetic: 17 B of codec
        budget against a 26 B floor for an absolute position."""
        small = fm_config.link_config("fast", codename="H_256_768_22",
                                      repeats=2)
        self.assertEqual(small.capacity_bytes() - 12, 17)
        self.assertLess(small.capacity_bytes() - 12, 26)


class TestLinkConfigArithmetic(unittest.TestCase):
    """Burst duration, which is the number that decides whether this link
    delivers: ~1.5 s measured 40-100%, ~7.4 s measured 0/10 in BOTH
    directions. It depends on five things jointly, and this is where they are
    finally in one object."""

    def test_the_operating_point_is_2_95_seconds(self):
        """From the code table and the symbol rate alone -- no libcodec2. 4FSK,
        400 Bd, 1 copy of H_1024_2048_4f, 0.10 s CM108 lead-in.

        2.95 s and not Phase 0's 2.35 s: the extra 0.6 s is what the 128 B code
        costs over the 32 B one at this rate, and it buys 96 B of capacity --
        the whole position path. See fm_config.DEFAULT_CODE.
        """
        self.assertAlmostEqual(fm_config.OPERATING_POINT.burst_duration_s(),
                               2.95, places=6)

    def test_the_code_cost_six_tenths_of_a_second_and_bought_96_bytes(self):
        """The trade, as the two numbers that decided it, so neither can drift
        without this failing."""
        op = fm_config.OPERATING_POINT
        small = fm_config.link_config("fast", codename="H_256_768_22",
                                      repeats=2)
        self.assertAlmostEqual(op.burst_duration_s() - small.burst_duration_s(),
                               0.60, delta=0.01)
        self.assertEqual(op.capacity_bytes() - small.capacity_bytes(), 96)

    def test_duration_is_the_lead_in_plus_the_preamble_plus_the_copies(self):
        op = fm_config.OPERATING_POINT
        rs = float(op.profile.symbol_rate_bd)
        self.assertAlmostEqual(
            op.burst_duration_s(),
            op.lead_in_s + op.preamble_symbols() / rs
            + op.repeats * op.frame_symbols() / rs)

    def test_a_frame_is_the_codeword_plus_the_unique_word(self):
        op = fm_config.OPERATING_POINT
        self.assertEqual(op.frame_symbols(),
                         (2048 + fm_config.UNIQUE_WORD_BITS) // 2)

    def test_the_preamble_scales_with_the_tone_count(self):
        """50 symbols at M=2, 100 at M=4. Measured, and since codec2 supports
        2 and 4 only those two measurements are the whole domain."""
        op = fm_config.OPERATING_POINT
        self.assertEqual(op.preamble_symbols(), 100)

    def test_the_single_copy_lever_is_now_SPENT(self):
        """It was the biggest untested lever Phase 0 left. Phase 4 spent it: the
        128 B code the position path needs costs 5.55 s at 2 copies, which sits
        in the range where the old bench first saw per-attempt failures, and
        2.95 s at 1.

        Asserted as what the second copy WOULD cost, because that is the figure
        a bench run has to weigh -- and because it is now coupled to the code
        change, so a run that finds this operating point weak cannot attribute
        it without separating the two.
        """
        op = fm_config.OPERATING_POINT
        self.assertEqual(op.repeats, 1)
        two = fm_config.link_config("fast", repeats=2)
        self.assertAlmostEqual(two.burst_duration_s(), 5.55, delta=0.01)
        saved = 1 - op.burst_duration_s() / two.burst_duration_s()
        self.assertAlmostEqual(saved, 0.47, delta=0.01)

    def test_the_fec_rate_lever_is_spent_too(self):
        """Phase 0 listed rate 1/3 -> 1/2 as worth 27%. This code IS rate 1/2,
        so that saving is already inside the 2.95 s, and there is no stronger
        code at this size to trade back. Both of Phase 0's remaining levers are
        now consumed."""
        op = fm_config.OPERATING_POINT
        self.assertAlmostEqual(op.code_rate(), 0.5, places=6)
        # And there is no stronger FEC to trade back to at this payload size:
        # the only codes with a lower rate are the 32 B one the position path
        # could not use.
        stronger = [c for c in fm_config.CODES
                    if fm_config.code_rate(c) < op.code_rate()
                    and fm_config.code_bytes(c) >= fm_config.code_bytes(
                        op.codename)]
        self.assertEqual(stronger, [])

    def test_the_teardown_baseline_was_three_and_a_half_times_longer(self):
        """10.32 s: the SAME 128 B code at 250 Bd with a 1.6 s VOX lead-in.

        3.5x rather than Phase 0's 4.4x -- and the better claim, because this
        one holds the payload CONSTANT. The 4.4x compared a 126 B frame against
        a 30 B one, so part of it was capacity given up rather than airtime won.
        Here the code is identical at both ends of the comparison and the whole
        ratio is rate, keying and the copy count.
        """
        old = fm_config.LinkConfig(
            name="teardown-baseline", profile=fm_config.NARROW,
            codename="H_1024_2048_4f", repeats=2, keying=fm_config.KEYING_VOX,
            lead_in_s=1.6)
        self.assertAlmostEqual(old.burst_duration_s(), 10.32, delta=0.01)
        op = fm_config.OPERATING_POINT
        self.assertEqual(old.codename, op.codename)
        self.assertEqual(old.capacity_bytes(), op.capacity_bytes())
        ratio = old.burst_duration_s() / op.burst_duration_s()
        self.assertAlmostEqual(ratio, 3.5, delta=0.05)

    def test_goodput_is_payload_over_the_whole_transmission(self):
        """The honest figure. The raw symbol rate describes a link about five
        times faster than this one."""
        op = fm_config.OPERATING_POINT
        cap = op.capacity_bytes()
        self.assertAlmostEqual(op.goodput_bps(cap),
                               cap * 8.0 / op.burst_duration_s())
        self.assertLess(op.goodput_bps(cap), op.profile.raw_bps() / 2.0)

    def test_the_lead_in_fraction_is_surfaced(self):
        """Small now, and that is the point: on the previous transport it was
        most of a short burst, and nobody could see it."""
        op = fm_config.OPERATING_POINT
        self.assertAlmostEqual(op.lead_in_fraction(), 0.10 / 2.95, places=4)

    def test_the_budget_itemises_where_the_airtime_goes(self):
        """The 4.4x did NOT come from the waveform, and nobody could see that
        while the burst was one number."""
        lines = fm_config.OPERATING_POINT.budget_lines()
        self.assertEqual(len(lines), 3)
        self.assertIn("lead-in", lines[0])
        self.assertIn("preamble", lines[1])
        self.assertIn("H_1024_2048_4f", lines[2])
        self.assertIn("88.1%", lines[2])


class TestLinkConfigCeiling(unittest.TestCase):
    """The ceiling is checked when a configuration is CHOSEN, not when a burst
    is built. That is what the arithmetic above buys: no libcodec2, no audio
    device, no radio."""

    def test_an_over_long_burst_cannot_be_constructed(self):
        """The teardown baseline plus one more in-burst copy: 14.5 s against an
        11.5 s ceiling. A ~7.4 s burst measured 0/10 in both directions, so a
        configuration like this cannot be allowed to exist, let alone ship."""
        with self.assertRaises(fm_config.LinkConfigError) as caught:
            fm_config.LinkConfig(
                name="too-long", profile=fm_config.NARROW,
                codename="H_1024_2048_4f", repeats=3,
                keying=fm_config.KEYING_VOX)
        self.assertIn("14.48 s", str(caught.exception))

    def test_a_code_too_large_to_frame_is_a_configuration_error(self):
        """The 512 B code: its 509 B of capacity cannot be described by a
        1-byte length prefix. A caller asking "can this link come up" must not
        need to know the prefix lives in another module to catch the answer."""
        with self.assertRaises(fm_config.LinkConfigError) as caught:
            fm_config.link_config("fast", codename="H_4096_8192_3d")
        self.assertIn("length prefix", str(caught.exception))

    def test_the_refusal_guides_toward_the_levers_not_the_ceiling(self):
        with self.assertRaises(fm_config.LinkConfigError) as caught:
            fm_config.link_config("fast", repeats=30)
        msg = str(caught.exception)
        self.assertIn("Reduce repeats", msg)
        self.assertIn("do NOT raise the ceiling", msg)
        # And it must quote the evidence, not just the rule.
        self.assertIn("0/10 in both directions", msg)

    def test_a_link_config_error_is_catchable_as_a_profile_error(self):
        """The startup path asks one question -- can this link come up -- and
        should not need two excepts to ask it."""
        self.assertTrue(issubclass(fm_config.LinkConfigError,
                                   fm_config.ProfileError))

    def test_every_shipped_placement_fits_at_the_operating_point(self):
        """Including `narrow`, whose chain's 1.6 s lead-in and 250 Bd make it
        the longest burst of the three."""
        for name in fm_config.profile_names():
            with self.subTest(profile=name):
                cfg = fm_config.link_config(name)
                self.assertLessEqual(cfg.burst_duration_s(), cfg.max_burst_s)

    def test_zero_copies_is_refused(self):
        with self.assertRaises(fm_config.LinkConfigError):
            fm_config.link_config("fast", repeats=0)

    def test_an_unknown_code_is_refused_at_configuration_time(self):
        with self.assertRaises(fm_config.ProfileError):
            fm_config.link_config("fast", codename="H_nope")


class TestKeyingAndLeadIn(unittest.TestCase):
    """The lead-in is a property of the KEYING, and the previous transport's
    single global figure was a VOX number applied to a CM108-keyed link."""

    def test_cm108_keying_has_the_measured_tenth_of_a_second(self):
        self.assertEqual(fm_config.KEYING_LEAD_IN_S[fm_config.KEYING_CM108],
                         0.10)

    def test_vox_needs_more_than_cm108(self):
        """The transmitter is OFF until VOX fires, so whatever is playing then
        is simply not transmitted. 85-90 ms of measured attack, 0.30 s to leave
        margin."""
        self.assertGreater(fm_config.KEYING_LEAD_IN_S[fm_config.KEYING_VOX],
                           fm_config.KEYING_LEAD_IN_S[fm_config.KEYING_CM108])

    def test_unknown_keying_is_refused(self):
        with self.assertRaises(fm_config.LinkConfigError) as caught:
            fm_config.link_config("fast", keying="magic")
        self.assertIn("sets the lead-in", str(caught.exception))

    def test_a_chain_overrides_the_keying_default(self):
        """The handheld chain's 1.6 s. NOT a VOX figure despite that chain
        being VOX-keyed -- it is buying a transmission long enough that data
        starts after a received-level collapse settles, and it applies under
        deterministic PTT too. A single global lead-in cannot express that."""
        self.assertEqual(fm_config.NARROW.chain.lead_in_s, 1.6)
        cfg = fm_config.link_config("narrow")
        self.assertEqual(cfg.lead_in_s, 1.6)

    def test_the_chain_override_beats_the_keying_but_loses_to_the_caller(self):
        """Resolution order, most specific first: explicit, then chain, then
        keying."""
        self.assertEqual(fm_config.link_config("narrow",
                                               lead_in_s=0.2).lead_in_s, 0.2)
        self.assertEqual(fm_config.link_config("fast").lead_in_s, 0.10)

    def test_a_negative_lead_in_is_refused(self):
        with self.assertRaises(fm_config.LinkConfigError):
            fm_config.link_config("fast", lead_in_s=-0.1)


class TestDeemphasisIsPerChain(unittest.TestCase):
    """Phase 0's hardest-won requirement. The K1 PRO receiving decoded 3 of 48
    copies without ~250 us of de-emphasis and 34 of 40 with it, and codec2's
    estimator would not reach even trial sync against the untreated tilt at any
    normalisation peak. It is the CABLE, not the radio pairing: swapping in a
    second Digirig with both radios fixed took the same direction to 20/20
    unfiltered."""

    def test_a_config_can_state_what_its_chain_needs(self):
        needs = fm_config.Chain(
            name="k1pro receiving", radios="r", cables="K1 PRO receiving",
            measured_on="2026-10-02", delivered="34 of 40 with de-emphasis",
            deemphasis_us=250.0)
        prof = fm_config.Profile(
            name="k1", first_tone_hz=1500, tone_spacing_hz=250, tones=4,
            symbol_rate_bd=250, chain=needs)
        cfg = fm_config.LinkConfig(name="k1", profile=prof)
        self.assertEqual(cfg.deemphasis_us(), 250.0)

    def test_the_digirig_chain_needs_none_and_says_so(self):
        """Zero is a measurement here, not an omission -- which is the whole
        reason it is expressible per chain rather than a global default."""
        self.assertEqual(fm_config.OPERATING_POINT.deemphasis_us(), 0.0)

    def test_the_handheld_chain_does_not_borrow_the_k1_pro_figure(self):
        """The K1 PRO-as-receiver finding was measured on the DESKTOP bench.
        The handheld chain carries K1 PRO-class cables and delivered 7 of 9
        unfiltered, so asserting 250 us there would be importing another
        chain's evidence -- the one thing this module exists to stop."""
        self.assertEqual(fm_config.link_config("narrow").deemphasis_us(), 0.0)


class TestLinkConfigReporting(unittest.TestCase):

    def test_the_summary_names_every_number_that_must_match(self):
        """A receiver that disagrees recovers no frames, and so cannot be told
        that it disagrees. Two operators reading this line to each other is the
        only mismatch check that can exist."""
        op = fm_config.OPERATING_POINT
        line = op.summary_line()
        # DERIVED from the config, not written out: this line's job is to carry
        # whatever the operating point currently IS, so a test that restates the
        # numbers only checks that somebody updated two places at once. The
        # operating point's own values are pinned in TestLinkConfigArithmetic.
        for expected in ("/".join(str(t) for t in op.profile.tone_list()),
                         "%d Bd" % op.profile.symbol_rate_bd,
                         op.codename,
                         "%d copies" % op.repeats,
                         op.keying,
                         "%.2f s" % op.burst_duration_s(),
                         "%d B" % op.capacity_bytes(),
                         "both ends must match"):
            with self.subTest(expected=expected):
                self.assertIn(expected, line)

    def test_the_evidence_line_flags_a_sample_rate_mismatch(self):
        """The desktop bench ran 48000 Hz and the phone runs 8000, and
        confusing the two is one of the three errors Phase 0 found in the
        record. It is not bookkeeping: codec2 needs Fs %% Rs == 0, so two of the
        three rates the record says FAILED cannot be constructed at 8000 at
        all."""
        line = fm_config.FAST.evidence_line()
        self.assertIn("48000", line)
        self.assertIn("8000", line)
        self.assertIn("NOTE", line)

    def test_a_config_resolves_an_unknown_placement_rather_than_raising(self):
        """The name arrives from Kotlin as a bare SharedPreferences string, and
        raising would take the bridge's startup down over a stale preference."""
        cfg, substituted = fm_config.resolve_link_config("gone")
        self.assertTrue(substituted)
        self.assertIs(cfg.profile, fm_config.FAST)

        cfg, substituted = fm_config.resolve_link_config("narrow")
        self.assertFalse(substituted)
        self.assertIs(cfg.profile, fm_config.NARROW)


class TestLevelConstants(unittest.TestCase):
    """On an FM audio link the volume knob is the gain stage, and these are
    the band an operator is aiming the knob at."""

    def test_the_target_band_sits_between_silent_and_saturated(self):
        self.assertLess(fm_config.SILENT_PEAK, fm_config.TARGET_PEAK_LOW)
        self.assertLess(fm_config.TARGET_PEAK_LOW, fm_config.TARGET_PEAK_HIGH)
        self.assertLess(fm_config.TARGET_PEAK_HIGH, fm_config.SATURATED_PEAK)

    def test_the_burst_ceiling_is_the_measured_figure(self):
        self.assertEqual(fm_config.MAX_BURST_S, 11.5)


if __name__ == "__main__":
    unittest.main()
