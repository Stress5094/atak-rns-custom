"""Desktop tests for peers.py.

This file is the point of extracting the peer table out of rns_bridge.py. Two
of the four bugs the 2026-08-12 three-node bench found were in this table, and
neither could have had a test while it lived in the module that imports RNS and
pyserial -- so both were found on a radio instead. Every one of those failures
is pinned here.

The clock is injected throughout, so ageing is exact rather than slept for.
"""

import unittest

import peers
import relay

A = "a1b2c3"
B = "d4e5f6"
C = "aaaa08"

CONNECTED_S = 180.0
FORGET_S = 600.0


class TestTargetedCoverage(unittest.TestCase):
    """The table's half of relay.covers_target: it supplies the sender's
    advertised digest and our own direct neighbours."""

    def table(self):
        return peers.PeerTable(connected_s=180, forget_s=600,
                               clock=lambda: 1000.0)

    def test_no_opinion_about_a_frame_addressed_to_its_own_sender(self):
        t = self.table()
        self.assertIsNone(t.covers_target("0a0a0a", "0a0a0a"))

    def test_no_opinion_without_a_target(self):
        self.assertIsNone(self.table().covers_target("0a0a0a", None))

    def test_relays_toward_a_neighbour_the_sender_cannot_reach(self):
        t = self.table()
        t.note_frame("0a0a0a", direct=True)          # the sender
        t.note_frame("0c0c0c", direct=True)          # the target, ours alone
        t.note_beacon("0a0a0a", seq=1,
                      digest=relay.digest_build(["0b0b0b"]))
        self.assertFalse(t.covers_target("0a0a0a", "0c0c0c"))

    def test_stays_quiet_when_the_sender_already_reaches_the_target(self):
        t = self.table()
        t.note_frame("0a0a0a", direct=True)
        t.note_frame("0c0c0c", direct=True)
        t.note_beacon("0a0a0a", seq=1,
                      digest=relay.digest_build(["0c0c0c"]))
        self.assertTrue(t.covers_target("0a0a0a", "0c0c0c"))


class FakeClock(object):
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def table(clock=None, connected_s=CONNECTED_S, forget_s=FORGET_S):
    clock = clock or FakeClock()
    return peers.PeerTable(connected_s=connected_s, forget_s=forget_s,
                           clock=clock), clock


class TestReachableVersusAdjacent(unittest.TestCase):
    """Bug 1, the one that shut relaying off. A relayed frame proves a node is
    REACHABLE and says nothing about whether this radio can reach it."""

    def test_a_direct_frame_makes_a_node_adjacent(self):
        t, _ = table()
        t.note_frame(A, direct=True)
        self.assertEqual(t.adjacent(), [A])

    def test_a_relayed_frame_does_not(self):
        t, _ = table()
        t.note_frame(A, direct=False, hops_away=2)
        self.assertEqual(t.adjacent(), [])

    def test_a_relayed_frame_still_makes_a_node_reachable(self):
        """It must stay addressable, ackable and visible in the node list --
        the fix is to file the fact correctly, not to discard it."""
        t, _ = table()
        t.note_frame(A, direct=False, hops_away=2)
        self.assertEqual(t.connected(), [A])
        self.assertEqual(t.connected_count(), 1)
        self.assertEqual(t.snapshot()[0]["node_id"], A)

    def test_the_digest_advertises_only_adjacent_nodes(self):
        """The exact failure: advertising a node reached through somebody else
        tells the relay to stop relaying for it, which is what made it
        reachable in the first place."""
        t, _ = table()
        t.note_frame(A, direct=True)
        t.note_frame(B, direct=False, hops_away=2)
        d = t.neighbour_digest()
        self.assertTrue(relay.digest_contains(d, A))
        self.assertFalse(relay.digest_contains(d, B))

    def test_a_direct_sighting_promotes_a_previously_relayed_node(self):
        t, clock = table()
        t.note_frame(A, direct=False, hops_away=2)
        self.assertEqual(t.adjacent(), [])
        clock.advance(1.0)
        t.note_frame(A, direct=True)
        self.assertEqual(t.adjacent(), [A])

    def test_adjacency_expires_but_reachability_can_outlive_it(self):
        """A node that stops being heard first-hand while relayed frames keep
        arriving is exactly the multi-hop case, and must not stay in the
        digest on the strength of the relayed ones."""
        t, clock = table()
        t.note_frame(A, direct=True)
        for _ in range(4):
            clock.advance(CONNECTED_S / 2.0)
            t.note_frame(A, direct=False, hops_away=2)
        self.assertEqual(t.adjacent(), [])
        self.assertEqual(t.connected(), [A])


class TestSignalAndLoss(unittest.TestCase):
    """The two quieter halves of bug 1."""

    def test_a_relayed_frame_never_writes_the_signal(self):
        """The reading belongs to the RELAY. Filing it under the originator
        misreports link quality and scrambles the relay delay ordering."""
        t, _ = table()
        t.note_frame(A, rssi=-40, snr=12.0, direct=True)
        t.note_frame(A, rssi=-100, snr=-15.0, direct=False, hops_away=2)
        p = t.snapshot()[0]
        self.assertEqual(p["rssi"], -40)
        self.assertEqual(p["snr"], 12.0)

    def test_a_missing_signal_leaves_the_last_reading_standing(self):
        t, _ = table()
        t.note_frame(A, rssi=-40, snr=12.0, direct=True)
        t.note_frame(A, rssi=None, snr=None, direct=True)
        p = t.snapshot()[0]
        self.assertEqual(p["rssi"], -40)

    def test_a_node_heard_only_through_a_relay_reports_no_loss(self):
        """The bench's case: a node two hops away reported `10/37 lost` on a
        link it was not listening to. There is no direct link to measure, so
        the honest answer is no measurement rather than total loss."""
        t, _ = table()
        for seq in range(1, 20):
            t.note_frame(A, seq=seq, direct=False, hops_away=2)
        p = t.snapshot()[0]
        self.assertEqual(p["lost"], 0)
        self.assertEqual(p["heard"], 0)

    def test_a_gap_spanning_relayed_copies_is_still_loss_on_the_direct_link(self):
        """The other side of the same rule, and deliberately NOT zero: if
        frames are arriving relayed while their direct copies are not, the
        direct link really is dropping them, and that is worth reporting."""
        t, _ = table()
        t.note_frame(A, seq=1, direct=True)
        for seq in range(2, 20):
            t.note_frame(A, seq=seq, direct=False, hops_away=2)
        t.note_frame(A, seq=20, direct=True)
        self.assertEqual(t.snapshot()[0]["lost"], 18)

    def test_a_gap_between_direct_frames_is_counted(self):
        t, _ = table()
        t.note_frame(A, seq=1, direct=True)
        t.note_frame(A, seq=5, direct=True)
        self.assertEqual(t.snapshot()[0]["lost"], 3)

    def test_an_incredible_gap_is_not_counted(self):
        """A 16-bit sequence restarting at zero would otherwise report tens of
        thousands of losses for one reboot."""
        t, _ = table()
        t.note_frame(A, seq=60000, direct=True)
        t.note_frame(A, seq=3, direct=True)
        self.assertEqual(t.snapshot()[0]["lost"], 0)


class TestLastLink(unittest.TestCase):
    """Which of this node's own radios last carried traffic involving a
    peer -- for LoRa-primary/FM-backup routing to pick where to originate a
    message to them, before anything has failed yet."""

    def test_unseen_peer_has_no_last_link(self):
        t, _ = table()
        self.assertIsNone(t.last_link(A))

    def test_a_frame_with_no_link_given_leaves_it_unset(self):
        t, _ = table()
        t.note_frame(A)
        self.assertIsNone(t.last_link(A))

    def test_records_the_link_a_frame_arrived_on(self):
        t, _ = table()
        t.note_frame(A, link="lora")
        self.assertEqual(t.last_link(A), "lora")

    def test_the_latest_frame_wins(self):
        t, _ = table()
        t.note_frame(A, link="lora")
        t.note_frame(A, link="fm")
        self.assertEqual(t.last_link(A), "fm")

    def test_a_relayed_frame_still_updates_it(self):
        """Unlike rssi/snr/loss, this is a REACHABLE fact, not an ADJACENT
        one: a relayed frame really did arrive over one of this node's own
        local radios, and that is real evidence for routing, even though it
        says nothing about the ORIGINATOR's own link quality."""
        t, _ = table()
        t.note_frame(A, link="lora", direct=False, hops_away=2)
        self.assertEqual(t.last_link(A), "lora")

    def test_appears_in_the_snapshot(self):
        t, _ = table()
        t.note_frame(A, link="fm")
        self.assertEqual(t.snapshot()[0]["last_link"], "fm")

    def test_a_frame_with_no_link_given_does_not_clear_a_known_one(self):
        """Same conditional-store shape as rssi/snr: an interface that did
        not report which link (or a caller that never passes one) must not
        blank out a link learned from an earlier frame."""
        t, _ = table()
        t.note_frame(A, link="lora")
        t.note_frame(A)
        self.assertEqual(t.last_link(A), "lora")

    def test_heard_on_is_false_for_an_unseen_peer(self):
        t, _ = table()
        self.assertFalse(t.heard_on(A, "lora"))

    def test_heard_on_is_false_for_a_link_never_seen(self):
        t, _ = table()
        t.note_frame(A, link="fm")
        self.assertFalse(t.heard_on(A, "lora"))

    def test_heard_on_is_true_once_seen(self):
        t, _ = table()
        t.note_frame(A, link="lora")
        self.assertTrue(t.heard_on(A, "lora"))

    def test_heard_on_stays_true_after_moving_to_another_link(self):
        """The whole point, distinct from last_link(): a peer who moved off
        LoRa still has real LoRa history, which a caller deciding whether to
        even try LoRa first needs to know."""
        t, _ = table()
        t.note_frame(A, link="lora")
        t.note_frame(A, link="fm")
        self.assertEqual(t.last_link(A), "fm")
        self.assertTrue(t.heard_on(A, "lora"))
        self.assertTrue(t.heard_on(A, "fm"))


class TestRestartDetection(unittest.TestCase):
    """The open finding from 2026-08-12: a node that restarts is blind to every
    peer's positions until that peer's keyframe interval comes round, because
    nothing noticed the restart."""

    def test_a_changed_epoch_reports_a_restart(self):
        t, _ = table()
        self.assertFalse(t.note_frame(A, epoch=3).restarted)
        self.assertTrue(t.note_frame(A, epoch=4).restarted)

    def test_a_steady_epoch_reports_nothing(self):
        t, _ = table()
        t.note_frame(A, epoch=3)
        for _ in range(10):
            self.assertFalse(t.note_frame(A, epoch=3).restarted)

    def test_a_first_sighting_is_new_rather_than_restarted(self):
        """Both would force a keyframe, but calling a first contact a restart
        would put a misleading line in the log every time anyone joins."""
        s = table()[0].note_frame(A, epoch=7)
        self.assertTrue(s.fresh)
        self.assertFalse(s.restarted)

    def test_a_restart_is_detected_through_a_relay(self):
        """The bench's case exactly: the node that rebooted was two hops from
        the one that needed to re-key, so this cannot depend on adjacency."""
        t, _ = table()
        t.note_frame(A, epoch=3, direct=False, hops_away=2)
        self.assertTrue(t.note_frame(A, epoch=4, direct=False,
                                     hops_away=2).restarted)

    def test_a_restart_does_not_count_the_sequence_reset_as_loss(self):
        t, _ = table()
        t.note_frame(A, seq=900, epoch=3, direct=True)
        t.note_frame(A, seq=0, epoch=4, direct=True)
        self.assertEqual(t.snapshot()[0]["lost"], 0)

    def test_frames_without_an_epoch_never_report_a_restart(self):
        t, _ = table()
        t.note_frame(A, epoch=None)
        self.assertFalse(t.note_frame(A, epoch=None).restarted)


class TestCoverage(unittest.TestCase):

    def test_a_sender_that_already_reaches_everyone_earns_no_relay(self):
        t, _ = table()
        t.note_frame(A, direct=True)
        t.note_frame(B, direct=True)
        t.note_beacon(A, digest=relay.digest_build([B]))
        self.assertTrue(t.sender_covers_our_neighbours(A))

    def test_a_sender_missing_one_of_our_neighbours_earns_a_relay(self):
        t, _ = table()
        t.note_frame(A, direct=True)
        t.note_frame(B, direct=True)
        t.note_beacon(A, digest=relay.digest_build([]))
        self.assertFalse(t.sender_covers_our_neighbours(A))

    def test_only_adjacent_nodes_count_as_coverage_to_add(self):
        """A node we only reach through a relay is not coverage we can offer,
        so its absence from the sender's digest must not earn a relay."""
        t, _ = table()
        t.note_frame(A, direct=True)
        t.note_frame(B, direct=False, hops_away=2)
        t.note_beacon(A, digest=relay.digest_build([]))
        self.assertTrue(t.sender_covers_our_neighbours(A))

    def test_a_sender_we_have_no_beacon_from_is_left_alone(self):
        t, _ = table()
        t.note_frame(A, direct=True)
        t.note_frame(B, direct=True)
        self.assertTrue(t.sender_covers_our_neighbours(A))


class TestDistance(unittest.TestCase):

    def test_an_unknown_node_reads_as_adjacent(self):
        """Which is the timing everything had before relaying existed."""
        self.assertEqual(table()[0].hops_away("nobody"), 1)

    def test_a_relayed_node_reports_its_distance(self):
        t, _ = table()
        t.note_frame(A, direct=False, hops_away=3)
        self.assertEqual(t.hops_away(A), 3)

    def test_a_live_direct_link_beats_a_remembered_distance(self):
        """`hops_away` is only the last thing observed, and may describe a
        relayed copy that arrived while the direct one was lost to noise."""
        t, _ = table()
        t.note_frame(A, direct=False, hops_away=3)
        t.note_frame(A, direct=True, hops_away=1)
        self.assertEqual(t.hops_away(A), 1)

    def test_distance_survives_the_direct_link_going_stale(self):
        t, clock = table()
        t.note_frame(A, direct=True, hops_away=1)
        t.note_frame(A, direct=False, hops_away=2)
        clock.advance(CONNECTED_S + 1)
        self.assertEqual(t.hops_away(A), 2)

    def test_a_nonsense_distance_falls_back_to_adjacent(self):
        t, _ = table()
        t.note_frame(A, direct=False, hops_away="not a number")
        self.assertEqual(t.hops_away(A), 1)


class TestBeaconFields(unittest.TestCase):

    def test_a_beacon_records_name_callsign_and_digest(self):
        t, _ = table()
        t.note_frame(A, direct=True)
        t.note_beacon(A, seq=4, name="Pixel 7", callsign="ALPHA",
                      digest=relay.digest_build([B]))
        p = t.snapshot()[0]
        self.assertEqual((p["name"], p["callsign"], p["seq"]),
                         ("Pixel 7", "ALPHA", 4))

    def test_a_beacon_without_a_name_keeps_the_one_already_known(self):
        """An older peer or a padded diagnostic beacon carries neither, and
        forgetting one already learned makes the node list flicker."""
        t, _ = table()
        t.note_beacon(A, name="Pixel 7", callsign="ALPHA")
        t.note_beacon(A, name=None, callsign=None)
        p = t.snapshot()[0]
        self.assertEqual((p["name"], p["callsign"]), ("Pixel 7", "ALPHA"))

    def test_a_digest_is_replaced_wholesale_not_merged(self):
        """It says who they can hear NOW, and a neighbour they have lost is the
        whole reason the answer changed."""
        t, _ = table()
        t.note_beacon(A, digest=relay.digest_build([B, C]))
        t.note_beacon(A, digest=relay.digest_build([B]))
        t.note_frame(C, direct=True)
        self.assertFalse(t.sender_covers_our_neighbours(A))


class TestAgeingAndReporting(unittest.TestCase):

    def test_a_peer_goes_disconnected_but_is_remembered(self):
        t, clock = table()
        t.note_frame(A, direct=True)
        clock.advance(CONNECTED_S + 1)
        p = t.snapshot()[0]
        self.assertFalse(p["connected"])
        self.assertFalse(p["direct"])

    def test_a_long_departed_peer_is_pruned(self):
        t, clock = table()
        t.note_frame(A, direct=True)
        clock.advance(FORGET_S + 1)
        self.assertEqual(t.snapshot(), [])

    def test_the_snapshot_is_freshest_first(self):
        t, clock = table()
        t.note_frame(A, direct=True)
        clock.advance(10.0)
        t.note_frame(B, direct=True)
        self.assertEqual([p["node_id"] for p in t.snapshot()], [B, A])

    def test_a_label_falls_back_to_the_id(self):
        t, _ = table()
        self.assertEqual(t.label(A), A)
        t.note_beacon(A, name="Pixel 7")
        self.assertEqual(t.label(A), "Pixel 7")

    def test_labels_of_nobody_says_so(self):
        self.assertEqual(table()[0].labels([]), "nobody")

    def test_callsign_lookup_returns_every_match(self):
        """More than one is ambiguous and the caller refuses rather than
        guesses -- two nodes answering to one name would misroute an ack."""
        t, _ = table()
        t.note_beacon(A, callsign="ALPHA")
        t.note_beacon(B, callsign="ALPHA")
        self.assertEqual(sorted(t.by_callsign("ALPHA")), sorted([A, B]))

    def test_clear_forgets_everybody(self):
        t, _ = table()
        t.note_frame(A, direct=True)
        t.clear()
        self.assertEqual(len(t), 0)


class TestNeverRaises(unittest.TestCase):
    """This runs on the thread that reads the radio. A bookkeeping failure must
    never be able to interrupt reception."""

    def test_a_broken_clock_does_not_propagate(self):
        def boom():
            raise RuntimeError("clock exploded")
        t = peers.PeerTable(CONNECTED_S, FORGET_S, clock=boom)
        self.assertEqual(t.note_frame(A), peers.Sighting(False, False))

    def test_odd_field_types_are_survivable(self):
        t, _ = table()
        for bad in (None, "x", object(), -1):
            t.note_frame(A, rssi=bad, snr=bad, seq=bad, hops_away=bad,
                         epoch=bad)
        self.assertGreaterEqual(len(t), 1)


class TestSequenceGapCallback(unittest.TestCase):
    """The hook that lets the bridge LOG which numbered frame went missing.
    The table counts losses either way; this is what makes a count readable
    alongside the per-burst verdicts in fm_airlog."""

    def test_a_skipped_sequence_calls_back_with_what_was_missed(self):
        t, clock = table()
        seen = []
        t.on_gap = lambda *a: seen.append(a)
        t.note_frame(A, seq=5)
        clock.advance(24.0)
        t.note_frame(A, seq=8)
        self.assertEqual(len(seen), 1)
        node, previous, seq, missed, since_s, name = seen[0]
        self.assertEqual((node, previous, seq, missed), (A, 5, 8, 2))
        self.assertAlmostEqual(since_s, 24.0)

    def test_consecutive_frames_do_not_call_back(self):
        t, _ = table()
        seen = []
        t.on_gap = lambda *a: seen.append(a)
        for n in (1, 2, 3):
            t.note_frame(A, seq=n)
        self.assertEqual(seen, [])

    def test_a_restart_is_not_a_gap(self):
        t, _ = table()
        seen = []
        t.on_gap = lambda *a: seen.append(a)
        t.note_frame(A, seq=50, epoch=1)
        t.note_frame(A, seq=2, epoch=2)
        self.assertEqual(seen, [])

    def test_the_callback_runs_outside_the_lock_and_may_read_the_table(self):
        t, _ = table()
        t.on_gap = lambda *a: t.snapshot()      # would deadlock if held
        t.note_frame(A, seq=1)
        t.note_frame(A, seq=4)

    def test_a_failing_callback_never_interrupts_reception(self):
        t, _ = table()

        def boom(*a):
            raise RuntimeError("logger broke")
        t.on_gap = boom
        t.note_frame(A, seq=1)
        sighting = t.note_frame(A, seq=4)
        self.assertFalse(sighting.restarted)
        self.assertEqual(t.snapshot()[0]["lost"], 2)

    def test_no_callback_set_is_the_old_behaviour(self):
        t, _ = table()
        t.note_frame(A, seq=1)
        t.note_frame(A, seq=4)
        self.assertEqual(t.snapshot()[0]["lost"], 2)


def radios(t, node=A):
    """The per-radio rows for one node, keyed by radio kind."""
    for peer in t.snapshot():
        if peer["node_id"] == node:
            return {r["radio"]: r for r in peer["radios"]}
    return {}


class TestPerRadioRecords(unittest.TestCase):
    """The mesh view draws one line per radio a peer is heard on, so each
    radio's facts must stay apart: an FM frame carries no signal reading, and
    the whole-node record would leave a LoRa number standing under an FM line."""

    def test_a_frame_with_no_link_records_no_radio(self):
        t, _ = table()
        t.note_frame(A, rssi=-80)
        self.assertEqual(radios(t), {})

    def test_the_radio_defaults_to_the_link(self):
        t, _ = table()
        t.note_frame(A, link="fm")
        self.assertEqual(set(radios(t)), {"fm"})
        self.assertEqual(radios(t)["fm"]["link"], "fm")

    def test_signal_stays_with_the_radio_that_measured_it(self):
        t, _ = table()
        t.note_frame(A, rssi=-90, snr=4.0, link="lora", radio="rnode")
        t.note_frame(A, link="fm")
        rows = radios(t)
        self.assertEqual(rows["rnode"]["snr"], 4.0)
        self.assertIsNone(rows["fm"]["snr"])
        self.assertIsNone(rows["fm"]["rssi"])

    def test_the_kind_is_stamped_at_arrival_not_looked_up_later(self):
        # A board swap in the LoRa slot: what the RNode heard stays the
        # RNode's, and the Meshtastic board starts its own record.
        t, clock = table()
        t.note_frame(A, snr=5.0, link="lora", radio="rnode")
        clock.advance(30)
        t.note_frame(A, snr=-2.0, link="lora", radio="meshtastic")
        rows = radios(t)
        self.assertEqual(rows["rnode"]["snr"], 5.0)
        self.assertEqual(rows["rnode"]["age_s"], 30.0)
        self.assertEqual(rows["meshtastic"]["snr"], -2.0)
        self.assertEqual(rows["meshtastic"]["link"], "lora")

    def test_each_radio_ages_on_its_own(self):
        t, clock = table()
        t.note_frame(A, link="fm")
        clock.advance(100)
        t.note_frame(A, link="lora", radio="rnode")
        rows = radios(t)
        self.assertEqual(rows["fm"]["age_s"], 100.0)
        self.assertEqual(rows["rnode"]["age_s"], 0.0)
        self.assertEqual(
            [r["radio"] for r in t.snapshot()[0]["radios"]], ["rnode", "fm"])

    def test_a_relayed_frame_refreshes_the_radio_but_measures_nothing(self):
        t, clock = table()
        t.note_frame(A, rssi=-70, snr=8.0, seq=1, link="lora", radio="rnode")
        clock.advance(50)
        t.note_frame(A, rssi=-120, snr=-15.0, seq=9, direct=False,
                     link="lora", radio="rnode")
        row = radios(t)["rnode"]
        self.assertEqual(row["age_s"], 0.0)
        self.assertEqual(row["direct_age_s"], 50.0)
        self.assertEqual(row["snr"], 8.0)
        self.assertEqual(row["heard"], 1)
        self.assertEqual(row["lost"], 0)

    def test_a_radio_that_only_ever_carried_relays_has_no_direct_facts(self):
        t, _ = table()
        t.note_frame(A, seq=1, direct=False, link="lora", radio="rnode")
        row = radios(t)["rnode"]
        self.assertIsNone(row["direct_age_s"])
        self.assertIsNone(row["lost"])

    def test_loss_is_counted_per_radio_for_a_single_radio_peer(self):
        t, _ = table()
        t.note_frame(A, seq=1, link="fm")
        t.note_frame(A, seq=4, link="fm")
        self.assertEqual(radios(t)["fm"]["lost"], 2)
        self.assertEqual(radios(t)["fm"]["heard"], 2)

    def test_loss_is_unknown_once_a_peer_uses_two_radios(self):
        # One sequence counter across the sender's radios: frames 2 and 3 went
        # over LoRa, and counting them as FM loss would invent a bad FM link.
        t, _ = table()
        t.note_frame(A, seq=1, link="fm")
        t.note_frame(A, seq=2, link="lora", radio="rnode")
        t.note_frame(A, seq=3, link="lora", radio="rnode")
        t.note_frame(A, seq=4, link="fm")
        rows = radios(t)
        self.assertIsNone(rows["fm"]["lost"])
        self.assertIsNone(rows["rnode"]["lost"])
        # The whole-node count is untouched by any of this.
        self.assertEqual(t.snapshot()[0]["lost"], 0)

    def test_a_restart_seen_first_on_another_radio_is_not_loss_here(self):
        # The restart flag fires once, on whichever radio hears the first frame
        # of the new epoch. This radio's last sequence predates it.
        t, _ = table()
        # A forward jump small enough to be credible loss, so only the epoch
        # can tell it apart -- a backward one is thrown out by the gap cap.
        t.note_frame(A, seq=1, epoch=1, link="fm")
        t.note_frame(A, seq=0, epoch=2, link="lora", radio="rnode")
        t.note_frame(A, seq=4, epoch=2, link="fm")
        record = t._peers[A]["radios"]["fm"]
        self.assertEqual(record.get("lost", 0), 0)

    def test_pruning_takes_the_radio_rows_with_it(self):
        t, clock = table()
        t.note_frame(A, link="fm")
        clock.advance(FORGET_S + 1)
        self.assertEqual(t.snapshot(), [])


class TestProbablePairs(unittest.TestCase):
    """The mesh view's remote lines: who a peer's digest says it hears."""

    def test_a_digest_names_the_nodes_it_contains(self):
        t, _ = table()
        t.note_frame(A)
        t.note_frame(B)
        t.note_frame(C)
        t.note_beacon(A, digest=relay.digest_build([B]))
        self.assertEqual(t.probable_pairs(), [(A, B)])

    def test_a_peer_does_not_pair_with_itself(self):
        t, _ = table()
        t.note_frame(A)
        t.note_beacon(A, digest=relay.digest_build([A]))
        self.assertEqual(t.probable_pairs(), [])

    def test_no_digest_no_pairs(self):
        t, _ = table()
        t.note_frame(A)
        t.note_frame(B)
        self.assertEqual(t.probable_pairs(), [])

    def test_a_disconnected_peers_digest_is_ignored(self):
        t, clock = table()
        t.note_frame(A)
        t.note_frame(B)
        t.note_beacon(A, digest=relay.digest_build([B]))
        clock.advance(CONNECTED_S + 1)
        t.note_frame(B)
        self.assertEqual(t.probable_pairs(), [])


if __name__ == "__main__":
    unittest.main()
