"""That blind time-spread resending schedules what it should, and -- more
importantly -- that the properties the rest of the system depends on hold:
a stable msg_id across attempts, a bounded span, and no acknowledgement
machinery anywhere in it."""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cot_codec
import fm_config
import fm_resend
import mesh
import reliable


class _Clock(object):
    """A clock that only moves when a test says so."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _engine(clock=None, **kw):
    # Seeded rng so jitter is reproducible: a flaky scheduling test is
    # worse than no scheduling test.
    kw.setdefault("rng", random.Random(12345))
    return fm_resend.FmResend(clock=clock or _Clock(), **kw)


class TestScheduling(unittest.TestCase):

    def test_nothing_is_due_immediately_after_registering(self):
        clock = _Clock()
        eng = _engine(clock)
        eng.register(1, [b"pkt"], "chat")
        self.assertEqual(eng.tick(), [])

    def test_a_resend_comes_due_after_the_interval(self):
        clock = _Clock()
        # first_retry pinned to match interval_s: this test is about the
        # general "due after the interval" mechanic, not the front-loaded
        # first gap (see TestFrontLoadedFirstRetry for that).
        eng = _engine(clock, interval_s=240.0, jitter_frac=0.0,
                      first_retry_s=240.0, first_retry_jitter_s=0.0)
        eng.register(1, [b"pkt"], "chat")
        clock.advance(239.0)
        self.assertEqual(eng.tick(), [])
        clock.advance(2.0)
        due = eng.tick()
        self.assertEqual(len(due), 1)
        msg_id, packets, label, attempt = due[0]
        self.assertEqual((msg_id, packets, label), (1, [b"pkt"], "chat"))
        # The caller already sent attempt 1, so the first resend is 2.
        self.assertEqual(attempt, 2)

    def test_it_stops_after_the_configured_attempts(self):
        clock = _Clock()
        eng = _engine(clock, attempts=4, interval_s=100.0, jitter_frac=0.0)
        eng.register(1, [b"pkt"], "chat")
        seen = []
        for _ in range(10):
            clock.advance(101.0)
            seen.extend(eng.tick())
        # 4 total transmissions = the caller's first + 3 resends here.
        self.assertEqual([a for _, _, _, a in seen], [2, 3, 4])
        self.assertEqual(eng.snapshot()["inflight"], [])

    def test_attempts_of_one_schedules_no_resends_at_all(self):
        clock = _Clock()
        eng = _engine(clock, attempts=1)
        eng.register(1, [b"pkt"], "chat")
        clock.advance(100000.0)
        self.assertEqual(eng.tick(), [])
        self.assertEqual(eng.stats()["completed"], 1)

    def test_forget_stops_a_schedule(self):
        clock = _Clock()
        eng = _engine(clock, interval_s=100.0, jitter_frac=0.0)
        eng.register(1, [b"pkt"], "chat")
        eng.forget(1)
        clock.advance(101.0)
        self.assertEqual(eng.tick(), [])

    def test_several_messages_are_scheduled_independently(self):
        clock = _Clock()
        eng = _engine(clock, interval_s=100.0, jitter_frac=0.0,
                      first_retry_s=100.0, first_retry_jitter_s=0.0)
        eng.register(1, [b"a"], "one")
        clock.advance(50.0)
        eng.register(2, [b"b"], "two")
        clock.advance(51.0)
        due = eng.tick()
        self.assertEqual([m for m, _, _, _ in due], [1])
        clock.advance(50.0)
        due = eng.tick()
        self.assertEqual([m for m, _, _, _ in due], [2])

    def test_the_oldest_schedule_is_shed_when_too_many_are_in_flight(self):
        clock = _Clock()
        eng = _engine(clock, max_inflight=2, interval_s=100.0)
        eng.register(1, [b"a"], "one")
        eng.register(2, [b"b"], "two")
        eng.register(3, [b"c"], "three")
        live = [e["msg_id"] for e in eng.snapshot()["inflight"]]
        self.assertEqual(live, [2, 3])
        self.assertEqual(eng.stats()["displaced"], 1)


class TestFrontLoadedFirstRetry(unittest.TestCase):
    """Attempt 2 recovers a single missed burst fast; attempts 3+ still
    sample the channel minutes apart, exactly as before this feature."""

    def test_the_first_resend_uses_the_short_gap_not_the_interval(self):
        clock = _Clock()
        eng = _engine(clock, interval_s=240.0, jitter_frac=0.0,
                      first_retry_s=15.0, first_retry_jitter_s=0.0)
        eng.register(1, [b"pkt"], "chat")
        clock.advance(16.0)
        due = eng.tick()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0][3], 2)   # attempt 2

    def test_later_resends_still_use_the_full_interval(self):
        clock = _Clock()
        eng = _engine(clock, attempts=3, interval_s=240.0, jitter_frac=0.0,
                      first_retry_s=15.0, first_retry_jitter_s=0.0)
        eng.register(1, [b"pkt"], "chat")
        clock.advance(16.0)
        eng.tick()                       # attempt 2, the fast one
        clock.advance(239.0)
        self.assertEqual(eng.tick(), [])
        clock.advance(2.0)
        due = eng.tick()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0][3], 3)   # attempt 3, full interval later

    def test_default_first_retry_falls_in_the_documented_10_to_20s_range(self):
        # Pins the "~10-20s" claim in the module docstring/comment.
        lo = fm_resend.DEFAULT_FIRST_RETRY_S - fm_resend.DEFAULT_FIRST_RETRY_JITTER_S
        hi = fm_resend.DEFAULT_FIRST_RETRY_S + fm_resend.DEFAULT_FIRST_RETRY_JITTER_S
        self.assertEqual((lo, hi), (10.0, 20.0))

    def test_span_s_accounts_for_the_short_first_gap_separately(self):
        span = fm_resend.span_s(attempts=5, interval_s=240.0,
                                jitter_frac=0.15, first_retry_s=15.0,
                                first_retry_jitter_s=5.0)
        # 1 short gap (worst case 20s) + 3 full-interval gaps (worst case
        # 240 * 1.15 each), not 4 uniform interval gaps.
        self.assertAlmostEqual(span, 20.0 + 3 * 240.0 * 1.15)

    def test_span_s_of_two_attempts_is_just_the_first_gap(self):
        # attempts=2 means the whole schedule IS the fast retry -- no full
        # interval gap ever happens.
        span = fm_resend.span_s(attempts=2, interval_s=240.0,
                                jitter_frac=0.15, first_retry_s=15.0,
                                first_retry_jitter_s=5.0)
        self.assertEqual(span, 20.0)

    def test_bumping_the_attempt_count_preserves_the_old_cluster_escaping_span(self):
        # The whole reason DEFAULT_ATTEMPTS went 4 -> 5 rather than swapping
        # one of the original four for the fast retry: the three remaining
        # full-interval gaps plus the old 4-attempt uniform span should be
        # comparable, not smaller -- a front-loaded schedule must not make
        # the worst-case escape window WORSE than the un-front-loaded one it
        # replaces.
        old_four_attempt_span = 3 * fm_resend.DEFAULT_INTERVAL_S * (
            1.0 + fm_resend.DEFAULT_JITTER_FRAC)
        self.assertGreaterEqual(fm_resend.span_s(), old_four_attempt_span)


class TestTheContractCallersDependOn(unittest.TestCase):
    """The properties other modules are sized against. Each of these failing
    is a user-visible bug somewhere else, not a scheduling nicety."""

    def test_every_attempt_carries_the_same_payload_bytes(self):
        # The receiver combines fragments across attempts by msg_id. If a
        # resend carried re-encoded (and therefore possibly differently
        # fragmented) bytes, the fragments would not line up and the
        # accumulation this link depends on would silently stop working.
        clock = _Clock()
        eng = _engine(clock, attempts=3, interval_s=10.0, jitter_frac=0.0,
                      first_retry_s=10.0, first_retry_jitter_s=0.0)
        packets = [b"frag0", b"frag1"]
        eng.register(7, packets, "chat")
        for _ in range(2):
            clock.advance(11.0)
            for _, got, _, _ in eng.tick():
                self.assertEqual(got, packets)

    def test_mutating_the_caller_list_afterwards_cannot_change_a_resend(self):
        clock = _Clock()
        eng = _engine(clock, interval_s=10.0, jitter_frac=0.0,
                      first_retry_s=10.0, first_retry_jitter_s=0.0)
        packets = [b"frag0"]
        eng.register(7, packets, "chat")
        packets.append(b"injected")
        clock.advance(11.0)
        (_, got, _, _), = eng.tick()
        self.assertEqual(got, [b"frag0"])

    def test_message_ids_do_not_repeat_while_messages_are_in_flight(self):
        eng = _engine()
        ids = [eng.next_msg_id() for _ in range(fm_resend.DEFAULT_MAX_INFLIGHT * 4)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_message_ids_stay_inside_the_wire_field(self):
        eng = _engine()
        # The wire field is 16 bits but wrap_reliable masks to 15 (the top bit
        # is the any-ack flag). An id above that arrives as a different number,
        # so the heard-tag never matches. The old bound here was 0xFFFF, which
        # is how it shipped.
        import cot_codec
        self.assertEqual(fm_resend.MSG_ID_MASK, cot_codec.MSG_ID_MASK)
        for _ in range(70000):
            msg_id = eng.next_msg_id()
            self.assertEqual(cot_codec.wrap_reliable(b"x", "n", msg_id)[0], msg_id)

    def test_the_span_covers_every_attempt_including_jitter(self):
        # span_s() is what the receive-side duplicate window and the
        # reassembly timeout are sized against, so it must never be shorter
        # than a schedule can actually run.
        attempts, interval, jf = 4, 240.0, 0.15
        clock = _Clock()
        eng = _engine(clock, attempts=attempts, interval_s=interval,
                      jitter_frac=jf)
        eng.register(1, [b"p"], "chat")
        start = clock.t
        last = start
        for _ in range(200):
            clock.advance(5.0)
            if eng.tick():
                last = clock.t
        self.assertLessEqual(last - start,
                             fm_resend.span_s(attempts, interval, jf))

    def test_the_default_span_no_longer_outlasts_the_worst_recorded_cluster(self):
        # 2026-09-20: DEFAULT_INTERVAL_S was deliberately shortened 240s ->
        # 60s on explicit user request, AFTER being shown that this trades
        # away full coverage of the worst measured cluster (a 19-trial dead
        # stretch across 9.6 minutes = 576s) for ~4x faster typical-case
        # recovery. This test used to assert span_s() >= 600.0 -- the
        # design's original goal. It now pins the actual (smaller) number
        # instead, so a FUTURE accidental change to these constants is still
        # caught, without pretending the original guarantee still holds.
        # See fm_resend.py's DEFAULT_INTERVAL_S comment and
        # docs/open-questions.md for the full reasoning.
        WORST_RECORDED_CLUSTER_S = 576.0
        self.assertLess(fm_resend.span_s(), WORST_RECORDED_CLUSTER_S)
        self.assertAlmostEqual(fm_resend.span_s(), 227.0, delta=1.0)

    def test_it_has_no_acknowledgement_surface_at_all(self):
        # The FM link's hard constraint is structural, so pin it
        # structurally: nothing here may accept or account for an ack.
        for name in dir(fm_resend.FmResend):
            self.assertNotIn("ack", name.lower())
            self.assertNotIn("partial", name.lower())


class TestHeardReports(unittest.TestCase):
    """The link's only completion signal. It can cancel a schedule and must
    never start one -- that is what keeps it inside the no-acknowledgements
    constraint rather than an ack by another name."""

    def test_a_matching_report_stops_further_attempts(self):
        clock = _Clock()
        eng = _engine(clock, interval_s=100.0, jitter_frac=0.0)
        eng.register(1, [b"p"], "chat", tag=b"\x01\x02")
        self.assertEqual(eng.note_heard([b"\x01\x02"]), ["chat"])
        clock.advance(1000.0)
        self.assertEqual(eng.tick(), [])
        self.assertEqual(eng.stats()["confirmed"], 1)

    def test_a_report_for_someone_elses_message_changes_nothing(self):
        # The dangerous direction: cancelling on a tag we did not send
        # silently abandons a message that never arrived.
        clock = _Clock()
        eng = _engine(clock, interval_s=100.0, jitter_frac=0.0)
        eng.register(1, [b"p"], "chat", tag=b"\x01\x02")
        self.assertEqual(eng.note_heard([b"\xaa\xbb"]), [])
        clock.advance(101.0)
        self.assertEqual(len(eng.tick()), 1)

    def test_an_empty_report_is_not_a_confirmation_of_everything(self):
        clock = _Clock()
        eng = _engine(clock, interval_s=100.0, jitter_frac=0.0)
        eng.register(1, [b"p"], "chat", tag=b"\x01\x02")
        eng.note_heard([])
        eng.note_heard(None)
        clock.advance(101.0)
        self.assertEqual(len(eng.tick()), 1)

    def test_a_message_registered_without_a_tag_is_never_cancelled(self):
        clock = _Clock()
        eng = _engine(clock, interval_s=100.0, jitter_frac=0.0)
        eng.register(1, [b"p"], "chat")           # no tag
        eng.note_heard([b"\x01\x02", b"\x00\x00"])
        clock.advance(101.0)
        self.assertEqual(len(eng.tick()), 1)

    def test_it_never_causes_a_transmission(self):
        # note_heard returns labels for logging and nothing transmittable;
        # only tick() ever yields packets.
        eng = _engine()
        eng.register(1, [b"p"], "chat", tag=b"\x01\x02")
        got = eng.note_heard([b"\x01\x02"])
        self.assertTrue(all(isinstance(x, str) for x in got))

    def test_a_confirmed_message_shows_as_completed(self):
        eng = _engine()
        eng.register(4, [b"p"], "marker x", tag=b"\x01\x02")
        eng.note_heard([b"\x01\x02"])
        snap = eng.snapshot()
        self.assertEqual(snap["inflight"], [])
        self.assertEqual(snap["completed"][-1]["msg_id"], 4)


class TestTheDigestFitsOnTheBeacon(unittest.TestCase):
    """The carriage. note_heard() is useless without a way for the tags to
    travel, and the only free ride is the periodic beacon.

    THIS CLASS CHANGED MEANING ON 2026-10-02, and the old version is worth
    describing because the arithmetic in it was correct and is now simply about
    a different link. Through Phase 2a the operating point was the 32 B code,
    whose beacon budget was 17 B after the mesh envelope:

        beacon floor (2 B name, no callsign)      10 B plaintext
        + 3 heard tags, no neighbour digest       17 B   -- EXACTLY full
        + the 8 B neighbour digest alone          18 B   -- over
        + 3 tags AND the digest                   25 B   -- well over

    So _FM_HEARD_MAX = 3 was a CEILING with zero slack, carrying the digest was
    impossible, and every 2 bytes given back to the node name cost a whole tag.

    The position path then forced the 128 B code (see fm_config.DEFAULT_CODE),
    which took the budget to 113 B. None of those constraints bind any more.
    What these tests now assert is the headroom and the two things it unblocked,
    because "it fits" is exactly the kind of claim that rots silently.
    """

    def budget(self):
        return (fm_config.OPERATING_POINT.capacity_bytes() - mesh.OVERHEAD)

    def beacon(self, tags=None, digest=None, name="Pi", callsign=""):
        return cot_codec.encode_beacon(
            "abcdef12", 7, name=name, callsign=callsign,
            heard=tags or None, digest=digest)

    def tags(self, n):
        return [cot_codec.heard_tag("abcdef12", i) for i in range(n)]

    def test_the_budget_is_no_longer_the_binding_constraint(self):
        """Three tags used to consume it exactly. Now they are a sixth of it."""
        self.assertEqual(self.budget(), 113)
        self.assertLess(len(self.beacon(self.tags(3))), self.budget() // 4)

    def test_the_heard_cap_is_now_a_choice_rather_than_a_ceiling(self):
        """_FM_HEARD_MAX = 3 was arithmetic; it is now a judgement -- a sender's
        schedule runs for minutes while the beacon repeats every ~90 s, so the
        most recent few are the ones still worth confirming. Asserted as a
        choice so nobody re-derives it as a limit: four, eight and sixteen tags
        would all fit."""
        for n in (3, 4, 8, 16):
            with self.subTest(tags=n):
                self.assertLess(len(self.beacon(self.tags(n))), self.budget())

    def test_the_neighbour_digest_and_the_heard_digest_NOW_COEXIST(self):
        """They could not at the 32 B code, which is why _beacon_tick dropped
        the digest on FM. It still does -- relaying is LoRa-only, so the digest
        buys nothing there -- but that is now a decision about USEFULNESS rather
        than about room, and _send_one_beacon takes them as separate arguments
        so the two cannot be confused again."""
        both = self.beacon(self.tags(3), digest=bytes(range(8)))
        self.assertLess(len(both), self.budget())

    # THE NODE-NAME CAPS ARE TESTED IN test_rns_bridge_wiring, not here. They
    # are rns_bridge's constants, and this module deliberately imports nothing
    # from the bridge -- fm_resend.py itself is free of RNS and Android, which
    # is what lets the whole schedule be tested on a desktop, and a test module
    # that reaches for rns_bridge would quietly undo that discipline.

    def test_a_tag_is_two_bytes_of_hashed_origin_and_id(self):
        """Not the msg_id itself: ids are unique per SENDER only, so two nodes'
        message 7 are the same number, and a false positive abandons a message
        that never arrived."""
        self.assertEqual(cot_codec.HEARD_TAG_LEN, 2)
        a = cot_codec.heard_tag("node-a", 7)
        b = cot_codec.heard_tag("node-b", 7)
        self.assertEqual(len(a), 2)
        self.assertNotEqual(a, b, "the origin is not folded in, so two nodes' "
                                  "identical message ids collide "
                                  "systematically rather than by accident")


class TestTheSpanSizesTheReceiveSide(unittest.TestCase):
    """span_s() is exported because two things on the RECEIVING end have to be
    sized against it, and sizing them by hand is how they drift apart. These
    assert the relationship against the real library defaults, because the
    consequence of getting it wrong is not a crash -- it is a quiet collapse in
    delivery that looks like a bad radio.
    """

    def test_the_duplicate_window_outlasts_a_whole_schedule(self):
        """A resend arriving after the receiver has forgotten the message is
        delivered to ATAK twice -- a chat message rendered again, or a marker
        that reappears after being cleared."""
        self.assertGreater(reliable.DEFAULT_SEEN_TTL_S, fm_resend.span_s())

    def test_the_reassembler_library_default_is_far_too_short(self):
        """THE MOST CONSEQUENTIAL NUMBER IN THIS MODULE'S CONTRACT.
        cot_codec.Reassembler's 30 s constructor default expires attempt 1's
        fragments long before attempt 4 arrives -- by a factor of 7.

        That is the difference between the two regimes in fm_resend's
        docstring. At p=0.5 with 8 fragments over 4 attempts, fragments
        accumulating across attempts gives ~60% deliverable and each attempt
        having to land whole gives ~1.6%. The measured figures agree
        (docs/open-questions.md:2291: ~60% against under 2%).

        rns_bridge does NOT use that default -- it derives the window from this
        span, and test_rns_bridge_wiring asserts the derivation. This test
        guards the constructor default itself, because it is what any future
        second caller gets for free, and because a library default that happens
        to be adequate would make the derivation look like ceremony.
        """
        default_timeout = 30.0
        self.assertLess(default_timeout, fm_resend.span_s(),
                        "if the Reassembler default ever exceeds the span, "
                        "this test and the warning in fm_resend's docstring "
                        "should both be revisited")
        self.assertGreater(fm_resend.span_s() / default_timeout, 7.0)

    def test_the_span_is_what_a_caller_can_actually_size_against(self):
        """It must be computable without constructing an engine or moving a
        clock, or the two call sites that need it cannot use it at startup."""
        self.assertGreater(fm_resend.span_s(), 0.0)
        self.assertEqual(fm_resend.span_s(attempts=1), 0.0)


class TestConcurrency(unittest.TestCase):
    """register() runs on the CoT listener thread and tick() on the retry
    thread. rns_bridge holds _reliable_lock across both, but a raise here
    would land in _reliable_loop's broad except and silently skip that tick
    for LoRa's ARQ retries too -- a much wider blast radius than the FM
    schedule it came from."""

    def test_registering_during_a_tick_does_not_raise(self):
        clock = _Clock()
        # first_retry pinned to interval_s=10/jitter=0 so every registered
        # message is deterministically due at clock.advance(11.0) below --
        # otherwise the front-loaded first gap's own jitter (10-20s) could
        # leave nothing due yet, and the racing_uniform hook would never
        # fire (see the assertTrue(fired, ...) at the bottom).
        eng = _engine(clock, attempts=4, interval_s=10.0, jitter_frac=0.0,
                      first_retry_s=10.0, first_retry_jitter_s=0.0)
        for i in range(5):
            eng.register(i, [b"p"], "m%d" % i)
        clock.advance(11.0)

        # Simulates the interleaving deterministically, with no sleep and no
        # race to lose. _delay() is called from INSIDE tick()'s loop when it
        # reschedules an entry, and it goes through _rng.uniform -- so
        # hooking that runs register() at exactly the moment another thread
        # would have, mid-iteration. Hooking the clock instead does NOT
        # work: tick() reads it once, before the loop.
        real_uniform = eng._rng.uniform
        fired = []

        def racing_uniform(a, b):
            if not fired:
                fired.append(True)
                eng.register(99, [b"late"], "late")
            return real_uniform(a, b)

        eng._rng.uniform = racing_uniform
        eng.tick()          # must not raise RuntimeError
        self.assertTrue(fired, "the hook never ran; test proves nothing")


class TestReporting(unittest.TestCase):

    def test_snapshot_counts_down_to_the_next_attempt(self):
        clock = _Clock()
        eng = _engine(clock, interval_s=100.0, jitter_frac=0.0,
                      first_retry_s=100.0, first_retry_jitter_s=0.0)
        eng.register(1, [b"p"], "chat")
        clock.advance(40.0)
        (entry,) = eng.snapshot()["inflight"]
        self.assertEqual(entry["attempts"], 1)
        self.assertEqual(entry["of"], eng.attempts)
        self.assertAlmostEqual(entry["next_in_s"], 60.0, places=1)

    def test_a_finished_message_moves_to_completed(self):
        clock = _Clock()
        eng = _engine(clock, attempts=2, interval_s=10.0, jitter_frac=0.0,
                      first_retry_s=10.0, first_retry_jitter_s=0.0)
        eng.register(9, [b"p"], "marker abc")
        clock.advance(11.0)
        eng.tick()
        snap = eng.snapshot()
        self.assertEqual(snap["inflight"], [])
        self.assertEqual(snap["completed"][-1],
                         {"msg_id": 9, "label": "marker abc", "attempts": 2})

    def test_the_summary_line_mentions_what_happened(self):
        clock = _Clock()
        eng = _engine(clock, attempts=2, interval_s=10.0, jitter_frac=0.0,
                      first_retry_s=10.0, first_retry_jitter_s=0.0)
        eng.register(1, [b"p"], "chat")
        clock.advance(11.0)
        eng.tick()
        line = eng.summary_line()
        self.assertIn("fm-resend", line)
        self.assertIn("1 resent", line)


if __name__ == "__main__":
    unittest.main()


class TestPendingTagsIsTheOtherHalfOfTheComparison(unittest.TestCase):
    """pending_tags() exists so a failed cancel can be diagnosed at all.

    On the first two-phone hardware run (2026-10-03) beacons went out
    carrying real tags, the peer received them, and nothing was ever
    cancelled. Only the SUCCESS path logged anything, so "the digest was
    empty" and "the digest did not match what was pending" looked identical
    -- and they want opposite fixes.
    """

    def test_it_reports_the_tag_a_digest_would_have_to_carry(self):
        eng = _engine()
        eng.register(1, [b"a"], "chat one", tag=b"\xaa\xbb")
        self.assertEqual(eng.pending_tags(), {b"\xaa\xbb": "chat one"})

    def test_a_schedule_with_no_tag_is_omitted(self):
        """It can never be cancelled by a digest, so listing it beside tags
        that can would invite exactly the wrong conclusion."""
        eng = _engine()
        eng.register(1, [b"a"], "untaggable")
        eng.register(2, [b"b"], "taggable", tag=b"\x01\x02")
        self.assertEqual(eng.pending_tags(), {b"\x01\x02": "taggable"})

    def test_a_cancelled_schedule_leaves_the_listing(self):
        eng = _engine()
        eng.register(1, [b"a"], "chat one", tag=b"\xaa\xbb")
        self.assertTrue(eng.pending_tags())
        eng.note_heard([b"\xaa\xbb"])
        self.assertEqual(eng.pending_tags(), {})

    def test_it_shows_the_mismatch_that_explains_a_failed_cancel(self):
        """The whole diagnostic: what arrived against what was waiting."""
        eng = _engine()
        eng.register(1, [b"a"], "chat one", tag=b"\xaa\xbb")
        self.assertEqual(eng.note_heard([b"\xcc\xdd"]), [])
        self.assertEqual(eng.pending_tags(), {b"\xaa\xbb": "chat one"})

    def test_it_does_not_mutate_anything(self):
        """Diagnosis only -- note_heard() is the one thing allowed to cancel."""
        eng = _engine()
        eng.register(1, [b"a"], "chat one", tag=b"\xaa\xbb")
        before = eng.snapshot()
        eng.pending_tags()
        eng.pending_tags()
        self.assertEqual(eng.snapshot(), before)
