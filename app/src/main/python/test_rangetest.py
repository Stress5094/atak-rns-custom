import unittest

import rangetest
import tx_policy


class Clock(object):
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def make(clock=None, **kw):
    return rangetest.RangeTest(distance_fn=tx_policy.haversine_m,
                               clock=clock or Clock(), **kw)


# ~111 m apart: a tenth of a degree of latitude is ~11.1 km, so a thousandth
# is ~111 m. Far enough to be a real separation, small enough to be a walk.
HERE = (35.000000, -79.000000)
THERE = (35.001000, -79.000000)


class TestPositionBook(unittest.TestCase):

    def test_a_fix_is_remembered(self):
        r = make()
        self.assertTrue(r.note_position("aaa", *HERE))
        self.assertAlmostEqual(r.fix_for("aaa").lat, 35.0)

    def test_null_island_is_refused(self):
        """What a device with no fix reports, and a real coordinate 5000 km
        from anywhere -- so without this it becomes the most distant contact
        of every test."""
        r = make()
        self.assertFalse(r.note_position("aaa", 0.0, 0.0))
        self.assertIsNone(r.fix_for("aaa"))

    def test_missing_and_impossible_coordinates_are_refused(self):
        r = make()
        for lat, lon in ((None, -79.0), (35.0, None), (91.0, 0.0), (0.0, 181.0)):
            self.assertFalse(r.note_position("aaa", lat, lon), (lat, lon))

    def test_the_book_is_bounded(self):
        """A long test in a busy area must not grow memory without limit."""
        r = make(max_nodes=4)
        for i in range(20):
            r.note_position("node-%d" % i, 35.0 + i / 1000.0, -79.0)
        self.assertLessEqual(len(r._fixes), 4)
        # The most recent survive; the oldest are dropped.
        self.assertIsNotNone(r.fix_for("node-19"))
        self.assertIsNone(r.fix_for("node-0"))

    def test_a_refreshed_fix_is_not_evicted_as_old(self):
        """Recency is by last update, not by first sight -- the stationary
        node in a range test is heard from constantly and must never be the
        one dropped."""
        r = make(max_nodes=3)
        r.note_position("base", *HERE)
        for i in range(3):
            r.note_position("other-%d" % i, 35.0, -79.0 + i / 1000.0)
            r.note_position("base", *HERE)
        self.assertIsNotNone(r.fix_for("base"))


class TestRows(unittest.TestCase):

    def test_distance_between_the_two_ends(self):
        r = make()
        r.note_position("me", *HERE)
        r.note_position("them", *THERE)
        row = r.note_frame("me", "them", "OnePlus", 7, -95, -3.5, -110)
        self.assertAlmostEqual(row["distance_m"], 111.0, delta=2.0)

    def test_margin_is_signal_over_the_noise_floor(self):
        """The figure that says how close to the edge a link was -- which
        signal strength alone does not, since the floor moves."""
        r = make()
        row = r.note_frame("me", "them", "OnePlus", 7, -95, -3.5, -110)
        self.assertEqual(row["margin_db"], 15)

    def test_a_reading_with_no_fix_is_still_recorded(self):
        """The signal reading is the measurement; the position is an
        annotation. Dropping readings taken before the first GPS lock would
        throw away the start of every walk."""
        r = make()
        row = r.note_frame("me", "them", "OnePlus", 7, -95, -3.5, -110)
        self.assertIsNotNone(row)
        self.assertIsNone(row["distance_m"])
        self.assertEqual(row["usable"], "no")
        self.assertEqual(r.skipped_no_fix, 1)

    def test_one_end_missing_is_not_a_distance(self):
        r = make()
        r.note_position("me", *HERE)
        row = r.note_frame("me", "them", "OnePlus", 7, -95, -3.5, -110)
        self.assertIsNone(row["distance_m"])
        self.assertIsNotNone(row["rx_lat"])
        self.assertIsNone(row["tx_lat"])

    def test_fix_age_is_recorded_for_both_ends(self):
        clock = Clock()
        r = make(clock)
        r.note_position("them", *THERE)
        clock.advance(20.0)
        r.note_position("me", *HERE)
        clock.advance(5.0)
        row = r.note_frame("me", "them", "OnePlus", 7, -95, -3.5, -110)
        self.assertAlmostEqual(row["rx_fix_age_s"], 5.0)
        self.assertAlmostEqual(row["tx_fix_age_s"], 25.0)

    def test_a_stale_fix_makes_the_row_unusable_but_still_written(self):
        """90 seconds is ~100 m at walking pace, the same order as the
        distances being measured. The row keeps its signal reading and says
        the distance cannot be trusted, rather than being silently averaged
        into the result."""
        clock = Clock()
        r = make(clock, max_fix_age_s=60.0)
        r.note_position("me", *HERE)
        r.note_position("them", *THERE)
        clock.advance(90.0)
        row = r.note_frame("me", "them", "OnePlus", 7, -95, -3.5, -110)
        self.assertEqual(row["usable"], "no")
        self.assertIsNotNone(row["distance_m"])
        self.assertGreater(row["tx_fix_age_s"], 60.0)

    def test_fresh_fixes_are_usable(self):
        clock = Clock()
        r = make(clock, max_fix_age_s=60.0)
        r.note_position("me", *HERE)
        r.note_position("them", *THERE)
        clock.advance(5.0)
        self.assertEqual(
            r.note_frame("me", "them", "OnePlus", 7, -95, -3.5, -110)["usable"],
            "yes")

    def test_a_parked_node_stays_usable_however_old_its_fix(self):
        """The 2026-08-21 drive threw away 26 of 29 rows on the STATIONARY node
        because its fixes were old. A parked node's position does not rot."""
        clock = Clock()
        r = make(clock)
        for _ in range(3):                      # twice seen, so speed is known
            r.note_position("me", *HERE)
            r.note_position("them", *THERE)
            clock.advance(5.0)
        clock.advance(600.0)
        row = r.note_frame("me", "them", "OnePlus", 7, -95, -3.5, -110)
        self.assertEqual(row["usable"], "yes")
        self.assertEqual(row["fix_error_m"], 0.0)

    def test_gps_jitter_does_not_fake_a_speed(self):
        """Three metres of wander five seconds apart reads as 0.6 m/s, which
        over a quarter of an hour would condemn a parked node to 500 m of
        imaginary uncertainty."""
        clock = Clock()
        r = make(clock)
        jitter = (HERE[0] + 0.000027, HERE[1])   # ~3 m
        for position in (HERE, jitter, HERE, jitter):
            r.note_position("me", *position)
            r.note_position("them", *THERE)
            clock.advance(5.0)
        clock.advance(300.0)
        self.assertEqual(
            r.note_frame("me", "them", "OnePlus", 7, -95, -3.5, -110)["usable"],
            "yes")

    def test_a_moving_node_is_rejected_while_still_inside_the_old_limit(self):
        """A car at ~13 m/s is hundreds of metres adrift after 45 seconds --
        which the flat 60 s rule this replaced would have accepted."""
        clock = Clock()
        r = make(clock)
        r.note_position("them", *THERE)
        r.note_position("me", *HERE)
        clock.advance(1.0)
        r.note_position("me", *HERE)
        clock.advance(60.0)
        r.note_position("them", THERE[0] + 0.0072, THERE[1])   # ~800 m on
        clock.advance(45.0)
        row = r.note_frame("me", "them", "OnePlus", 7, -95, -3.5, -110)
        self.assertEqual(row["usable"], "no")
        self.assertGreater(row["fix_error_m"], 500.0)
        self.assertLess(row["tx_fix_age_s"], 60.0)   # the old rule's blind spot

    def test_a_fix_older_than_the_backstop_is_refused_however_still(self):
        """A node that sat, drove somewhere and came back looks stationary from
        two identical fixes an hour apart. Nothing that old is evidence."""
        clock = Clock()
        r = make(clock)
        for _ in range(2):
            r.note_position("me", *HERE)
            r.note_position("them", *THERE)
            clock.advance(5.0)
        clock.advance(rangetest.DEFAULT_MAX_FIX_AGE_S + 60.0)
        self.assertEqual(
            r.note_frame("me", "them", "OnePlus", 7, -95, -3.5, -110)["usable"],
            "no")

    def test_link_is_recorded_when_given(self):
        r = make()
        row = r.note_frame("me", "them", "OnePlus", 7, -95, -3.5, -110,
                            link="lora")
        self.assertEqual(row["link"], "lora")

    def test_link_defaults_to_none_when_not_given(self):
        """FM has no signal metering at all, so a range test run on FM would
        still call this with rssi/snr/noise_floor all None -- link should not
        be a silent crash just because a caller doesn't pass it."""
        r = make()
        row = r.note_frame("me", "them", "OnePlus", 7, None, None, None)
        self.assertIsNone(row["link"])

    def test_a_broken_distance_function_does_not_lose_the_reading(self):
        def boom(*a):
            raise ValueError("nope")
        r = rangetest.RangeTest(distance_fn=boom, clock=Clock())
        r.note_position("me", *HERE)
        r.note_position("them", *THERE)
        row = r.note_frame("me", "them", "OnePlus", 7, -95, -3.5, -110)
        self.assertIsNone(row["distance_m"])
        self.assertEqual(row["usable"], "no")


class TestCsv(unittest.TestCase):

    def test_header_matches_the_row_keys(self):
        """The header is what a plotting script binds to, so it and the row
        must not be able to drift apart."""
        r = make()
        row = r.note_frame("me", "them", "OnePlus", 7, -95, -3.5, -110)
        self.assertEqual(set(rangetest.COLUMNS) - {"time"}, set(row))
        self.assertEqual(len(rangetest.header_line().split(",")),
                         len(rangetest.COLUMNS))

    def test_a_line_has_one_field_per_column(self):
        r = make()
        r.note_position("me", *HERE)
        r.note_position("them", *THERE)
        row = r.note_frame("me", "them", "OnePlus", 7, -95, -3.5, -110)
        line = rangetest.format_row(row, "2026-08-15T14:00:00Z")
        self.assertEqual(len(line.split(",")), len(rangetest.COLUMNS))

    def test_missing_values_are_empty_not_zero(self):
        """A 0 in a latitude column is a place, and it is the one place a
        device reports when it has no idea where it is."""
        r = make()
        row = r.note_frame("me", "them", "OnePlus", 7, None, None, None)
        fields = rangetest.format_row(row, "t").split(",")
        columns = dict(zip(rangetest.COLUMNS, fields))
        self.assertEqual(columns["rx_lat"], "")
        self.assertEqual(columns["rssi_dbm"], "")
        self.assertEqual(columns["link"], "")

    def test_link_appears_in_its_own_column(self):
        r = make()
        row = r.note_frame("me", "them", "OnePlus", 7, -95, -3.5, -110,
                            link="fm")
        fields = rangetest.format_row(row, "t").split(",")
        columns = dict(zip(rangetest.COLUMNS, fields))
        self.assertEqual(columns["link"], "fm")

    def test_a_callsign_with_a_comma_does_not_shift_the_columns(self):
        r = make()
        row = r.note_frame("me", "them", 'Bravo, "Six"', 7, -95, -3.5, -110)
        line = rangetest.format_row(row, "t")
        self.assertEqual(len(next(__import__("csv").reader([line]))),
                         len(rangetest.COLUMNS))

    def test_coordinates_keep_enough_precision_to_be_a_place(self):
        r = make()
        r.note_position("me", 35.1234567, -79.7654321)
        r.note_position("them", *THERE)
        row = r.note_frame("me", "them", "OnePlus", 7, -95, -3.5, -110)
        line = rangetest.format_row(row, "t")
        self.assertIn("35.123457", line)


# The wall clock is the monotonic one shifted to a realistic epoch, so a
# report's stamp and the book's ages can be compared in one timeline.
EPOCH = 1_790_000_000.0


def make_stamped(**kw):
    clock = Clock()
    r = make(clock=clock, wall_clock=lambda: clock.t + EPOCH, **kw)
    return r, clock


def wall(clock, ago=0.0):
    """The stamp of a report taken `ago` seconds before now."""
    return clock.t + EPOCH - ago


class TestReportTimestamps(unittest.TestCase):
    """2026-11-07 FM walk: the walker re-sent its start-up keyframe blind
    while already 70 m out, and the stationary phone booked each copy as a
    fresh fix at the start point -- rows of 1 m, usable, against a real
    143-170 m."""

    def test_a_resent_report_does_not_replace_a_newer_one(self):
        r, clock = make_stamped()
        old = wall(clock)
        r.note_position("walker", *HERE, stamp=old)
        clock.advance(60)
        r.note_position("walker", *THERE, stamp=wall(clock))
        clock.advance(30)
        # The blind resend of the first report arrives now.
        self.assertFalse(r.note_position("walker", *HERE, stamp=old))
        self.assertAlmostEqual(r.fix_for("walker").lat, THERE[0])

    def test_a_resend_does_not_reset_the_age_or_the_speed(self):
        r, clock = make_stamped()
        r.note_position("walker", *HERE, stamp=wall(clock))
        clock.advance(60)
        stamp = wall(clock)
        r.note_position("walker", *THERE, stamp=stamp)
        speed = r.fix_for("walker").speed
        clock.advance(45)
        self.assertFalse(r.note_position("walker", *THERE, stamp=stamp))
        fix = r.fix_for("walker")
        self.assertEqual(fix.speed, speed)
        self.assertAlmostEqual(clock.t - fix.at, 45.0)

    def test_a_fix_is_aged_from_when_it_was_taken(self):
        r, clock = make_stamped()
        r.note_position("walker", *HERE, stamp=wall(clock, ago=40))
        self.assertAlmostEqual(clock.t - r.fix_for("walker").at, 40.0)

    def test_the_walk_no_longer_writes_a_usable_wrong_distance(self):
        """The sequence from the OnePlus log, compressed: base fixed, walker
        reports twice from the start, walks 111 m and reports, then the old
        start-up report lands again. The row must say ~111 m, or not be
        usable -- never 0 m and usable."""
        r, clock = make_stamped()
        r.note_position("base", *HERE, stamp=wall(clock))
        first = wall(clock)
        r.note_position("walker", *HERE, stamp=first)
        clock.advance(30)
        r.note_position("base", *HERE, stamp=wall(clock))
        r.note_position("walker", *HERE, stamp=wall(clock))
        clock.advance(80)
        r.note_position("walker", *THERE, stamp=wall(clock))
        clock.advance(20)
        r.note_position("walker", *HERE, stamp=first)    # the blind resend
        row = r.note_frame("base", "walker", "Pixel", 14, None, None, None)
        self.assertAlmostEqual(row["distance_m"], 111.0, delta=2.0)

    def test_a_stamp_from_a_wildly_wrong_clock_is_aged_by_arrival(self):
        r, clock = make_stamped()
        r.note_position("walker", *HERE, stamp=wall(clock, ago=-3600))
        self.assertAlmostEqual(r.fix_for("walker").at, clock.t)
        r2, clock2 = make_stamped()
        r2.note_position("walker", *HERE, stamp=wall(clock2, ago=10 * 3600))
        self.assertAlmostEqual(r2.fix_for("walker").at, clock2.t)

    def test_small_clock_skew_ahead_counts_as_now(self):
        r, clock = make_stamped()
        r.note_position("walker", *HERE, stamp=wall(clock, ago=-2))
        self.assertAlmostEqual(r.fix_for("walker").at, clock.t)

    def test_the_bridge_passes_every_stamp_and_the_wall_clock(self):
        """The fix is inert unless rns_bridge hands the stamp over at every
        call and gives every book a wall clock; an unstamped call silently
        reverts to arrival time, which is exactly the 2026-11-07 failure."""
        import os
        import re
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "rns_bridge.py")).read()
        calls = re.findall(r"_rangetest\.note_position\(([^)]*)\)", src)
        self.assertGreaterEqual(len(calls), 2)
        for args in calls:
            self.assertIn("stamp=summary.time", args)
        books = re.findall(r"rangetest\.RangeTest\(([^)]*)\)", src)
        self.assertGreaterEqual(len(books), 2)
        for args in books:
            self.assertIn("wall_clock=time.time", args)

    def test_refused_reports_are_counted_in_the_summary(self):
        r, clock = make_stamped()
        stamp = wall(clock)
        r.note_position("walker", *HERE, stamp=stamp)
        clock.advance(60)
        r.note_position("walker", *HERE, stamp=stamp)
        r.note_position("walker", *HERE, stamp=stamp - 5)
        self.assertEqual(r.stale_refused, 2)
        self.assertIn("2 stale report(s) ignored", r.summary_line())

    def test_unstamped_reports_behave_as_before(self):
        """Callers with no timestamp keep the arrival-time behaviour."""
        r, clock = make_stamped()
        r.note_position("walker", *HERE)
        clock.advance(30)
        self.assertTrue(r.note_position("walker", *HERE))
        self.assertAlmostEqual(r.fix_for("walker").at, clock.t)

    def test_speed_is_measured_between_the_times_taken(self):
        """Two reports 60 s apart in the field that arrive 5 s apart must
        give ~111 m / 60 s, not / 5 s."""
        r, clock = make_stamped()
        r.note_position("walker", *HERE, stamp=wall(clock, ago=60))
        clock.advance(5)
        r.note_position("walker", *THERE, stamp=wall(clock))
        self.assertAlmostEqual(r.fix_for("walker").speed,
                               (111.2 - rangetest._GPS_JITTER_M) / 65.0,
                               delta=0.05)


class TestDriveSpeed(unittest.TestCase):
    """One phone parked, one in a car (2026-11-08). The flat 50 m rule was a
    walking rule: at 10 m/s, five seconds of fix age is already 50 m, so it
    would refuse nearly every row of a drive -- including the ones kilometres
    out where 50 m is noise. Now: 50 m or a tenth of the distance."""

    def _drive(self, clock, r, far_lat):
        # Parked end, seen twice so its speed (zero) is known.
        r.note_position("base", *HERE)
        clock.advance(1.0)
        r.note_position("base", *HERE)
        # Car: two fixes 10 s apart, ~100 m apart -> ~9 m/s once jitter is off.
        r.note_position("car", far_lat - 0.0009, HERE[1])
        clock.advance(10.0)
        r.note_position("car", far_lat, HERE[1])

    def test_a_far_row_from_a_car_is_usable_with_a_few_seconds_of_age(self):
        clock = Clock()
        r = make(clock)
        self._drive(clock, r, HERE[0] + 0.018)       # ~2 km out
        clock.advance(8.0)                            # beacon fix, 8 s old
        row = r.note_frame("base", "car", "Pixel", 30, None, None, None)
        self.assertGreater(row["fix_error_m"], rangetest.DEFAULT_MAX_FIX_ERROR_M)
        self.assertEqual(row["usable"], "yes")

    def test_the_same_age_close_in_is_still_refused(self):
        """Below 500 m the 50 m floor is the larger and still decides, so the
        walking-scale rule is unchanged."""
        clock = Clock()
        r = make(clock)
        self._drive(clock, r, HERE[0] + 0.0027)      # ~300 m out
        clock.advance(8.0)
        row = r.note_frame("base", "car", "Pixel", 30, None, None, None)
        self.assertGreater(row["fix_error_m"], rangetest.DEFAULT_MAX_FIX_ERROR_M)
        self.assertEqual(row["usable"], "no")

    def test_a_minute_old_car_fix_is_refused_however_far(self):
        """What the 2026-11-08 drive had: the far end's copy of the car was
        about a minute old. ~500 m of error is more than a tenth of 2 km."""
        clock = Clock()
        r = make(clock)
        self._drive(clock, r, HERE[0] + 0.018)
        clock.advance(60.0)
        self.assertEqual(
            r.note_frame("base", "car", "Pixel", 30, None, None, None)["usable"],
            "no")

    def test_the_fraction_can_be_turned_off(self):
        clock = Clock()
        r = make(clock, max_fix_error_frac=0.0)
        self._drive(clock, r, HERE[0] + 0.018)
        clock.advance(8.0)
        self.assertEqual(
            r.note_frame("base", "car", "Pixel", 30, None, None, None)["usable"],
            "no")


if __name__ == "__main__":
    unittest.main()
