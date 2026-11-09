"""Desktop tests for the bench instrument itself.

    python3 -m unittest discover -s tools -p 'test_*.py' -v

WHY AN INSTRUMENT NEEDS TESTS. Everything else in this repo is tested because
a bug would break the link. This is tested because a bug would produce a
NUMBER -- and a number from this tool goes into docs/open-questions.md and
decides the operating point. Two defects of exactly that kind are what these
tests were written for:

  * The default configuration was not the operating point. `--code` was a
    hardcoded "H_256_768_22" and `--repeats` came from
    fm_config.DEFAULT_REPEATS (2), so `fm_bench.py --run` with no flags
    measured a 32 B code at 2 copies -- neither the operating point nor the
    one it superseded. Three of the five waveform defaults were already
    derived from fm_config, with a comment saying why; two were not.
  * The interleave axis was the symbol rate and only the symbol rate. The
    comparison actually outstanding holds the rate FIXED and varies the code
    and the copy count, so it could only be run as three separate blocks --
    and a bad patch on this channel lasts minutes, sits over one block, and
    reproduces any effect you like. That is how a direction asymmetry came to
    be claimed at p = 0.00022 here and retracted the same day.

Most of this needs no radio and no libcodec2. The one test that does -- the
end-to-end interleave order -- drives the real loop through --loopback rather
than re-implementing it, because re-implementing the thing under test is how a
test comes to agree with a bug.
"""

import contextlib
import csv
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "src", "main", "python"))

import fm_bench
import fm_config
import fm_modem

requires_codec2 = unittest.skipUnless(
    fm_modem.available(),
    "libcodec2 is not installed -- this test drives the real modem through "
    "--loopback, because re-implementing the run loop would only test the "
    "re-implementation")


BASELINE = {
    "rs": fm_bench.DEFAULT_SYMBOL_RATE_BD,
    "code": fm_bench.DEFAULT_CODE,
    "repeats": fm_bench.DEFAULT_REPEATS,
    "first_tone": fm_bench.DEFAULT_FIRST_TONE_HZ,
    "spacing": 0,
    "tones": fm_bench.DEFAULT_TONES,
    "lead_in": None,
    "payload": 0,
    "tx_level": None,
    "rx_reset": 0,
    # --channel only. Not part of any waveform, and ignored entirely by the
    # over-the-air modes.
    "snr_db": 12.0,
    "drive": 1.0,
}


class TestTheDefaultIsTheOperatingPoint(unittest.TestCase):
    """A figure is only valid for the configuration it was measured in, and
    the configuration nobody types is the one nobody checks."""

    def test_every_waveform_default_comes_from_the_operating_point(self):
        op = fm_config.OPERATING_POINT
        self.assertEqual(fm_bench.DEFAULT_CODE, op.codename)
        self.assertEqual(fm_bench.DEFAULT_REPEATS, op.repeats)
        self.assertEqual(fm_bench.DEFAULT_SYMBOL_RATE_BD,
                         op.profile.symbol_rate_bd)
        self.assertEqual(fm_bench.DEFAULT_FIRST_TONE_HZ,
                         op.profile.first_tone_hz)
        self.assertEqual(fm_bench.DEFAULT_TONES, op.profile.tones)
        self.assertEqual(fm_bench.DEFAULT_SPACING_HZ,
                         op.profile.tone_spacing_hz)

    def test_the_repeat_default_is_not_fm_configs_own(self):
        """The two deliberately disagree -- fm_config.DEFAULT_REPEATS is 2 and
        the operating point runs 1 -- so taking the convenient one is how this
        defaulted to a configuration that was never on the air."""
        self.assertNotEqual(fm_config.DEFAULT_REPEATS,
                            fm_config.OPERATING_POINT.repeats)
        self.assertEqual(fm_bench.DEFAULT_REPEATS,
                         fm_config.OPERATING_POINT.repeats)

    def test_the_baseline_cell_reproduces_the_operating_points_burst(self):
        """The whole point of deriving the defaults: a bare --run measures the
        configuration the rest of the project quotes."""
        cell = fm_bench.build_cells([], None, BASELINE)[0]
        self.assertEqual(cell["code"], fm_config.OPERATING_POINT.codename)
        self.assertEqual(cell["repeats"], fm_config.OPERATING_POINT.repeats)

    def test_the_sample_rate_is_deliberately_not_the_operating_points(self):
        """The bench cards run 48000 and the phone runs 8000; two of the rates
        in the record cannot be CONSTRUCTED at 8000, and a figure is only
        comparable at the rate it was taken at. The one default that must not
        follow the operating point."""
        import fm_bench_audio
        self.assertEqual(fm_bench_audio.BENCH_FS, 48000)
        self.assertNotEqual(fm_config.OPERATING_POINT.profile.sample_rate_hz,
                            fm_bench_audio.BENCH_FS)


class TestCellSpecParsing(unittest.TestCase):
    """Refused by NAME before a radio is keyed, never ignored: a typo'd field
    silently measuring the baseline three times is a run that looks like a
    clean null result."""

    def test_a_plain_spec(self):
        self.assertEqual(
            fm_bench.parse_cell_spec("code=H_256_768_22,repeats=2"),
            {"code": "H_256_768_22", "repeats": 2})

    def test_whitespace_and_dashes_are_tolerated(self):
        self.assertEqual(
            fm_bench.parse_cell_spec(" first-tone = 1500 , rs=250 "),
            {"first_tone": 1500, "rs": 250})

    def test_a_field_without_a_value_is_refused(self):
        with self.assertRaises(fm_bench.CellSpecError) as caught:
            fm_bench.parse_cell_spec("repeats")
        self.assertIn("field=value", str(caught.exception))

    def test_an_unknown_field_is_named(self):
        with self.assertRaises(fm_bench.CellSpecError) as caught:
            fm_bench.parse_cell_spec("rate=400")
        self.assertIn("rate", str(caught.exception))
        self.assertIn("rs", str(caught.exception))

    def test_a_value_of_the_wrong_type_is_refused(self):
        with self.assertRaises(fm_bench.CellSpecError):
            fm_bench.parse_cell_spec("repeats=two")

    def test_a_field_set_twice_is_refused_not_silently_dropped(self):
        with self.assertRaises(fm_bench.CellSpecError) as caught:
            fm_bench.parse_cell_spec("repeats=1,repeats=2")
        self.assertIn("twice", str(caught.exception))

    def test_an_empty_spec_is_refused(self):
        with self.assertRaises(fm_bench.CellSpecError):
            fm_bench.parse_cell_spec(" , ")

    def test_every_documented_field_parses(self):
        """So the help text cannot advertise a field the parser rejects."""
        for name in fm_bench.CELL_FIELDS:
            value = "H_1024_2048_4f" if name == "code" else "1"
            self.assertIn(name, fm_bench.parse_cell_spec(
                "%s=%s" % (name, value)))


class TestBuildingCells(unittest.TestCase):

    def test_the_baseline_must_cover_every_cell_field(self):
        """Adding a name to CELL_FIELDS without a baseline value used to
        surface as a KeyError inside a set comprehension in varying_fields,
        a long way from the one-line omission that caused it."""
        short = dict(BASELINE)
        short.pop("tx_level")
        with self.assertRaises(fm_bench.CellSpecError) as caught:
            fm_bench.build_cells([], None, short)
        self.assertIn("tx_level", str(caught.exception))

    def test_this_tests_baseline_covers_every_field(self):
        """So these tests cannot pass while the real baseline has drifted."""
        self.assertEqual(sorted(BASELINE), sorted(fm_bench.CELL_FIELDS))

    def test_no_cells_and_no_sweep_is_one_baseline_cell(self):
        cells = fm_bench.build_cells([], None, BASELINE)
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0].values, BASELINE)

    def test_overrides_land_on_the_baseline(self):
        cells = fm_bench.build_cells(["repeats=2"], None, BASELINE)
        self.assertEqual(cells[0]["repeats"], 2)
        # Everything not overridden is inherited, not reset.
        self.assertEqual(cells[0]["code"], BASELINE["code"])
        self.assertEqual(cells[0]["rs"], BASELINE["rs"])

    def test_sweep_is_sugar_for_rate_only_cells(self):
        """Kept working so every Phase 0 invocation in the module docstring
        produces exactly the cells it used to, with one interleave mechanism
        underneath instead of two."""
        cells = fm_bench.build_cells([], "250,320,400", BASELINE)
        self.assertEqual([c["rs"] for c in cells], [250, 320, 400])
        for c in cells:
            self.assertEqual(c["code"], BASELINE["code"])
            self.assertEqual(c["repeats"], BASELINE["repeats"])

    def test_cell_and_sweep_together_are_refused(self):
        """Two ways of saying what gets interleaved."""
        with self.assertRaises(fm_bench.CellSpecError):
            fm_bench.build_cells(["rs=400"], "250,400", BASELINE)

    def test_a_non_numeric_sweep_rate_is_refused(self):
        with self.assertRaises(fm_bench.CellSpecError):
            fm_bench.build_cells([], "250,fast", BASELINE)

    def test_an_empty_sweep_is_refused(self):
        with self.assertRaises(fm_bench.CellSpecError):
            fm_bench.build_cells([], " , ", BASELINE)


class TestTheThreeCellComparisonTheSwitchLeftOwing(unittest.TestCase):
    """The figures CLAUDE.md states, reproduced from the tool that will take
    them. If the tool and the record disagree about what 2.95 s is, one of
    them is wrong and nobody finds out until the measurement is already made.
    """

    SPECS = ["code=H_256_768_22,repeats=2",
             "code=H_1024_2048_4f,repeats=1",
             "code=H_1024_2048_4f,repeats=2"]

    def _cells(self):
        return fm_bench.build_cells(self.SPECS, None, BASELINE)

    def _burst_s(self, cell):
        chain = fm_bench.bench_chain("test", "tx", "rx")
        prof = fm_bench.make_profile(
            cell["rs"], cell["first_tone"], cell["spacing"], cell["tones"],
            # The documented burst figures are the PHONE's, so they only
            # reproduce at the phone's sample rate -- which is itself the
            # reason --fs is not derived from the operating point.
            fm_config.OPERATING_POINT.profile.sample_rate_hz,
            cell["rs"], chain, slug=cell.slug)
        return fm_config.LinkConfig(
            name="t", profile=prof, codename=cell["code"],
            repeats=cell["repeats"], keying=fm_config.KEYING_CM108,
            lead_in_s=cell["lead_in"]).burst_duration_s()

    def test_the_three_bursts_are_2_35_2_95_and_5_55(self):
        got = [round(self._burst_s(c), 2) for c in self._cells()]
        self.assertEqual(got, [2.35, 2.95, 5.55])

    def test_the_middle_cell_is_the_operating_point_in_force(self):
        cells = self._cells()
        self.assertEqual(cells[1]["code"],
                         fm_config.OPERATING_POINT.codename)
        self.assertEqual(cells[1]["repeats"],
                         fm_config.OPERATING_POINT.repeats)

    def test_the_two_changes_are_coupled_which_is_why_there_are_three(self):
        """Code and copies both move between cell 0 and cell 1, so a two-cell
        run cannot attribute a weak result to either. The third cell is what
        separates them."""
        cells = self._cells()
        self.assertNotEqual(cells[0]["code"], cells[1]["code"])
        self.assertNotEqual(cells[0]["repeats"], cells[1]["repeats"])
        # Cell 2 shares its code with cell 1 and its copy count with cell 0.
        self.assertEqual(cells[2]["code"], cells[1]["code"])
        self.assertEqual(cells[2]["repeats"], cells[0]["repeats"])

    def test_two_cells_share_a_rate_so_the_rate_cannot_identify_a_cell(self):
        """The reason captures are keyed by slug rather than by rate."""
        cells = self._cells()
        self.assertEqual(len({c["rs"] for c in cells}), 1)
        self.assertEqual(len({c.slug for c in cells}), 3)


class TestLabels(unittest.TestCase):

    def test_a_label_names_only_what_varies(self):
        cells = fm_bench.build_cells(
            ["repeats=1", "repeats=2"], None, BASELINE)
        varying = fm_bench.varying_fields(cells)
        self.assertEqual(varying, ["repeats"])
        self.assertEqual([c.label(varying) for c in cells],
                         ["repeats=1", "repeats=2"])

    def test_a_rate_sweep_keeps_the_familiar_form(self):
        """Every existing sweep output and bench note reads '400 Bd'."""
        cells = fm_bench.build_cells([], "250,400", BASELINE)
        varying = fm_bench.varying_fields(cells)
        self.assertEqual([c.label(varying) for c in cells],
                         ["250 Bd", "400 Bd"])

    def test_a_single_cell_has_no_comparison_to_name(self):
        cells = fm_bench.build_cells([], None, BASELINE)
        self.assertEqual(fm_bench.varying_fields(cells), [])
        self.assertEqual(cells[0].label([]), "config")

    def test_varying_is_derived_from_resolved_values_not_from_what_was_typed(self):
        """Two cells that spell the same configuration differently are one
        configuration, and a label must not claim a difference that is not
        there."""
        cells = fm_bench.build_cells(
            ["repeats=%d" % BASELINE["repeats"], "rs=250"], None, BASELINE)
        self.assertEqual(fm_bench.varying_fields(cells), ["rs"])

    def test_labels_are_unique_when_more_than_one_field_varies(self):
        cells = fm_bench.build_cells(
            TestTheThreeCellComparisonTheSwitchLeftOwing.SPECS, None, BASELINE)
        varying = fm_bench.varying_fields(cells)
        labels = [c.label(varying) for c in cells]
        self.assertEqual(len(set(labels)), 3, labels)


class TestEveryOverTheAirModeIsGuarded(unittest.TestCase):
    """--note and --tx/--rx are mandatory for anything that keys a radio.

    `--both-directions` on its own used to escape both: the over-the-air test
    enumerated --run and --sweep and missed it, so that invocation reached the
    point of keying a radio with no recorded volume-knob position -- the exact
    loss the tool's own docstring says three investigations were confounded
    by. Driven through main() because the guard lives there.
    """

    def _error(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(
                io.StringIO()):
            with self.assertRaises(SystemExit):
                fm_bench.main(argv)
        return err.getvalue()

    def test_both_directions_alone_still_demands_a_note(self):
        self.assertIn("--note is required", self._error(["--both-directions"]))

    def test_run_demands_a_note(self):
        self.assertIn("--note is required", self._error(["--run"]))

    def test_cells_demand_a_note(self):
        self.assertIn("--note is required",
                      self._error(["--cell", "repeats=2"]))

    def test_a_noted_run_still_demands_the_cards(self):
        self.assertIn("--tx and --rx are required",
                      self._error(["--run", "--note", "knobs at 11"]))

    @requires_codec2
    def test_loopback_demands_neither(self):
        """It touches no hardware, so a note would be ceremony -- and
        requiring one is how a quick code check stops being quick."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = fm_bench.main(["--loopback", "--run", "--trials", "1",
                                "--quiet"])
        self.assertEqual(rc, 0, out.getvalue())


class TestTheTransmitLevelIsInterleavable(unittest.TestCase):
    """The axis the tool could not interleave, which is why a level comparison
    got run as two blocks of time and a p = 8.7e-07 was quoted off it. A later
    run at the same level went back to 0/5 and exposed it."""

    def test_tx_level_is_a_cell_field(self):
        self.assertIn("tx_level", fm_bench.CELL_FIELDS)

    def test_two_levels_make_two_cells_that_interleave(self):
        cells = fm_bench.build_cells(
            ["tx_level=-31", "tx_level=-20"], None, BASELINE)
        self.assertEqual([c["tx_level"] for c in cells], [-31.0, -20.0])
        self.assertEqual(fm_bench.varying_fields(cells), ["tx_level"])

    def test_the_label_names_the_level_in_dB(self):
        cells = fm_bench.build_cells(
            ["tx_level=-31", "tx_level=-20"], None, BASELINE)
        varying = fm_bench.varying_fields(cells)
        self.assertEqual([c.label(varying) for c in cells],
                         ["tx -31 dB", "tx -20 dB"])

    def test_omitting_it_leaves_the_mixer_alone(self):
        """Every invocation predating this field must behave exactly as it
        did -- no mixer call at all, not a call that happens to set the
        current value."""
        cells = fm_bench.build_cells([], None, BASELINE)
        self.assertIsNone(cells[0]["tx_level"])

    def test_a_negative_level_parses(self):
        """It is always negative in practice, and amixer's own parser chokes
        on it -- see set_tx_level's `--`."""
        self.assertEqual(fm_bench.parse_cell_spec("tx_level=-31.0"),
                         {"tx_level": -31.0})

    def test_the_level_is_recorded_in_the_csv(self):
        self.assertIn("tx_level_db", fm_bench.CSV_HEADER)

    def test_a_trial_records_the_read_back_level_not_the_request(self):
        """A control that snaps to its nearest step was set to something other
        than what was asked for, and the CSV has to say what was on the air."""
        t = fm_bench.Trial(400, "H_1024_2048_4f", 1, 125, 2.95,
                           tx_level_db=-20.0)
        row = t.row("note")
        self.assertIn("-20", row)

    def test_a_trial_without_a_level_leaves_the_column_empty(self):
        t = fm_bench.Trial(400, "H_1024_2048_4f", 1, 125, 2.95)
        self.assertEqual(len(t.row("n")), len(fm_bench.CSV_HEADER))
        self.assertEqual(t.row("n")[fm_bench.CSV_HEADER.index("tx_level_db")],
                         "")


class TestTheReceiverResetIsInterleavable(unittest.TestCase):
    """FreeDV's own GUI resets its receivers at the end of every burst, because
    "a demodulator left 'in sync' from before keying would miss the next
    burst". This transport has that gap and never reset anything, which makes
    it a candidate for the unexplained 0%-or-100% swing -- and a candidate is
    only testable interleaved."""

    def test_rx_reset_is_a_cell_field(self):
        self.assertIn("rx_reset", fm_bench.CELL_FIELDS)

    def test_the_two_states_make_two_cells(self):
        cells = fm_bench.build_cells(["rx_reset=0", "rx_reset=1"], None, BASELINE)
        self.assertEqual([c["rx_reset"] for c in cells], [0, 1])
        self.assertEqual(fm_bench.varying_fields(cells), ["rx_reset"])

    def test_the_labels_say_which_is_which(self):
        cells = fm_bench.build_cells(["rx_reset=0", "rx_reset=1"], None, BASELINE)
        varying = fm_bench.varying_fields(cells)
        self.assertEqual([c.label(varying) for c in cells],
                         ["no-rx-reset", "rx-reset"])

    def test_the_default_leaves_the_state_alone(self):
        """So every invocation predating this field behaves as it did."""
        self.assertEqual(
            fm_bench.build_cells([], None, BASELINE)[0]["rx_reset"], 0)

    def test_it_is_recorded_in_the_csv(self):
        self.assertIn("rx_reset", fm_bench.CSV_HEADER)
        t = fm_bench.Trial(400, "H_1024_2048_4f", 1, 125, 2.95, rx_reset=1)
        self.assertEqual(t.row("n")[fm_bench.CSV_HEADER.index("rx_reset")], "1")


class TestTheCsvRefusesAMismatchedHeader(unittest.TestCase):
    """The columns changed when tx_level_db was added. Appending new rows under
    an old header shifts every column from that point on, which is worse than
    losing the run: it corrupts the rows that were already good."""

    def test_appending_to_a_different_header_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.csv")
            with open(path, "w") as fh:
                fh.write("utc,direction,rs_bd,code\n")
            with self.assertRaises(ValueError) as caught:
                fm_bench.CsvLog(path, "note")
            self.assertIn("different header", str(caught.exception))

    def test_appending_to_a_matching_header_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ok.csv")
            log = fm_bench.CsvLog(path, "note")
            log.close()
            again = fm_bench.CsvLog(path, "note")
            again.close()

    def test_a_new_file_gets_the_current_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "new.csv")
            fm_bench.CsvLog(path, "note").close()
            with open(path) as fh:
                self.assertEqual(fh.readline().strip(),
                                 ",".join(fm_bench.CSV_HEADER))


class TestSummariseAlignment(unittest.TestCase):
    """The point of these lines is reading cells against each other, and
    ragged columns are how a per-copy rate gets compared against a delivery
    figure by eye."""

    def test_the_label_column_is_padded_to_the_requested_width(self):
        trial = fm_bench.Trial(400, "H_1024_2048_4f", 1, 125, 2.95)
        trial.frames, trial.exact = 1, 1
        wide = fm_bench.summarise("a", [trial], width=20)
        self.assertTrue(wide.startswith("a" + " " * 19))

    def test_no_trials_still_aligns(self):
        self.assertTrue(
            fm_bench.summarise("a", [], width=20).startswith("a" + " " * 19))


@requires_codec2
class TestTheRunLoopActuallyInterleaves(unittest.TestCase):
    """Driven through the REAL main() with --loopback: no sound card, no
    radio, the same build-and-decode path a live run uses.

    This is the test the whole cell refactor exists for. A comparison measured
    as blocks -- all of A, then all of B -- is measured against its own slice
    of a channel whose bad patches last minutes, and no amount of care in the
    analysis recovers it afterward.
    """

    SPECS = ["code=H_256_768_22,repeats=2", "code=H_1024_2048_4f,repeats=1"]

    def _rows(self, argv):
        """main()'s own progress output is captured, not silenced: a failing
        run needs it, and a passing one would otherwise bury the suite."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.csv")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = fm_bench.main(argv + ["--csv", path])
            self.assertEqual(rc, 0, "run failed:\n%s" % out.getvalue())
            with open(path) as fh:
                return list(csv.DictReader(fh))

    def test_cells_alternate_inside_each_round(self):
        rows = self._rows(["--loopback", "--rounds", "3", "--quiet"]
                          + [a for s in self.SPECS for a in ("--cell", s)])
        self.assertEqual(len(rows), 6)
        codes = [r["code"] for r in rows]
        # Round-major: A B A B A B, never A A A B B B.
        self.assertEqual(codes, ["H_256_768_22", "H_1024_2048_4f"] * 3, codes)

    def test_each_cells_burst_is_its_own(self):
        """Keyed by rate, two cells would have shared one LinkConfig and
        reported one burst duration for both."""
        rows = self._rows(["--loopback", "--rounds", "1", "--quiet"]
                          + [a for s in self.SPECS for a in ("--cell", s)])
        bursts = {r["code"]: float(r["burst_s"]) for r in rows}
        self.assertEqual(len(set(bursts.values())), 2, bursts)

    def test_each_cell_fills_its_own_frame(self):
        """A cell's code decides its payload: the 32 B and 128 B cells carry
        different amounts when left to fill, which is the honest unit because
        airtime here is FLAT."""
        rows = self._rows(["--loopback", "--rounds", "1", "--quiet"]
                          + [a for s in self.SPECS for a in ("--cell", s)])
        sizes = {r["code"]: int(r["payload_b"]) for r in rows}
        self.assertEqual(len(set(sizes.values())), 2, sizes)
        self.assertLess(sizes["H_256_768_22"], sizes["H_1024_2048_4f"])

    def test_a_sweep_interleaves_the_same_way(self):
        rows = self._rows(["--loopback", "--sweep", "250,400",
                           "--rounds", "2", "--quiet"])
        self.assertEqual([r["rs_bd"] for r in rows],
                         ["250", "400", "250", "400"])

    def test_a_single_cell_batch_honours_trials(self):
        rows = self._rows(["--loopback", "--run", "--trials", "3", "--quiet"])
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertEqual(r["code"], fm_config.OPERATING_POINT.codename)
            self.assertEqual(int(r["repeats"]),
                             fm_config.OPERATING_POINT.repeats)


if __name__ == "__main__":
    unittest.main()


class TestTheSimulatedChannelMode(unittest.TestCase):
    """--channel: the real modules through the modelled radio.

    This is Phase 6's reason for existing. Two radios on a desk cannot be
    made weak -- 210 bursts over the air on 2026-10-03 lost exactly one, to
    an impulse -- so no configuration can be RANKED there. The model can be
    turned down, and these assert that the mode is wired to the real cell
    machinery rather than being a second, parallel sweep.
    """

    PHONE = ["--fs", "8000", "--rs", "250",
             "--first-tone", "1500", "--spacing", "250"]

    def _rows(self, argv):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.csv")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = fm_bench.main(argv + ["--csv", path])
            self.assertEqual(rc, 0, "run failed:\n%s" % out.getvalue())
            with open(path) as fh:
                return list(csv.DictReader(fh))

    def _refused(self, argv):
        """The stderr and return code of a run that should not happen."""
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                rc = fm_bench.main(argv)
            except SystemExit as exc:          # argparse's own refusals
                return 2, err.getvalue() + out.getvalue() + str(exc)
        return rc, err.getvalue() + out.getvalue()

    @requires_codec2
    def test_snr_is_a_cell_so_a_survival_curve_is_interleaved(self):
        rows = self._rows(["--channel", "--response", "flat", "--rounds", "2",
                           "--quiet", "--cell", "snr_db=0",
                           "--cell", "snr_db=12"] + self.PHONE)
        self.assertEqual([r["snr_db"] for r in rows],
                         ["0", "12", "0", "12"])

    @requires_codec2
    def test_the_snr_reaches_the_channel_rather_than_only_the_label(self):
        """A column that says 0 dB while the model ran at 12 would make every
        survival curve flat and look like a strong modem."""
        low = self._rows(["--channel", "--response", "flat", "--run",
                          "--trials", "6", "--quiet", "--snr", "-9"]
                         + self.PHONE)
        high = self._rows(["--channel", "--response", "flat", "--run",
                           "--trials", "6", "--quiet", "--snr", "20"]
                          + self.PHONE)
        self.assertEqual(sum(int(r["delivered"]) for r in high), 6)
        self.assertLess(sum(int(r["delivered"]) for r in low), 6)

    @requires_codec2
    def test_an_over_the_air_row_leaves_the_snr_column_empty(self):
        """The column separates simulated rows from field rows. A loopback
        run has no channel either, so it must not claim an SNR."""
        rows = self._rows(["--loopback", "--run", "--trials", "1", "--quiet"])
        self.assertEqual(rows[0]["snr_db"], "")

    def test_a_response_is_required_and_has_no_default(self):
        rc, msg = self._refused(["--channel", "--run", "--trials", "1"])
        self.assertEqual(rc, 2)
        self.assertIn("--response", msg)

    def test_an_unknown_response_names_the_ones_that_exist(self):
        rc, msg = self._refused(["--channel", "--response", "nope",
                                 "--run", "--trials", "1"] + self.PHONE)
        self.assertEqual(rc, 2)
        self.assertIn("pixel-rx-2026-09-30", msg)

    @requires_codec2
    def test_tones_the_response_never_measured_are_refused(self):
        """Today's desktop operating point starts at 1000 Hz; the only
        measured response spans 1500-3000. Held flat outside its span, it
        would rank nothing -- so the run must stop rather than produce a
        number nobody can qualify."""
        rc, msg = self._refused(
            ["--channel", "--response", "pixel-rx-2026-09-30", "--run",
             "--trials", "1", "--quiet", "--fs", "8000", "--rs", "400",
             "--first-tone", "1000", "--spacing", "400"])
        self.assertEqual(rc, 2)
        self.assertIn("1500", msg)
        self.assertIn("HELD FLAT", msg)

    @requires_codec2
    def test_allow_unmeasured_is_the_only_way_past_it(self):
        rows = self._rows(
            ["--channel", "--response", "pixel-rx-2026-09-30", "--run",
             "--trials", "1", "--quiet", "--allow-unmeasured",
             "--fs", "8000", "--rs", "400", "--first-tone", "1000",
             "--spacing", "400"])
        self.assertEqual(len(rows), 1)

    def test_a_simulated_run_needs_no_note_and_no_cards(self):
        """--note and --tx/--rx are enforced over the air because an
        unrecorded knob position confounded three investigations. A model has
        no knob, so demanding one would be ceremony -- and the guard is keyed
        on 'not simulated' rather than on a list of the real modes, which is
        how --both-directions slipped past it once."""
        rc, msg = self._refused(["--channel", "--response", "flat",
                                 "--run", "--trials", "0"] + self.PHONE)
        self.assertNotIn("--note is required", msg)
        self.assertNotIn("--tx and --rx are required", msg)

    def test_channel_and_loopback_are_different_experiments(self):
        rc, msg = self._refused(["--channel", "--loopback", "--response",
                                 "flat", "--run", "--trials", "1"])
        self.assertEqual(rc, 2)
        self.assertIn("loopback", msg)

    def test_both_directions_is_refused_because_a_model_has_one_chain(self):
        rc, msg = self._refused(["--channel", "--response", "flat",
                                 "--both-directions", "--rounds", "1"])
        self.assertEqual(rc, 2)
        self.assertIn("one chain", msg)

    def test_a_transmit_level_cell_is_refused_not_ignored(self):
        """tx_level drives a sound card's mixer. Accepting it here and doing
        nothing would produce a two-cell comparison whose cells are
        identical, reported as a null result."""
        rc, msg = self._refused(["--channel", "--response", "flat",
                                 "--rounds", "1", "--cell", "tx_level=-20",
                                 "--cell", "tx_level=-30"] + self.PHONE)
        self.assertEqual(rc, 2)
        self.assertIn("no sound card", msg)

    def test_response_without_channel_is_refused(self):
        rc, msg = self._refused(["--loopback", "--run", "--trials", "1",
                                 "--response", "flat"])
        self.assertEqual(rc, 2)


class TestTheBenchSettingsAreData(unittest.TestCase):
    """--setting: knob positions in their own column, not buried in prose.

    "Three investigations were confounded by an unrecorded level" is the
    plan's wording, and --note alone did not fix it -- a note is prose, and
    two runs' notes cannot be joined on. These assert the column exists, is
    normalised, and cannot be silently skipped.
    """

    def test_it_normalises_so_two_runs_can_be_compared(self):
        """Sorted and joined the same way whatever order it was typed in; a
        column that reorders itself per run cannot be grouped on."""
        a = fm_bench.parse_settings(["squelch=open", "rx_volume=11oclock"])
        b = fm_bench.parse_settings(["rx_volume=11oclock", "squelch=open"])
        self.assertEqual(a, b)
        self.assertEqual(a, "rx_volume=11oclock;squelch=open")

    def test_an_unknown_key_is_refused_and_lists_the_real_ones(self):
        with self.assertRaises(fm_bench.SettingError) as caught:
            fm_bench.parse_settings(["volume=11oclock"])
        self.assertIn("rx_volume", str(caught.exception))

    def test_a_key_with_no_value_points_at_unknown(self):
        with self.assertRaises(fm_bench.SettingError) as caught:
            fm_bench.parse_settings(["rx_volume="])
        self.assertIn("unknown", str(caught.exception))

    def test_a_value_carrying_a_separator_is_refused(self):
        """A comma would split the CSV column; a semicolon would split the
        field. Either corrupts every row from that point on."""
        for bad in ("a,b", "a;b"):
            with self.assertRaises(fm_bench.SettingError):
                fm_bench.parse_settings(["rx_volume=%s" % bad])

    def test_the_same_key_twice_with_different_values_is_refused(self):
        with self.assertRaises(fm_bench.SettingError):
            fm_bench.parse_settings(["squelch=open", "squelch=closed"])

    def test_every_key_carries_a_reason(self):
        """A vocabulary nobody can read is one nobody fills in."""
        for key, why in fm_bench.RUN_SETTINGS.items():
            self.assertTrue(why.strip(), key)
        for key in fm_bench.REQUIRED_SETTINGS:
            self.assertIn(key, fm_bench.RUN_SETTINGS)

    def test_it_lands_in_its_own_csv_column(self):
        self.assertIn("settings", fm_bench.CSV_HEADER)
        trial = fm_bench.Trial(400, "H_1024_2048_4f", 1, 29, 2.95)
        row = trial.row("some prose", "rx_volume=11oclock")
        self.assertEqual(len(row), len(fm_bench.CSV_HEADER))
        self.assertEqual(row[fm_bench.CSV_HEADER.index("settings")],
                         "rx_volume=11oclock")

    def test_an_over_the_air_run_cannot_omit_the_receive_volume(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                fm_bench.main(["--run", "--trials", "1", "--tx", "0",
                               "--rx", "1", "--note", "knobs at 11"])
        self.assertIn("rx_volume", err.getvalue())

    def test_it_can_be_declined_on_the_record_but_not_forgotten(self):
        """'unknown' is accepted, and is recorded. A blank column and a
        column saying nobody looked are different facts."""
        self.assertEqual(fm_bench.parse_settings(["rx_volume=unknown"]),
                         "rx_volume=unknown")

    def test_a_simulated_run_is_not_asked_for_knob_positions(self):
        """The model has no knobs. Demanding them would be ceremony, and
        ceremony is what gets worked around."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fm_bench.main(["--channel", "--response", "flat", "--run",
                                "--trials", "1", "--quiet", "--fs", "8000",
                                "--rs", "250", "--first-tone", "1500",
                                "--spacing", "250"])
        self.assertEqual(rc, 0, err.getvalue())
