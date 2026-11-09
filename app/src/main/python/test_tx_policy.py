"""
Desktop tests for the milestone 3 send policy.

    python3 -m unittest discover -s app/src/main/python -p 'test_*.py' -v

The clock is injected throughout, so rate-limit behaviour is tested by moving
time rather than by sleeping.
"""

import unittest

import cot_codec as codec
import tx_policy


def pli(uid="ANDROID-1", lat=35.0, lon=-79.0, callsign="BRAVO01",
        team="Cyan", role="Team Member", cot_type="a-f-G-U-C",
        time_s="2026-07-31T10:00:00Z"):
    # <takv> is what makes this a self-SA rather than a marker that happens to
    # be shaped like one, and every real ATAK position report carries it. It
    # was missing here until 2026-08-15, which cost nothing while the codec
    # classified any a-* event with coordinates as a position -- and would
    # have kept costing nothing while these tests quietly stopped describing
    # the traffic they are named for. See TestPositionClassification.
    return (
        '<event version="2.0" uid="%s" type="%s" time="%s" stale="2026-07-31T10:02:00Z" how="m-g">'
        '<point lat="%.7f" lon="%.7f" hae="100.0" ce="9.5" le="9999999.0"/>'
        '<detail>'
        '<takv os="29" version="5.1.0.0" device="GOOGLE PIXEL 6" platform="ATAK-CIV"/>'
        '<contact endpoint="*:-1:stcp" callsign="%s"/>'
        '<__group role="%s" name="%s"/></detail></event>'
        % (uid, cot_type, time_s, lat, lon, callsign, role, team)
    ).encode("utf-8")


def marker(uid="0ef9c3a9-bf54-46e5-9655-29956a0d80ba", lat=35.0, lon=-79.0,
           cot_type="a-u-G", time_s="2026-07-31T10:00:00Z"):
    """A user-placed marker: an a-* event WITH coordinates and WITHOUT <takv>.

    The shape that broke on the 2026-08-15 range test -- indistinguishable
    from a self-SA by type prefix alone, and nothing like one in meaning.
    """
    return (
        '<event version="2.0" uid="%s" type="%s" time="%s" stale="2026-07-31T11:00:00Z" how="h-e">'
        '<point lat="%.7f" lon="%.7f" hae="381.0" ce="9.5" le="9999999.0"/>'
        '<detail><contact callsign="Marker 1"/><remarks>observed movement</remarks>'
        '</detail></event>'
        % (uid, cot_type, time_s, lat, lon)
    ).encode("utf-8")


CHAT = (
    b'<event version="2.0" uid="GeoChat.X.1" type="b-t-f" '
    b'time="2026-07-31T10:00:00Z" stale="2026-07-31T10:01:00Z" how="h-g-i-g-o">'
    b'<point lat="35.0" lon="-79.0" hae="1.0" ce="1.0" le="1.0"/>'
    b'<detail><remarks>moving now</remarks></detail></event>'
)

KEEPALIVE = (
    b'<event version="2.0" uid="rns-bridge-ping" type="t-x-c-t" '
    b'time="2026-07-31T10:00:00Z" stale="2026-07-31T10:00:20Z" how="m-g">'
    b'<point lat="0.0" lon="0.0" hae="0.0" ce="9999999.0" le="9999999.0"/>'
    b'</event>'
)


def atak_ping(uid="ANDROID-0123456789abcdef-ping"):
    """ATAK's own connection keepalive, as captured on the 2026-08-12 bench.

    Note the uid: nothing about it resembles the bridge's "rns-bridge-ping",
    which is exactly why matching on uid alone let these onto the radio.
    """
    return (
        '<event version="2.0" uid="%s" type="t-x-c-t" how="m-g" '
        'time="2026-08-12T22:58:25.000Z" start="2026-08-12T22:58:25.000Z" '
        'stale="2026-08-12T22:59:25.000Z">'
        '<point lat="0.0" lon="0.0" hae="0.0" ce="9999999.0" le="9999999.0"/>'
        '<detail/></event>' % uid
    ).encode("utf-8")


ATAK_PING = atak_ping()


class Clock(object):
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def make(clock=None, **kw):
    return tx_policy.TxPolicy(clock=clock or Clock(), **kw)


def walker(clock):
    """A policy with every milestone 5 interval pinned, so these tests keep
    testing the rule rather than tracking whatever the defaults become."""
    return make(clock, min_distance_m=25.0, moving_interval_s=60.0,
                settle_s=90.0, heartbeat_s=60.0)


class TestSummarize(unittest.TestCase):

    def test_position_event(self):
        s = codec.summarize(pli())
        self.assertEqual(s.uid, "ANDROID-1")
        self.assertEqual(s.callsign, "BRAVO01")
        self.assertEqual(s.team, "Cyan")
        self.assertEqual(s.role, "Team Member")
        self.assertAlmostEqual(s.lat, 35.0)
        self.assertTrue(s.is_position)

    def test_chat_is_not_a_position(self):
        self.assertFalse(codec.summarize(CHAT).is_position)

    def test_malformed_raises_encode_error(self):
        self.assertRaises(codec.CotEncodeError, codec.summarize, b"not xml")


class TestHaversine(unittest.TestCase):

    def test_known_distances(self):
        # ~111.32 km per degree of latitude anywhere.
        self.assertAlmostEqual(
            tx_policy.haversine_m(0.0, 0.0, 1.0, 0.0), 111195, delta=200)
        self.assertEqual(tx_policy.haversine_m(35.0, -79.0, 35.0, -79.0), 0.0)
        # A degree of longitude shrinks with latitude -- the reason this is
        # haversine and not a flat-earth approximation.
        equator = tx_policy.haversine_m(0.0, 0.0, 0.0, 1.0)
        high = tx_policy.haversine_m(60.0, 0.0, 60.0, 1.0)
        self.assertAlmostEqual(high / equator, 0.5, delta=0.01)

    def test_small_distance(self):
        # ~0.0001 degrees latitude is ~11 m.
        d = tx_policy.haversine_m(35.0, -79.0, 35.0001, -79.0)
        self.assertAlmostEqual(d, 11.1, delta=0.5)


class TestJitterWindow(unittest.TestCase):
    """Sizes both staggered lanes in rns_bridge -- acks and movement onsets."""

    def test_one_slot_per_peer(self):
        self.assertAlmostEqual(tx_policy.jitter_window(4, 0.25, 5.0), 1.0)
        self.assertAlmostEqual(tx_policy.jitter_window(16, 0.25, 5.0), 4.0)

    def test_capped_so_delay_stays_bounded_as_the_mesh_grows(self):
        self.assertAlmostEqual(tx_policy.jitter_window(100, 0.25, 5.0), 5.0)
        self.assertAlmostEqual(tx_policy.jitter_window(1000, 0.25, 5.0), 5.0)

    def test_never_collapses_to_zero(self):
        """A window of 0 would put every node back in lockstep, which is the
        one outcome this exists to prevent."""
        for peers in (0, -1):
            self.assertGreater(tx_policy.jitter_window(peers, 0.25, 5.0), 0.0)

    def test_a_lone_peer_gets_a_small_window_not_the_cap(self):
        """The measured harm on the 2026-07-31 bench was a FIXED window: with
        one peer there is no collision to avoid, and the jitter pushed an ack
        past the sender's retry timer."""
        self.assertLess(tx_policy.jitter_window(1, 0.15, 1.0), 1.0)


class TestPositionThrottling(unittest.TestCase):

    def test_first_report_always_sends(self):
        p = make()
        d = p.decide(codec.summarize(pli()))
        self.assertTrue(d.send)
        self.assertIn("new", d.reason)

    def test_stationary_repeat_is_suppressed(self):
        clock = Clock()
        p = make(clock)
        self.assertTrue(p.decide(codec.summarize(pli())).send)
        clock.advance(2.0)
        d = p.decide(codec.summarize(pli()))
        self.assertFalse(d.send)
        self.assertIn("rate limited", d.reason)

    def test_heartbeat_sends_when_stationary(self):
        clock = Clock()
        p = make(clock, heartbeat_s=30.0)
        p.decide(codec.summarize(pli()))
        clock.advance(29.0)
        self.assertFalse(p.decide(codec.summarize(pli())).send)
        clock.advance(2.0)
        d = p.decide(codec.summarize(pli()))
        self.assertTrue(d.send)
        self.assertEqual(d.reason, "heartbeat")

    def test_movement_sends_immediately(self):
        clock = Clock()
        p = make(clock, min_distance_m=25.0)
        p.decide(codec.summarize(pli(lat=35.0)))
        clock.advance(1.0)
        # ~0.0005 deg latitude is ~55 m -- well over the threshold.
        d = p.decide(codec.summarize(pli(lat=35.0005)))
        self.assertTrue(d.send)
        self.assertIn("started moving", d.reason)

    def test_gps_jitter_does_not_retrigger(self):
        clock = Clock()
        p = make(clock, min_distance_m=25.0)
        p.decide(codec.summarize(pli(lat=35.0)))
        clock.advance(1.0)
        # ~0.0001 deg is ~11 m: realistic GPS noise, under the threshold.
        self.assertFalse(p.decide(codec.summarize(pli(lat=35.0001))).send)

    def test_distance_is_measured_from_last_SENT_not_last_seen(self):
        """Otherwise a unit creeping just under the threshold every time would
        never trigger, no matter how far it actually travelled."""
        clock = Clock()
        p = make(clock, min_distance_m=25.0, heartbeat_s=1e9)
        p.decide(codec.summarize(pli(lat=35.0)))
        sent = 0
        for i in range(1, 8):  # 7 steps of ~11 m each
            clock.advance(1.0)
            if p.decide(codec.summarize(pli(lat=35.0 + 0.0001 * i))).send:
                sent += 1
        self.assertGreater(sent, 0, "creeping movement never triggered a send")

    def test_callsign_change_sends(self):
        clock = Clock()
        p = make(clock)
        p.decide(codec.summarize(pli(callsign="BRAVO01")))
        clock.advance(1.0)
        d = p.decide(codec.summarize(pli(callsign="ALPHA02")))
        self.assertTrue(d.send)
        self.assertEqual(d.reason, "attributes changed")

    def test_team_and_role_changes_send(self):
        for kw in ({"team": "Red"}, {"role": "Medic"}, {"cot_type": "a-h-G-U-C"}):
            clock = Clock()
            p = make(clock)
            p.decide(codec.summarize(pli()))
            clock.advance(1.0)
            self.assertTrue(p.decide(codec.summarize(pli(**kw))).send,
                            "change of %s should send" % list(kw)[0])

    def test_separate_uids_tracked_independently(self):
        clock = Clock()
        p = make(clock)
        self.assertTrue(p.decide(codec.summarize(pli(uid="A"))).send)
        self.assertTrue(p.decide(codec.summarize(pli(uid="B"))).send)
        clock.advance(1.0)
        self.assertFalse(p.decide(codec.summarize(pli(uid="A"))).send)
        self.assertFalse(p.decide(codec.summarize(pli(uid="B"))).send)


class TestNullIsland(unittest.TestCase):
    """0,0 is what ATAK emits while GPS reacquires, not a place anyone is.

    Seen on the 2026-08-04 bench: both phones reported a ~12,000 km "move" out
    to 0,0 and another back, each qualifying as a movement onset -- the one
    category that is never rate limited -- and putting the marker in the
    Atlantic on every other device in between.
    """

    def test_a_null_fix_is_not_transmitted(self):
        p = make()
        self.assertFalse(p.decide(codec.summarize(pli(lat=0.0, lon=0.0))).send)

    def test_the_return_to_a_real_fix_is_not_an_onset(self):
        # The half that is easy to miss. Dropping the outbound 0,0 is only
        # useful if it is also not remembered -- otherwise coming back looks
        # like a fresh 12,000 km onset and one of the two spurious sends
        # survives.
        clock = Clock()
        p = walker(clock)
        self.assertTrue(p.decide(codec.summarize(pli(lat=35.0, lon=-79.0))).send)
        clock.advance(5.0)
        self.assertFalse(p.decide(codec.summarize(pli(lat=0.0, lon=0.0))).send)
        clock.advance(5.0)
        decision = p.decide(codec.summarize(pli(lat=35.0, lon=-79.0)))
        self.assertFalse(decision.send, "a return to the same spot is not news")
        self.assertNotEqual(decision.kind, tx_policy.SEND_ONSET)

    def test_a_whole_gps_dropout_costs_nothing(self):
        clock = Clock()
        p = walker(clock)
        p.decide(codec.summarize(pli(lat=35.0, lon=-79.0)))
        before = p.stats().get("sent", 0)
        for _ in range(10):
            clock.advance(5.0)
            p.decide(codec.summarize(pli(lat=0.0, lon=0.0)))
        clock.advance(5.0)
        p.decide(codec.summarize(pli(lat=35.0, lon=-79.0)))
        self.assertEqual(p.stats().get("sent", 0), before)

    def test_real_movement_after_a_dropout_still_reports(self):
        # Must not have broken the actual job: a node that genuinely moved
        # while GPS was out reports as soon as it has a fix again.
        clock = Clock()
        p = walker(clock)
        p.decide(codec.summarize(pli(lat=35.0, lon=-79.0)))
        clock.advance(5.0)
        p.decide(codec.summarize(pli(lat=0.0, lon=0.0)))
        clock.advance(5.0)
        self.assertTrue(p.decide(codec.summarize(pli(lat=35.01, lon=-79.0))).send)

    def test_nearby_real_positions_are_unaffected(self):
        # The tolerance is centimetres, not a region: it exists to catch the
        # exact zeros ATAK emits.
        self.assertFalse(tx_policy.is_null_island(0.001, 0.001))
        self.assertFalse(tx_policy.is_null_island(0.0, -79.0))
        self.assertFalse(tx_policy.is_null_island(35.0, 0.0))
        self.assertTrue(tx_policy.is_null_island(0.0, 0.0))

    def test_missing_coordinates_are_not_mistaken_for_null_island(self):
        self.assertFalse(tx_policy.is_null_island(None, None))

    def test_the_drop_is_counted_and_named(self):
        p = make()
        p.decide(codec.summarize(pli(lat=0.0, lon=0.0)))
        self.assertIn("nofix=1", p.summary_line())

    def test_self_position_pending_starts_false(self):
        # Nothing has happened yet -- a quiet policy is not a stuck one.
        self.assertFalse(make().self_position_pending())

    def test_self_position_pending_after_a_cold_gps_race(self):
        # The exact bench scenario this flag exists for: ATAK's connection
        # comes up before GPS has locked, so the very first (and, absent a
        # reconnect, only) self-SA this policy ever sees is null island.
        p = make()
        p.decide(codec.summarize(pli(lat=0.0, lon=0.0)))
        self.assertTrue(p.self_position_pending())

    def test_self_position_pending_clears_once_a_real_fix_sends(self):
        p = make()
        p.decide(codec.summarize(pli(lat=0.0, lon=0.0)))
        p.decide(codec.summarize(pli(lat=35.0, lon=-79.0)))
        self.assertFalse(p.self_position_pending())

    def test_self_position_pending_stays_false_for_an_ordinary_dropout(self):
        # A node that already had a real fix before GPS briefly went to 0,0
        # is not stuck -- test_real_movement_after_a_dropout_still_reports
        # already shows it recovers on its own, so this must not also flag
        # it for a forced reconnect it does not need.
        clock = Clock()
        p = walker(clock)
        p.decide(codec.summarize(pli(lat=35.0, lon=-79.0)))
        clock.advance(5.0)
        p.decide(codec.summarize(pli(lat=0.0, lon=0.0)))
        self.assertFalse(p.self_position_pending())


class TestNeverThrottled(unittest.TestCase):
    """One-shot, user-initiated traffic has no later event to supersede it, so
    dropping one loses information permanently."""

    def test_chat_always_sends(self):
        clock = Clock()
        p = make(clock)
        for _ in range(5):
            d = p.decide(codec.summarize(CHAT))
            self.assertTrue(d.send)
            self.assertEqual(d.reason, "not a position report")

    def test_non_position_types_pass(self):
        for cot_type in ("b-t-f", "b-m-p-w-GOTO", "u-d-f", "b-a-o-tbl"):
            p = make()
            body = pli(cot_type=cot_type)
            for _ in range(3):
                self.assertTrue(p.decide(codec.summarize(body)).send,
                                "%s must not be throttled" % cot_type)

    def test_markers_are_never_throttled(self):
        """A marker is one-shot: nothing supersedes it, so a dropped one is
        gone. Dropping several pins in quick succession must send every one.

        Before 2026-08-15 this failed. The range test placed six markers and
        the log shows one refused outright as "rate limited (8s ago,
        moved 0m)" -- the movement throttle, applied to a pin on a map.
        """
        clock = Clock()
        p = make(clock)
        for i in range(4):
            d = p.decide(codec.summarize(marker(uid="marker-%d" % i)))
            self.assertTrue(d.send, "marker %d was throttled" % i)
            clock.advance(2.0)

    def test_re_placing_the_same_marker_is_not_throttled(self):
        """ATAK re-emits a marker when it is edited or moved, under the same
        uid. That is a new user action, not a periodic refresh of an old one.

        The range test saw exactly this suppressed 8 seconds after the first
        send, which is how an edit silently fails to reach anybody.
        """
        clock = Clock()
        p = make(clock)
        self.assertTrue(p.decide(codec.summarize(marker())).send)
        clock.advance(8.0)
        d = p.decide(codec.summarize(marker()))
        self.assertTrue(d.send, d.reason)

    def test_a_marker_never_becomes_a_heartbeat(self):
        """Positions get re-sent on the heartbeat timer so a stationary node
        does not vanish. A marker has nothing to keep alive, and the range
        test logged one going out again as reason "heartbeat" -- airtime
        spent restating something the receiver already had."""
        clock = Clock()
        p = make(clock, heartbeat_s=30.0)
        p.decide(codec.summarize(marker()))
        clock.advance(31.0)
        self.assertNotEqual(p.decide(codec.summarize(marker())).reason,
                            "heartbeat")

    def test_markers_do_not_displace_position_tracking(self):
        """The two must not share the throttle's per-uid state: a marker
        passing through cannot be allowed to reset or occupy the slot the
        sender's own position report is tracked in."""
        clock = Clock()
        p = make(clock)
        self.assertTrue(p.decide(codec.summarize(pli())).send)
        self.assertTrue(p.decide(codec.summarize(marker())).send)
        clock.advance(2.0)
        # The position is still inside its rate limit, marker or no marker.
        self.assertFalse(p.decide(codec.summarize(pli())).send)

    def test_chat_receipts_are_dropped_by_default(self):
        # ~60% of a direct chat's airtime, restating what the bridge's own ack
        # already established. Measured on the bench, not assumed.
        for cot_type in ("b-t-f-d", "b-t-f-r"):
            p = make()
            d = p.decide(codec.summarize(pli(cot_type=cot_type)))
            self.assertFalse(d.send, "%s should not earn airtime" % cot_type)
            self.assertEqual(d.reason, "chat receipt")

    def test_chat_receipts_can_be_re_enabled(self):
        # The flag exists because the cost of dropping them is visible in
        # ATAK's chat UI, so it has to be reversible without a code change.
        p = tx_policy.TxPolicy(clock=Clock(), drop_chat_receipts=False)
        self.assertTrue(p.decide(codec.summarize(pli(cot_type="b-t-f-d"))).send)

    def test_the_chat_message_itself_still_sends(self):
        # The dropped set is matched exactly, never as a `b-t-f-*` prefix.
        p = make()
        self.assertTrue(p.decide(codec.summarize(pli(cot_type="b-t-f"))).send)

    def test_unknown_chat_subtype_is_relayed(self):
        # Fail open: a bigger packet is a far cheaper mistake than silently
        # losing an event type nobody anticipated.
        p = make()
        self.assertTrue(p.decide(codec.summarize(pli(cot_type="b-t-f-q"))).send)

    def test_receipts_are_counted_separately(self):
        p = make()
        p.decide(codec.summarize(pli(cot_type="b-t-f-d")))
        p.decide(codec.summarize(pli(cot_type="b-t-f-r")))
        self.assertIn("receipt=2", p.summary_line())

    def test_position_without_coords_is_not_throttled(self):
        # Can't compute distance, so it must not be silently suppressed.
        body = (b'<event version="2.0" uid="X" type="a-f-G-U-C" '
                b'time="2026-07-31T10:00:00Z" how="m-g"><detail/></event>')
        p = make()
        for _ in range(3):
            self.assertTrue(p.decide(codec.summarize(body)).send)


class TestEchoSuppression(unittest.TestCase):

    def test_injected_event_is_not_retransmitted(self):
        clock = Clock()
        p = make(clock)
        remote = codec.summarize(pli(uid="REMOTE-1"))
        p.note_injected(remote)
        d = p.decide(remote)
        self.assertFalse(d.send)
        self.assertIn("echo", d.reason)

    def test_echo_expires(self):
        clock = Clock()
        p = make(clock, echo_ttl_s=120.0)
        remote = codec.summarize(pli(uid="REMOTE-1"))
        p.note_injected(remote)
        clock.advance(200.0)
        self.assertTrue(p.decide(remote).send)
        self.assertEqual(p.stats()["echo_cache"], 0)

    def test_different_time_is_not_an_echo(self):
        """A genuinely newer report for the same uid must still go out."""
        clock = Clock()
        p = make(clock)
        p.note_injected(codec.summarize(pli(uid="R", time_s="2026-07-31T10:00:00Z")))
        later = codec.summarize(pli(uid="R", time_s="2026-07-31T10:05:00Z"))
        self.assertTrue(p.decide(later).send)

    def test_keepalive_never_transmitted(self):
        p = make()
        d = p.decide(codec.summarize(KEEPALIVE))
        self.assertFalse(d.send)
        self.assertIn("keepalive", d.reason)

    def test_ataks_own_ping_never_transmitted(self):
        """The uid rule alone is not enough, and hardware proved it.

        ATAK sends its own keepalive with uid "ANDROID-<device hex>-ping" --
        nothing like the bridge's "rns-bridge-ping" -- roughly every five
        seconds while a server connection is up. Found on the addressed fork's
        two-phone bench, where the channel was quiet enough to notice; this
        milestone had the identical rule and relayed them just the same.

        The uid below is the one observed on that run, not an invented one.
        """
        p = make()
        d = p.decide(codec.summarize(ATAK_PING))
        self.assertFalse(d.send)
        self.assertIn("keepalive", d.reason)

    def test_a_ping_is_dropped_whatever_its_uid(self):
        """Matched on type, so a different device's ping is caught too."""
        p = make()
        for uid in ("ANDROID-deadbeef-ping", "some-other-ping", "x"):
            summary = codec.summarize(atak_ping(uid=uid))
            self.assertFalse(p.decide(summary).send, uid)


class TestBoundsAndRobustness(unittest.TestCase):

    def test_tracked_uids_bounded(self):
        p = make(max_tracked=16)
        for i in range(200):
            p.decide(codec.summarize(pli(uid="UID-%d" % i)))
        self.assertLessEqual(p.stats()["tracked_uids"], 16)

    def test_echo_cache_bounded(self):
        p = make(max_tracked=16)
        for i in range(200):
            p.note_injected(codec.summarize(pli(uid="UID-%d" % i)))
        self.assertLessEqual(p.stats()["echo_cache"], 16)

    def test_decide_never_raises_and_fails_open(self):
        """A policy bug must cost airtime, never take the bridge off the air."""
        p = make()
        # The uid has to be tracked first, or the "new contact" branch returns
        # before any arithmetic happens and nothing can go wrong.
        p.decide(codec.summarize(pli(uid="X")))
        broken = codec.EventSummary(uid="X", type="a-f-G-U-C", time=1,
                                    lat="not-a-number", lon=None,
                                    callsign="BRAVO01", team="Cyan",
                                    role="Team Member", is_position=True,
                                    is_self=False)
        d = p.decide(broken)
        self.assertTrue(d.send)
        self.assertIn("failing open", d.reason)
        self.assertGreaterEqual(p.stats().get("error", 0), 1)

    def test_note_injected_never_raises(self):
        p = make()
        p.note_injected(None)  # must not raise
        self.assertGreaterEqual(p.stats().get("error", 0), 1)

    def test_summary_line_reports_suppression(self):
        clock = Clock()
        p = make(clock)
        p.decide(codec.summarize(pli()))
        for _ in range(4):
            clock.advance(1.0)
            p.decide(codec.summarize(pli()))
        line = p.summary_line()
        self.assertIn("sent=1", line)
        self.assertIn("suppressed=4", line)
        self.assertIn("80%", line)


class TestRealisticLoad(unittest.TestCase):
    """The measurement that motivated this milestone: two nodes at ATAK's
    active report rate produced ~12% channel utilisation."""

    def test_sustained_movement_is_capped(self):
        clock = Clock()
        p = walker(clock)
        p.decide(codec.summarize(pli(lat=35.0)))
        clock.advance(1.0)
        self.assertTrue(p.decide(codec.summarize(pli(lat=35.0005))).send)
        clock.advance(20.0)
        d = p.decide(codec.summarize(pli(lat=35.0010)))
        self.assertFalse(d.send)
        self.assertIn("rate limited", d.reason)
        self.assertIn("moving", d.reason)

    def test_move_after_the_interval_sends_as_moving_not_onset(self):
        clock = Clock()
        p = walker(clock)
        p.decide(codec.summarize(pli(lat=35.0)))
        clock.advance(1.0)
        p.decide(codec.summarize(pli(lat=35.0005)))
        clock.advance(61.0)
        d = p.decide(codec.summarize(pli(lat=35.0010)))
        self.assertTrue(d.send)
        self.assertIn("moving", d.reason)
        self.assertNotIn("started", d.reason)

    def test_stopping_then_moving_again_is_a_fresh_onset(self):
        clock = Clock()
        p = walker(clock)
        p.decide(codec.summarize(pli(lat=35.0)))
        clock.advance(1.0)
        p.decide(codec.summarize(pli(lat=35.0005)))
        # Stand still long enough for the settle window to lapse.
        clock.advance(90.0)
        p.decide(codec.summarize(pli(lat=35.0005)))
        clock.advance(1.0)
        d = p.decide(codec.summarize(pli(lat=35.0010)))
        self.assertTrue(d.send)
        self.assertIn("started moving", d.reason)

    def test_one_unbroken_walk_produces_exactly_one_onset(self):
        """The invariant the settle window exists to protect. An onset is the
        expensive, never-throttled case, so a node that simply keeps walking
        must not manufacture a new one every time the cap releases."""
        clock = Clock()
        p = walker(clock)
        for i in range(150):  # 300 s of continuous walking at ~1.4 m/s
            p.decide(codec.summarize(pli(lat=35.0 + (i * 2.8) / 111195.0)))
            clock.advance(2.0)
        self.assertEqual(p.stats().get("started moving", 0), 1)
        self.assertGreater(p.stats().get("moving", 0), 0)

    def test_stationary_node_collapses_to_heartbeat(self):
        clock = Clock()
        p = make(clock, heartbeat_s=30.0)
        sent = 0
        # 5 minutes of a stationary device reporting every 2 seconds.
        for _ in range(150):
            if p.decide(codec.summarize(pli())).send:
                sent += 1
            clock.advance(2.0)
        # 150 reports collapse to roughly one per heartbeat.
        self.assertLessEqual(sent, 12)
        self.assertGreaterEqual(sent, 9)

    def test_walking_node_settles_to_the_moving_interval(self):
        """Milestone 5's headline change. Milestone 3 sent on every 25 m, which
        at walking pace is a report every ~18 s; this asserts the cap has taken
        over and the rate is set by moving_interval_s instead."""
        clock = Clock()
        p = walker(clock)
        sent = 0
        # Walking pace: ~1.4 m/s, sampled every 2 s => ~2.8 m per step.
        # 150 steps is 300 s of continuous walking.
        for i in range(150):
            lat = 35.0 + (i * 2.8) / 111195.0
            if p.decide(codec.summarize(pli(lat=lat))).send:
                sent += 1
            clock.advance(2.0)
        # 300 s at one per 60 s, plus the new-contact report and the onset.
        # Milestone 3's rule sent 17 over the same walk.
        self.assertGreaterEqual(sent, 5)
        self.assertLessEqual(sent, 8)

    def test_onset_is_never_delayed_by_the_cap(self):
        """The cap must not swallow the report that says movement began --
        that is the update a plain fixed interval gets wrong, and the whole
        reason the onset case exists."""
        clock = Clock()
        p = walker(clock)
        p.decide(codec.summarize(pli(lat=35.0)))
        # Stand still well past the settle window, so the node is idle.
        clock.advance(120.0)
        p.decide(codec.summarize(pli(lat=35.0)))
        # Now step off. This is one second after a send, deep inside the cap.
        clock.advance(1.0)
        d = p.decide(codec.summarize(pli(lat=35.0005)))
        self.assertTrue(d.send)
        self.assertIn("started moving", d.reason)


class TestDiagnosticReasons(unittest.TestCase):
    """The bench produced 'moved 12044711m', which is a GPS acquisition rather
    than travel -- but the log gave no way to tell. Implausible jumps now show
    both fixes."""

    def test_implausible_move_reports_coordinates(self):
        # Seeded away from 0,0 on purpose. This used to start there, but a null
        # fix is now dropped outright (see TestNullIsland), and the diagnostic
        # is still wanted for the implausible jumps that are NOT null island --
        # a stale last-known position, or a fix that changed nature.
        clock = Clock()
        p = make(clock)
        p.decide(codec.summarize(pli(lat=1.0, lon=1.0)))
        clock.advance(1.0)
        d = p.decide(codec.summarize(pli(lat=37.0, lon=-122.0)))
        self.assertTrue(d.send)
        self.assertIn("km", d.reason)
        self.assertIn("GPS acquisition?", d.reason)
        self.assertIn("1.00000,1.00000", d.reason)
        self.assertIn("37.00000,-122.00000", d.reason)

    def test_ordinary_move_stays_terse(self):
        clock = Clock()
        p = make(clock)
        p.decide(codec.summarize(pli(lat=35.0)))
        clock.advance(1.0)
        d = p.decide(codec.summarize(pli(lat=35.0005)))
        self.assertIn("56m", d.reason)
        self.assertNotIn("GPS acquisition", d.reason)
        self.assertNotIn("km", d.reason)


if __name__ == "__main__":
    unittest.main()
