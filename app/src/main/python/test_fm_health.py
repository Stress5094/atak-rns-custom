"""That the FM link's status reading distinguishes the states a user can
act on, and -- the part that matters more -- that it never turns a short
observation into a promise about the next transmission."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fm_config
import fm_health
import fm_resend


class _Clock(object):
    def __init__(self):
        self.t = 500.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _health(clock=None, **kw):
    return fm_health.LinkHealth(clock=clock or _Clock(), **kw)


class TestStates(unittest.TestCase):

    def test_no_audio_at_all_is_down(self):
        self.assertEqual(_health().state(), fm_health.DOWN)

    def test_audio_that_stopped_arriving_goes_back_to_down(self):
        # The capture path dying is the one state here that is definitely a
        # bug rather than a radio condition, so it must not linger as
        # whatever the last reading happened to be.
        clock = _Clock()
        h = _health(clock, audio_timeout_s=10.0)
        h.note_audio(0.3)
        h.note_decoded(1)
        self.assertEqual(h.state(), fm_health.HEARING)
        clock.advance(11.0)
        self.assertEqual(h.state(), fm_health.DOWN)

    def test_flat_audio_is_deaf_not_quiet(self):
        # Distinct because the fixes are different: `deaf` points at the
        # radio (off, squelched, volume down, wrong input), `quiet` does
        # not point anywhere.
        h = _health()
        h.note_audio(0.001)
        self.assertEqual(h.state(), fm_health.DEAF)

    def test_real_audio_with_no_decodes_is_quiet(self):
        h = _health()
        h.note_audio(0.4)
        self.assertEqual(h.state(), fm_health.QUIET)

    def test_a_decode_makes_it_hearing(self):
        h = _health()
        h.note_audio(0.4)
        h.note_decoded(1)
        self.assertEqual(h.state(), fm_health.HEARING)

    def test_hearing_decays_back_to_quiet_after_the_window(self):
        # The honesty requirement: a decode ten minutes ago is not evidence
        # about now, on a link measured to swing between 20% and 100% on
        # exactly that timescale.
        clock = _Clock()
        h = _health(clock, window_s=300.0)
        h.note_audio(0.4)
        h.note_decoded(1)
        clock.advance(301.0)
        h.note_audio(0.4)
        self.assertEqual(h.state(), fm_health.QUIET)


class TestGarbled(unittest.TestCase):
    """"Something arrived" and "something arrived that we could use" are
    different claims, and conflating them cost a whole bench evening.

    2026-09-30: the decode counter was fed the RAW demodulator count, so a
    link whose every frame was being thrown away by the framing check
    displayed "Hearing -- decoded 2 frame(s)" while rxb sat at 0 and not
    one byte reached the mesh.
    """

    def test_frames_that_do_not_survive_framing_are_not_hearing(self):
        h = _health()
        h.note_audio(0.5, 0.4)
        h.note_rejected(2)
        self.assertEqual(h.state(), fm_health.GARBLED)

    def test_it_says_the_payload_is_corrupt_and_points_at_the_level(self):
        h = _health()
        h.note_audio(0.5, 0.4)
        h.note_rejected(2)
        summary = h.summary()
        self.assertIn("NONE", summary)
        self.assertIn("level", summary)

    def test_one_usable_frame_outranks_rejects(self):
        h = _health()
        h.note_audio(0.5, 0.4)
        h.note_rejected(3)
        h.note_decoded(1)
        self.assertEqual(h.state(), fm_health.HEARING)
        self.assertIn("3 more recovered but unusable", h.summary())

    def test_garbled_beats_a_level_reading_between_bursts(self):
        # Proof the physical layer works outranks whatever the current
        # chunk's level happens to be in the gap between bursts.
        c = _Clock()
        h = _health(clock=c)
        h.note_audio(0.5, 0.4)
        h.note_rejected(1)
        c.advance(3.0)
        h.note_audio(0.001, 0.001)      # silence between bursts
        self.assertEqual(h.state(), fm_health.GARBLED)

    def test_rejects_age_out_of_the_window(self):
        c = _Clock()
        h = _health(clock=c, window_s=60.0)
        h.note_audio(0.5, 0.4)
        h.note_rejected(1)
        c.advance(61.0)
        h.note_audio(0.5, 0.4)
        self.assertNotEqual(h.state(), fm_health.GARBLED)

    def test_the_snapshot_separates_usable_from_rejected(self):
        h = _health()
        h.note_audio(0.5, 0.4)
        h.note_decoded(1)
        h.note_rejected(2)
        snap = h.snapshot()
        self.assertEqual(snap["decodes_in_window"], 1)
        self.assertEqual(snap["rejects_in_window"], 2)
        self.assertEqual(snap["rejects_total"], 2)


class TestSaturation(unittest.TestCase):
    """Saturation is the one condition here that is both measured and
    directly fixable by the operator -- 2/4 at a saturated level against
    15/16 at any non-saturated one -- and its symptom (a strong signal)
    reads as healthy."""

    def test_a_pinned_level_is_flagged(self):
        h = _health()
        h.note_audio(1.0)
        self.assertTrue(h.saturated())

    def test_a_healthy_level_is_not(self):
        h = _health()
        h.note_audio(0.35)   # the bench's own target range
        self.assertFalse(h.saturated())

    def test_it_is_reported_even_while_decoding(self):
        # A saturated link can still decode -- just worse than it would
        # otherwise -- so this must not be suppressed by success.
        h = _health()
        h.note_audio(1.0)
        h.note_decoded(1)
        self.assertEqual(h.state(), fm_health.HEARING)
        self.assertTrue(h.saturated())
        self.assertIn("SATURATED", h.summary())

    def test_the_advice_names_the_receiving_radio(self):
        # tx_gain was measured NOT to control this: the receiving radio's
        # own volume knob sets the level into the cable. Advice pointing at
        # the transmitter would send someone the wrong way.
        h = _health()
        h.note_audio(1.0)
        self.assertIn("receiving", h.summary())


class TestItRefusesToOverclaim(unittest.TestCase):
    """This project has repeatedly been fooled by short observations and
    wrote it down each time. The status screen must not repeat that on the
    user's behalf."""

    def test_the_summary_never_claims_the_link_is_good(self):
        clock = _Clock()
        h = _health(clock)
        for peak, decodes in ((0.0, 0), (0.001, 0), (0.4, 0), (0.4, 5),
                              (1.0, 5)):
            h.note_audio(peak)
            h.note_decoded(decodes)
            text = h.summary().lower()
            for banned in ("link is good", "connected", "reliable", "healthy"):
                self.assertNotIn(banned, text)

    def test_a_decode_count_is_always_qualified_by_its_window(self):
        h = _health(window_s=300.0)
        h.note_audio(0.4)
        h.note_decoded(3)
        self.assertIn("3 frame(s)", h.summary())
        self.assertIn("5 min", h.summary())

    def test_quiet_admits_it_cannot_tell_the_two_causes_apart(self):
        # "Nobody is transmitting" and "transmissions are not arriving" are
        # genuinely indistinguishable from the receive side on a link with
        # no acknowledgements, and saying so is the point.
        h = _health()
        h.note_audio(0.4)
        self.assertIn("cannot tell apart", h.summary())


class TestSnapshot(unittest.TestCase):

    def test_it_is_json_ready_and_carries_the_window(self):
        import json
        clock = _Clock()
        h = _health(clock)
        h.note_audio(0.4)
        h.note_decoded(2)
        clock.advance(5.0)
        snap = h.snapshot()
        json.dumps(snap)          # must not raise
        self.assertEqual(snap["state"], fm_health.HEARING)
        self.assertEqual(snap["decodes_in_window"], 2)
        self.assertEqual(snap["window_s"], h.window_s)
        self.assertAlmostEqual(snap["last_decode_age_s"], 5.0, places=1)

    def test_age_is_none_rather_than_zero_before_any_decode(self):
        # Zero would read as "just now", which is the opposite of the truth.
        h = _health()
        h.note_audio(0.4)
        self.assertIsNone(h.snapshot()["last_decode_age_s"])


class TestReceiveLevelReadout(unittest.TestCase):
    """The level readout exists so an operator can set a radio's volume
    against a number instead of by ear.

    Motivated by a real 2026-09-30 session: the only level feedback was the
    binary saturation warning, so correcting an over-hot radio overshot
    straight past the target into silence -- twice -- with nothing on screen
    showing how far was too far.
    """

    def test_it_reports_the_loudest_recent_burst_not_the_last_chunk(self):
        # The crux: a burst, then silence between bursts. The instantaneous
        # peak is now the noise floor, but the level being SET is the
        # burst's, so that is what has to stay on screen.
        c = _Clock()
        h = _health(clock=c, peak_window_s=120.0)
        h.note_audio(0.33)
        for _ in range(10):
            c.advance(1.0)
            h.note_audio(0.001)
        self.assertAlmostEqual(h.recent_peak(), 0.33)
        self.assertAlmostEqual(h.snapshot()["peak"], 0.001)

    def test_an_old_burst_goes_stale(self):
        c = _Clock()
        h = _health(clock=c, peak_window_s=60.0)
        h.note_audio(0.80)
        c.advance(61.0)
        self.assertAlmostEqual(h.recent_peak(), 0.0)

    def test_a_quieter_later_burst_replaces_a_louder_earlier_one(self):
        # The bug this pins, caught on the bench 2026-09-30: as a rolling
        # MAXIMUM the readout stuck at 1.00 and ignored every later burst
        # for the whole window -- so an operator turning the volume DOWN,
        # which is the entire reason they were watching it, saw no response.
        c = _Clock()
        h = _health(clock=c, peak_window_s=120.0)
        h.note_audio(1.0, 0.80)              # saturated burst
        c.advance(10.0)                      # silence, ends the burst
        h.note_audio(0.001, 0.001)
        c.advance(10.0)
        h.note_audio(0.35, 0.25)             # operator turned it down
        self.assertAlmostEqual(h.recent_level(), 0.25)
        self.assertEqual(h.level_state(), "good")

    def test_one_burst_reports_its_own_loudest_chunk(self):
        c = _Clock()
        h = _health(clock=c)
        for p in (0.10, 0.40, 0.22):         # contiguous, one burst
            h.note_audio(p)
            c.advance(0.1)
        self.assertAlmostEqual(h.recent_peak(), 0.40)

    def test_quiet_chunks_between_bursts_do_not_end_the_reading(self):
        # Between bursts the capture path still delivers noise-floor
        # chunks; those must not blank the last real reading.
        c = _Clock()
        h = _health(clock=c, peak_window_s=120.0)
        h.note_audio(0.33)
        for _ in range(50):
            c.advance(0.1)
            h.note_audio(0.001)
        self.assertAlmostEqual(h.recent_peak(), 0.33)

    def test_level_states_cover_the_band(self):
        # Graded on RMS; peak passed alongside at the constant-envelope
        # ratio these bursts actually have.
        for rms, expected in ((0.001, "none"), (0.08, "low"),
                              (0.25, "good"), (0.50, "hot")):
            h = _health()
            h.note_audio(rms * (2 ** 0.5), rms)
            self.assertEqual(h.level_state(), expected, rms)
        h = _health()
        h.note_audio(1.0, 0.80)          # sustained loud AND clipping
        self.assertEqual(h.level_state(), "saturated")

    def test_one_loud_click_does_not_make_a_quiet_link_read_too_hot(self):
        # The exact 2026-09-30 bench symptom, reported by the operator: the
        # Pixel's radio was turned almost all the way DOWN and the readout
        # still insisted "TOO HOT", because a single key-up transient hit
        # full scale and the reading was graded on peak. The link was
        # simultaneously reporting itself "Silent".
        h = _health()
        h.note_audio(1.0, 0.004)         # a click: full-scale peak, no body
        self.assertEqual(h.level_state(), "none")
        self.assertNotIn("TOO HOT", h.level_line())

    def test_a_click_does_not_latch_over_a_real_later_burst(self):
        c = _Clock()
        h = _health(clock=c)
        h.note_audio(1.0, 0.004)         # click
        c.advance(5.0)
        h.note_audio(0.35, 0.25)         # a real, in-band burst
        self.assertEqual(h.level_state(), "good")
        self.assertAlmostEqual(h.recent_level(), 0.25)

    def test_sustained_loud_without_clipping_is_hot_not_saturated(self):
        h = _health()
        h.note_audio(0.60, 0.42)
        self.assertEqual(h.level_state(), "hot")
        self.assertIn("ease", h.level_line())

    def test_levels_of_returns_both_in_one_pass(self):
        peak, rms = fm_health.levels_of([0.5, -0.5, 0.5, -0.5])
        self.assertAlmostEqual(peak, 0.5)
        self.assertAlmostEqual(rms, 0.5)
        self.assertEqual(fm_health.levels_of([]), (0.0, 0.0))

    def test_a_constant_envelope_tone_reads_rms_below_its_peak(self):
        # Sanity on the derivation behind TARGET_RMS_*: a sine's RMS is
        # its amplitude over sqrt(2).
        import math
        samples = [0.4 * math.sin(i * 0.3) for i in range(2000)]
        peak, rms = fm_health.levels_of(samples)
        self.assertAlmostEqual(rms, peak / (2 ** 0.5), places=2)

    def test_the_target_band_matches_the_documented_bench_procedure(self):
        self.assertLess(fm_health.SILENT_PEAK, fm_health.TARGET_PEAK_LOW)
        self.assertLess(fm_health.TARGET_PEAK_LOW, fm_health.TARGET_PEAK_HIGH)
        self.assertLess(fm_health.TARGET_PEAK_HIGH, fm_health.SATURATED_PEAK)

    def test_the_line_names_a_direction_to_turn_the_knob(self):
        h = _health()
        h.note_audio(0.05)
        self.assertIn("up", h.level_line())
        h = _health()
        h.note_audio(0.99)
        self.assertIn("down", h.level_line())

    def test_the_line_says_how_old_the_reading_is(self):
        # A persisted reading set two radios wrong on 2026-09-30: seconds
        # after an adjustment it still showed the PRE-adjustment level and
        # called it "in band". The age is what makes that visible.
        c = _Clock()
        h = _health(clock=c)
        h.note_audio(0.35, 0.25)
        self.assertIn("now", h.level_line())
        c.advance(45.0)
        self.assertIn("45s ago", h.level_line())
        self.assertAlmostEqual(h.snapshot()["level_age_s"], 45.0)

    def test_age_is_none_before_anything_is_heard(self):
        h = _health()
        self.assertIsNone(h.level_age_s())
        self.assertIsNone(h.snapshot()["level_age_s"])

    def test_the_line_carries_the_number_and_the_target(self):
        h = _health()
        h.note_audio(0.35, 0.25)
        line = h.level_line()
        self.assertIn("0.25", line)                      # the reading
        self.assertIn("%.2f" % fm_health.TARGET_RMS_LOW, line)
        self.assertIn("%.2f" % fm_health.TARGET_RMS_HIGH, line)

    def test_it_says_so_rather_than_inventing_a_level_from_silence(self):
        h = _health()
        h.note_audio(0.0)
        self.assertEqual(h.level_state(), "none")
        self.assertNotIn("0.00 (", h.level_line())

    def test_the_snapshot_carries_it_for_the_ui(self):
        h = _health()
        h.note_audio(0.35, 0.25)
        snap = h.snapshot()
        self.assertAlmostEqual(snap["level_recent"], 0.25)
        self.assertAlmostEqual(snap["peak_recent"], 0.35)
        self.assertEqual(snap["level_state"], "good")
        self.assertIn("in band", snap["level_line"])

    def test_it_keeps_no_per_chunk_history(self):
        # note_audio() runs per audio chunk on the audio thread, so the
        # reading must cost O(1) state, not a growing buffer.
        c = _Clock()
        h = _health(clock=c)
        for _ in range(2000):
            c.advance(0.01)
            h.note_audio(0.3)
        self.assertAlmostEqual(h.recent_peak(), 0.3)
        self.assertFalse(
            [a for a in vars(h).values() if isinstance(a, (list, tuple))
             and len(a) > 100],
            "a per-chunk buffer crept back into the level tracking")


class TestPeakOf(unittest.TestCase):

    def test_it_finds_the_largest_magnitude_either_sign(self):
        self.assertAlmostEqual(fm_health.peak_of([0.1, -0.7, 0.3]), 0.7)

    def test_an_empty_block_is_zero_not_an_error(self):
        self.assertEqual(fm_health.peak_of([]), 0.0)


class TestItDoesNotKeepItsOwnCopyOfTheOperatingPoint(unittest.TestCase):
    """fm_health used to redefine fm_config's four level constants. Two modules
    each holding "what a good receive level is" is one silent disagreement away
    from a status screen grading a level the transport does not want."""

    def test_the_level_constants_come_from_fm_config(self):
        self.assertEqual(fm_health.SILENT_PEAK, fm_config.SILENT_PEAK)
        self.assertEqual(fm_health.SATURATED_PEAK, fm_config.SATURATED_PEAK)
        self.assertEqual(fm_health.TARGET_PEAK_LOW, fm_config.TARGET_PEAK_LOW)
        self.assertEqual(fm_health.TARGET_PEAK_HIGH, fm_config.TARGET_PEAK_HIGH)

    def test_the_rms_band_is_the_peak_band_over_root_two(self):
        """DERIVED, not separately measured -- these bursts are
        constant-envelope 4FSK so rms = peak / sqrt(2) for the tone itself. The
        readout grades RMS because a single key-up click pins a PEAK to full
        scale regardless of how the real signal sits."""
        self.assertAlmostEqual(fm_health.TARGET_RMS_LOW,
                               fm_config.TARGET_PEAK_LOW / (2 ** 0.5))
        self.assertAlmostEqual(fm_health.TARGET_RMS_HIGH,
                               fm_config.TARGET_PEAK_HIGH / (2 ** 0.5))
        # The band an operator is actually told to aim at.
        self.assertAlmostEqual(fm_health.TARGET_RMS_LOW, 0.18, delta=0.01)
        self.assertAlmostEqual(fm_health.TARGET_RMS_HIGH, 0.32, delta=0.01)


class TestTheDecodeWindowCoversAWholeResendSchedule(unittest.TestCase):
    """Same failure shape as the reassembly window, in the status screen
    instead of the receive buffer: if a decode stops "counting" before the
    sender's next attempt, the display drops to `quiet` mid-schedule and tells
    an operator the link died when it did not."""

    def test_the_window_outlasts_the_senders_whole_schedule(self):
        self.assertGreater(fm_health.DEFAULT_WINDOW_S, fm_resend.span_s())

    def test_it_never_shrinks_below_the_figure_the_bench_used(self):
        """300 s is what every bench session was read against; the derived term
        may widen it but must not narrow it."""
        self.assertGreaterEqual(fm_health.DEFAULT_WINDOW_S, 300.0)

    def test_a_decode_still_counts_after_a_full_schedule_has_elapsed(self):
        clock = _Clock()
        health = fm_health.LinkHealth(clock=clock)
        health.note_audio(0.3, 0.22)
        health.note_decoded(1)
        self.assertEqual(health.state(), fm_health.HEARING)
        # Right up to the last attempt of the slowest schedule fm_resend runs.
        clock.advance(fm_resend.span_s())
        health.note_audio(0.3, 0.22)
        self.assertEqual(health.state(), fm_health.HEARING,
                         "a decode stopped counting before the sender had "
                         "finished resending, so the screen would go quiet "
                         "mid-schedule")


if __name__ == "__main__":
    unittest.main()
