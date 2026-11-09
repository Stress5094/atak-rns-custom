"""Tests for the per-link eligibility gate.

The fact these pin: Reticulum advertises a 564-byte MTU, and one full-size
frame is 4.3 s on LoRa and 10.6 s on FM. The MTU is a protocol limit and was
never a claim about what is sensible to transmit.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import airtime
import eligibility
import relay

T_COMPACT, T_ZXML, T_BEACON = 0x0, 0x1, 0x3


def lora():
    return airtime.lora(lambda: 1074, fallback_bps=3125)


def fm():
    return airtime.fm(442)


class TestTheCapIsInTime(unittest.TestCase):
    """A byte limit right for one link is silently wrong for the other."""

    def test_the_same_cap_gives_different_byte_budgets(self):
        slow = eligibility.Gate(fm(), max_frame_s=2.0)
        fast = eligibility.Gate(lora(), max_frame_s=2.0)
        self.assertEqual(fast.max_frame_bytes(), 249)
        self.assertEqual(slow.max_frame_bytes(), 88)

    def test_the_budget_really_is_the_cap(self):
        for link in (lora(), fm()):
            gate = eligibility.Gate(link, max_frame_s=2.0)
            n = gate.max_frame_bytes()
            self.assertLessEqual(link.frame_s(n), 2.0)
            self.assertGreater(link.frame_s(n + 1), 2.0)

    def test_a_full_mtu_frame_is_refused_everywhere(self):
        # 564 bytes: 4.3 s on LoRa, 10.6 s on FM.
        gates = eligibility.Gates([eligibility.Gate(lora()),
                                   eligibility.Gate(fm())])
        self.assertEqual(gates.links_for(T_COMPACT, 564), [])

    def test_a_small_frame_is_allowed_everywhere(self):
        gates = eligibility.Gates([eligibility.Gate(lora()),
                                   eligibility.Gate(fm())])
        self.assertEqual(gates.links_for(T_COMPACT, 60), ["lora", "fm"])

    def test_a_middling_frame_fits_the_fast_link_only(self):
        gates = eligibility.Gates([eligibility.Gate(lora()),
                                   eligibility.Gate(fm())])
        self.assertEqual(gates.links_for(T_COMPACT, 120), ["lora"])

    def test_a_bigger_cap_buys_more_bytes(self):
        small = eligibility.Gate(fm(), max_frame_s=1.0).max_frame_bytes()
        large = eligibility.Gate(fm(), max_frame_s=4.0).max_frame_bytes()
        self.assertGreater(large, small)


class TestImpossibleCap(unittest.TestCase):
    """A cap under the link's own per-frame overhead is a configuration error,
    not a busy channel, and has to read like one."""

    def _gate(self):
        # FM's fixed cost is ~0.40 s; 0.2 s can never be met.
        return eligibility.Gate(fm(), max_frame_s=0.2)

    def test_no_payload_fits(self):
        self.assertEqual(self._gate().max_frame_bytes(), 0)

    def test_even_an_empty_frame_is_refused(self):
        self.assertFalse(self._gate().allows(T_COMPACT, 0).ok)

    def test_the_reason_blames_the_configuration(self):
        reason = self._gate().allows(T_COMPACT, 0).reason
        self.assertIn("overhead", reason)
        self.assertIn("max_frame_s", reason)

    def test_a_nonpositive_cap_is_refused_at_construction(self):
        for bad in (0, -1.0):
            with self.assertRaises(ValueError):
                eligibility.Gate(fm(), max_frame_s=bad)


class TestDeniedKinds(unittest.TestCase):
    """The fallback path is the one classification the frame type supports.

    Every position, marker, drawing and route is T_COMPACT, so "drop drawings,
    keep positions" cannot be decided here. A frame that reached T_ZXML is one
    the compact encoder could not shrink, and that one IS visible.
    """

    def test_a_denied_kind_is_refused_however_small(self):
        gate = eligibility.Gate(fm(), deny_kinds=(T_ZXML,))
        self.assertFalse(gate.allows(T_ZXML, 1).ok)

    def test_other_kinds_are_unaffected(self):
        gate = eligibility.Gate(fm(), deny_kinds=(T_ZXML,))
        self.assertTrue(gate.allows(T_COMPACT, 60).ok)
        self.assertTrue(gate.allows(T_BEACON, 30).ok)

    def test_the_reason_names_the_kind_and_the_link(self):
        gate = eligibility.Gate(fm(), deny_kinds=(T_ZXML,))
        reason = gate.allows(T_ZXML, 1).reason
        self.assertIn("0x1", reason)
        self.assertIn("fm", reason)

    def test_a_kind_denied_on_one_link_still_goes_on_the_other(self):
        gates = eligibility.Gates([eligibility.Gate(lora()),
                                   eligibility.Gate(fm(), deny_kinds=(T_ZXML,))])
        self.assertEqual(gates.links_for(T_ZXML, 60), ["lora"])


class TestRelayableCeiling(unittest.TestCase):
    """A frame can be originated and still never be forwarded."""

    def test_fm_can_only_relay_very_small_frames(self):
        budget = relay.RelayBudget.from_duty(relay.DEFAULT_RELAY_DUTY)
        gate = eligibility.Gate(fm())
        self.assertEqual(gate.relayable_bytes(budget), 60)

    def test_the_fast_link_relays_more(self):
        budget = relay.RelayBudget.from_duty(relay.DEFAULT_RELAY_DUTY)
        self.assertGreater(eligibility.Gate(lora()).relayable_bytes(budget),
                           eligibility.Gate(fm()).relayable_bytes(budget))

    def test_a_frame_may_pass_the_gate_and_still_be_unrelayable(self):
        # 88 bytes fits FM's 2 s cap but exceeds its 1.5 s relay budget, so it
        # is originate-only. Worth knowing before wondering why multi-hop
        # stopped working on the slow link.
        budget = relay.RelayBudget.from_duty(relay.DEFAULT_RELAY_DUTY)
        gate = eligibility.Gate(fm())
        self.assertTrue(gate.allows(T_COMPACT, 88).ok)
        self.assertGreater(88, gate.relayable_bytes(budget))

    def test_a_budget_smaller_than_the_overhead_relays_nothing(self):
        budget = relay.RelayBudget(0.2)
        self.assertEqual(eligibility.Gate(fm()).relayable_bytes(budget), 0)


class TestGatesContainer(unittest.TestCase):

    def _gates(self):
        return eligibility.Gates([eligibility.Gate(lora()),
                                  eligibility.Gate(fm())])

    def test_verdicts_carry_a_reason_for_every_link(self):
        v = self._gates().verdicts(T_COMPACT, 400)
        self.assertFalse(v["lora"].ok)
        self.assertFalse(v["fm"].ok)
        self.assertIn("cap", v["fm"].reason)

    def test_an_allowed_link_has_no_complaint(self):
        self.assertEqual(self._gates().verdicts(T_COMPACT, 60)["fm"].reason, "")

    def test_links_are_returned_in_configured_order(self):
        gates = eligibility.Gates([eligibility.Gate(fm()),
                                   eligibility.Gate(lora())])
        self.assertEqual(gates.links_for(T_COMPACT, 60), ["fm", "lora"])

    def test_an_unknown_link_is_a_loud_error(self):
        with self.assertRaises(KeyError) as cm:
            self._gates()["packet"]
        self.assertIn("lora", str(cm.exception))

    def test_duplicate_links_are_refused(self):
        with self.assertRaises(ValueError):
            eligibility.Gates([eligibility.Gate(fm()), eligibility.Gate(fm())])

    def test_no_links_is_refused(self):
        with self.assertRaises(ValueError):
            eligibility.Gates([])

    def test_summary_names_every_link_and_its_budget(self):
        line = self._gates().summary_line()
        self.assertIn("lora<=249B", line)
        self.assertIn("fm<=88B", line)


class TestLiveBitrate(unittest.TestCase):

    def test_the_byte_budget_follows_the_bitrate(self):
        # A radio reconfigured to a slower preset should tighten the gate
        # without anything being reconfigured by hand.
        rate = [1074]
        gate = eligibility.Gate(
            airtime.lora(lambda: rate[0], fallback_bps=3125))
        wide = gate.max_frame_bytes()
        rate[0] = 537
        self.assertLess(gate.max_frame_bytes(), wide)


class TestNoFragmentingOnFm(unittest.TestCase):
    """The frame gate cannot catch a large message, because its fragments are
    each individually small.

    A 231-byte route passes a 143-byte gate twice, as two frames, each paying
    the fixed cost again; a 476-byte drawing passes four times. Six seconds of
    a channel nobody else can use, arriving as a burst, with every frame
    individually within the rules. The refusal has to happen once, before
    encoding, on a link that does not fragment.
    """

    def _fm(self):
        return eligibility.fm_gate(fm())

    def test_a_message_that_fits_one_frame_is_allowed(self):
        for n in (46, 55, 64, 119, 132, 143):
            self.assertTrue(self._fm().allows_message(n).ok, "%d bytes" % n)

    def test_a_message_needing_two_frames_is_refused(self):
        self.assertFalse(self._fm().allows_message(144).ok)

    def test_a_route_and_a_drawing_are_refused(self):
        for n in (231, 476):
            self.assertFalse(self._fm().allows_message(n).ok)

    def test_the_reason_gives_the_transmission_count_and_the_total(self):
        reason = self._fm().allows_message(476).reason
        self.assertIn("4 transmissions", reason)
        self.assertIn("12.0 s", reason)

    def test_every_fragment_would_have_passed_the_frame_gate(self):
        # Which is the whole point: each is small, the burst is not.
        gate = self._fm()
        self.assertTrue(gate.allows(T_COMPACT, 120).ok)
        self.assertFalse(gate.allows_message(240).ok)

    def test_a_fragmenting_link_allows_the_same_message(self):
        self.assertTrue(eligibility.lora_gate(lora()).allows_message(476).ok)

    def test_frames_needed_counts_transmissions(self):
        gate = self._fm()
        self.assertEqual(gate.frames_needed(143), 1)
        self.assertEqual(gate.frames_needed(144), 2)
        self.assertEqual(gate.frames_needed(476), 4)


class TestConfiguredLinks(unittest.TestCase):
    """The two links as they are actually set up."""

    def _gates(self):
        return eligibility.Gates([eligibility.lora_gate(lora()),
                                  eligibility.fm_gate(fm())])

    def test_fm_carries_positions_waypoints_chat_and_markers(self):
        gates = self._gates()
        for name, size in (("position", 55), ("waypoint", 46), ("chat", 64),
                           ("marker", 119), ("rectangle", 117), ("circle", 132)):
            self.assertIn("fm", gates.links_for_message(size), name)

    def test_fm_refuses_routes_and_drawings(self):
        gates = self._gates()
        for name, size in (("route", 231), ("drawing", 476)):
            self.assertEqual(gates.links_for_message(size), ["lora"], name)

    def test_fm_does_not_relay_and_lora_does(self):
        gates = self._gates()
        self.assertFalse(gates["fm"].relays)
        self.assertTrue(gates["lora"].relays)

    def test_fm_does_not_fragment_and_lora_does(self):
        gates = self._gates()
        self.assertFalse(gates["fm"].fragments)
        self.assertTrue(gates["lora"].fragments)

    def test_the_fm_cap_is_the_one_that_admits_markers(self):
        # 2.0 s would have been 88 bytes and refused a 119-byte marker; the cap
        # went to 3.0 s only because dropping relay removed the reason for it
        # to be tight.
        self.assertEqual(eligibility.FM_MAX_FRAME_S, 3.0)
        self.assertEqual(eligibility.fm_gate(fm()).max_frame_bytes(), 143)

    def test_repr_shows_the_fm_restrictions(self):
        text = repr(eligibility.fm_gate(fm()))
        self.assertIn("no-frag", text)
        self.assertIn("no-relay", text)
