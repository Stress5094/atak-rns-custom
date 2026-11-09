"""Tests for cross-link forwarding.

The thing this exists for: an FM handheld and a LoRa node cannot hear each
other at all, so without a node carrying both radios and passing traffic
between them, an FM operator reaches direct FM range and nobody else.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import airtime
import eligibility
import gateway
import relay

T_COMPACT = 0x0

POSITION, MARKER, ROUTE = 55, 119, 231


class FakeClock(object):
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def build(clock=None, duty=gateway.DEFAULT_GATEWAY_DUTY):
    clock = clock or FakeClock()
    gates = eligibility.Gates([
        eligibility.lora_gate(airtime.lora(lambda: 1074, fallback_bps=3125)),
        eligibility.fm_gate(airtime.fm(442)),
    ])
    budgets = relay.LinkBudgets(gates.names(), duty=duty, clock=clock)
    gw = gateway.Gateway(gates, budgets, jitter=lambda w: 0.0, clock=clock)
    return gw, gates, budgets, clock


class TestReachingTheOtherRadio(unittest.TestCase):

    def test_a_position_from_fm_is_lifted_onto_lora(self):
        gw, _, _, _ = build()
        out = gw.decide("fm", T_COMPACT, POSITION, hops=2)
        self.assertEqual([f.link for f in out], ["lora"])

    def test_a_position_from_lora_is_lifted_onto_fm(self):
        gw, _, _, _ = build()
        out = gw.decide("lora", T_COMPACT, POSITION, hops=2)
        self.assertEqual([f.link for f in out], ["fm"])

    def test_it_is_never_sent_back_where_it_came_from(self):
        # Same-link forwarding is the relay policy's decision, and on FM the
        # answer is a flat no. Answering it here too would double-charge the
        # budget or quietly overrule that.
        gw, _, _, _ = build()
        for arrived in ("fm", "lora"):
            out = gw.decide(arrived, T_COMPACT, POSITION, hops=3)
            self.assertNotIn(arrived, [f.link for f in out])

    def test_no_coverage_test_is_applied(self):
        # A frame that arrived on FM has by definition not been heard on LoRa,
        # so the test that suppresses most same-link relays does not apply.
        gw, _, _, _ = build()
        self.assertTrue(gw.decide("fm", T_COMPACT, POSITION, hops=1))

    def test_the_reason_says_why_it_is_worth_it(self):
        gw, _, _, _ = build()
        self.assertIn("did not hear it",
                      gw.decide("fm", T_COMPACT, POSITION, hops=2)[0].reason)


class TestRefusals(unittest.TestCase):

    def test_an_exhausted_hop_count_forwards_nothing(self):
        gw, _, _, _ = build()
        self.assertEqual(gw.decide("fm", T_COMPACT, POSITION, hops=0), [])

    def test_a_frame_too_big_for_the_destination_is_refused(self):
        gw, _, _, _ = build()
        self.assertEqual(gw.decide("lora", T_COMPACT, ROUTE, hops=3), [])

    def test_the_refusal_says_what_it_would_have_cost(self):
        gw, _, _, _ = build()
        ok, reason = gw.explain("lora", T_COMPACT, ROUTE, 3)["fm"]
        self.assertFalse(ok)
        self.assertIn("4.6 s", reason)
        self.assertIn("3.0 s cap", reason)

    def test_a_marker_still_crosses_to_fm(self):
        # The cap went to 3.0 s precisely so this would fit.
        gw, _, _, _ = build()
        self.assertEqual([f.link for f in
                          gw.decide("lora", T_COMPACT, MARKER, hops=3)], ["fm"])

    def test_an_unknown_arrival_link_is_a_loud_error(self):
        gw, _, _, _ = build()
        with self.assertRaises(KeyError):
            gw.decide("packet", T_COMPACT, POSITION, hops=2)

    def test_a_gate_without_a_budget_is_refused_at_construction(self):
        gates = eligibility.Gates([
            eligibility.lora_gate(airtime.lora(lambda: 1074, 3125)),
            eligibility.fm_gate(airtime.fm(442))])
        with self.assertRaises(ValueError):
            gateway.Gateway(gates, relay.LinkBudgets(["lora"]))


class TestBudget(unittest.TestCase):

    def test_the_destination_budget_pays_not_the_source(self):
        gw, gates, budgets, _ = build()
        before_fm = budgets["fm"].tokens()
        before_lora = budgets["lora"].tokens()
        gw.decide("fm", T_COMPACT, POSITION, hops=2)     # forwards onto lora
        self.assertAlmostEqual(budgets["fm"].tokens(), before_fm)
        self.assertLess(budgets["lora"].tokens(), before_lora)

    def test_an_exhausted_destination_budget_refuses(self):
        gw, gates, budgets, _ = build()
        budgets["lora"].charge(budgets["lora"].tokens() - 0.01)
        self.assertEqual(gw.decide("fm", T_COMPACT, POSITION, hops=2), [])

    def test_explain_charges_nothing(self):
        # So a refusal can be logged without changing the decision.
        gw, _, budgets, _ = build()
        before = budgets["lora"].tokens()
        gw.explain("fm", T_COMPACT, POSITION, 2)
        self.assertAlmostEqual(budgets["lora"].tokens(), before)


class TestCancellation(unittest.TestCase):
    """Two nodes carrying both radios will both forward the same frame."""

    def test_cancelling_gives_the_airtime_back(self):
        gw, gates, budgets, _ = build()
        before = budgets["lora"].tokens()
        forward = gw.decide("fm", T_COMPACT, POSITION, hops=2)[0]
        self.assertLess(budgets["lora"].tokens(), before)
        gw.cancelled(forward, POSITION)
        self.assertAlmostEqual(budgets["lora"].tokens(), before, places=9)

    def test_a_cancelled_forward_is_not_counted_as_sent(self):
        gw, _, _, _ = build()
        forward = gw.decide("fm", T_COMPACT, POSITION, hops=2)[0]
        gw.cancelled(forward, POSITION)
        line = gw.summary_line()
        self.assertIn("forwarded=0", line)
        self.assertIn("cancelled=1", line)

    def test_a_refund_cannot_overfill_the_bucket(self):
        # It would hand back airtime the minute never contained.
        budget = relay.RelayBudget(1.5, clock=FakeClock())
        self.assertEqual(budget.refund(10.0), 0.0)
        self.assertAlmostEqual(budget.tokens(), 1.5)

    def test_a_refund_returns_what_it_actually_gave(self):
        budget = relay.RelayBudget(1.5, clock=FakeClock())
        budget.charge(1.0)
        self.assertAlmostEqual(budget.refund(0.4), 0.4)
        self.assertAlmostEqual(budget.tokens(), 0.9)


def build_with_wifi(clock=None):
    clock = clock or FakeClock()
    gates = eligibility.Gates([
        eligibility.lora_gate(airtime.lora(lambda: 1074, fallback_bps=3125)),
        eligibility.wifi_gate(airtime.wifi()),
    ])
    budgets = relay.LinkBudgets(gates.names(), unmetered=("wifi",), clock=clock)
    gw = gateway.Gateway(gates, budgets, jitter=lambda w: 0.0, clock=clock)
    return gw, gates, budgets, clock


class TestUnmeteredDestinationNeverRefuses(unittest.TestCase):
    """WiFi's own gateway budget is exempted from metering entirely (see
    relay.UnmeteredBudget) -- a forward onto it must never be refused for
    being 'over budget', no matter how many have already gone out."""

    def test_forwarding_onto_wifi_is_never_refused_for_budget(self):
        gw, _, _, _ = build_with_wifi()
        for i in range(500):
            out = gw.decide("lora", T_COMPACT, POSITION, hops=2)
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0].link, "wifi")

    def test_wifi_tokens_never_drop_no_matter_what_crosses(self):
        gw, _, budgets, _ = build_with_wifi()
        for i in range(50):
            gw.decide("lora", T_COMPACT, ROUTE, hops=2)
        self.assertEqual(budgets["wifi"].tokens(), float("inf"))


class TestWindowScalesWithTheSlowLink(unittest.TestCase):
    """A fixed window is right on LoRa and wrong on FM.

    The second gateway must have HEARD the first before its own timer fires. On
    FM the frame itself can take three seconds, so a flat two-second window
    would have the second one keying up mid-transmission -- which is the
    collision the window exists to prevent.
    """

    def test_the_fm_window_clears_the_fm_frame(self):
        gw, gates, _, _ = build()
        fm = gates["fm"]
        window = gw.window_for(fm, MARKER)
        self.assertGreater(window, fm.link.frame_s(MARKER))

    def test_a_slow_destination_gets_a_longer_window(self):
        gw, gates, _, _ = build()
        self.assertGreater(gw.window_for(gates["fm"], MARKER),
                           gw.window_for(gates["lora"], MARKER))

    def test_the_floor_still_applies_to_tiny_frames(self):
        gw, gates, _, _ = build()
        self.assertGreaterEqual(gw.window_for(gates["lora"], 10),
                                gateway.DEFAULT_WINDOW_S)

    def test_the_delay_is_inside_the_window(self):
        gates = eligibility.Gates([
            eligibility.lora_gate(airtime.lora(lambda: 1074, 3125)),
            eligibility.fm_gate(airtime.fm(442))])
        budgets = relay.LinkBudgets(gates.names(),
                                    duty=gateway.DEFAULT_GATEWAY_DUTY,
                                    clock=FakeClock())
        gw = gateway.Gateway(gates, budgets, jitter=lambda w: w)
        forward = gw.decide("lora", T_COMPACT, MARKER, hops=3)[0]
        self.assertAlmostEqual(forward.delay_s,
                               gw.window_for(gates["fm"], MARKER))


class TestGatewayBudgetIsItsOwn(unittest.TestCase):
    """A forward across links is not a relay, and is not paid for like one.

    A same-link relay usually reaches nobody new, so a stingy allowance is
    right. A gateway forward reaches nodes that could not have heard the frame
    at all, so its second of airtime buys strictly more -- and inheriting the
    relay budget put a 119-byte marker (2.55 s on FM) permanently outside a
    1.5 s/min allowance, which is not a decision anybody made on purpose.
    """

    def test_the_gateway_allowance_exceeds_the_relay_one(self):
        self.assertGreater(gateway.DEFAULT_GATEWAY_DUTY,
                           relay.DEFAULT_RELAY_DUTY)

    def test_a_marker_crosses_on_the_gateway_allowance(self):
        gw, _, _, _ = build()
        self.assertEqual([f.link for f in
                          gw.decide("lora", T_COMPACT, MARKER, hops=3)], ["fm"])

    def test_a_marker_would_not_cross_on_the_relay_allowance(self):
        # Pinned because it is the reason the two budgets are separate.
        gw, _, _, _ = build(duty=relay.DEFAULT_RELAY_DUTY)
        self.assertEqual(gw.decide("lora", T_COMPACT, MARKER, hops=3), [])

    def test_the_fm_allowance_is_about_one_marker_a_minute(self):
        # Thin, and honest: it is what the channel has.
        gw, gates, budgets, _ = build()
        cost = gates["fm"].link.frame_s(MARKER)
        self.assertAlmostEqual(budgets["fm"].relays_per_minute(cost), 1.2,
                               delta=0.2)
