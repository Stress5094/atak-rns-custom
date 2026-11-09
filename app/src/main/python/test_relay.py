"""Milestone 6 step 3: the relay decision.

The governing fact these tests pin is that even ONE relay per frame does not
fit the channel (11.0% duty at 16 nodes against a 10% ceiling), so the default
has to be no relay and each one has to earn its airtime.
"""

import unittest

import relay


class TestTargetedCoverage(unittest.TestCase):
    """A frame with ONE destination gets a narrower question than a broadcast.
    An ack is the only such traffic here, and it already names the node it has
    to reach -- so this is source routing's benefit without a path on the wire.
    """

    A, B, C = "0a0a0a", "0b0b0b", "0c0c0c"

    def test_no_relay_when_the_sender_already_reaches_the_target(self):
        digest = relay.digest_build([self.A, self.B])
        self.assertTrue(relay.covers_target(digest, self.A, {self.A, self.C}))

    def test_relay_when_we_reach_the_target_and_the_sender_does_not(self):
        digest = relay.digest_build([self.B])
        self.assertFalse(relay.covers_target(digest, self.A, {self.A, self.C}))

    def test_no_opinion_about_a_stranger(self):
        """The middle of a three-hop path has heard of neither end, and must
        defer to the broadcast test rather than refuse and strand the ack."""
        digest = relay.digest_build([self.B])
        self.assertIsNone(relay.covers_target(digest, self.A, {self.C}))

    def test_no_opinion_without_a_target(self):
        """Which is every frame that is not an ack."""
        self.assertIsNone(
            relay.covers_target(relay.digest_build([self.B]), None, {self.A}))

    def test_a_missing_digest_does_not_read_as_reaching_the_target(self):
        """digest_contains() must not let absence mean membership -- otherwise
        a peer whose beacon has not arrived silences every ack toward it."""
        self.assertFalse(relay.covers_target(None, self.A, {self.A}))

    def test_it_is_strictly_narrower_than_the_broadcast_test(self):
        """The point of the whole change: a node that WOULD relay a broadcast
        because it reaches somebody new declines an ack aimed elsewhere."""
        digest = relay.digest_build([self.A])          # sender reaches A
        neighbours = [self.A, self.C]                  # we also reach C
        self.assertFalse(relay.covers(digest, neighbours))   # broadcast: relay
        self.assertTrue(relay.covers_target(digest, self.A, set(neighbours)))


class FakeClock(object):
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def airtime(n):
    """Close enough to SF8/125kHz for the bucket arithmetic to be meaningful."""
    return 0.05 + n * 0.0032


NODES = ["a1b2c3", "d4e5f6", "aaaa08", "aaaa09"]

T_COMPACT, T_FRAG, T_BEACON, T_RELIABLE, T_ACK = 0x0, 0x2, 0x3, 0x4, 0x5


def policy(clock=None, budget_s=relay.DEFAULT_BUDGET_S_PER_MIN):
    clock = clock or FakeClock()
    return relay.RelayPolicy(
        airtime_fn=airtime,
        budget=relay.RelayBudget(budget_s, clock=clock),
        clock=clock,
        jitter=lambda: 0.0,          # deterministic delays in tests
    )


class TestDigest(unittest.TestCase):

    def test_a_node_in_the_filter_is_reported_present(self):
        d = relay.digest_build(NODES)
        for node in NODES:
            self.assertTrue(relay.digest_contains(d, node), node)

    def test_an_empty_filter_contains_nobody(self):
        d = relay.digest_build([])
        for node in NODES:
            self.assertFalse(relay.digest_contains(d, node), node)

    def test_a_missing_digest_covers_nobody(self):
        # The safe direction for the pure test: absence must not read as
        # "covers everything", which would silence relaying entirely. The
        # bridge layers its own conservative default on top of this.
        self.assertFalse(relay.digest_contains(None, "a1b2c3"))
        self.assertFalse(relay.digest_contains(b"\x00" * 3, "a1b2c3"))

    def test_the_filter_is_small_enough_to_ride_a_beacon(self):
        self.assertLessEqual(len(relay.digest_build(NODES)), 8)

    def test_false_positives_stay_near_the_predicted_rate(self):
        """A false positive means a MISSED relay, which the next position
        repairs -- so the rate matters but a few points either way does not."""
        d = relay.digest_build(NODES)
        strangers = ["node%04d" % i for i in range(2000)]
        hits = sum(1 for s in strangers if relay.digest_contains(d, s))
        self.assertLess(hits / float(len(strangers)), 0.10)

    def test_bytes_and_str_ids_agree(self):
        d = relay.digest_build([b"a1b2c3"])
        self.assertTrue(relay.digest_contains(d, "a1b2c3"))


class TestCoverage(unittest.TestCase):
    """The mechanism the entire relay budget rests on. One relay per frame is
    11.0% duty at 16 nodes against a 10% ceiling, so the single-cell case has
    to cost nothing."""

    def test_a_squad_standing_together_relays_nothing(self):
        # Everyone hears everyone: the sender's digest already names every
        # node we know, so a relay would reach nobody.
        sender_hears = relay.digest_build(NODES)
        we_hear = [n for n in NODES if n != NODES[0]]
        self.assertTrue(relay.covers(sender_hears, we_hear))

    def test_a_node_the_sender_cannot_hear_earns_a_relay(self):
        sender_hears = relay.digest_build(NODES[:2])
        self.assertFalse(relay.covers(sender_hears, ["a-node-far-away"]))

    def test_knowing_nobody_else_means_nothing_to_add(self):
        self.assertTrue(relay.covers(relay.digest_build(NODES), []))

    def test_a_sender_we_have_never_heard_a_beacon_from_is_left_alone(self):
        """The asymmetry with digest_contains, and it is deliberate: at this
        level the question is whether to spend airtime, and treating a missing
        digest as covering nobody would have every frame that node sends
        relayed by everyone in earshot."""
        self.assertTrue(relay.covers(None, NODES))
        self.assertFalse(relay.digest_contains(None, NODES[0]))

    def test_a_sender_that_hears_nobody_earns_a_relay_for_anyone_we_know(self):
        self.assertFalse(relay.covers(relay.digest_build([]), ["a1b2c3"]))


class TestBudget(unittest.TestCase):

    def test_it_refuses_once_empty(self):
        b = relay.RelayBudget(1.0, clock=FakeClock())
        self.assertTrue(b.charge(0.9))
        self.assertFalse(b.charge(0.2))
        self.assertEqual(b.refused, 1)

    def test_it_refills_continuously_not_on_the_minute(self):
        clock = FakeClock()
        b = relay.RelayBudget(6.0, clock=clock)
        self.assertTrue(b.charge(6.0))
        self.assertFalse(b.charge(0.5))
        clock.advance(10.0)          # a sixth of a minute
        self.assertAlmostEqual(b.tokens(), 1.0, places=6)
        self.assertTrue(b.charge(0.9))

    def test_it_never_overfills(self):
        clock = FakeClock()
        b = relay.RelayBudget(2.0, clock=clock)
        clock.advance(3600.0)
        self.assertAlmostEqual(b.tokens(), 2.0, places=6)


class TestBurstIsSeparateFromRefill(unittest.TestCase):
    """The bucket holds DEFAULT_RELAY_BURST_S but refills at the duty, so a
    burst of relays fits without the long-run share of the channel moving."""

    def test_a_third_relay_now_fits_where_it_was_refused(self):
        # The 2026-10-14 pattern: ~0.7 s relays back to back. 1.5 s held two.
        old = relay.RelayBudget(1.5, clock=FakeClock())
        new = relay.RelayBudget(1.5, clock=FakeClock(), burst_s=2.0)
        for b in (old, new):
            self.assertTrue(b.charge(0.7))
            self.assertTrue(b.charge(0.7))
        self.assertFalse(old.charge(0.55))
        self.assertTrue(new.charge(0.55))

    def test_it_refills_at_the_duty_not_at_the_burst(self):
        clock = FakeClock()
        b = relay.RelayBudget(1.5, clock=clock, burst_s=2.0)
        self.assertTrue(b.charge(2.0))
        clock.advance(20.0)          # a third of a minute of 1.5 s/min
        self.assertAlmostEqual(b.tokens(), 0.5, places=6)

    def test_it_holds_the_burst_and_no_more(self):
        clock = FakeClock()
        b = relay.RelayBudget(1.5, clock=clock, burst_s=2.0)
        clock.advance(3600.0)
        self.assertAlmostEqual(b.tokens(), 2.0, places=6)
        self.assertAlmostEqual(b.refund(5.0), 0.0)

    def test_the_duty_is_still_the_refill(self):
        b = relay.RelayBudget(1.5, clock=FakeClock(), burst_s=2.0)
        self.assertAlmostEqual(b.duty(), relay.DEFAULT_RELAY_DUTY)
        self.assertAlmostEqual(b.relays_per_minute(0.75), 2.0)

    def test_no_burst_means_the_old_behaviour(self):
        b = relay.RelayBudget(1.5, clock=FakeClock())
        self.assertAlmostEqual(b.capacity, 1.5)

    def test_a_default_policy_gets_the_burst(self):
        p = relay.RelayPolicy(airtime_fn=lambda n: 0.5, clock=FakeClock())
        self.assertAlmostEqual(p.budget.capacity, relay.DEFAULT_RELAY_BURST_S)
        self.assertAlmostEqual(p.budget.per_minute_s,
                               relay.DEFAULT_BUDGET_S_PER_MIN)

    def test_link_budgets_pass_the_burst_through(self):
        lb = relay.LinkBudgets(["lora"], clock=FakeClock(), burst_s=2.0)
        self.assertAlmostEqual(lb["lora"].capacity, 2.0)
        self.assertAlmostEqual(lb["lora"].per_minute_s, 1.5)

    def test_an_empty_burst_is_refused(self):
        for bad in (0, -1.0):
            with self.assertRaises(ValueError):
                relay.RelayBudget(1.5, burst_s=bad)


class TestRelayDecision(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()
        self.p = policy(self.clock)

    def decide(self, **kw):
        args = dict(hops=2, kind=T_COMPACT, packet_len=55, snr=0.0,
                    covered=False)
        args.update(kw)
        return self.p.decide(**args)

    # -- the cases that refuse ---------------------------------------------

    def test_a_beacon_is_never_relayed_whatever_its_hop_count_says(self):
        """The beacon defines the one-hop neighbour set the coverage test runs
        on. Flooding it makes every node look adjacent to every other, which
        would silently destroy the mechanism the whole budget rests on -- so
        this is enforced on receipt, where a forged hop count cannot reach."""
        for hops in (0, 1, 7, 255):
            d = self.decide(hops=hops, kind=T_BEACON, covered=False)
            self.assertFalse(d.relay, hops)
            self.assertEqual(d.reason, "beacon")

    def test_a_spent_frame_is_not_relayed(self):
        self.assertFalse(self.decide(hops=0).relay)

    def test_the_single_cell_case_relays_nothing(self):
        """Everyone hears everyone, so no node can extend coverage. This is the
        ordinary state of a squad standing together and the reason flooding is
        free until the group splits."""
        d = self.decide(covered=True)
        self.assertFalse(d.relay)
        self.assertEqual(d.reason, "no new coverage")

    def test_over_budget_refuses(self):
        p = policy(self.clock, budget_s=0.1)
        self.assertFalse(p.decide(hops=2, kind=T_COMPACT, packet_len=500,
                                  snr=0.0, covered=False).relay)

    # -- the case that relays ----------------------------------------------

    def test_a_frame_reaching_somebody_new_is_relayed(self):
        d = self.decide()
        self.assertTrue(d.relay)
        self.assertEqual(d.reason, "extends coverage")

    # -- the delay ---------------------------------------------------------

    def test_a_weaker_signal_goes_first(self):
        """Meshtastic's trick: the node that heard the original worst is
        furthest away, so its relay reaches the most new ground."""
        far = self.decide(snr=-15.0).delay_s
        mid = self.decide(snr=0.0).delay_s
        near = self.decide(snr=15.0).delay_s
        self.assertLess(far, mid)
        self.assertLess(mid, near)

    def test_the_delay_stays_inside_the_window(self):
        for snr in (-40.0, -10.0, 0.0, 10.0, 40.0, None):
            d = self.decide(snr=snr)
            self.assertGreaterEqual(d.delay_s, 0.0)
            self.assertLessEqual(d.delay_s, relay.DEFAULT_WINDOW_S + 0.01)

    def test_an_unknown_snr_lands_mid_window(self):
        self.assertAlmostEqual(self.decide(snr=None).delay_s,
                               relay.DEFAULT_WINDOW_S / 2, places=6)

    # -- whole messages ----------------------------------------------------

    def test_a_multi_fragment_message_is_charged_as_a_whole(self):
        """Half a relayed drawing cannot be reassembled by anyone, so the
        airtime is spent for nothing. The refusal has to happen before the
        first fragment rather than during the fourth."""
        p = policy(self.clock, budget_s=1.0)
        one = p.decide(hops=3, kind=T_FRAG, packet_len=200, snr=0.0,
                       covered=False, msg_id=77, fragments=5)
        self.assertFalse(one.relay)
        self.assertEqual(one.reason, "over budget")

    def test_every_fragment_of_an_accepted_message_is_relayed(self):
        p = policy(self.clock, budget_s=30.0)
        first = p.decide(hops=3, kind=T_FRAG, packet_len=200, snr=0.0,
                         covered=False, msg_id=77, fragments=5)
        self.assertTrue(first.relay)
        spent_after_first = p.budget.spent
        for _ in range(4):
            later = p.decide(hops=3, kind=T_FRAG, packet_len=200, snr=0.0,
                             covered=False, msg_id=77, fragments=5)
            self.assertTrue(later.relay)
        # Charged once, for the whole message, not five times.
        self.assertAlmostEqual(p.budget.spent, spent_after_first, places=6)

    def test_a_refused_message_stays_refused_for_every_fragment(self):
        p = policy(self.clock, budget_s=1.0)
        first = p.decide(hops=3, kind=T_FRAG, packet_len=200, snr=0.0,
                         covered=False, msg_id=77, fragments=5)
        self.assertFalse(first.relay)
        refused_before = p.budget.refused
        again = p.decide(hops=3, kind=T_FRAG, packet_len=200, snr=0.0,
                         covered=False, msg_id=77, fragments=5)
        self.assertFalse(again.relay)
        self.assertEqual(again.reason, "message already refused")
        # The bucket was not asked a second time: the message was judged once,
        # as a unit, which is the whole point of remembering the verdict.
        self.assertEqual(p.budget.refused, refused_before)

    def test_the_verdict_is_forgotten_once_the_message_can_no_longer_arrive(self):
        p = policy(self.clock, budget_s=1000.0)
        p.decide(hops=3, kind=T_FRAG, packet_len=60, snr=0.0, covered=False,
                 msg_id=77, fragments=2)
        self.clock.advance(relay.DEFAULT_MSG_TTL_S + 1)
        self.assertEqual(p._recall(77), None)

    def test_message_memory_is_bounded(self):
        p = policy(self.clock, budget_s=1000.0)
        for i in range(relay.DEFAULT_MAX_MSGS * 3):
            p.decide(hops=3, kind=T_FRAG, packet_len=60, snr=0.0,
                     covered=False, msg_id=i, fragments=1)
        self.assertLessEqual(len(p._msgs), relay.DEFAULT_MAX_MSGS)


class TestHopCaps(unittest.TestCase):

    def test_a_beacon_is_stamped_unrelayable(self):
        self.assertEqual(relay.hop_cap(is_position=False, is_beacon=True), 0)

    def test_a_position_gets_the_smaller_cap(self):
        """A dropped position is replaced within the minute; a dropped marker
        is gone for good."""
        self.assertLess(relay.hop_cap(True, False), relay.hop_cap(False, False))

    def test_every_cap_fits_the_three_bits_the_header_has(self):
        for cap in (relay.HOPS_BEACON, relay.HOPS_POSITION, relay.HOPS_ONESHOT):
            self.assertLessEqual(cap, 7)

    def test_an_ack_can_travel_as_far_as_what_it_acknowledges(self):
        """Milestone 4 over more than one hop depends on this.

        Only one-shot traffic is acknowledged, so an ack must be able to
        retrace HOPS_ONESHOT hops. It was stamped zero until the 2026-08-12
        bench, where every multi-hop chat arrived, was acknowledged, and was
        still reported UNDELIVERED because the ack could not get home.
        """
        ack_cap = relay.hop_cap(is_position=False, is_beacon=False)
        self.assertGreaterEqual(ack_cap, relay.HOPS_ONESHOT)

    def test_an_ack_is_not_refused_the_way_a_beacon_is(self):
        """A beacon is refused whatever its hop field says, because flooding it
        would destroy the coverage test. An ack carries no such claim, so it
        has to be allowed to earn a relay on the ordinary rules."""
        p = policy()
        d = p.decide(hops=relay.HOPS_ONESHOT, kind=T_ACK, packet_len=33,
                     snr=0.0, covered=False)
        self.assertTrue(d.relay, d.reason)

    def test_an_ack_still_relays_only_when_it_reaches_somebody_new(self):
        """The larger cap must not become a licence to flood: in one cell an
        ack is refused like anything else, which is what bounds its cost."""
        p = policy()
        d = p.decide(hops=relay.HOPS_ONESHOT, kind=T_ACK, packet_len=33,
                     snr=0.0, covered=True)
        self.assertFalse(d.relay)
        self.assertEqual(d.reason, "no new coverage")


class TestBudgetArithmetic(unittest.TestCase):
    """The numbers the design is built on, pinned so a change to the constants
    cannot quietly put relaying back over the channel."""

    def test_the_default_budget_fits_inside_the_measured_headroom(self):
        # 5.8% measured at 16 nodes leaves ~2.5 s/min under a 10% ceiling.
        # Per node, the bucket must stay well inside that once every node is
        # spending it.
        self.assertLessEqual(relay.DEFAULT_BUDGET_S_PER_MIN, 2.5)

    def test_the_window_is_long_enough_to_cancel_and_short_enough_to_use(self):
        self.assertGreaterEqual(relay.DEFAULT_WINDOW_S, 1.0)
        self.assertLessEqual(relay.DEFAULT_WINDOW_S, 5.0)


if __name__ == "__main__":
    unittest.main()


# The pieces above are unit-tested in isolation; this exercises them against
# the real mesh envelope, because the thing most likely to be wrong is how they
# fit together rather than any one of them.
import mesh  # noqa: E402


@unittest.skipUnless(mesh.HAVE_AEAD, "no AEAD backend")
class TestThreeNodesInALine(unittest.TestCase):
    """A --- B --- C, where A and C cannot hear each other.

    This is the case relaying exists for, and the case the coverage test has to
    tell apart from a squad standing together.
    """

    def setUp(self):
        key = mesh.derive_key("line test", rounds=1)
        self.a = mesh.Mesh(b"\xaa\xaa\xaa", key)
        self.b = mesh.Mesh(b"\xbb\xbb\xbb", key)
        self.c = mesh.Mesh(b"\xcc\xcc\xcc", key)
        self.ids = {"a": "aaaaaa", "b": "bbbbbb", "c": "cccccc"}
        self.clock = FakeClock()
        self.b_policy = policy(self.clock)

    def b_decides(self, inbound, a_hears, b_hears):
        """B's decision on a frame from A, given who each of them can hear."""
        others = [n for n in b_hears if n != self.ids["a"]]
        return self.b_policy.decide(
            hops=inbound.hops,
            kind=inbound.frame[0] & 0x0F,
            packet_len=64,
            snr=3.0,
            covered=relay.covers(relay.digest_build(a_hears), others),
        )

    def test_the_middle_node_relays_and_the_far_node_receives(self):
        packet = self.a.wrap(b"\x50a position", hops=relay.HOPS_POSITION)

        # C cannot hear A at all: nothing arrives directly.
        inbound = self.b.unwrap(packet)
        self.assertIsNotNone(inbound)

        decision = self.b_decides(inbound, a_hears=[self.ids["b"]],
                                  b_hears=[self.ids["a"], self.ids["c"]])
        self.assertTrue(decision.relay, decision.reason)
        self.assertEqual(decision.reason, "extends coverage")

        forwarded = self.b.forward(packet)
        arrived = self.c.unwrap(forwarded)
        self.assertIsNotNone(arrived)
        self.assertEqual(arrived.frame, b"\x50a position")
        # C must see A as the sender, not B.
        self.assertEqual(arrived.origin, self.a.origin)

    def test_a_squad_standing_together_relays_nothing(self):
        packet = self.a.wrap(b"\x50a position", hops=relay.HOPS_POSITION)
        inbound = self.b.unwrap(packet)
        decision = self.b_decides(
            inbound,
            a_hears=[self.ids["b"], self.ids["c"]],   # A hears everyone
            b_hears=[self.ids["a"], self.ids["c"]],
        )
        self.assertFalse(decision.relay)
        self.assertEqual(decision.reason, "no new coverage")

    def test_a_relay_cannot_loop_forever(self):
        """Every hop spends one, and the frame keeps its identity, so the
        second copy is a duplicate everywhere it lands."""
        packet = self.a.wrap(b"\x50a position", hops=relay.HOPS_POSITION)
        self.c.unwrap(self.b.forward(packet))
        # C would relay it onward; A hears that and recognises its own frame.
        onward = self.c.forward(self.b.forward(packet))
        self.assertIsNotNone(onward)
        back = self.a.unwrap(onward)
        self.assertEqual(back.origin, self.a.origin)   # dropped as our own
        # ...and it is now spent.
        self.assertIsNone(self.b.forward(onward))

    def test_a_beacon_never_leaves_its_cell(self):
        beacon = self.a.wrap(bytes([0x53]) + b"beaconish", hops=relay.HOPS_BEACON)
        inbound = self.b.unwrap(beacon)
        decision = self.b_decides(inbound, a_hears=[], b_hears=[self.ids["c"]])
        self.assertFalse(decision.relay)
        self.assertEqual(decision.reason, "beacon")
        self.assertIsNone(self.b.forward(beacon))


class TestBudgetAsDuty(unittest.TestCase):
    """Seconds per minute only look portable between links.

    1.5 s a minute is 2.5% duty either way, but it buys 2.5 relays a minute on
    LoRa and 1.0 on FM, because an FM frame costs 2.5x more seconds. Stating
    the allowance as duty keeps the number meaning one thing; asking what it
    buys is a separate question, and one worth asking out loud.
    """

    def test_the_duty_constant_matches_the_seconds_one(self):
        self.assertAlmostEqual(relay.DEFAULT_RELAY_DUTY,
                               relay.DEFAULT_BUDGET_S_PER_MIN / 60.0)

    def test_from_duty_gives_the_same_capacity(self):
        b = relay.RelayBudget.from_duty(0.025, clock=FakeClock())
        self.assertAlmostEqual(b.capacity, 1.5)

    def test_duty_reports_back_what_it_was_built_with(self):
        b = relay.RelayBudget.from_duty(0.04, clock=FakeClock())
        self.assertAlmostEqual(b.duty(), 0.04)

    def test_an_impossible_duty_is_refused(self):
        for bad in (0.0, -0.1, 1.5):
            with self.assertRaises(ValueError):
                relay.RelayBudget.from_duty(bad)

    def test_a_budget_of_nothing_is_refused(self):
        # It would refuse every relay while looking like a configured policy.
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                relay.RelayBudget(bad)

    def test_the_same_budget_buys_fewer_relays_on_the_slower_link(self):
        b = relay.RelayBudget.from_duty(relay.DEFAULT_RELAY_DUTY,
                                        clock=FakeClock())
        lora_frame, fm_frame = 0.588, 1.485      # 60 bytes, measured bitrates
        self.assertAlmostEqual(b.relays_per_minute(lora_frame), 2.55, places=1)
        self.assertAlmostEqual(b.relays_per_minute(fm_frame), 1.01, places=1)

    def test_relays_per_minute_of_a_free_frame_is_not_an_infinity(self):
        b = relay.RelayBudget.from_duty(0.025, clock=FakeClock())
        self.assertEqual(b.relays_per_minute(0.0), 0.0)


class TestLinkBudgets(unittest.TestCase):
    """Two radios are two channels, so they get two buckets."""

    def _budgets(self, clock=None):
        return relay.LinkBudgets(["lora", "fm"], clock=clock or FakeClock())

    def test_each_link_starts_with_its_own_full_bucket(self):
        b = self._budgets()
        for name in ("lora", "fm"):
            self.assertAlmostEqual(b[name].tokens(), 1.5)

    def test_spending_one_link_does_not_touch_the_other(self):
        # The whole reason for the split: airtime on LoRa does not occupy FM's
        # channel, and a shared bucket would have to be set for the worse link.
        b = self._budgets()
        self.assertTrue(b.charge("fm", 1.4))
        self.assertLess(b["fm"].tokens(), 0.2)
        self.assertAlmostEqual(b["lora"].tokens(), 1.5)

    def test_exhausting_the_slow_link_still_leaves_the_fast_one_relaying(self):
        clock = FakeClock()
        b = self._budgets(clock)
        fm = relay.RelayPolicy(airtime_fn=airtime, budget=b["fm"],
                               clock=clock, jitter=lambda: 0.0, name="fm")
        lora = relay.RelayPolicy(airtime_fn=airtime, budget=b["lora"],
                                 clock=clock, jitter=lambda: 0.0, name="lora")
        for i in range(20):
            fm.decide(hops=2, kind=T_COMPACT, packet_len=200, snr=0.0,
                      covered=False, msg_id="m%d" % i)
        self.assertFalse(fm.decide(hops=2, kind=T_COMPACT, packet_len=200,
                                   snr=0.0, covered=False, msg_id="x").relay)
        self.assertTrue(lora.decide(hops=2, kind=T_COMPACT, packet_len=200,
                                    snr=0.0, covered=False, msg_id="x").relay)

    def test_an_unknown_link_is_a_loud_error(self):
        # A mistyped name that quietly refused every relay would look exactly
        # like a congested channel.
        b = self._budgets()
        with self.assertRaises(KeyError) as cm:
            b["lore"]
        self.assertIn("lora", str(cm.exception))

    def test_duplicate_names_are_refused(self):
        with self.assertRaises(ValueError):
            relay.LinkBudgets(["fm", "fm"])

    def test_no_links_is_refused(self):
        with self.assertRaises(ValueError):
            relay.LinkBudgets([])

    def test_membership_and_iteration(self):
        b = self._budgets()
        self.assertIn("fm", b)
        self.assertNotIn("packet", b)
        self.assertEqual(b.names(), ["lora", "fm"])
        self.assertEqual(len(b), 2)

    def test_summary_names_every_link(self):
        line = self._budgets().summary_line()
        self.assertIn("lora=", line)
        self.assertIn("fm=", line)


class TestPolicyPerLink(unittest.TestCase):

    class _Link(object):
        name = "fm"

        @staticmethod
        def frame_s(n):
            return 0.055 + (n + 19) * 8.0 / 442

    def test_for_link_costs_frames_with_that_link(self):
        p = relay.RelayPolicy.for_link(self._Link(), clock=FakeClock())
        self.assertAlmostEqual(p.airtime_fn(60), self._Link.frame_s(60))

    def test_for_link_names_the_policy(self):
        p = relay.RelayPolicy.for_link(self._Link(), clock=FakeClock())
        self.assertEqual(p.name, "fm")
        self.assertIn("[fm]", p.summary_line())

    def test_an_unnamed_policy_reports_as_before(self):
        # The shipped single-radio summary must not gain a stray "[None]".
        self.assertTrue(policy().summary_line().startswith("relay:"))

    def test_two_links_decide_a_message_independently(self):
        # A frame refused on the slow link may still be worth relaying on the
        # fast one, so the per-message memory cannot be shared.
        clock = FakeClock()
        budgets = relay.LinkBudgets(["lora", "fm"], clock=clock)
        fm = relay.RelayPolicy(airtime_fn=lambda n: 10.0, budget=budgets["fm"],
                               clock=clock, jitter=lambda: 0.0, name="fm")
        lora = relay.RelayPolicy(airtime_fn=airtime, budget=budgets["lora"],
                                 clock=clock, jitter=lambda: 0.0, name="lora")
        args = dict(hops=2, kind=T_COMPACT, packet_len=60, snr=0.0,
                    covered=False, msg_id="same-message")
        self.assertFalse(fm.decide(**args).relay)     # 10 s does not fit 1.5 s
        self.assertTrue(lora.decide(**args).relay)


class TestNoRelayLink(unittest.TestCase):
    """FM originates and does not forward.

    At a 1.5 s/min budget only frames of 60 bytes or less could ever have been
    forwarded there, so relaying on FM was barely functional. Expressed as an
    object rather than as an absent one, so that "does not relay" and "somebody
    forgot to wire the relay up" do not look the same in a log.
    """

    def test_it_refuses_everything(self):
        n = relay.NoRelay("fm")
        for args in (dict(hops=3, kind=T_COMPACT, packet_len=10),
                     dict(hops=1, kind=T_ACK, packet_len=6),
                     dict(hops=9, kind=T_RELIABLE, packet_len=60)):
            self.assertFalse(n.decide(snr=0.0, covered=False, **args).relay)

    def test_the_reason_names_the_link(self):
        self.assertIn("fm", relay.NoRelay("fm").decide(
            hops=3, kind=T_COMPACT, packet_len=10, snr=0.0,
            covered=False).reason)

    def test_it_never_asks_for_a_delay(self):
        d = relay.NoRelay("fm").decide(hops=3, kind=T_COMPACT, packet_len=10,
                                       snr=-10.0, covered=False)
        self.assertEqual(d.delay_s, 0.0)

    def test_it_counts_what_it_declined(self):
        n = relay.NoRelay("fm")
        for i in range(3):
            n.decide(hops=3, kind=T_COMPACT, packet_len=10, snr=0.0,
                     covered=False, msg_id="m%d" % i)
        self.assertIn("3 frames declined", n.summary_line())

    def test_the_summary_says_it_is_off_rather_than_idle(self):
        line = relay.NoRelay("fm").summary_line()
        self.assertIn("off", line)
        self.assertIn("originates only", line)

    def test_it_has_the_same_shape_as_a_real_policy(self):
        # So the caller does not branch on which kind of link it has.
        real, off = policy(), relay.NoRelay("fm")
        for attr in ("decide", "summary_line", "stats"):
            self.assertTrue(hasattr(real, attr) and hasattr(off, attr), attr)


class TestUnmeteredBudget(unittest.TestCase):
    """WiFi's real constraint is not airtime scarcity, so its budget does
    not meter it at all -- shaped like RelayBudget (same as NoRelay is
    shaped like a RelayPolicy above) so RelayPolicy/Gateway/LinkBudgets
    need no branch for it."""

    def test_charge_always_succeeds_no_matter_the_amount(self):
        b = relay.UnmeteredBudget()
        for cost in (0.001, 1.0, 1000.0):
            self.assertTrue(b.charge(cost))

    def test_tokens_and_capacity_report_no_ceiling(self):
        b = relay.UnmeteredBudget()
        self.assertEqual(b.tokens(), float("inf"))
        self.assertEqual(b.capacity, float("inf"))

    def test_spent_still_accumulates_for_reporting(self):
        # Not metered, but still worth showing on the status screen.
        b = relay.UnmeteredBudget()
        b.charge(2.0)
        b.charge(3.0)
        self.assertAlmostEqual(b.spent, 5.0)

    def test_refund_undoes_the_bookkeeping_charge_did(self):
        b = relay.UnmeteredBudget()
        b.charge(5.0)
        b.refund(2.0)
        self.assertAlmostEqual(b.spent, 3.0)

    def test_relays_per_minute_and_duty_report_no_ceiling(self):
        b = relay.UnmeteredBudget()
        self.assertEqual(b.relays_per_minute(0.5), float("inf"))
        self.assertEqual(b.duty(), 1.0)

    def test_a_policy_backed_by_it_never_refuses_on_budget_grounds(self):
        # The one thing this whole change is for: a frame too big or too
        # frequent for LoRa's budget must still go through on WiFi's.
        p = relay.RelayPolicy(airtime_fn=lambda n: 10.0,
                              budget=relay.UnmeteredBudget(),
                              jitter=lambda: 0.0, name="wifi")
        for i in range(50):
            self.assertTrue(p.decide(hops=2, kind=T_COMPACT, packet_len=1000,
                                     snr=0.0, covered=False,
                                     msg_id="m%d" % i).relay)

    def test_linkbudgets_gives_only_the_named_link_an_unmetered_budget(self):
        b = relay.LinkBudgets(["lora", "wifi"], unmetered=("wifi",))
        self.assertIsInstance(b["wifi"], relay.UnmeteredBudget)
        self.assertIsInstance(b["lora"], relay.RelayBudget)

    def test_linkbudgets_with_no_unmetered_links_is_unchanged(self):
        b = relay.LinkBudgets(["lora", "fm"])
        self.assertIsInstance(b["lora"], relay.RelayBudget)
        self.assertIsInstance(b["fm"], relay.RelayBudget)
