"""That the bridge's link wiring actually holds together.

The only tests this module has. rns_bridge needs RNS and pyserial, neither of
which is on the desktop, so nothing here has ever been import-checked off the
phone -- which means a NameError in the wiring would first appear as an app
that will not start.

Stubbed rather than skipped for exactly that reason -- see rns_stub.py, which
holds the stub so this module and test_fm_link.py cannot drift apart over it.
"""

import collections
import copy
import hashlib
import json
import math
import os
import inspect
import struct
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rns_stub

rns_stub.install()

import cot_codec
import eligibility
import gateway as gateway_mod
import lora_health
import mesh
import relay
import rangetest
import reliable
import fm_config
import fm_link
import fm_resend
import rns_bridge
import tx_policy
import wifi_health


# A SYNTHETIC MIDDLE-PRIORITY LINK, for the failover tests below.
#
# These tests cover the N-way failover machinery -- _set_active_link,
# _check_auto_switch, _startup_active_link, set_link_override, the beacon
# slot grid -- which is deliberately generic over _LINK_PRIORITY. They used
# to use the real LINK_FM as their middle-priority link, and were moved onto a
# synthetic one when the FM transport was removed on 2026-10-01.
#
# KEPT SYNTHETIC even though FM came back on 2026-10-02, because it is the
# better test: it exercises the machinery on a link the production code knows
# nothing about, which is exactly the property that made re-adding FM one entry
# in _LINK_PRIORITY rather than a new set of decisions. setUpModule below
# substitutes the whole tuple, so LINK_FM is absent for the length of this
# module -- the FM-specific tests further down do not read _LINK_PRIORITY, and
# any that ever do must put it back for themselves.
#
# _state_for() falls back to a hard-signal-only reading (online -> UP) for
# any link with no entry in _link_health, which is what FM itself did, so
# this link behaves exactly as FM did without needing a health tracker.
LINK_BACKUP = "testbackup"

# How long a burst the beacon slot grid has to clear, for the two tests that
# assert slots never place two transmissions inside one burst of each other.
# 11.5 s is the figure FM actually shipped (its MAX_BURST_S), kept as a plain
# constant now that the module defining it is gone -- the property under test
# is the grid's, not FM's.
LONG_BURST_S = 11.5

# FM's real budgets at the Phase 0 operating point, DERIVED rather than written
# out: 29 B of per-burst capacity, of which 17 B is usable once mesh.OVERHEAD is
# paid. Every FM test below sizes itself from these, so a change to the code or
# the framing moves the tests with it instead of leaving them asserting against
# a budget the link no longer has. The pre-teardown tests hard-coded 18 and 113
# -- figures from two different codes, one of which had already been abandoned.
FM_MAX_PACKET = fm_link.hw_mtu()
FM_MAX_PAYLOAD = FM_MAX_PACKET - mesh.OVERHEAD

# A budget at which a compact self-SA KEYFRAME fits one packet. It existed
# because FM's own 17 B did not, so the delta/keyframe/piggyback wiring had to
# be exercised at some other size or it would have been testing nothing.
#
# IT IS NOW THE SAME NUMBER as FM's own budget, because the operating point moved
# to the 128 B code on 2026-10-02 for exactly this reason (see
# fm_config.DEFAULT_CODE). Kept as a separate name, and asserted equal, rather
# than collapsed into one: the two mean different things -- "what this link
# carries" against "what the position path needs" -- and their having differed is
# the whole content of the change. If they diverge again the position path has
# broken again, and this is where that shows.
SA_CAPABLE_PAYLOAD = 113


def setUpModule():
    """Insert the synthetic link into the priority order, between LoRa and
    WiFi, exactly where FM sat. The production tuple is read live by every
    function under test, and a link present in it with no interface
    registered is explicitly handled ("degrades gracefully"), so this is
    invisible to every test that does not opt in by registering one.
    """
    global _ORIG_LINK_PRIORITY
    _ORIG_LINK_PRIORITY = rns_bridge._LINK_PRIORITY
    rns_bridge._LINK_PRIORITY = (rns_bridge.LINK_LORA, LINK_BACKUP,
                                 rns_bridge.LINK_WIFI)


def tearDownModule():
    rns_bridge._LINK_PRIORITY = _ORIG_LINK_PRIORITY


class TestTheBridgeImports(unittest.TestCase):
    """A NameError here is an app that does not start."""

    def test_it_imports_at_all(self):
        self.assertTrue(hasattr(rns_bridge, "_gates"))

    def test_the_link_is_defined_before_the_gates_that_use_it(self):
        # It was not, at first: _lora_link sat 1700 lines below its first use.
        self.assertIs(rns_bridge._gates["lora"].link, rns_bridge._lora_link)


class TestLinkWiring(unittest.TestCase):

    def test_there_is_one_link_today_and_it_is_lora(self):
        self.assertEqual(rns_bridge._gates.names(), [rns_bridge.LINK_LORA])

    def test_every_gate_has_a_relay_budget(self):
        for name in rns_bridge._gates.names():
            self.assertIn(name, rns_bridge._relay_budgets)

    def test_every_gate_has_a_gateway_budget(self):
        for name in rns_bridge._gates.names():
            self.assertIn(name, rns_bridge._gateway_budgets)

    def test_every_gate_has_a_relay_policy(self):
        for name in rns_bridge._gates.names():
            self.assertIsNotNone(rns_bridge._relay_policy_for(name))

    def test_a_link_that_does_not_exist_has_no_policy(self):
        self.assertIsNone(rns_bridge._relay_policy_for("nosuchlink"))

    def test_the_fm_gate_can_be_built_and_refuses_to_relay_or_fragment(self):
        """start() registers this once FM is built, so it is not in the
        import-time _gates above. Its two refusals are the whole point: at this
        link's airtime budget a relayed frame could only ever have been ~60 B,
        and a message needing two transmissions holds a channel nobody else can
        use for twice the burst."""
        gate = eligibility.fm_gate(rns_bridge._fm_link)
        self.assertFalse(gate.relays)
        self.assertFalse(gate.fragments)

    def test_every_tone_placement_clears_its_own_frame_cap(self):
        """EVERY placement, not just the default, because this was a live bug.

        eligibility.FM_MAX_FRAME_S is 3.0 s, derived from an airtime model of
        bytes over a bitrate. FM's cost is flat per burst, and at the 128 B code
        the two HANDHELD-measured placements run 6.16 s and 4.66 s -- so the
        default cap refused every FM frame on exactly the placements a handheld
        operator would select, including the gatewayed traffic that is the only
        way an FM-only node reaches the LoRa mesh. start() now derives the cap
        from the live burst.

        Both ends of the size range are checked: a full frame (the case that
        broke) and a tiny one (which must not be refused either, and would be if
        the cap were ever derived the other way round).
        """
        import airtime
        import fm_config
        orig = fm_link.profile_name()
        self.addCleanup(fm_link.set_profile, orig)
        for name in fm_config.profile_names():
            with self.subTest(placement=name):
                fm_link.set_profile(name)
                link = airtime.fm(lambda: 0,
                                  fallback_bps=fm_link.bitrate_bps())
                gate = eligibility.fm_gate(
                    link, max_frame_s=fm_link.burst_duration_s() * 1.01)
                self.assertTrue(
                    gate.allows(relay.HOPS_ONESHOT, fm_link.hw_mtu()).ok,
                    "a full frame is refused on %r" % name)
                self.assertTrue(gate.allows(relay.HOPS_ONESHOT, 6).ok,
                                "a 6 B frame is refused on %r" % name)

    def test_the_default_cap_would_refuse_a_handheld_placement(self):
        """The bug itself, pinned -- so if anyone ever drops the derived cap and
        falls back to the constant, this says what breaks."""
        import airtime
        orig = fm_link.profile_name()
        self.addCleanup(fm_link.set_profile, orig)
        fm_link.set_profile("narrow")
        link = airtime.fm(lambda: 0, fallback_bps=fm_link.bitrate_bps())
        default_gate = eligibility.fm_gate(link)   # no max_frame_s
        self.assertGreater(fm_link.burst_duration_s(),
                           eligibility.FM_MAX_FRAME_S)
        self.assertFalse(
            default_gate.allows(relay.HOPS_ONESHOT, fm_link.hw_mtu()).ok)

    def test_the_gateway_allowance_is_the_larger_one(self):
        self.assertGreater(rns_bridge._gateway_budgets["lora"].capacity,
                           rns_bridge._relay_budgets["lora"].capacity)


class TestNothingShippedChanged(unittest.TestCase):
    """The LoRa path has to behave exactly as it did."""

    def test_lora_still_relays(self):
        self.assertIsInstance(rns_bridge._relay_policy_for("lora"),
                              relay.RelayPolicy)

    def test_the_relay_allowance_is_unchanged(self):
        # The refill is the shipped duty; only what the bucket holds grew
        # (2026-10-14, relay.DEFAULT_RELAY_BURST_S).
        budget = rns_bridge._relay_budgets["lora"]
        self.assertAlmostEqual(budget.per_minute_s,
                               relay.DEFAULT_BUDGET_S_PER_MIN)
        self.assertAlmostEqual(budget.capacity, relay.DEFAULT_RELAY_BURST_S)

    def test_lora_is_uncapped_so_full_size_packets_still_go(self):
        # 464 bytes sealed is 3.60 s at the shipped preset. Any of the caps
        # that suit FM would have refused traffic working since milestone 1.
        gate = rns_bridge._gates["lora"]
        self.assertIsNone(gate.max_frame_s)
        self.assertTrue(gate.allows(0x0, cot_codec.RNS_MAX_PAYLOAD).ok)

    def test_airtime_matches_the_formula_it_replaced(self):
        for n in (0, 60, 464):
            self.assertAlmostEqual(rns_bridge._airtime_s(n),
                                   ((n + 19) * 8.0) / rns_bridge._FALLBACK_BITRATE,
                                   places=12)

    def test_consider_relay_defaults_to_the_one_radio(self):
        args = rns_bridge._consider_relay.__code__.co_varnames
        self.assertIn("link_name", args)
        self.assertEqual(rns_bridge._consider_relay.__defaults__,
                         (rns_bridge.LINK_LORA,))


class TestRelayDueTracksOriginLink(unittest.TestCase):
    """A relay must go back out on the link it was HEARD on, not this
    node's own _active_link -- previously lost entirely, since
    _relay_pending only ever stored (send_after, packet) and the one
    caller of _relay_due() hardcoded LINK_LORA as the outgoing link
    regardless. Harmless while LoRa was the only link that could ever
    relay (eligibility.fm_gate() sets relays=False), but wrong the moment
    WiFi -- whose gate DOES relay, unlike FM's -- is what a relay actually
    arrived on."""

    def setUp(self):
        self._orig_relay_pending = rns_bridge._relay_pending
        rns_bridge._relay_pending = collections.OrderedDict()

    def tearDown(self):
        rns_bridge._relay_pending = self._orig_relay_pending

    def test_due_relay_carries_the_link_it_arrived_on(self):
        past = time.monotonic() - 1.0
        rns_bridge._relay_pending["frame-1"] = (past, b"sealed-bytes",
                                                rns_bridge.LINK_WIFI)
        packet, link = rns_bridge._relay_due()
        self.assertEqual(packet, b"sealed-bytes")
        self.assertEqual(link, rns_bridge.LINK_WIFI)

    def test_not_yet_due_returns_none(self):
        future = time.monotonic() + 60.0
        rns_bridge._relay_pending["frame-1"] = (future, b"sealed-bytes",
                                                rns_bridge.LINK_LORA)
        self.assertIsNone(rns_bridge._relay_due())

    def test_due_relay_is_removed_from_the_pending_table(self):
        past = time.monotonic() - 1.0
        rns_bridge._relay_pending["frame-1"] = (past, b"sealed-bytes",
                                                LINK_BACKUP)
        rns_bridge._relay_due()
        self.assertNotIn("frame-1", rns_bridge._relay_pending)


class TestMaxPacketSizing(unittest.TestCase):
    """_max_packet/_max_payload (what start() hands the encoder/fragmenter)
    must come from whichever interface is actually active.

    A LINK THAT DOES NOT CROSS RNS.Packet HAS TO BE ASKED. FM has no RNS-native
    MTU concept at all, so sizing it against LoRa's PLAIN_MDU is what let the
    very first live FM beacon (42 B) exceed its own 30 B code with no complaint
    from RNS and only fail once it reached the interface -- taking the whole link
    offline on its first ever send.
    """

    def setUp(self):
        self._orig_transport = rns_bridge._transport
        self._orig_active_link = rns_bridge._active_link
        self._orig_iface = rns_bridge._fm_iface

    def tearDown(self):
        rns_bridge._transport = self._orig_transport
        rns_bridge._active_link = self._orig_active_link
        rns_bridge._fm_iface = self._orig_iface

    def test_lora_transport_sizes_against_plain_mdu(self):
        rns_bridge._transport = "usb"
        rns_bridge._active_link = rns_bridge.LINK_LORA
        rns_bridge._fm_iface = None
        self.assertEqual(
            rns_bridge._compute_max_packet(),
            min(cot_codec.RNS_MAX_PAYLOAD, 464),
        )

    def test_fm_transport_sizes_against_the_fm_interfaces_own_hw_mtu(self):
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM

        class _StubIface(object):
            HW_MTU = FM_MAX_PACKET

        rns_bridge._fm_iface = _StubIface()
        self.assertEqual(rns_bridge._compute_max_packet(), FM_MAX_PACKET)

    def test_fm_hw_mtu_is_not_clamped_up_to_plain_mdu(self):
        # The whole point: FM's real per-burst capacity is an order of magnitude
        # smaller than LoRa's, and must never be silently rounded up to it.
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM

        class _StubIface(object):
            HW_MTU = 126  # a larger code's payload, still far below PLAIN_MDU

        rns_bridge._fm_iface = _StubIface()
        max_packet = rns_bridge._compute_max_packet()
        self.assertEqual(max_packet, 126)
        self.assertLess(max_packet, cot_codec.RNS_MAX_PAYLOAD)

    def test_the_real_interface_reports_the_configs_capacity(self):
        """Not a stub: the live arithmetic, so a code change moves this with it
        rather than leaving the stubs above agreeing with nothing."""
        self.assertEqual(fm_link.hw_mtu(), fm_link.config().capacity_bytes())
        # Still far below PLAIN_MDU, which is the property that makes asking
        # necessary. The margin shrank when the operating point moved to the
        # 128 B code; a frame sized between the two is exactly the one that
        # would sail past RNS unnoticed and fail at the modem.
        self.assertLess(fm_link.hw_mtu(), 464 // 3)


def _pli_xml(uid, lat=35.0, lon=-79.0, callsign="BRAVO01",
             cot_type="a-f-G-U-C", time_s="2026-07-31T10:00:00Z"):
    """A real self-position report -- <takv> is what makes cot_codec treat
    it as one (see test_tx_policy.py's own pli() for the same shape and the
    same reasoning)."""
    return (
        '<event version="2.0" uid="%s" type="%s" time="%s" '
        'stale="2026-07-31T10:02:00Z" how="m-g">'
        '<point lat="%.7f" lon="%.7f" hae="100.0" ce="9.5" le="9999999.0"/>'
        '<detail>'
        '<takv os="29" version="5.1.0.0" device="GOOGLE PIXEL 6" platform="ATAK-CIV"/>'
        '<contact endpoint="*:-1:stcp" callsign="%s"/>'
        '<__group role="Team Member" name="Cyan"/></detail></event>'
        % (uid, cot_type, time_s, lat, lon, callsign)
    ).encode("utf-8")


class TestTransmitSealedFmBypass(unittest.TestCase):
    """_transmit_sealed() is the one place FM traffic skips RNS.Packet's own
    ~19 B of framing (see fm_link.py's module docstring) -- every other
    link must still go through RNS.Packet exactly as before."""

    def setUp(self):
        self._orig_transport = rns_bridge._transport
        self._orig_active_link = rns_bridge._active_link
        self._orig_iface = rns_bridge._fm_iface

    def tearDown(self):
        rns_bridge._transport = self._orig_transport
        rns_bridge._active_link = self._orig_active_link
        rns_bridge._fm_iface = self._orig_iface

    def test_non_fm_transport_still_uses_rns_packet(self):
        rns_bridge._transport = "usb"
        rns_bridge._active_link = rns_bridge.LINK_LORA
        self.assertTrue(rns_bridge._transmit_sealed(b"sealed"))

    def test_fm_transport_calls_process_outgoing_directly_not_rns_packet(self):
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM

        class _StubIface(object):
            def __init__(self):
                self.sent = []

            def process_outgoing(self, data):
                self.sent.append(data)
                return True

        iface = _StubIface()
        rns_bridge._fm_iface = iface
        self.assertTrue(rns_bridge._transmit_sealed(b"sealed"))
        self.assertEqual(iface.sent, [b"sealed"])

    def test_fm_transport_with_no_interface_reports_failure_not_a_crash(self):
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM
        rns_bridge._fm_iface = None
        self.assertFalse(rns_bridge._transmit_sealed(b"sealed"))

class TestFmSuppressesAcknowledgements(unittest.TestCase):
    """The FM link carries CoT payload only -- no acks, no ARQ retries. That
    constraint is stated in fm_link.py's docstring and was, until this
    was added, not actually enforced anywhere: _queue_ack had no transport
    check and reliable.ReliableTx would retransmit on a half-duplex ~99 bps
    channel."""

    def setUp(self):
        self._orig_transport = rns_bridge._transport
        self._orig_active_link = rns_bridge._active_link
        with rns_bridge._ack_pending_lock:
            rns_bridge._ack_pending.clear()

    def tearDown(self):
        rns_bridge._transport = self._orig_transport
        rns_bridge._active_link = self._orig_active_link
        with rns_bridge._ack_pending_lock:
            rns_bridge._ack_pending.clear()

    def _wrapper(self):
        class _W(object):
            # A real node id, so encode_ack() on the non-FM path builds a
            # genuine frame rather than failing for an unrelated reason and
            # making the FM assertion pass for the wrong one.
            origin = rns_bridge._NODE_ID
            msg_id = 7
            addressee = None
            any_ack = True
        return _W()

    def _queued(self):
        with rns_bridge._ack_pending_lock:
            return len(rns_bridge._ack_pending)

    def test_fm_queues_no_ack_for_a_reliable_frame(self):
        # Gated on the FRAME's own link now, not the node's current default
        # (_active_link/_transport) -- a frame that arrived on FM never
        # earns an ack regardless of which link this node prefers to
        # transmit on. See _queue_ack's own comment.
        rns_bridge._queue_ack(self._wrapper(), rns_bridge.LINK_FM)
        self.assertEqual(self._queued(), 0)

    def test_every_other_link_still_acks(self):
        # The control: proves the FM assertion above is the transport check
        # doing its job, not the ack path being broken for all links.
        rns_bridge._transport = "usb"
        rns_bridge._active_link = rns_bridge.LINK_LORA
        rns_bridge._queue_ack(self._wrapper())
        self.assertEqual(self._queued(), 1)

class TestDeliveredSaysWhatTheAckProves(unittest.TestCase):
    """One ack settles an unaddressed message, and proves only that the node
    which sent it holds the message. A bare "DELIVERED to A" read as "everyone
    has it" -- and was wrong about a room chat in the 2026-10-14 split-mesh
    run. The line must name who answered, how it heard us, and who is NOT
    confirmed."""

    def _done(self, acked, expected, attempts=1):
        return types.SimpleNamespace(label="b-t-f GeoChat.x", acked=set(acked),
                                     expected=frozenset(expected), attempts=attempts)

    def setUp(self):
        orig = rns_bridge._peers.hops_away
        self.addCleanup(setattr, rns_bridge._peers, "hops_away", orig)
        rns_bridge._peers.hops_away = lambda n: 2 if n == "aaaa03" else 1

    def test_a_near_ack_names_who_is_not_confirmed(self):
        line = rns_bridge._delivered_line(self._done(["aaaa01"], ["aaaa01", "aaaa03"]))
        self.assertIn("acknowledged by aaaa01 (direct)", line)
        self.assertIn("NOT confirmed for aaaa03", line)

    def test_an_ack_through_a_relay_says_so(self):
        line = rns_bridge._delivered_line(self._done(["aaaa03"], ["aaaa01", "aaaa03"], 3))
        self.assertIn("aaaa03 (through a relay)", line)
        self.assertIn("after 3 transmission(s)", line)

    def test_a_single_recipient_needs_no_caveat(self):
        line = rns_bridge._delivered_line(self._done(["aaaa01"], ["aaaa01"]))
        self.assertNotIn("NOT confirmed", line)
        self.assertTrue(line.startswith("DELIVERED "))   # what log greps look for

    def test_everyone_answering_says_so(self):
        line = rns_bridge._delivered_line(self._done(["aaaa01", "aaaa03"], ["aaaa01", "aaaa03"]))
        self.assertIn("every expected node answered", line)


class TestFmOneShotUsesBlindResendNotArq(unittest.TestCase):
    """One-shot traffic (chat, markers, routes) must reach fm_resend on FM
    and reliable.ReliableTx everywhere else. Registering with the wrong one
    is invisible until it is on the air."""

    def setUp(self):
        self._orig_transport = rns_bridge._transport
        self._orig_active_link = rns_bridge._active_link
        self._orig_max_payload = rns_bridge._max_payload
        rns_bridge._max_payload = FM_MAX_PAYLOAD
        self._orig_resend = rns_bridge._fm_resend
        rns_bridge._fm_resend = fm_resend.FmResend()

    def tearDown(self):
        rns_bridge._transport = self._orig_transport
        rns_bridge._active_link = self._orig_active_link
        rns_bridge._max_payload = self._orig_max_payload
        rns_bridge._fm_resend = self._orig_resend

    def _summary(self):
        class _S(object):
            type = "a-f-G"
            uid = "TEST-1"
            is_position = False
        return _S()

    def test_it_registers_a_schedule_and_returns_transmittable_packets(self):
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM
        packets = rns_bridge._build_fm_oneshot(b"<event/>", self._summary())
        self.assertTrue(packets)
        self.assertTrue(all(len(p) <= rns_bridge._max_payload for p in packets))
        self.assertEqual(len(rns_bridge._fm_resend.snapshot()["inflight"]), 1)

    def test_it_registers_nothing_with_the_arq_engine(self):
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM
        before = rns_bridge._reliable_tx.stats().get("sent", 0)
        rns_bridge._build_fm_oneshot(b"<event/>", self._summary())
        self.assertEqual(rns_bridge._reliable_tx.stats().get("sent", 0), before)

    def test_every_resend_is_byte_identical_to_the_first_transmission(self):
        # The receiver combines fragments across attempts by msg_id, so a
        # resend that re-encoded the event could fragment differently and
        # silently stop accumulating.
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM
        # Configured BEFORE registering: register() computes the next attempt
        # time from the interval as it is at that moment, so changing it
        # afterwards would leave the resend scheduled minutes out and this
        # test asserting on an empty list.
        rns_bridge._fm_resend = fm_resend.FmResend(interval_s=0.0,
                                                   jitter_frac=0.0,
                                                   first_retry_s=0.0,
                                                   first_retry_jitter_s=0.0)
        packets = rns_bridge._build_fm_oneshot(b"<event/>", self._summary())
        due = rns_bridge._fm_resend.tick()
        self.assertEqual([p for _, p, _, _ in due], [packets])

class TestEscalateToFm(unittest.TestCase):
    """LoRa's own ARQ schedule exhausted with no ack at all -- last resort:
    the same message, forced onto FM, reusing its own msg_id."""

    def setUp(self):
        self._orig_max_payload_fm = rns_bridge._max_payload_fm
        rns_bridge._max_payload_fm = FM_MAX_PAYLOAD
        self._orig_fm_resend = rns_bridge._fm_resend
        # Zero delay: register() computes the first attempt's due time from
        # the interval as it is AT REGISTRATION, so a due-immediately
        # schedule has to be configured before _escalate_to_fm() registers,
        # same reasoning test_every_resend_is_byte_identical... uses above.
        rns_bridge._fm_resend = fm_resend.FmResend(
            interval_s=0.0, jitter_frac=0.0,
            first_retry_s=0.0, first_retry_jitter_s=0.0)
        self._orig_queue = rns_bridge._tx_queue
        rns_bridge._tx_queue = rns_bridge.queue.Queue(maxsize=32)

    def tearDown(self):
        rns_bridge._max_payload_fm = self._orig_max_payload_fm
        rns_bridge._fm_resend = self._orig_fm_resend
        rns_bridge._tx_queue = self._orig_queue

    def _record(self, payload=None):
        """An Undelivered the shape _retire() builds, carrying the frame
        _build_reliable would really have registered.

        ENCODED FIRST, which the pre-teardown version of this test did not do:
        it handed register() raw XML, so record.frame was ~440 B of uncompressed
        event rather than the ~43 B compact frame the production path produces.
        That was invisible at the 113 B budget this class used to assume and is
        not at FM's real 17 B, where 440 B needs 34 fragments against
        cot_codec.MAX_FRAGMENTS' 16 and the escalation is declined outright.
        """
        encoded = cot_codec.encode(payload or _pli_xml("ANDROID-escalate-src"),
                                   cot_codec.RNS_MAX_PAYLOAD - mesh.OVERHEAD)
        _msg_id, wrapped = rns_bridge._reliable_tx.register(
            encoded, rns_bridge._NODE_ID, [], label="chat")
        # register() with no peers returns unwitnessed and tracks nothing --
        # need a real _Pending to get a real Undelivered with a frame, so
        # build the record directly the same shape _retire() does.
        return reliable.Undelivered(
            msg_id=_msg_id, label="chat", missing=frozenset(), attempts=4,
            age_s=16.0, reason=reliable.NO_ACK, frame=wrapped)

    def test_registers_with_fm_resend_under_the_same_msg_id(self):
        record = self._record()
        rns_bridge._escalate_to_fm(record)
        inflight = rns_bridge._fm_resend.snapshot()["inflight"]
        self.assertEqual(len(inflight), 1)
        self.assertEqual(inflight[0]["msg_id"], record.msg_id)

    def test_the_packets_are_re_fragmented_not_re_encoded(self):
        # Byte-identical to fragmenting record.frame directly -- the same
        # bytes reliable.py's own ARQ already built and tried on LoRa, not
        # a fresh cot_codec.encode() of the raw payload (which would mint a
        # NEW msg_id/wrapper and break reassembly continuity with whatever
        # LoRa already sent).
        record = self._record()
        expected = cot_codec.fragment_reliable(record.frame, rns_bridge._max_payload_fm)
        rns_bridge._escalate_to_fm(record)
        due = rns_bridge._fm_resend.tick()
        self.assertEqual(len(due), 1)
        _, packets, _, _ = due[0]
        self.assertEqual(packets, expected)

    def test_is_enqueued_forced_to_fm(self):
        record = self._record()
        rns_bridge._escalate_to_fm(record)
        packets, queued_link = rns_bridge._tx_queue.get_nowait()
        self.assertEqual(queued_link, rns_bridge.LINK_FM)
        self.assertTrue(packets)

    def test_a_missing_fm_budget_is_a_harmless_noop(self):
        # Defensive: _fm_iface is not None is expected to imply this is set,
        # but a caller bug here must not crash the reliable loop.
        rns_bridge._max_payload_fm = None
        record = self._record()
        rns_bridge._escalate_to_fm(record)
        self.assertEqual(len(rns_bridge._fm_resend.snapshot()["inflight"]), 0)

    def test_a_message_too_large_for_fms_fragment_cap_is_declined(self):
        """A REAL CEILING OF THIS LINK, pinned so it stays a known boundary.

        cot_codec caps a message at MAX_FRAGMENTS (16), which at FM's 113 B
        budget is 1728 B of wrapped frame. That was 192 B at the 32 B code, where
        an ordinary drawing could not cross at all; 1728 B clears every message
        type this app sends except a long route. Declining is still the right
        end: the alternative is a message that could never complete occupying the
        channel for a minute while it fails.
        """
        cap = cot_codec.MAX_FRAGMENTS * rns_bridge._max_payload_fm
        oversized = reliable.Undelivered(
            msg_id=9, label="a very long route", missing=frozenset(),
            attempts=4, age_s=16.0, reason=reliable.NO_ACK,
            frame=self._record().frame + b"\x00" * cap)
        rns_bridge._escalate_to_fm(oversized)
        self.assertEqual(len(rns_bridge._fm_resend.snapshot()["inflight"]), 0)
        self.assertTrue(rns_bridge._tx_queue.empty(),
                        "a message that cannot complete must not be sent")

    def test_the_ceiling_is_far_higher_than_it_was(self):
        """What the switch bought, as the number a future reader will want."""
        self.assertEqual(rns_bridge._max_payload_fm, FM_MAX_PAYLOAD)
        self.assertGreater(cot_codec.MAX_FRAGMENTS * FM_MAX_PAYLOAD, 1700)

    def test_an_ordinary_chat_message_does_fit(self):
        """The control: the limit above is a real ceiling, not the escalation
        path being broken for everything."""
        rns_bridge._escalate_to_fm(self._record(DIRECT_CHAT_XML))
        self.assertEqual(len(rns_bridge._fm_resend.snapshot()["inflight"]), 1)


DIRECT_CHAT_XML = (
    b'<event version="2.0" uid="GeoChat.ANDROID-escalate-sender.'
    b'ANDROID-escalate-target.msgid-esc-1" type="b-t-f" '
    b'time="2026-08-04T15:21:32.000Z" start="2026-08-04T15:21:32.000Z" '
    b'stale="2026-08-04T15:22:32.000Z" how="h-g-i-g-o">'
    b'<point lat="20.9089800" lon="-140.5829100" hae="381.0" ce="9.5" le="9999999.0"/>'
    b'<detail><__chat parent="RootContactGroup" groupOwner="false" '
    b'messageId="msgid-esc-1" chatroom="Target" '
    b'id="ANDROID-escalate-target" senderCallsign="tester">'
    b'<chatgrp uid0="ANDROID-escalate-sender" uid1="ANDROID-escalate-target" '
    b'id="ANDROID-escalate-target"/>'
    b'</__chat><link uid="ANDROID-escalate-sender" type="a-f-G-U-C" relation="p-p"/>'
    b'<remarks source="BAO.F.ATAK.ANDROID-escalate-sender" '
    b'to="ANDROID-escalate-target" '
    b'time="2026-08-04T15:21:32.000Z">test message</remarks>'
    b'</detail></event>'
)

class TestNeverHeardOnLoraRouting(unittest.TestCase):
    """LoRa-primary/FM-backup routing: a one-shot message to a peer heard
    only on FM skips LoRa's ~16 s ARQ schedule and goes straight to FM's
    one-shot path -- retrying on a link that specific peer is not known to
    be able to hear would just be wasted time. Any peer with LoRa history,
    or no history at all, keeps the normal LoRa-first behaviour."""

    TARGET_NODE = "aa11bb22cc33"

    def setUp(self):
        self._orig_max_payload = rns_bridge._max_payload
        rns_bridge._max_payload = 200
        self._orig_max_payload_fm = rns_bridge._max_payload_fm
        rns_bridge._max_payload_fm = FM_MAX_PAYLOAD
        self._orig_fm_iface = rns_bridge._fm_iface
        rns_bridge._fm_iface = object()  # only needs to be not-None here
        self._orig_active_link = rns_bridge._active_link
        rns_bridge._active_link = rns_bridge.LINK_LORA
        self._orig_queue = rns_bridge._tx_queue
        rns_bridge._tx_queue = rns_bridge.queue.Queue(maxsize=32)
        self._orig_delayed_queue = rns_bridge._delayed_queue
        rns_bridge._delayed_queue = rns_bridge.queue.Queue(maxsize=32)
        self._orig_uid_map = dict(rns_bridge._atak_uid_to_node)
        rns_bridge._atak_uid_to_node.clear()
        rns_bridge._atak_uid_to_node["ANDROID-escalate-target"] = self.TARGET_NODE
        self._orig_fm_resend = rns_bridge._fm_resend
        rns_bridge._fm_resend = fm_resend.FmResend()
        # A fresh table per test: _peers is shared module state, and a
        # note_frame() from one test (e.g. "heard on LoRa") must not leak
        # into another that specifically wants an FM-only or unknown peer.
        self._orig_peers = rns_bridge._peers
        rns_bridge._peers = rns_bridge.peers_mod.PeerTable(
            connected_s=rns_bridge._PEER_CONNECTED_S,
            forget_s=rns_bridge._PEER_FORGET_S)

    def tearDown(self):
        rns_bridge._max_payload = self._orig_max_payload
        rns_bridge._max_payload_fm = self._orig_max_payload_fm
        rns_bridge._fm_iface = self._orig_fm_iface
        rns_bridge._active_link = self._orig_active_link
        rns_bridge._tx_queue = self._orig_queue
        rns_bridge._delayed_queue = self._orig_delayed_queue
        rns_bridge._atak_uid_to_node.clear()
        rns_bridge._atak_uid_to_node.update(self._orig_uid_map)
        rns_bridge._fm_resend = self._orig_fm_resend
        rns_bridge._peers = self._orig_peers

    def test_a_peer_only_ever_heard_on_fm_skips_lora_arq(self):
        rns_bridge._peers.note_frame(self.TARGET_NODE, link=rns_bridge.LINK_FM)
        before = rns_bridge._reliable_tx.stats().get("sent", 0)
        rns_bridge._handle_outbound_cot(DIRECT_CHAT_XML)
        self.assertEqual(rns_bridge._reliable_tx.stats().get("sent", 0), before)
        self.assertEqual(len(rns_bridge._fm_resend.snapshot()["inflight"]), 1)

    def test_a_peer_with_any_lora_history_still_tries_lora_first(self):
        rns_bridge._peers.note_frame(self.TARGET_NODE, link=rns_bridge.LINK_LORA)
        rns_bridge._peers.note_frame(self.TARGET_NODE, link=rns_bridge.LINK_FM)
        rns_bridge._handle_outbound_cot(DIRECT_CHAT_XML)
        self.assertEqual(len(rns_bridge._fm_resend.snapshot()["inflight"]), 0)

    def test_an_unknown_peer_gets_no_special_treatment(self):
        # Never heard on either link -- no positive evidence FM would help.
        rns_bridge._handle_outbound_cot(DIRECT_CHAT_XML)
        self.assertEqual(len(rns_bridge._fm_resend.snapshot()["inflight"]), 0)

    def test_the_routed_packets_are_forced_to_fm_on_the_queue(self):
        rns_bridge._peers.note_frame(self.TARGET_NODE, link=rns_bridge.LINK_FM)
        rns_bridge._handle_outbound_cot(DIRECT_CHAT_XML)
        # One-shot FM traffic is always staggered -- lands in
        # _delayed_queue, not _tx_queue directly.
        _, _, queued_link = rns_bridge._delayed_queue.get_nowait()
        self.assertEqual(queued_link, rns_bridge.LINK_FM)

    def test_without_fm_backup_attached_lora_is_tried_as_normal(self):
        rns_bridge._fm_iface = None
        rns_bridge._peers.note_frame(self.TARGET_NODE, link=rns_bridge.LINK_FM)
        rns_bridge._handle_outbound_cot(DIRECT_CHAT_XML)
        self.assertEqual(len(rns_bridge._fm_resend.snapshot()["inflight"]), 0)

class TestFmPositionGetsOneQuickExtraTry(unittest.TestCase):
    """Positions get no retry from _fm_resend/_build_fm_oneshot at all --
    wants_ack is False for them by design. This is the separate, much
    smaller fix for the gap that leaves: a single missed heartbeat had no
    second chance until the next 90s cycle. One extra try, not the one-shot
    schedule -- see _fm_position_resend's own comment in rns_bridge.py.

    AT SA_CAPABLE_PAYLOAD, NOT FM's own budget, and that is the point of the
    constant: the wiring under test here (which schedule a keyframe goes to,
    that a replay is byte-identical, that a delta is stashed for the beacon) can
    only run when the encoder actually produces single-packet keyframes and
    deltas. FM's real 17 B capacity produces neither, which is its own finding
    and is pinned in TestFmPositionsDoNotFitTheOperatingPoint below.

    _sa_tx is reset AND warmed up (one throwaway send) in setUp so every
    test in this class starts from the same known state: an established
    binding, ready to produce an ordinary DELTA next -- matching real
    in-service behaviour (a node's very first position is unconditionally
    a keyframe, per SaTx.__init__'s own forced=True, but by its second and
    every later one it is a delta). Tests that specifically want a
    keyframe force one explicitly rather than relying on the initial
    state, so they still pass if this warm-up ever changes."""

    def setUp(self):
        self._orig_transport = rns_bridge._transport
        self._orig_active_link = rns_bridge._active_link
        self._orig_max_payload = rns_bridge._max_payload
        rns_bridge._max_payload = SA_CAPABLE_PAYLOAD
        self._orig_pos_resend = rns_bridge._fm_position_resend
        rns_bridge._fm_position_resend = fm_resend.FmResend(attempts=2)
        self._orig_kf_resend = rns_bridge._fm_keyframe_resend
        rns_bridge._fm_keyframe_resend = fm_resend.FmResend(attempts=4)
        self._orig_sa_tx = rns_bridge._sa_tx
        rns_bridge._sa_tx = cot_codec.SaTx()
        cot_codec.build_packets(_pli_xml("ANDROID-warmup"), SA_CAPABLE_PAYLOAD,
                                rns_bridge._sa_tx)
        self._orig_last_delta = rns_bridge._fm_last_position_delta
        rns_bridge._fm_last_position_delta = None
        self._orig_queue = rns_bridge._tx_queue
        rns_bridge._tx_queue = rns_bridge.queue.Queue(maxsize=32)
        # A routine FM heartbeat is now staggered (2026-09-22, real hardware
        # found two nodes restarted together collide on every heartbeat, not
        # just the first) -- it lands in _delayed_queue, not _tx_queue
        # directly. Isolated the same way for the same reason.
        self._orig_delayed_queue = rns_bridge._delayed_queue
        rns_bridge._delayed_queue = rns_bridge.queue.Queue(maxsize=32)

    def tearDown(self):
        rns_bridge._transport = self._orig_transport
        rns_bridge._active_link = self._orig_active_link
        rns_bridge._max_payload = self._orig_max_payload
        rns_bridge._fm_position_resend = self._orig_pos_resend
        rns_bridge._fm_keyframe_resend = self._orig_kf_resend
        rns_bridge._sa_tx = self._orig_sa_tx
        rns_bridge._delayed_queue = self._orig_delayed_queue
        rns_bridge._fm_last_position_delta = self._orig_last_delta
        rns_bridge._tx_queue = self._orig_queue

    def test_a_position_on_fm_gets_one_extra_try_scheduled(self):
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM
        rns_bridge._handle_outbound_cot(_pli_xml("ANDROID-pos-1"))
        self.assertEqual(
            len(rns_bridge._fm_position_resend.snapshot()["inflight"]), 1)
        self.assertEqual(
            len(rns_bridge._fm_keyframe_resend.snapshot()["inflight"]), 0)

    def test_a_keyframe_gets_the_bigger_keyframe_schedule_instead(self):
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM
        rns_bridge._sa_tx.force_keyframe()
        rns_bridge._handle_outbound_cot(_pli_xml("ANDROID-pos-kf"))
        self.assertEqual(
            len(rns_bridge._fm_keyframe_resend.snapshot()["inflight"]), 1)
        self.assertEqual(
            len(rns_bridge._fm_position_resend.snapshot()["inflight"]), 0)

    def test_a_position_on_usb_lora_gets_no_extra_try(self):
        # This whole fix is scoped to FM's measured loss rate -- the RNode/
        # LoRa link's own firmware/CSMA backstop is a different mechanism,
        # untouched here. "usb" is the real transport string set_transport()
        # uses for that link (there is no literal "lora" value -- LINK_LORA
        # names a relay-policy key, not a _transport value).
        rns_bridge._transport = "usb"
        rns_bridge._active_link = rns_bridge.LINK_LORA
        rns_bridge._handle_outbound_cot(_pli_xml("ANDROID-pos-2"))
        self.assertEqual(
            len(rns_bridge._fm_position_resend.snapshot()["inflight"]), 0)
        self.assertEqual(
            len(rns_bridge._fm_keyframe_resend.snapshot()["inflight"]), 0)

    def test_a_one_shot_event_does_not_use_the_position_schedule(self):
        # Chat/markers/etc. already get the full _fm_resend schedule --
        # registering them here too would double-send them for nothing.
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM
        marker_xml = (
            b'<event version="2.0" uid="drawing-1" type="a-u-G" '
            b'time="2026-07-31T10:00:00Z" stale="2026-07-31T11:00:00Z" how="h-e">'
            b'<point lat="35.0" lon="-79.0" hae="381.0" ce="9.5" le="9999999.0"/>'
            b'<detail/></event>'
        )
        rns_bridge._handle_outbound_cot(marker_xml)
        self.assertEqual(
            len(rns_bridge._fm_position_resend.snapshot()["inflight"]), 0)
        self.assertEqual(
            len(rns_bridge._fm_keyframe_resend.snapshot()["inflight"]), 0)

    def test_the_resend_replays_the_exact_bytes_sent_the_first_time(self):
        # Must NOT re-encode: a second real encode call would silently
        # advance _sa_tx's delta/keyframe generation again (cot_codec.py's
        # SaTx docstring) and hand the receiver a base it never asked for.
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM
        rns_bridge._fm_position_resend = fm_resend.FmResend(
            attempts=2, first_retry_s=0.0, first_retry_jitter_s=0.0)
        rns_bridge._handle_outbound_cot(_pli_xml("ANDROID-pos-3"))
        # Staggered, not enqueued directly -- see _delayed_queue's own
        # isolation comment in setUp.
        _, first_sent, _ = rns_bridge._delayed_queue.get_nowait()
        due = rns_bridge._fm_position_resend.tick()
        self.assertEqual(len(due), 1)
        _, replayed_packets, _, _ = due[0]
        self.assertEqual(replayed_packets, first_sent)

    def test_a_delta_is_stashed_for_the_next_beacon_to_piggyback(self):
        # See _fm_last_position_delta's own comment: read-and-cleared by
        # _beacon_loop, so this is the ONLY way to observe it from outside.
        # Must be the exact same bytes just transmitted, not a re-encode --
        # same reasoning as test_the_resend_replays_the_exact_bytes... above
        # -- so compared against what was actually enqueued for the first
        # send, the one place the real bytes are observable.
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM
        rns_bridge._handle_outbound_cot(_pli_xml("ANDROID-pos-4"))
        # Staggered, not enqueued directly -- see _delayed_queue's own
        # isolation comment in setUp.
        _, first_sent, _ = rns_bridge._delayed_queue.get_nowait()
        self.assertEqual(rns_bridge._fm_last_position_delta, first_sent[0])

    def test_a_keyframe_is_never_stashed_for_a_beacon(self):
        # Too big -- see encode_beacon's own `position` comment for why a
        # keyframe stays off the beacon entirely.
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM
        rns_bridge._sa_tx.force_keyframe()
        rns_bridge._handle_outbound_cot(_pli_xml("ANDROID-pos-kf2"))
        self.assertIsNone(rns_bridge._fm_last_position_delta)

    def test_a_routine_heartbeat_is_staggered_not_sent_immediately(self):
        # Found 2026-09-22 on real hardware: two nodes restarted within a
        # second of each other collided on every position send, not just
        # the first -- tx_policy's heartbeat check is a pure elapsed-time
        # threshold with no jitter, so nothing before this fix ever spread
        # two synchronized nodes' heartbeats apart. Lands in _delayed_queue
        # (see that queue's own isolation comment in setUp), never directly
        # in _tx_queue.
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM
        rns_bridge._handle_outbound_cot(_pli_xml("ANDROID-pos-stagger"))
        self.assertEqual(list(rns_bridge._tx_queue.queue), [])
        self.assertEqual(rns_bridge._delayed_queue.qsize(), 1)

    def test_the_heartbeat_window_has_a_1s_floor_when_the_scaled_one_is_smaller(self):
        # _ONSET_SLOT_S's peer-scaled window collapses to its smallest value
        # (one slot, 0.25s) whenever peer_count is 0 -- which is always true
        # for a node's very first send, precisely BECAUSE it just started,
        # not because collision risk is low. Requested by name (1s) after
        # the un-floored 0.25s window measured too narrow on real hardware.
        # _stagger_window() mocked directly (rather than driven through
        # _connected_peer_count()/the real peer table) so this test does
        # not depend on how many peers some OTHER test in the same run has
        # left registered -- that table is shared module state, not reset
        # between tests in this file.
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM
        orig_stagger_window = rns_bridge._stagger_window
        rns_bridge._stagger_window = lambda: 0.05
        windows = []
        orig_enqueue_delayed = rns_bridge._enqueue_delayed
        def spy(packets, window, link=None):
            windows.append(window)
            orig_enqueue_delayed(packets, window, link=link)
        rns_bridge._enqueue_delayed = spy
        try:
            rns_bridge._handle_outbound_cot(_pli_xml("ANDROID-pos-floor"))
        finally:
            rns_bridge._stagger_window = orig_stagger_window
            rns_bridge._enqueue_delayed = orig_enqueue_delayed
        self.assertEqual(windows, [rns_bridge._FM_STAGGER_MIN_S])

    def test_a_keyframe_is_staggered_too(self):
        # The very first position after (re)start -- unconditionally a
        # keyframe (SaTx.__init__'s own forced=True) -- is exactly the
        # collision found on real hardware: two nodes that both just
        # started send this at nearly the same instant. It needs the same
        # spread as an ordinary heartbeat, not an exemption.
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM
        rns_bridge._sa_tx.force_keyframe()
        rns_bridge._handle_outbound_cot(_pli_xml("ANDROID-pos-kf-stagger"))
        self.assertEqual(list(rns_bridge._tx_queue.queue), [])
        self.assertEqual(rns_bridge._delayed_queue.qsize(), 1)

class TestPositionsFitTheOperatingPoint(unittest.TestCase):
    """THE REASON THE OPERATING POINT MOVED, kept as the assertions that would
    fail if it moved back.

    This class asserted the OPPOSITE until 2026-10-02, and its measurements were
    right for the 32 B code Phase 0 chose. At that code FM handed the CoT codec
    17 B, and:

        a position DIFFERENCE          16 B   fitted
        a position                     26 B   floor, identity stripped
        a position stating identity    55 B   real 24-char uid + callsign

    So a position could not be sent. cot_codec.encode() rightly refuses to
    fragment a frame carrying session state -- fragmenting a fire-and-forget
    position strands every later delta against a base that never arrived -- so it
    aborted the session and fell back to zlib'd XML: 12 bursts, 28.2 s, for ONE
    position. And because the session aborted, `forced` stayed True, so the
    difference-coding that WOULD have fitted could never start.

    The fix was capacity, not byte-shaving: there is nothing to shave when the
    floor for an absolute position is 26 B and the budget is 17. The 128 B code
    costs 0.6 s more burst and gives 113 B.
    """

    REAL_UID = "ANDROID-0123456789abcdef"   # the real shape: ANDROID-<16 hex>

    def setUp(self):
        self._orig = dict(
            transport=rns_bridge._transport,
            active_link=rns_bridge._active_link,
            max_payload=rns_bridge._max_payload,
            pos=rns_bridge._fm_position_resend,
            kf=rns_bridge._fm_keyframe_resend,
            sa_tx=rns_bridge._sa_tx,
            last_delta=rns_bridge._fm_last_position_delta,
            tx_queue=rns_bridge._tx_queue,
            delayed=rns_bridge._delayed_queue,
        )
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM
        rns_bridge._max_payload = FM_MAX_PAYLOAD
        rns_bridge._fm_position_resend = fm_resend.FmResend(attempts=2)
        rns_bridge._fm_keyframe_resend = fm_resend.FmResend(attempts=4)
        rns_bridge._sa_tx = cot_codec.SaTx()
        rns_bridge._fm_last_position_delta = None
        rns_bridge._tx_queue = rns_bridge.queue.Queue(maxsize=32)
        rns_bridge._delayed_queue = rns_bridge.queue.Queue(maxsize=32)

    def tearDown(self):
        rns_bridge._transport = self._orig["transport"]
        rns_bridge._active_link = self._orig["active_link"]
        rns_bridge._max_payload = self._orig["max_payload"]
        rns_bridge._fm_position_resend = self._orig["pos"]
        rns_bridge._fm_keyframe_resend = self._orig["kf"]
        rns_bridge._sa_tx = self._orig["sa_tx"]
        rns_bridge._fm_last_position_delta = self._orig["last_delta"]
        rns_bridge._tx_queue = self._orig["tx_queue"]
        rns_bridge._delayed_queue = self._orig["delayed"]

    def _pli(self, **kw):
        return _pli_xml(self.REAL_UID, callsign="BRAVO01", **kw)

    def test_the_two_budgets_now_agree(self):
        """What the switch was for, in one line."""
        self.assertEqual(FM_MAX_PAYLOAD, SA_CAPABLE_PAYLOAD)

    def test_a_self_sa_keyframe_fits_one_burst(self):
        """With a REAL 24-character ATAK uid, not a short test one. The uid alone
        costs 23 B on the wire (9 B since ANDROID-<hex> ids are packed), and measuring
        this with a 1-character uid is what made the gap look like 5 bytes when
        it was 38."""
        keyframe_b = len(cot_codec.encode(self._pli(),
                                          cot_codec.RNS_MAX_PAYLOAD,
                                          cot_codec.SaTx()))
        self.assertGreater(keyframe_b, 35, "a real uid should still cost ~41 B")
        self.assertLessEqual(keyframe_b, FM_MAX_PAYLOAD)

    def test_so_a_position_is_one_burst(self):
        tx = cot_codec.SaTx()
        packets = cot_codec.build_packets(self._pli(), FM_MAX_PAYLOAD, tx)
        self.assertEqual(len(packets), 1)
        self.assertAlmostEqual(len(packets) * fm_link.burst_duration_s(),
                               2.95, delta=0.01)

    def test_and_the_session_advances_so_differences_follow(self):
        """The compounding half, now working: the session commits, so the next
        position is a 16 B difference rather than another keyframe."""
        tx = cot_codec.SaTx()
        cot_codec.build_packets(self._pli(), FM_MAX_PAYLOAD, tx)
        self.assertFalse(tx.forced, "the session did not commit")
        second = cot_codec.build_packets(self._pli(lat=35.001),
                                         FM_MAX_PAYLOAD, tx)
        self.assertEqual(len(second), 1)
        self.assertLess(len(second[0]), 20, "not a delta -- too big")

    def test_the_smaller_code_could_not_have_done_either(self):
        """The comparison, so the 0.6 s the switch costs stays justified by
        something checkable rather than by a docstring."""
        small = fm_config.link_config("fast", codename="H_256_768_22",
                                      repeats=2)
        small_budget = small.capacity_bytes() - mesh.OVERHEAD
        tx = cot_codec.SaTx()
        packets = cot_codec.build_packets(self._pli(), small_budget, tx)
        self.assertGreater(len(packets), 5)
        self.assertTrue(tx.forced, "the session should have aborted")
        self.assertGreater(len(packets) * small.burst_duration_s(), 20.0)

    def test_a_position_gets_its_quick_extra_try(self):
        """Which the multi-burst fallback was explicitly denied, because four
        attempts at 26 s each is 104 s on a 90 s heartbeat cycle. At one burst it
        is affordable again."""
        rns_bridge._handle_outbound_cot(self._pli())
        scheduled = (
            len(rns_bridge._fm_position_resend.snapshot()["inflight"])
            + len(rns_bridge._fm_keyframe_resend.snapshot()["inflight"]))
        self.assertEqual(scheduled, 1)

    def test_and_the_beacon_piggyback_slot_can_be_filled(self):
        """It takes a single-packet DELTA, so at the old capacity it was never
        filled -- the one position mechanism cheap enough for this link was also
        unavailable.

        The session is warmed through cot_codec directly rather than with a
        second _handle_outbound_cot call: tx_policy throttles a position by
        ELAPSED TIME, so two sends in the same instant would see the second
        suppressed and this would fail for a reason that has nothing to do with
        the piggyback. The warm-up is what the first real heartbeat does, and
        a keyframe never rides a beacon anyway.
        """
        cot_codec.build_packets(self._pli(), FM_MAX_PAYLOAD, rns_bridge._sa_tx)
        rns_bridge._fm_last_position_delta = None
        rns_bridge._handle_outbound_cot(self._pli(lat=35.001))
        self.assertIsNotNone(rns_bridge._fm_last_position_delta)

    def test_the_airtime_warning_no_longer_fires_on_ordinary_traffic(self):
        """The guard stays, because nothing guarantees every position fits -- a
        jump plus a staleness change still produces an absolute frame, and a
        future code change could shrink the budget again. It just should not fire
        on a routine heartbeat any more."""
        warned = []
        orig = rns_bridge._fm_position_airtime_warning
        try:
            rns_bridge._fm_position_airtime_warning = (
                lambda summary, packets: warned.append(len(packets)))
            rns_bridge._handle_outbound_cot(self._pli())
        finally:
            rns_bridge._fm_position_airtime_warning = orig
        self.assertEqual(warned, [])


class TestFmBlindResendSurvivesTheChannel(unittest.TestCase):
    """The end-to-end property the whole design rests on, exercised through
    the real codec: four blind copies of one message must render ONCE, and
    fragments lost on some attempts must still combine across them.

    Nothing else covers this -- the scheduler tests know nothing about the
    codec, and the codec tests know nothing about blind resending.

    THE PAYLOAD HAS TO FRAGMENT, or the accumulation half of this class tests
    nothing. A short `<event><chat/></event>` fragmented at the 32 B code's 17 B
    budget and stopped doing so at 113 B, which turned two of these tests into
    no-ops. So the message is sized FROM the budget rather than written out, and
    the first test below is the guard that says so out loud.
    """

    def setUp(self):
        self._orig_transport = rns_bridge._transport
        self._orig_active_link = rns_bridge._active_link
        self._orig_max_payload = rns_bridge._max_payload
        self._orig_resend = rns_bridge._fm_resend
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM
        rns_bridge._max_payload = FM_MAX_PAYLOAD
        rns_bridge._fm_resend = fm_resend.FmResend(attempts=4, interval_s=0.0,
                                                   jitter_frac=0.0,
                                                   first_retry_s=0.0,
                                                   first_retry_jitter_s=0.0)

    def tearDown(self):
        rns_bridge._transport = self._orig_transport
        rns_bridge._active_link = self._orig_active_link
        rns_bridge._max_payload = self._orig_max_payload
        rns_bridge._fm_resend = self._orig_resend

    def _summary(self):
        class _S(object):
            type = "b-t-f"
            uid = "CHAT-1"
            is_position = False
        return _S()

    def _attempts(self):
        """The packets as they actually go on the air: attempt 1 from the
        build, the rest from the scheduler."""
        first = rns_bridge._build_fm_oneshot(self._multi_fragment_event(),
                                             self._summary())
        out = [first]
        for _ in range(rns_bridge._fm_resend.attempts - 1):
            out.extend(p for _, p, _, _ in rns_bridge._fm_resend.tick())
        return out

    def _multi_fragment_event(self):
        """A chat message whose compact encoding needs several fragments at this
        link's budget. Sized FROM the budget so it stays multi-fragment if the
        budget changes again -- the remarks body is the only field here that can
        be made arbitrarily long without confusing the codec.

        Hex digits rather than one repeated letter: a run of "x" survived only
        while it went as zlib'd XML, and the generic elements put it on the
        compact path, where the dictionary squeeze folds it into one fragment."""
        body = "".join(hashlib.sha256(b"%d" % i).hexdigest()
                       for i in range(6 * FM_MAX_PAYLOAD // 64 + 1))
        return (
            '<event version="2.0" uid="GeoChat.A.B.msgid-frag" type="b-t-f" '
            'time="2026-08-04T15:21:32.000Z" start="2026-08-04T15:21:32.000Z" '
            'stale="2026-08-04T15:22:32.000Z" how="h-g-i-g-o">'
            '<point lat="20.8175" lon="-140.5133" hae="381.0" ce="9.5" le="9999999.0"/>'
            '<detail><remarks>%s</remarks></detail></event>' % body
        ).encode("utf-8")

    def test_the_test_message_really_does_fragment(self):
        """The guard. Both accumulation tests below are vacuous on a
        single-fragment message, and that is exactly what happened when the
        operating point's budget grew from 17 B to 113 B."""
        self.assertGreater(len(self._attempts()[0]), 1)

    def _receive(self, attempts, drop=lambda attempt, i: False, window_s=None):
        """Feed attempts through a receiver, with the clock advancing by a
        real resend interval between them, returning how many times a whole
        message was delivered upward.

        The clock matters: both receive-side windows are time-based, and a
        test that fed four attempts inside one microsecond would pass no
        matter how badly they were sized.
        """
        if window_s is None:
            window_s = fm_resend.span_s() * 1.25
        now = [1000.0]
        clock = lambda: now[0]
        reasm = cot_codec.Reassembler(timeout=window_s, clock=clock)
        rx = reliable.ReliableRx(ttl_s=window_s, clock=clock)
        delivered = 0
        for n, packets in enumerate(attempts):
            if n:
                now[0] += fm_resend.DEFAULT_INTERVAL_S
            for i, pkt in enumerate(packets):
                if drop(n, i):
                    continue
                whole = reasm.offer(pkt, sender="peer")
                if whole is None:
                    continue
                w = cot_codec.unwrap_reliable(whole, "peer")
                if rx.should_deliver(w.origin, w.msg_id):
                    delivered += 1
        return delivered

    def test_four_blind_copies_render_the_message_exactly_once(self):
        # Without duplicate suppression this is 4, i.e. a chat message on
        # screen four times -- the regression blind resending would
        # otherwise introduce.
        self.assertEqual(self._receive(self._attempts()), 1)

    def test_every_attempt_carries_the_same_message_id(self):
        # What makes the dedup above and the accumulation below possible.
        ids = set()
        for packets in self._attempts():
            for pkt in packets:
                # fragment() lays a packet out as CTRL_FRAG, then
                # >HBB of (msg_id, index, count).
                if pkt and pkt[0] == cot_codec.CTRL_FRAG:
                    ids.add(struct.unpack(">H", pkt[1:3])[0])
        self.assertEqual(len(ids), 1)

    def test_a_message_still_arrives_when_no_single_attempt_gets_through(self):
        # The real point of spacing attempts out. Each attempt loses a
        # DIFFERENT fragment, so no attempt is complete on its own and a
        # receiver that did not accumulate across them would deliver
        # nothing at all.
        attempts = self._attempts()
        self.assertGreater(len(attempts[0]), 1,
                           "need a multi-fragment message to test this")
        self.assertEqual(
            self._receive(attempts, drop=lambda a, i: i == a % len(attempts[0])),
            1)

    def test_the_duplicate_window_being_too_short_is_what_renders_it_twice(self):
        # The counter-test that makes the sizing in start() load-bearing
        # rather than decorative: with a window shorter than the gap
        # between resends, the same several blind attempts spread over time
        # are delivered more than once -- a chat message appearing
        # repeatedly on the far screen.
        #
        # 2026-09-20: this used to reach the failure using reliable.py's OWN
        # stock default (300s, sized for ARQ finishing in ~16s) -- true
        # while DEFAULT_INTERVAL_S was 240s (3 gaps of it = 720s >> 300s).
        # Shortening it to 60s (explicit user request) shrank the real
        # schedule's own gap span (3 * 60s = 180s) below that same 300s
        # default, so the default alone no longer reproduces this failure
        # (see the sibling fact pinned in TestFmReceiveWindowsCoverTheResendSpan).
        # A deliberately-too-short window stands in for it here instead,
        # keeping this test about the mechanism rather than a constant that
        # has since moved.
        self.assertGreater(
            self._receive(self._attempts(), window_s=30.0),
            1)

    def test_the_reassembly_window_being_too_short_loses_the_message(self):
        # The other half: with a window shorter than the gap between
        # attempts, fragments stop accumulating and a message that no single
        # attempt delivers whole is lost entirely.
        attempts = self._attempts()
        self.assertEqual(
            self._receive(attempts,
                          drop=lambda a, i: i == a % len(attempts[0]),
                          window_s=fm_resend.DEFAULT_INTERVAL_S / 2.0),
            0)

class TestTheHeardDigestIsWiredEndToEnd(unittest.TestCase):
    """THE PHASE 4 DELIVERABLE, and the one thing two phases of work left
    dangling: fm_resend.note_heard() shipped before the teardown, was restored
    intact in Phase 2a with the cancel path working, and had NO CALLER. The
    pieces either side of it -- cot_codec.heard_tag(), encode_beacon's `heard`
    field, decode_beacon reading it back -- all survived and are covered above.
    What is covered HERE is the bridge actually joining them up:

        _handle_frame records a tag  ->  _take_fm_beacon_extras hands it to the
        beacon  ->  the far end's _handle_beacon cancels its schedule.

    Every link in that chain is in rns_bridge, and a break anywhere in it is
    silent: the schedule simply runs to completion, which is also what happens
    when the report legitimately never arrives.
    """

    def setUp(self):
        self._orig = dict(
            heard=list(rns_bridge._fm_heard),
            resend=rns_bridge._fm_resend,
            last_delta=rns_bridge._fm_last_position_delta,
        )
        rns_bridge._fm_heard.clear()
        rns_bridge._fm_resend = fm_resend.FmResend(interval_s=100.0,
                                                   jitter_frac=0.0)
        rns_bridge._fm_last_position_delta = None

    def tearDown(self):
        rns_bridge._fm_heard.clear()
        rns_bridge._fm_heard.extend(self._orig["heard"])
        rns_bridge._fm_resend = self._orig["resend"]
        rns_bridge._fm_last_position_delta = self._orig["last_delta"]

    SENDER = "aabbccddeeff"

    def _reliable_frame(self, msg_id):
        """A real reliable-wrapped frame, as it arrives off the air."""
        encoded = cot_codec.encode(DIRECT_CHAT_XML,
                                   cot_codec.RNS_MAX_PAYLOAD - mesh.OVERHEAD)
        _, wrapped = cot_codec.wrap_reliable(encoded, self.SENDER, msg_id,
                                            any_ack=False, addressee=None)
        return wrapped

    # -- the receiving half -------------------------------------------------

    def test_a_frame_heard_on_fm_is_tagged_for_the_next_beacon(self):
        rns_bridge._handle_frame(self._reliable_frame(11), None, None,
                                 self.SENDER, link=rns_bridge.LINK_FM)
        self.assertIn(cot_codec.heard_tag(self.SENDER, 11),
                      list(rns_bridge._fm_heard))

    def test_a_duplicate_is_tagged_again_rather_than_dropped(self):
        """Deliberately recorded BEFORE the dedup check returns: hearing the
        same message twice means the sender has not learned we got it, so our
        report is exactly what is missing."""
        frame = self._reliable_frame(12)
        rns_bridge._handle_frame(frame, None, None, self.SENDER,
                                 link=rns_bridge.LINK_FM)
        rns_bridge._fm_heard.clear()
        rns_bridge._handle_frame(frame, None, None, self.SENDER,
                                 link=rns_bridge.LINK_FM)
        self.assertIn(cot_codec.heard_tag(self.SENDER, 12),
                      list(rns_bridge._fm_heard))

    def test_a_frame_heard_on_lora_is_not_tagged(self):
        """LoRa acks, so it has a real delivery signal and needs no digest --
        and the gate is on the FRAME's own link, not the node's default."""
        rns_bridge._handle_frame(self._reliable_frame(13), None, None,
                                 self.SENDER, link=rns_bridge.LINK_LORA)
        self.assertEqual(list(rns_bridge._fm_heard), [])

    def test_the_ring_holds_only_what_the_beacon_can_carry(self):
        """Bounded by what FITS, newest kept -- a sender's schedule runs for
        minutes while the beacon repeats every ~90 s, so the messages most likely
        to still be in flight are the newest."""
        for msg_id in range(rns_bridge._FM_HEARD_MAX + 3):
            rns_bridge._handle_frame(self._reliable_frame(msg_id), None, None,
                                     self.SENDER, link=rns_bridge.LINK_FM)
        self.assertEqual(len(rns_bridge._fm_heard), rns_bridge._FM_HEARD_MAX)
        newest = cot_codec.heard_tag(self.SENDER,
                                    rns_bridge._FM_HEARD_MAX + 2)
        self.assertIn(newest, list(rns_bridge._fm_heard))
        self.assertNotIn(cot_codec.heard_tag(self.SENDER, 0),
                         list(rns_bridge._fm_heard))

    # -- the beacon that carries it ----------------------------------------

    def test_the_beacon_extras_are_read_and_cleared(self):
        """Read-and-cleared, so a stashed position delta rides exactly one beacon
        rather than a stale one repeated indefinitely."""
        rns_bridge._handle_frame(self._reliable_frame(14), None, None,
                                 self.SENDER, link=rns_bridge.LINK_FM)
        rns_bridge._fm_last_position_delta = b"a delta"
        heard, delta = rns_bridge._take_fm_beacon_extras()
        self.assertEqual(heard, list(rns_bridge._fm_heard))
        self.assertEqual(delta, b"a delta")
        self.assertIsNone(rns_bridge._fm_last_position_delta)

    def test_a_digestless_beacon_carries_the_tags(self):
        """digest=None is what SELECTS the heard report, because only FM has both
        the need for one and the budget pressure that makes dropping the digest
        worth it. Asserted through the real encoder, since that substitution is
        the whole mechanism."""
        rns_bridge._handle_frame(self._reliable_frame(15), None, None,
                                 self.SENDER, link=rns_bridge.LINK_FM)
        tags = list(rns_bridge._fm_heard)
        frame = cot_codec.encode_beacon(
            rns_bridge._NODE_ID, 1, 0, callsign="", name="ab",
            digest=None, heard=tags)
        self.assertEqual(cot_codec.decode_beacon(frame).heard, tags)
        self.assertLessEqual(len(frame), FM_MAX_PAYLOAD)

    # -- the sending half, closing the loop --------------------------------

    def _schedule(self, msg_id, label="chat"):
        rns_bridge._fm_resend.register(
            msg_id, [b"pkt"], label,
            tag=cot_codec.heard_tag(rns_bridge._NODE_ID, msg_id))

    def _beacon_from_peer(self, tags, peer="bbccddeeff00"):
        return cot_codec.encode_beacon(peer, 1, 0, callsign="", name="ab",
                                       digest=None, heard=tags)

    def test_a_peers_report_cancels_our_schedule(self):
        self._schedule(21)
        frame = self._beacon_from_peer(
            [cot_codec.heard_tag(rns_bridge._NODE_ID, 21)])
        rns_bridge._handle_beacon(frame, None, None, "bbccddeeff00",
                                  link=rns_bridge.LINK_FM)
        self.assertEqual(rns_bridge._fm_resend.snapshot()["inflight"], [])
        self.assertEqual(rns_bridge._fm_resend.stats().get("confirmed"), 1)

    def test_a_report_of_somebody_elses_message_cancels_nothing(self):
        """The reason a tag is a hash of (origin, msg_id) rather than the id: ids
        are unique per SENDER only, and a false positive abandons a message that
        never arrived -- the exact failure the resends exist to prevent."""
        self._schedule(22)
        frame = self._beacon_from_peer([cot_codec.heard_tag("ffffffffffff", 22)])
        rns_bridge._handle_beacon(frame, None, None, "bbccddeeff00",
                                  link=rns_bridge.LINK_FM)
        self.assertEqual(
            len(rns_bridge._fm_resend.snapshot()["inflight"]), 1)

    def test_a_report_arriving_on_lora_cancels_nothing(self):
        """The digest is FM's mechanism. A beacon on LoRa confirms nothing about
        what crossed FM, and LoRa traffic has real acks of its own."""
        self._schedule(23)
        frame = self._beacon_from_peer(
            [cot_codec.heard_tag(rns_bridge._NODE_ID, 23)])
        rns_bridge._handle_beacon(frame, None, None, "bbccddeeff00",
                                  link=rns_bridge.LINK_LORA)
        self.assertEqual(
            len(rns_bridge._fm_resend.snapshot()["inflight"]), 1)

    def test_an_empty_report_is_not_an_error(self):
        """Every beacon this link ever sent before Phase 4 carried heard=[], and
        a node on an older build still does."""
        self._schedule(24)
        frame = self._beacon_from_peer([])
        rns_bridge._handle_beacon(frame, None, None, "bbccddeeff00",
                                  link=rns_bridge.LINK_FM)
        self.assertEqual(
            len(rns_bridge._fm_resend.snapshot()["inflight"]), 1)

    # -- what the beacon now has room for -------------------------------

    def test_a_readable_node_name_and_callsign_fit_alongside_the_tags(self):
        """WHAT THE 128 B CODE BOUGHT BACK. Through Phase 2a the beacon budget
        was 17 B, which left exactly 2 B for name and callsign COMBINED -- and
        that cap was applied to every link, so LoRa was paying it with ~452 B to
        spare. The worst-case beacon now carries the digest, three tags, a
        16-byte name and a 12-byte callsign in 51 B of 113."""
        import mesh
        worst = cot_codec.encode_beacon(
            rns_bridge._NODE_ID, 7,
            name="N" * rns_bridge._BEACON_NAME_MAX_B,
            callsign="C" * rns_bridge._BEACON_CALLSIGN_MAX_B,
            heard=[cot_codec.heard_tag("abcdef12", i)
                   for i in range(rns_bridge._FM_HEARD_MAX)],
            digest=bytes(range(8)))
        budget = fm_link.hw_mtu() - mesh.OVERHEAD
        self.assertLessEqual(len(worst), budget)
        # With real headroom, not barely. A 37 B beacon against a 30 B code once
        # took the whole FM interface offline on its first live send, so the
        # failure mode here is a raise rather than a degradation.
        self.assertLess(len(worst), budget // 2)

    def test_the_caps_would_still_overflow_the_code_that_forced_them(self):
        """Kept so the comment explaining why the caps were ever 2+0 stays
        checkable rather than becoming folklore: at the old 17 B budget this
        same beacon does not fit."""
        import mesh
        import fm_config
        worst = cot_codec.encode_beacon(
            rns_bridge._NODE_ID, 7,
            name="N" * rns_bridge._BEACON_NAME_MAX_B,
            callsign="C" * rns_bridge._BEACON_CALLSIGN_MAX_B,
            heard=[cot_codec.heard_tag("abcdef12", i)
                   for i in range(rns_bridge._FM_HEARD_MAX)],
            digest=bytes(range(8)))
        small = fm_config.link_config("fast", codename="H_256_768_22",
                                      repeats=2)
        self.assertGreater(len(worst), small.capacity_bytes() - mesh.OVERHEAD)

    def test_the_digest_and_the_heard_report_are_separate_arguments(self):
        """They used to be one: `digest=None` both omitted the digest and
        SELECTED the heard report, because at 17 B they could not coexist. At
        113 B they can, and leaving them coupled would be a quiet trap --
        passing a digest on FM would silently drop the heard report, and the
        only symptom would be resend schedules that never cancel, which looks
        exactly like a report that never arrived."""
        import inspect
        sig = inspect.signature(rns_bridge._send_one_beacon)
        self.assertIn("fm_extras", sig.parameters)
        self.assertIn("digest", sig.parameters)

    def test_the_whole_loop_closes_through_both_halves_of_the_bridge(self):
        """RECEIVE SIDE AND SEND SIDE AGAINST EACH OTHER, which is the only test
        that would catch the two ends tagging the same message differently -- the
        failure mode where the feature is silently dead and every other test
        here still passes."""
        # This node schedules a message...
        msg_id = 25
        self._schedule(msg_id, label="the one that matters")
        # ...the far end hears it, and tags it the way _handle_frame does.
        far_end_heard = []
        # The far end's sender is US, so its tag is built from our node id.
        wrapper = cot_codec.unwrap_reliable(
            cot_codec.wrap_reliable(
                cot_codec.encode(DIRECT_CHAT_XML,
                                 cot_codec.RNS_MAX_PAYLOAD - mesh.OVERHEAD),
                rns_bridge._NODE_ID, msg_id,
                any_ack=False, addressee=None)[1],
            rns_bridge._NODE_ID)
        far_end_heard.append(cot_codec.heard_tag(wrapper.origin,
                                                wrapper.msg_id))
        # ...and it rides the far end's next beacon back to us.
        rns_bridge._handle_beacon(self._beacon_from_peer(far_end_heard),
                                  None, None, "bbccddeeff00",
                                  link=rns_bridge.LINK_FM)
        self.assertEqual(rns_bridge._fm_resend.snapshot()["inflight"], [])


class TestEarlyConfirmBeacon(unittest.TestCase):
    """The receiver does not wait for its scheduled beacon to say "I got it".

    Measured 2026-10-12 (four markers, Pixel -> OnePlus, all four received):
    confirmation rode the receiver's next scheduled beacon, which for three of
    them reached the sender only after all five attempts had been spent. A
    reliable FM arrival now asks for a beacon 1-3 s later. What is tested is the
    CHAIN -- arrival -> timer -> beacon built at that moment -> the sender's
    schedule cancelling -- because a break anywhere in it is silent: the
    resends just run to completion, which is also what a lost report looks like.
    """

    SENDER = "aabbccddeeff"

    class _FakeTimer(object):
        made = []

        def __init__(self, delay, fn):
            self.delay, self.fn = delay, fn
            self.daemon = False
            self.started = False
            type(self).made.append(self)

        def start(self):
            self.started = True

        def fire(self):
            self.fn()

    def setUp(self):
        rb = rns_bridge
        self._orig = dict(
            heard=list(rb._fm_heard), resend=rb._fm_resend,
            timer=rb._confirm_timer, last=rb._last_confirm_at,
            clock=rb._confirm_clock, factory=rb._confirm_timer_factory,
            send=rb._send_one_beacon, enabled=rb._beacon_enabled,
            dest=rb._destination, sweep=rb._sweep_active, seq=rb._beacon_seq,
        )
        # An arriving chat is injected, which records who it came from in these
        # tables. They are shared module state and other classes assume them
        # empty, so what this class adds is taken back out in tearDown.
        self._tables = {n: (getattr(rb, n), type(getattr(rb, n))(getattr(rb, n)))
                        for n in ("_atak_uid_to_node", "_last_marker", "_sa_rx")}
        # Same for the objects an arrival feeds: the outbound policy remembers
        # what was injected so it is not echoed back out (so a later test that
        # SENDS the same chat finds it suppressed), the receiver remembers
        # message ids, and the range test keeps the sender's fix. Their
        # internals are put back rather than the objects replaced, so anything
        # holding a reference still holds the live one.
        self._objects = {n: (getattr(rb, n), copy.deepcopy(vars(getattr(rb, n))))
                         for n in ("_tx_policy", "_reliable_rx", "_rangetest")}
        self._now = [1000.0]
        self._FakeTimer.made = []
        self.sent = []

        def fake_send(link, seq, digest, note, fm_extras=False):
            heard, _ = rb._take_fm_beacon_extras() if fm_extras else (None, None)
            self.sent.append(dict(link=link, seq=seq, digest=digest,
                                  note=note, fm_extras=fm_extras, heard=heard))
            return b"frame", b"sealed"

        rb._fm_heard.clear()
        rb._fm_resend = fm_resend.FmResend(interval_s=100.0, jitter_frac=0.0)
        rb._confirm_timer = None
        rb._last_confirm_at = None
        rb._confirm_clock = lambda: self._now[0]
        rb._confirm_timer_factory = self._FakeTimer
        rb._send_one_beacon = fake_send
        rb._beacon_enabled = True
        rb._destination = object()
        rb._sweep_active = False

    def tearDown(self):
        rb = rns_bridge
        rb._fm_heard.clear()
        rb._fm_heard.extend(self._orig["heard"])
        rb._fm_resend = self._orig["resend"]
        rb._confirm_timer = self._orig["timer"]
        rb._last_confirm_at = self._orig["last"]
        rb._confirm_clock = self._orig["clock"]
        rb._confirm_timer_factory = self._orig["factory"]
        rb._send_one_beacon = self._orig["send"]
        rb._beacon_enabled = self._orig["enabled"]
        rb._destination = self._orig["dest"]
        rb._sweep_active = self._orig["sweep"]
        rb._beacon_seq = self._orig["seq"]
        for obj, saved in self._objects.values():
            vars(obj).clear()
            vars(obj).update(saved)
        for live, saved in self._tables.values():
            live.clear()
            if isinstance(live, dict):
                live.update(saved)
            elif hasattr(live, "extend"):
                live.extend(saved)
            else:
                live.update(saved)

    def _frame(self, msg_id):
        encoded = cot_codec.encode(DIRECT_CHAT_XML,
                                   cot_codec.RNS_MAX_PAYLOAD - mesh.OVERHEAD)
        _, wrapped = cot_codec.wrap_reliable(encoded, self.SENDER, msg_id,
                                            any_ack=False, addressee=None)
        return wrapped

    def _arrive(self, msg_id, link=None):
        rns_bridge._handle_frame(self._frame(msg_id), None, None, self.SENDER,
                                 link=link or rns_bridge.LINK_FM)

    # -- scheduling ----------------------------------------------------------

    def test_a_reliable_arrival_on_fm_schedules_a_beacon_in_one_to_three_seconds(self):
        self._arrive(1)
        self.assertEqual(len(self._FakeTimer.made), 1)
        t = self._FakeTimer.made[0]
        self.assertTrue(t.started)
        self.assertTrue(t.daemon)
        lo, hi = rns_bridge._CONFIRM_BEACON_DELAY_S
        self.assertTrue(lo <= t.delay <= hi, t.delay)
        self.assertEqual(self.sent, [])  # not before the timer fires

    def test_an_arrival_on_lora_schedules_nothing(self):
        """LoRa acks, so it has a delivery signal of its own."""
        self._arrive(2, link=rns_bridge.LINK_LORA)
        self.assertEqual(self._FakeTimer.made, [])

    def test_arrivals_while_one_is_pending_share_it(self):
        """A two-packet message and its duplicates must cost ONE beacon."""
        for i in (3, 4, 5):
            self._arrive(i)
        self.assertEqual(len(self._FakeTimer.made), 1)

    def test_a_disabled_beacon_schedules_nothing(self):
        rns_bridge._beacon_enabled = False
        self._arrive(6)
        self.assertEqual(self._FakeTimer.made, [])

    # -- what it sends -------------------------------------------------------

    def test_the_beacon_goes_out_on_fm_carrying_the_report_built_at_fire_time(self):
        self._arrive(7)
        # A second message lands AFTER the timer was set, before it fires. The
        # regular beacon's list is frozen when that one is built; this one
        # must name both.
        self._arrive(8)
        self._FakeTimer.made[0].fire()
        self.assertEqual(len(self.sent), 1)
        b = self.sent[0]
        self.assertEqual(b["link"], rns_bridge.LINK_FM)
        self.assertTrue(b["fm_extras"])
        self.assertIsNone(b["digest"])
        self.assertIn("confirm", b["note"])
        self.assertEqual(
            set(b["heard"]),
            {cot_codec.heard_tag(self.SENDER, 7),
             cot_codec.heard_tag(self.SENDER, 8)})

    def test_firing_frees_the_slot_for_the_next_request(self):
        self._arrive(9)
        self._FakeTimer.made[0].fire()
        self._now[0] += 100.0
        self._arrive(10)
        self.assertEqual(len(self._FakeTimer.made), 2)

    def test_a_duplicate_asks_again(self):
        """Hearing it twice means the sender has not learned we have it."""
        self._arrive(11)
        self._FakeTimer.made[0].fire()
        self._now[0] += 100.0
        self._arrive(11)
        self.assertEqual(len(self._FakeTimer.made), 2)

    def test_a_request_inside_the_minimum_gap_is_deferred_not_dropped(self):
        self._arrive(12)
        self._FakeTimer.made[0].fire()
        self._now[0] += 5.0  # 15 s of the 20 s gap still to run
        self._arrive(13)
        self.assertEqual(len(self._FakeTimer.made), 2)
        lo, _ = rns_bridge._CONFIRM_BEACON_DELAY_S
        self.assertGreaterEqual(self._FakeTimer.made[1].delay, 15.0 + lo - 1e-9)

    def test_a_sweep_in_progress_sends_nothing(self):
        self._arrive(14)
        rns_bridge._sweep_active = True
        self._FakeTimer.made[0].fire()
        self.assertEqual(self.sent, [])

    def test_a_failing_send_does_not_wedge_later_requests(self):
        def boom(*a, **k):
            raise RuntimeError("radio fell over")
        rns_bridge._send_one_beacon = boom
        self._arrive(15)
        self._FakeTimer.made[0].fire()          # must not raise
        self._now[0] += 100.0
        self._arrive(16)
        self.assertEqual(len(self._FakeTimer.made), 2)

    # -- closing the loop ----------------------------------------------------

    def test_the_senders_schedule_cancels_on_the_early_beacon(self):
        """Receiver half: arrival -> early beacon. Sender half: that beacon's
        tags -> the schedule stops. Joined through the real encoder/decoder."""
        sender_resend = fm_resend.FmResend(interval_s=100.0, jitter_frac=0.0)
        tag = cot_codec.heard_tag(self.SENDER, 21)
        sender_resend.register(21, [b"pkt"], "marker", tag=tag)
        self.assertEqual(len(sender_resend.snapshot()["inflight"]), 1)

        self._arrive(21)
        self._FakeTimer.made[0].fire()
        wire = cot_codec.encode_beacon("bbccddeeff00", 1, 0, callsign="",
                                       name="ab", digest=None,
                                       heard=self.sent[0]["heard"])
        confirmed = sender_resend.note_heard(cot_codec.decode_beacon(wire).heard)
        self.assertTrue(confirmed)
        self.assertEqual(sender_resend.snapshot()["inflight"], [])

    # -- the sequence counter ------------------------------------------------

    def test_two_threads_never_share_a_beacon_sequence_number(self):
        """The beacon loop and the confirm timer both number beacons. The
        interpreter is told to switch threads every microsecond so a missing
        lock shows up here; at the default interval the race is real but rarely
        lands inside a test this short."""
        import threading
        got, lock = [], threading.Lock()

        def run():
            for _ in range(3000):
                n = rns_bridge._next_beacon_seq()
                with lock:
                    got.append(n)

        old = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            ts = [threading.Thread(target=run) for _ in range(6)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        finally:
            sys.setswitchinterval(old)
        self.assertEqual(len(got), 18000)
        self.assertEqual(len(set(got)), 18000)


class TestPiggybackedDeltaRoundTripsForReal(unittest.TestCase):
    """The two halves proven separately elsewhere (sender stashes a real
    delta; _handle_beacon dispatches a piggybacked one to the real decode
    path) -- this is the one test that the ACTUAL BYTES cross both without
    a mock in between: a real _handle_outbound_cot() build, riding a real
    encode_beacon(), decoded by cot_codec.decode() against a receiver
    session that already holds the matching binding."""

    def test_a_stashed_delta_decodes_correctly_on_the_other_end(self):
        sender = cot_codec.SaTx()
        # Establish the binding first, exactly as a real keyframe would --
        # a delta alone is meaningless without it. Not taken from
        # _fm_last_position_delta: a keyframe is never stashed there (see
        # test_a_keyframe_is_never_stashed_for_a_beacon).
        keyframe = cot_codec.build_packets(
            _pli_xml("ANDROID-e2e", lat=35.0, lon=-79.0), SA_CAPABLE_PAYLOAD,
            sender)
        receiver = cot_codec.SaRx()
        cot_codec.decode(keyframe[0], receiver, 600)

        # Now a real delta, via the real production call path.
        orig_transport, orig_max_payload = rns_bridge._transport, rns_bridge._max_payload
        orig_sa_tx, orig_last_delta = rns_bridge._sa_tx, rns_bridge._fm_last_position_delta
        orig_pos_resend = rns_bridge._fm_position_resend
        orig_queue = rns_bridge._tx_queue
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM
        rns_bridge._max_payload = SA_CAPABLE_PAYLOAD
        rns_bridge._sa_tx = sender
        rns_bridge._fm_position_resend = fm_resend.FmResend(attempts=2)
        rns_bridge._tx_queue = rns_bridge.queue.Queue(maxsize=32)
        try:
            rns_bridge._handle_outbound_cot(
                _pli_xml("ANDROID-e2e", lat=35.001, lon=-79.001))
            delta = rns_bridge._fm_last_position_delta
        finally:
            rns_bridge._transport = orig_transport
            rns_bridge._max_payload = orig_max_payload
            rns_bridge._sa_tx = orig_sa_tx
            rns_bridge._fm_last_position_delta = orig_last_delta
            rns_bridge._fm_position_resend = orig_pos_resend
            rns_bridge._tx_queue = orig_queue

        self.assertIsNotNone(delta)

        # Ride a real beacon, both ways.
        beacon = cot_codec.encode_beacon("bbbbbb", 5, position=delta)
        decoded_beacon = cot_codec.decode_beacon(beacon, node_id="bbbbbb")

        # And decode on the far end, against the SAME receiver session the
        # keyframe bound -- exactly what _decode_and_deliver_compact does.
        xml = cot_codec.decode(decoded_beacon.position, receiver, 600)
        summary = cot_codec.summarize(xml)
        self.assertAlmostEqual(summary.lat, 35.001, places=4)
        self.assertAlmostEqual(summary.lon, -79.001, places=4)
        self.assertEqual(summary.uid, "ANDROID-e2e")

class TestBeaconDeliversAPiggybackedPosition(unittest.TestCase):
    """2026-09-22: _handle_beacon()'s receive-side half of the piggyback
    feature. The send-side half (stashing a delta for the beacon loop to
    attach) is covered in TestFmPositionGetsOneQuickExtraTry; this is the
    other end, arriving as a real beacon.

    Spies on _decode_and_deliver_compact rather than standing up the whole
    ATAK-socket pipeline (nothing in this file does; _send_to_atak has no
    existing test harness to build on) -- what matters here is the WIRING:
    a beacon carrying a position hands it to the exact same decode-and-
    deliver path a normal inbound position frame uses, with the right
    sender, and a bad one does not take the beacon's own identity/heard
    processing down with it."""

    def setUp(self):
        self._orig = rns_bridge._decode_and_deliver_compact
        self.calls = []

    def tearDown(self):
        rns_bridge._decode_and_deliver_compact = self._orig

    def _spy(self, outcome=None):
        def spy(frame, sender, wrapper=None):
            self.calls.append((frame, sender, wrapper))
            if outcome is not None:
                raise outcome
        rns_bridge._decode_and_deliver_compact = spy

    def test_a_piggybacked_position_is_decoded_and_delivered(self):
        self._spy()
        payload = b"\x11\x02\xab\xcd"
        frame = cot_codec.encode_beacon("aaaaaa", 1, position=payload)
        rns_bridge._handle_beacon(frame, None, None, "aaaaaa")
        self.assertEqual(self.calls, [(payload, "aaaaaa", None)])

    def test_a_beacon_with_nothing_to_piggyback_calls_nothing(self):
        self._spy()
        frame = cot_codec.encode_beacon("aaaaaa", 1, callsign="X")
        rns_bridge._handle_beacon(frame, None, None, "aaaaaa")
        self.assertEqual(self.calls, [])

    def test_a_piggyback_that_fails_to_decode_does_not_raise(self):
        # The beacon's own job (identity, heard report) already succeeded
        # by the time this runs -- a garbled extra passenger must not turn
        # that into a dropped beacon.
        self._spy(outcome=cot_codec.CotDecodeError("garbage"))
        frame = cot_codec.encode_beacon("aaaaaa", 1, position=b"\x11\xff")
        rns_bridge._handle_beacon(frame, None, None, "aaaaaa")  # must not raise
        self.assertEqual(len(self.calls), 1)

    def test_a_codec_bug_decoding_the_piggyback_does_not_raise_either(self):
        self._spy(outcome=RuntimeError("codec bug"))
        frame = cot_codec.encode_beacon("aaaaaa", 1, position=b"\x11\xff")
        rns_bridge._handle_beacon(frame, None, None, "aaaaaa")  # must not raise


class TestFmHeardReportFitsAndWorks(unittest.TestCase):
    """The beacon-borne delivery report, and the caller fm_resend.note_heard()
    waited two phases for.

    THE CARRIAGE WAS THE BINDING CONSTRAINT UNTIL 2026-10-02. At the 32 B code
    the beacon budget was 17 B, which three 2-byte tags consumed EXACTLY, and
    only by giving up the 8 B neighbour digest. At the 128 B code (see
    fm_config.DEFAULT_CODE) it is 113 B, so the tags, the digest and a readable
    node name all fit together. What is pinned here now is the headroom and the
    loop, not the ceiling.
    """

    def _beacon(self, heard, digest):
        return cot_codec.encode_beacon(
            rns_bridge._NODE_ID, 1, 0,
            callsign="C" * rns_bridge._BEACON_CALLSIGN_MAX_B,
            name="N" * rns_bridge._BEACON_NAME_MAX_B,
            digest=digest,
            heard=heard)

    def test_the_ring_size_is_now_a_choice_not_a_ceiling(self):
        """_FM_HEARD_MAX = 3 WAS arithmetic: three tags filled the 17 B beacon
        exactly and a fourth did not fit. At 113 B it is a judgement -- a
        sender's schedule runs for minutes while the beacon repeats every ~90 s,
        so the most recent few are the ones still worth confirming. Asserted as a
        choice so nobody re-derives it as a limit."""
        for n in (rns_bridge._FM_HEARD_MAX, 4, 8, 16):
            with self.subTest(tags=n):
                tags = [cot_codec.heard_tag("node%d" % i, i) for i in range(n)]
                self.assertLess(len(self._beacon(tags, None)), FM_MAX_PAYLOAD)

    def test_the_tags_fit_with_a_readable_name_and_callsign(self):
        tags = [cot_codec.heard_tag("node%d" % i, i)
                for i in range(rns_bridge._FM_HEARD_MAX)]
        self.assertLessEqual(len(self._beacon(tags, None)), FM_MAX_PAYLOAD)

    def test_the_digest_no_longer_has_to_be_given_up_for_them(self):
        """It did at the 32 B code -- carrying both overflowed the beacon, so
        dropping the digest on FM was forced. _beacon_tick still drops it,
        because relaying is LoRa-only and it buys nothing there, but that is now
        a decision about usefulness rather than about room."""
        tags = [cot_codec.heard_tag("node%d" % i, i) for i in range(3)]
        self.assertLess(len(self._beacon(tags, b"\x01" * 8)), FM_MAX_PAYLOAD)

    def test_tags_survive_the_round_trip(self):
        tags = [cot_codec.heard_tag("abc", 7), cot_codec.heard_tag("abc", 8)]
        got = cot_codec.decode_beacon(self._beacon(tags, None))
        self.assertEqual(got.heard, tags)

    def test_an_old_style_beacon_decodes_with_an_empty_report(self):
        # Backward compatibility in the direction that matters: a node on an
        # older build sends no heard field at all, and must still be read.
        got = cot_codec.decode_beacon(self._beacon(None, b"\x01" * 8))
        self.assertEqual(got.heard, [])
        self.assertEqual(got.digest, b"\x01" * 8)

    def test_two_senders_using_the_same_message_id_get_different_tags(self):
        # The whole reason a tag is a hash of (origin, msg_id) rather than
        # the id itself: a collision here would let one node's report
        # cancel another node's undelivered message.
        self.assertNotEqual(cot_codec.heard_tag("aaaa", 7),
                            cot_codec.heard_tag("bbbb", 7))

    def test_the_same_message_always_tags_identically(self):
        # And the other half -- a sender must recognise its own message
        # coming back, across processes and restarts.
        self.assertEqual(cot_codec.heard_tag("aaaa", 7),
                         cot_codec.heard_tag("aaaa", 7))

    def test_the_whole_loop_closes_from_sender_to_beacon_and_back(self):
        """Sender schedules -> receiver hears it and tags it -> the tag rides
        the receiver's next beacon -> the sender stops resending. Each half
        is covered elsewhere; this is the only test that they agree on the
        tag, which is the one thing that has to match across two nodes."""
        sender_id = "aaaaaa"
        eng = fm_resend.FmResend(interval_s=100.0, jitter_frac=0.0)
        msg_id = eng.next_msg_id()

        # Sender side: what _build_fm_oneshot does.
        eng.register(msg_id, [b"pkt"], "chat",
                     tag=cot_codec.heard_tag(sender_id, msg_id))

        # Receiver side: what _handle_frame records, then what the beacon
        # loop puts on the air.
        received_tag = cot_codec.heard_tag(sender_id, msg_id)
        beacon = cot_codec.encode_beacon(
            "bbbbbb", 1, 0, callsign="", name="ab",
            digest=None, heard=[received_tag])

        # Back at the sender: what _handle_beacon does with it.
        decoded = cot_codec.decode_beacon(beacon, node_id="bbbbbb")
        self.assertEqual(eng.note_heard(decoded.heard), ["chat"])
        self.assertEqual(eng.snapshot()["inflight"], [])

    @unittest.skipUnless(mesh.HAVE_AEAD, "no AEAD backend")
    def test_the_two_ends_tag_the_same_message_identically_through_mesh(self):
        """The linchpin, and the one thing the loop test above CANNOT check
        because it uses the same literal id on both sides.

        The sender tags with _NODE_ID -- a hex string. The receiver tags
        with the mesh envelope's authenticated origin, which travels as
        RAW BYTES and is converted back by mesh.node_id_hex(). heard_tag()
        hashes a formatted string, so any difference in type, case or
        length between those two representations yields tags that never
        match: the feature would be silently dead while every other test
        passed. Verified through a real wrap/unwrap rather than by reading
        the conversion.
        """
        node_id = rns_bridge._NODE_ID
        key = mesh.derive_key("bench passphrase")
        tx = mesh.Mesh(node_id, key)
        rx = mesh.Mesh("ffffff", key)

        msg_id = 4242
        _, wrapped = cot_codec.wrap_reliable(b"payload", node_id, msg_id,
                                             any_ack=False, addressee=None)
        inbound = rx.unwrap(tx.wrap(wrapped))
        sender = mesh.node_id_hex(inbound.origin)
        w = cot_codec.unwrap_reliable(inbound.frame, sender)

        self.assertEqual(cot_codec.heard_tag(node_id, msg_id),
                         cot_codec.heard_tag(w.origin, w.msg_id))

    def test_the_loop_does_not_close_on_a_different_senders_message(self):
        # Same shape, but the receiver heard somebody ELSE's message that
        # happens to carry the same id. Nothing may be cancelled.
        eng = fm_resend.FmResend(interval_s=100.0, jitter_frac=0.0)
        msg_id = eng.next_msg_id()
        eng.register(msg_id, [b"pkt"], "chat",
                     tag=cot_codec.heard_tag("aaaaaa", msg_id))
        beacon = cot_codec.encode_beacon(
            "bbbbbb", 1, 0, callsign="", name="ab", digest=None,
            heard=[cot_codec.heard_tag("cccccc", msg_id)])
        decoded = cot_codec.decode_beacon(beacon, node_id="bbbbbb")
        self.assertEqual(eng.note_heard(decoded.heard), [])
        self.assertEqual(len(eng.snapshot()["inflight"]), 1)

    def test_a_malformed_tail_does_not_lose_the_beacon(self):
        # The field rides a beacon that may be padded or truncated; losing
        # the peer's identity over a bad tail would be the wrong trade.
        frame = self._beacon([cot_codec.heard_tag("abc", 1)], None) + b"\x09"
        got = cot_codec.decode_beacon(frame)
        self.assertEqual(got.seq, 1)


class TestFmStatusBlock(unittest.TestCase):
    """What the status screen is given on FM. The screen's own honesty depends on
    these fields existing and saying what they say -- see fm_health.py and
    MainActivity's FM block."""

    def setUp(self):
        self._orig_transport = rns_bridge._transport
        self._orig_active_link = rns_bridge._active_link
        rns_bridge._transport = "fm"
        rns_bridge._active_link = rns_bridge.LINK_FM

    def tearDown(self):
        rns_bridge._transport = self._orig_transport
        rns_bridge._active_link = self._orig_active_link
        fm_link.set_profile(fm_config.DEFAULT_PROFILE)

    def test_it_is_json_serialisable(self):
        # get_status() must not raise on the UI thread's poller, and a block
        # that cannot be serialised would take the whole status payload down
        # with it, not just itself.
        import json
        json.dumps(rns_bridge._fm_status())

    def test_it_states_that_delivery_cannot_be_confirmed(self):
        # The single most important thing the FM screen has to say, and the one
        # a label is most likely to get wrong by omission: silence on this link
        # means nothing at all, so the UI must not imply delivery from the
        # absence of an error.
        self.assertIs(rns_bridge._fm_status()["delivery_confirmable"], False)

    def test_it_carries_a_health_reading(self):
        self.assertIn("state", rns_bridge._fm_status()["health"])

    def test_no_audio_yet_reads_as_down_not_as_a_silent_link(self):
        """fm_link.health_snapshot() returns None before any audio has been fed,
        which is NOT the same as a link that is hearing nothing -- the block has
        to say which."""
        fm_link.reset_rx()
        health = rns_bridge._fm_status()["health"]
        self.assertEqual(health["state"], "down")
        self.assertIn("no audio", health["summary"])

    def test_it_names_the_operating_point_in_effect(self):
        # The only FM setting whose mismatch produces no symptom but silence on
        # both ends, so the screen has to state it. Every number here must match
        # at the far end.
        block = rns_bridge._fm_status()
        self.assertEqual(block["profile"], fm_link.profile_name())
        self.assertIn(fm_link.config().codename, block["operating_point"])
        self.assertIn("both ends must match", block["operating_point"])
        self.assertEqual(block["capacity_bytes"], fm_link.hw_mtu())
        self.assertAlmostEqual(block["burst_s"], fm_link.burst_duration_s(),
                               places=2)

    def test_it_follows_a_profile_change(self):
        """Shows what is CARRYING TRAFFIC, not what Kotlin has stored -- between
        saving a profile change and the restart that lands it, the two differ."""
        fm_link.set_profile("wide")
        block = rns_bridge._fm_status()
        self.assertEqual(block["profile"], "wide")
        self.assertIs(block["profile_is_default"], False)
        fm_link.set_profile(fm_config.DEFAULT_PROFILE)
        self.assertIs(rns_bridge._fm_status()["profile_is_default"], True)

    def test_it_carries_the_placements_evidence(self):
        """An operator on handhelds running the desktop-measured default is on an
        unqualified placement, and this is the only place that says so."""
        self.assertEqual(rns_bridge._fm_status()["evidence"],
                         fm_link.evidence_line())

    def test_a_broken_health_reading_does_not_cost_the_operating_point(self):
        # Two separate try blocks in _fm_status() on purpose: the operating point
        # is the one thing on this screen that cannot be recovered from anywhere
        # else, so a failed health reading must not take it down as collateral.
        orig = fm_link.health_snapshot
        try:
            fm_link.health_snapshot = lambda: (_ for _ in ()).throw(
                RuntimeError("no reading"))
            block = rns_bridge._fm_status()
            self.assertEqual(block["health"]["state"], "unknown")
            self.assertEqual(block["profile"], fm_link.profile_name())
            self.assertIs(block["delivery_confirmable"], False)
        finally:
            fm_link.health_snapshot = orig

    def test_it_carries_the_resend_schedules(self):
        """All three of them, because one-shot, position and keyframe traffic get
        structurally different schedules and a screen showing only the first
        would under-report what the link is still trying to get out."""
        block = rns_bridge._fm_status()
        for key in ("resend_summary", "position_resend_summary",
                    "keyframe_resend_summary"):
            self.assertIn("fm-resend", block[key])

    def test_setting_an_unknown_profile_does_not_raise(self):
        # set_fm_profile() is a Kotlin entry point called during startBridge();
        # a stored name this build no longer has must cost the default
        # placement, not the bridge.
        self.assertEqual(rns_bridge.set_fm_profile("nonsense"),
                         fm_config.DEFAULT_PROFILE)

    def test_custom_settings_are_applied_and_shown_by_net_code(self):
        import fm_custom
        self.addCleanup(rns_bridge.set_fm_profile, fm_config.DEFAULT_PROFILE)
        spec = fm_custom.CustomSpec.from_preset("fast")
        spec.first_tone_hz = 1200
        self.assertEqual(rns_bridge.set_fm_profile("custom", False, spec.to_json()),
                         "custom")
        self.assertEqual(rns_bridge._fm_status()["net_code"],
                         "4T-1200/400-400Bd-H1024.2048")

    def test_unusable_custom_settings_cost_the_default_not_the_bridge(self):
        self.addCleanup(rns_bridge.set_fm_profile, fm_config.DEFAULT_PROFILE)
        for bad in ("", "{", '{"tones": 3}'):
            self.assertEqual(rns_bridge.set_fm_profile("custom", False, bad),
                             fm_config.DEFAULT_PROFILE, bad)
        # An older Kotlin caller passing only (name, vox) is the preset path.
        self.assertEqual(rns_bridge.set_fm_profile("narrow", False), "narrow")

    def test_the_block_is_absent_entirely_for_a_node_with_no_fm(self):
        """Absent, not present-and-empty, so a UI cannot render "no audio" for a
        LoRa-only node that has no audio path to begin with."""
        import json
        orig = rns_bridge._fm_iface
        try:
            rns_bridge._fm_iface = None
            self.assertIsNone(json.loads(rns_bridge.get_status())["fm"])
        finally:
            rns_bridge._fm_iface = orig


class TestBeaconSlotWidthClearsTheBurst(unittest.TestCase):
    """A slot grid finer than one on-air burst does not separate anybody.

    This is half of the 2026-09-30 two-phone finding: at a flat 2.5 s grid, two
    FM nodes one slot apart still transmitted over each other for ~8.5 s of every
    ~11 s burst, and delivery measured 1-2 beacons out of 6.
    """

    def test_the_fm_slot_clears_a_full_burst_plus_both_nodes_jitter(self):
        # Two nodes each jitter independently, so the worst case closes the gap
        # by twice the jitter bound.
        worst_case_gap = (rns_bridge._beacon_slot_s(rns_bridge.LINK_FM)
                          - 2 * rns_bridge._BEACON_SLOT_JITTER_S)
        self.assertGreaterEqual(
            worst_case_gap, fm_link.burst_duration_s(),
            "the FM beacon slot no longer clears a whole burst -- two nodes one "
            "slot apart will transmit over each other")

    def test_it_is_sized_from_the_real_burst_not_the_ceiling(self):
        """THE CHANGE FROM THE PRE-TEARDOWN CODE, which used a flat 15 s sized
        above fm_config.MAX_BURST_S -- the longest burst any configuration is
        ALLOWED, not the length of the one on the air. At the Phase 0 operating
        point that is 6x wider than needed, and slot WIDTH costs slot COUNT,
        which is the collision probability this mechanism exists to lower."""
        self.assertLess(rns_bridge._beacon_slot_s(rns_bridge.LINK_FM),
                        fm_config.MAX_BURST_S)
        self.assertGreater(
            rns_bridge._beacon_slot_count(
                rns_bridge._beacon_interval(),
                rns_bridge._beacon_slot_s(rns_bridge.LINK_FM)),
            rns_bridge._beacon_slot_count(rns_bridge._beacon_interval(),
                                          fm_config.MAX_BURST_S + 1.0))

    def test_it_follows_the_operating_point(self):
        """Asked of the live configuration, so a profile change cannot leave the
        grid silently wrong."""
        orig = fm_link.burst_duration_s
        try:
            fm_link.burst_duration_s = lambda: 9.0
            self.assertAlmostEqual(
                rns_bridge._beacon_slot_s(rns_bridge.LINK_FM),
                9.0 + 2 * rns_bridge._BEACON_SLOT_JITTER_S)
        finally:
            fm_link.burst_duration_s = orig

    def test_it_is_never_narrower_than_loras_default(self):
        self.assertGreaterEqual(rns_bridge._beacon_slot_s(rns_bridge.LINK_FM),
                                rns_bridge._BEACON_SLOT_S_DEFAULT)

    def test_lora_keeps_its_finer_grid(self):
        # LoRa's beacon is a fraction of a second on air; widening its grid would
        # cost slot count for nothing.
        self.assertLess(rns_bridge._beacon_slot_s(rns_bridge.LINK_LORA),
                        rns_bridge._beacon_slot_s(rns_bridge.LINK_FM))

    def test_a_cadence_too_short_for_one_burst_collapses_to_one_slot(self):
        # Saying so with a single slot is honest, where handing out offsets that
        # overlap by construction would not be.
        self.assertEqual(rns_bridge._beacon_slot_count(3, 15.0), 1)

    def test_the_slot_count_never_reaches_zero(self):
        self.assertGreaterEqual(rns_bridge._beacon_slot_count(1, 15.0), 1)


class _StubLinkIface(object):
    def __init__(self, online=True):
        self.online = online


class TestSetActiveLink(unittest.TestCase):
    """Toggles .OUT so only the active link's broadcast sends reach the
    wire. Real bug, confirmed live (2026-09-25, both phones):
    AutoInterface.add_peer() spawns a SEPARATE sub-interface per
    discovered peer and copies `.OUT` from its parent exactly once, at
    spawn time -- never again. A peer discovered while its link was NOT
    active is born with `.OUT = False` baked in and stays permanently
    unable to transmit even once that link is later promoted, unless
    _set_active_link() also walks each interface's own
    `spawned_interfaces`, not just the parent objects in _link_ifaces."""

    def setUp(self):
        self._orig_link_ifaces = dict(rns_bridge._link_ifaces)
        self._orig_active_link = rns_bridge._active_link

    def tearDown(self):
        rns_bridge._link_ifaces = self._orig_link_ifaces
        rns_bridge._active_link = self._orig_active_link

    def test_only_the_named_link_ends_up_with_out_true(self):
        wifi, lora = _StubLinkIface(), _StubLinkIface()
        rns_bridge._link_ifaces = {rns_bridge.LINK_WIFI: wifi,
                                   rns_bridge.LINK_LORA: lora}
        rns_bridge._set_active_link(rns_bridge.LINK_LORA)
        self.assertFalse(wifi.OUT)
        self.assertTrue(lora.OUT)

    def test_a_peer_spawned_while_inactive_is_corrected_once_promoted(self):
        wifi = _StubLinkIface()
        wifi.spawned_interfaces = {}
        rns_bridge._link_ifaces = {rns_bridge.LINK_WIFI: wifi,
                                   LINK_BACKUP: _StubLinkIface()}
        rns_bridge._set_active_link(LINK_BACKUP)  # WiFi not active yet
        peer = _StubLinkIface()
        peer.OUT = wifi.OUT  # mirrors add_peer()'s own spawned_interface.OUT = self.OUT
        wifi.spawned_interfaces["fe80::peer"] = peer
        self.assertFalse(peer.OUT)
        rns_bridge._set_active_link(rns_bridge.LINK_WIFI)  # later promotion
        self.assertTrue(peer.OUT)

    def test_a_peer_on_a_link_that_is_not_active_stays_out_false(self):
        wifi = _StubLinkIface()
        wifi.spawned_interfaces = {"fe80::peer": _StubLinkIface()}
        rns_bridge._link_ifaces = {rns_bridge.LINK_WIFI: wifi,
                                   rns_bridge.LINK_LORA: _StubLinkIface()}
        rns_bridge._set_active_link(rns_bridge.LINK_LORA)
        self.assertFalse(wifi.spawned_interfaces["fe80::peer"].OUT)

    def test_a_link_with_no_spawned_interfaces_attribute_is_unaffected(self):
        # LoRa/FM have no such concept at all -- must not raise.
        lora = _StubLinkIface()
        rns_bridge._link_ifaces = {rns_bridge.LINK_LORA: lora}
        rns_bridge._set_active_link(rns_bridge.LINK_LORA)
        self.assertTrue(lora.OUT)


class TestAutoSwitch(unittest.TestCase):
    """Automatic failover/failback across however many of WiFi/LoRa/FM are
    built -- _check_auto_switch(), called every _reliable_loop tick.
    Priority order is LoRa, then FM, then WiFi (rns_bridge._LINK_PRIORITY):
    fail DOWN away from an unhealthy active link happens instantly, to
    whichever next-best link is healthy right now; fail UP to a
    higher-priority link only happens once it has been continuously
    healthy for _FAILBACK_DEBOUNCE_S. WiFi last means it can never preempt a
    working LoRa/FM link -- see _LINK_PRIORITY's own comment."""

    def setUp(self):
        self._orig_link_ifaces = dict(rns_bridge._link_ifaces)
        self._orig_link_health = dict(rns_bridge._link_health)
        self._orig_lora_health = rns_bridge._lora_health
        self._orig_active_link = rns_bridge._active_link
        self._orig_link_override = rns_bridge._link_override
        self._orig_link_up_since = dict(rns_bridge._link_up_since)
        # Fresh instances per test, same reason as before: the real ones
        # accumulate escalation samples across the whole test run
        # otherwise, which would make DEGRADED-state tests depend on what
        # ran before them.
        rns_bridge._link_health = {
            rns_bridge.LINK_LORA: lora_health.LoraHealth(),
            rns_bridge.LINK_WIFI: wifi_health.WifiHealth(),
        }
        rns_bridge._lora_health = rns_bridge._link_health[rns_bridge.LINK_LORA]
        rns_bridge._link_ifaces = {}
        rns_bridge._link_up_since = {l: None for l in rns_bridge._LINK_PRIORITY}
        rns_bridge._link_override = None

    def tearDown(self):
        rns_bridge._link_ifaces = self._orig_link_ifaces
        rns_bridge._link_health = self._orig_link_health
        rns_bridge._lora_health = self._orig_lora_health
        rns_bridge._active_link = self._orig_active_link
        rns_bridge._link_override = self._orig_link_override
        rns_bridge._link_up_since = self._orig_link_up_since

    def _build(self, **online_by_link):
        """Register a stub interface for each named link (wifi=/lora=/testbackup=),
        online or not. A link left out is simply not built."""
        for name, online in online_by_link.items():
            rns_bridge._link_ifaces[name] = _StubLinkIface(online=online)

    def test_noop_with_only_one_link_built(self):
        for link in rns_bridge._LINK_PRIORITY:
            with self.subTest(link=link):
                rns_bridge._link_ifaces = {link: _StubLinkIface(online=False)}
                rns_bridge._active_link = link
                rns_bridge._check_auto_switch()
                self.assertEqual(rns_bridge._active_link, link)

    def test_noop_under_manual_override(self):
        # LoRa healthy and past its debounce would normally fail back to
        # it (see test_fails_back_after_the_debounce); the override must
        # hold it on FM instead.
        self._build(lora=True, testbackup=True)
        rns_bridge._active_link = LINK_BACKUP
        rns_bridge._link_override = LINK_BACKUP
        rns_bridge._link_up_since[rns_bridge.LINK_LORA] = (
            time.monotonic() - rns_bridge._FAILBACK_DEBOUNCE_S - 1.0)
        rns_bridge._check_auto_switch()
        self.assertEqual(rns_bridge._active_link, LINK_BACKUP)

    def test_lora_offline_fails_over_to_fm(self):
        # Unchanged from the pre-WiFi behaviour when WiFi is simply not
        # built at all.
        self._build(lora=False, testbackup=True)
        rns_bridge._active_link = rns_bridge.LINK_LORA
        rns_bridge._check_auto_switch()
        self.assertEqual(rns_bridge._active_link, LINK_BACKUP)

    def test_wifi_down_falls_over_to_healthy_lora(self):
        self._build(wifi=False, lora=True)
        rns_bridge._active_link = rns_bridge.LINK_WIFI
        rns_bridge._check_auto_switch()
        self.assertEqual(rns_bridge._active_link, rns_bridge.LINK_LORA)

    def test_wifi_and_lora_both_down_falls_all_the_way_to_fm(self):
        self._build(wifi=False, lora=False, testbackup=True)
        rns_bridge._active_link = rns_bridge.LINK_WIFI
        rns_bridge._check_auto_switch()
        self.assertEqual(rns_bridge._active_link, LINK_BACKUP)

    def test_enough_escalations_fails_over_even_while_online(self):
        # The soft signal: attached but not actually getting frames
        # through. Proven on LoRa (as before) and on WiFi, since both now
        # share the same tracker shape.
        self._build(lora=True, testbackup=True)
        rns_bridge._active_link = rns_bridge.LINK_LORA
        for _ in range(lora_health.DEFAULT_MIN_SAMPLES):
            rns_bridge._link_health[rns_bridge.LINK_LORA].note_escalated()
        rns_bridge._check_auto_switch()
        self.assertEqual(rns_bridge._active_link, LINK_BACKUP)

    def test_wifi_soft_signal_fails_over_too(self):
        self._build(wifi=True, lora=True)
        rns_bridge._active_link = rns_bridge.LINK_WIFI
        for _ in range(wifi_health.DEFAULT_MIN_SAMPLES):
            rns_bridge._link_health[rns_bridge.LINK_WIFI].note_escalated()
        rns_bridge._check_auto_switch()
        self.assertEqual(rns_bridge._active_link, rns_bridge.LINK_LORA)

    def test_stays_on_backup_while_higher_priority_links_still_down(self):
        self._build(wifi=False, lora=False, testbackup=True)
        rns_bridge._active_link = LINK_BACKUP
        rns_bridge._check_auto_switch()
        self.assertEqual(rns_bridge._active_link, LINK_BACKUP)

    def test_does_not_fail_back_immediately(self):
        # LoRa reading UP for the first time since a failover must not
        # switch back instantly -- see _FAILBACK_DEBOUNCE_S.
        self._build(lora=True, testbackup=True)
        rns_bridge._active_link = LINK_BACKUP
        rns_bridge._link_up_since[rns_bridge.LINK_LORA] = None
        rns_bridge._check_auto_switch()
        self.assertEqual(rns_bridge._active_link, LINK_BACKUP)

    def test_fails_back_after_the_debounce(self):
        self._build(lora=True, testbackup=True)
        rns_bridge._active_link = LINK_BACKUP
        rns_bridge._link_up_since[rns_bridge.LINK_LORA] = (
            time.monotonic() - rns_bridge._FAILBACK_DEBOUNCE_S - 1.0)
        rns_bridge._check_auto_switch()
        self.assertEqual(rns_bridge._active_link, rns_bridge.LINK_LORA)

    def test_fails_back_to_lora_not_wifi(self):
        # Starting on FM with both WiFi and LoRa healthy and past their
        # debounce: LoRa wins, WiFi does not -- WiFi is last priority now,
        # so it must never preempt a working radio.
        self._build(wifi=True, lora=True, testbackup=True)
        rns_bridge._active_link = LINK_BACKUP
        stale = time.monotonic() - rns_bridge._FAILBACK_DEBOUNCE_S - 1.0
        rns_bridge._link_up_since[rns_bridge.LINK_WIFI] = stale
        rns_bridge._link_up_since[rns_bridge.LINK_LORA] = stale
        rns_bridge._check_auto_switch()
        self.assertEqual(rns_bridge._active_link, rns_bridge.LINK_LORA)

    def test_wifi_never_preempts_a_healthy_lora_link(self):
        # The regression this priority order exists to prevent: with the
        # WiFi toggle enabled and WiFi reporting perfectly healthy for well
        # past the debounce, an already-active LoRa link must stay active.
        self._build(wifi=True, lora=True)
        rns_bridge._active_link = rns_bridge.LINK_LORA
        rns_bridge._link_up_since[rns_bridge.LINK_WIFI] = (
            time.monotonic() - rns_bridge._FAILBACK_DEBOUNCE_S - 1.0)
        rns_bridge._check_auto_switch()
        self.assertEqual(rns_bridge._active_link, rns_bridge.LINK_LORA)

    def test_wifi_never_preempts_a_healthy_backup_link(self):
        # Same guard one tier down: FM is above WiFi too, so a healthy,
        # long-up WiFi must not pull an active FM session away.
        self._build(wifi=True, testbackup=True)
        rns_bridge._active_link = LINK_BACKUP
        rns_bridge._link_up_since[rns_bridge.LINK_WIFI] = (
            time.monotonic() - rns_bridge._FAILBACK_DEBOUNCE_S - 1.0)
        rns_bridge._check_auto_switch()
        self.assertEqual(rns_bridge._active_link, LINK_BACKUP)

    def test_a_drop_resets_the_up_since_clock(self):
        # Otherwise a brief flicker back to UP right at the debounce
        # deadline could fail back on stale evidence.
        self._build(lora=False, testbackup=True)
        rns_bridge._active_link = LINK_BACKUP
        rns_bridge._link_up_since[rns_bridge.LINK_LORA] = (
            time.monotonic() - rns_bridge._FAILBACK_DEBOUNCE_S - 1.0)
        rns_bridge._check_auto_switch()
        self.assertIsNone(rns_bridge._link_up_since[rns_bridge.LINK_LORA])
        self.assertEqual(rns_bridge._active_link, LINK_BACKUP)


class _StubTxIface(object):
    """Like _StubLinkIface, plus the counters _status_tick actually reads."""
    def __init__(self, txb=0, rxb=0, bitrate=1000, online=True):
        self.txb = txb
        self.rxb = rxb
        self.bitrate = bitrate
        self.online = online


class TestStatusTick(unittest.TestCase):
    """The periodic field-test heartbeat (_status_tick, called every
    _STATUS_INTERVAL_S by _status_loop). Added 2026-09-26 prepping for a
    real LoRa<->FM failover field test: this line used to hardcode
    _lora_iface for its online/txb/rxb/duty numbers, so during an FM
    failover it silently described LoRa's now-idle state instead of the
    link actually carrying traffic. See docs/open-questions.md."""

    def setUp(self):
        self._orig_link_ifaces = dict(rns_bridge._link_ifaces)
        self._orig_active_link = rns_bridge._active_link
        self._orig_last_txb = rns_bridge._last_txb
        self._orig_last_duty_pct = rns_bridge._last_duty_pct
        self._orig_duty_over_threshold = rns_bridge._duty_over_threshold
        self._orig_peers = rns_bridge._peers
        self._orig_last_connected = set(rns_bridge._last_connected_peer_ids)
        self._orig_ever_connected = set(rns_bridge._ever_connected_peer_ids)
        rns_bridge._peers = rns_bridge.peers_mod.PeerTable(
            connected_s=rns_bridge._PEER_CONNECTED_S,
            forget_s=rns_bridge._PEER_FORGET_S)
        rns_bridge._last_txb = {}
        rns_bridge._last_duty_pct = 0.0
        rns_bridge._duty_over_threshold = False
        self._orig_status_clock = rns_bridge._status_clock
        self.now = [1000.0]
        rns_bridge._status_clock = lambda: self.now[0]
        rns_bridge._last_connected_peer_ids = set()
        rns_bridge._ever_connected_peer_ids = set()

        self._orig_log = rns_bridge.RNS.log
        self.logged = []
        rns_bridge.RNS.log = lambda msg, level=None: self.logged.append(msg)

    def tearDown(self):
        rns_bridge._link_ifaces = self._orig_link_ifaces
        rns_bridge._active_link = self._orig_active_link
        rns_bridge._last_txb = self._orig_last_txb
        rns_bridge._last_duty_pct = self._orig_last_duty_pct
        rns_bridge._duty_over_threshold = self._orig_duty_over_threshold
        rns_bridge._status_clock = self._orig_status_clock
        rns_bridge._peers = self._orig_peers
        rns_bridge._last_connected_peer_ids = self._orig_last_connected
        rns_bridge._ever_connected_peer_ids = self._orig_ever_connected
        rns_bridge.RNS.log = self._orig_log

    def _status_lines(self):
        return [m for m in self.logged if m.startswith("[cot-bridge] status(")]

    def test_heartbeat_follows_the_active_link_not_lora(self):
        rns_bridge._link_ifaces = {
            rns_bridge.LINK_LORA: _StubTxIface(txb=9999, rxb=9999),
            LINK_BACKUP: _StubTxIface(txb=42, rxb=7),
        }
        rns_bridge._active_link = LINK_BACKUP
        rns_bridge._status_tick()
        lines = self._status_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("status(%s)" % LINK_BACKUP, lines[0])
        self.assertIn("txb=42", lines[0])
        self.assertIn("rxb=7", lines[0])
        self.assertNotIn("txb=9999", lines[0])

    def test_no_interface_built_names_the_active_link_not_rnode(self):
        rns_bridge._link_ifaces = {}
        rns_bridge._active_link = rns_bridge.LINK_WIFI
        rns_bridge._status_tick()
        self.assertEqual(len(self.logged), 1)
        self.assertIn("wifi", self.logged[0])
        self.assertNotIn("RNode", self.logged[0])

    def test_peer_rssi_snr_and_link_appear_in_the_heartbeat(self):
        rns_bridge._link_ifaces = {rns_bridge.LINK_LORA: _StubTxIface()}
        rns_bridge._active_link = rns_bridge.LINK_LORA
        rns_bridge._peers.note_frame(
            "abc123", rssi=-77, snr=8.5, link=rns_bridge.LINK_LORA)
        rns_bridge._status_tick()
        lines = self._status_lines()
        self.assertIn("lora -77dBm/8.5dB", lines[0])

    def test_peer_with_no_signal_reading_omits_the_suffix_cleanly(self):
        rns_bridge._link_ifaces = {rns_bridge.LINK_LORA: _StubTxIface()}
        rns_bridge._active_link = rns_bridge.LINK_LORA
        rns_bridge._peers.note_frame("abc123")  # no rssi/snr, e.g. a relay
        rns_bridge._status_tick()
        lines = self._status_lines()
        self.assertNotIn("dBm", lines[0])

    def test_duty_threshold_fires_once_on_crossing_not_every_tick(self):
        rns_bridge._link_ifaces = {
            rns_bridge.LINK_LORA: _StubTxIface(bitrate=1000)}
        rns_bridge._active_link = rns_bridge.LINK_LORA
        # duty% = 100 * delta_bytes * 8 / bitrate / _STATUS_INTERVAL_S.
        # bitrate=1000, interval=30s -> ~3750 bytes/tick crosses 100% busy;
        # pick a delta comfortably over the 30% warn threshold.
        busy_delta = int(0.5 * 1000 * rns_bridge._STATUS_INTERVAL_S / 8.0)
        rns_bridge._status_tick()                     # the baseline
        self.now[0] += rns_bridge._STATUS_INTERVAL_S
        rns_bridge._link_ifaces[rns_bridge.LINK_LORA].txb = busy_delta
        rns_bridge._status_tick()
        warnings = [m for m in self.logged if "exceeds" in m]
        self.assertEqual(len(warnings), 1)

        # Same high duty again next tick -- must not repeat the WARNING.
        self.logged.clear()
        self.now[0] += rns_bridge._STATUS_INTERVAL_S
        rns_bridge._link_ifaces[rns_bridge.LINK_LORA].txb = busy_delta * 2
        rns_bridge._status_tick()
        self.assertEqual([m for m in self.logged if "exceeds" in m], [])

        # Drops back under threshold -- exactly one clearing NOTICE.
        self.logged.clear()
        self.now[0] += rns_bridge._STATUS_INTERVAL_S
        rns_bridge._link_ifaces[rns_bridge.LINK_LORA].txb = busy_delta * 2 + 1
        rns_bridge._status_tick()
        clears = [m for m in self.logged if "back under" in m]
        self.assertEqual(len(clears), 1)

    def _tick(self, advance=None):
        self.now[0] += rns_bridge._STATUS_INTERVAL_S if advance is None else advance
        self.logged.clear()
        rns_bridge._status_tick()
        return self._status_lines()[0]

    def test_a_failover_does_not_compare_one_links_counter_with_anothers(self):
        # 2026-10-23: LoRa had sent 5530 B, FM 0; the first FM line read
        # duty=-427.5% because one baseline was shared by every link.
        lora = _StubTxIface(txb=5530, bitrate=6250)
        fm = _StubTxIface(txb=0, bitrate=340)
        rns_bridge._link_ifaces = {rns_bridge.LINK_LORA: lora, LINK_BACKUP: fm}
        rns_bridge._active_link = rns_bridge.LINK_LORA
        self._tick(0)
        rns_bridge._active_link = LINK_BACKUP      # failover
        line = self._tick()
        self.assertIn("duty=0.0%", line)              # FM sent nothing yet
        self.assertNotIn("-", line.split("duty=")[1].split(" ")[0])
        fm.txb = 41                                   # one beacon
        self.assertIn("duty=3.2%", self._tick())      # 41*8/340/30

    def test_a_failback_charges_lora_only_for_the_last_interval(self):
        lora = _StubTxIface(txb=1000, bitrate=6250)
        fm = _StubTxIface(txb=0, bitrate=340)
        rns_bridge._link_ifaces = {rns_bridge.LINK_LORA: lora, LINK_BACKUP: fm}
        rns_bridge._active_link = LINK_BACKUP
        self._tick(0)
        for _ in range(10):                           # five minutes on FM
            self._tick()
        # LoRa's counter can move while FM is active (it is still built);
        # the per-tick refresh absorbs that instead of saving it up.
        lora.txb = 1000 + 6000
        self._tick()
        rns_bridge._active_link = rns_bridge.LINK_LORA   # failback
        lora.txb += 470                               # one interval's traffic
        line = self._tick()
        self.assertIn("duty=2.0%", line)              # 470*8/6250/30
        self.assertEqual([m for m in self.logged if "exceeds" in m], [])

    def test_no_baseline_reads_n_a_and_a_rebuilt_interface_is_a_new_baseline(self):
        lora = _StubTxIface(txb=5000, bitrate=6250)
        rns_bridge._link_ifaces = {rns_bridge.LINK_LORA: lora}
        rns_bridge._active_link = rns_bridge.LINK_LORA
        self.assertIn("duty=n/a", self._tick(0))
        rns_bridge._link_ifaces = {rns_bridge.LINK_LORA: _StubTxIface(txb=12, bitrate=6250)}
        self.assertIn("duty=n/a", self._tick())       # reconnected: counter restarted
        self.assertIn("duty=0.0%", self._tick())

    def test_peer_lost_is_logged_even_with_no_marker_ever_sent(self):
        clock = [1000.0]
        rns_bridge._peers = rns_bridge.peers_mod.PeerTable(
            connected_s=10, forget_s=600, clock=lambda: clock[0])
        rns_bridge._link_ifaces = {rns_bridge.LINK_LORA: _StubTxIface()}
        rns_bridge._active_link = rns_bridge.LINK_LORA

        rns_bridge._peers.note_frame(
            "beaconly", rssi=-60, snr=5.0, link=rns_bridge.LINK_LORA)
        rns_bridge._status_tick()  # peer is connected, no transition yet
        self.assertFalse(any("has gone quiet" in m for m in self.logged))

        self.logged.clear()
        clock[0] += 11  # past connected_s=10, same peer never had a marker
        rns_bridge._status_tick()
        quiet = [m for m in self.logged if "has gone quiet" in m]
        self.assertEqual(len(quiet), 1)
        self.assertIn("lora", quiet[0])

        self.logged.clear()
        rns_bridge._status_tick()  # already gone; must not repeat
        self.assertFalse(any("has gone quiet" in m for m in self.logged))

    def test_returning_peer_is_logged_but_a_brand_new_peer_is_not(self):
        clock = [1000.0]
        rns_bridge._peers = rns_bridge.peers_mod.PeerTable(
            connected_s=10, forget_s=600, clock=lambda: clock[0])
        rns_bridge._link_ifaces = {rns_bridge.LINK_LORA: _StubTxIface()}
        rns_bridge._active_link = rns_bridge.LINK_LORA

        rns_bridge._peers.note_frame("newpeer", link=rns_bridge.LINK_LORA)
        rns_bridge._status_tick()
        # A first-ever appearance is _note_new_peer's job, in real time --
        # _note_peer_transitions must not also call it "back".
        self.assertFalse(any("is back" in m for m in self.logged))

        clock[0] += 11  # ages out
        self.logged.clear()
        rns_bridge._status_tick()
        self.assertTrue(any("has gone quiet" in m for m in self.logged))

        self.logged.clear()
        rns_bridge._peers.note_frame(
            "newpeer", rssi=-50, snr=9.0, link=rns_bridge.LINK_LORA)
        rns_bridge._status_tick()
        back = [m for m in self.logged if "is back" in m]
        self.assertEqual(len(back), 1)
        self.assertIn("lora", back[0])


class TestStartupActiveLink(unittest.TestCase):
    """start()'s correction of the ORIGINAL transport's own link (chosen
    before any build was attempted) against what actually got built --
    needed for two independent reasons _check_auto_switch() cannot cover
    on its own: it no-ops entirely with fewer than two links built (see
    TestAutoSwitch.test_noop_with_only_one_link_built), and it only ever
    revisits _active_link when something actually needs to change, not
    merely because a link's .OUT flag needs reasserting after a later
    build (see rns_bridge._startup_active_link's own docstring, item 2).
    """

    def test_intended_link_kept_when_it_actually_built(self):
        # The overwhelmingly common case: LoRa was the intended transport
        # and it came up fine. Kept regardless of how many OTHER links are
        # also built -- _check_auto_switch() handles refining this from
        # here, debounced.
        self.assertEqual(
            rns_bridge._startup_active_link(rns_bridge.LINK_LORA,
                                            [rns_bridge.LINK_LORA]),
            rns_bridge.LINK_LORA)
        self.assertEqual(
            rns_bridge._startup_active_link(
                rns_bridge.LINK_LORA,
                [rns_bridge.LINK_WIFI, rns_bridge.LINK_LORA]),
            rns_bridge.LINK_LORA)

    def test_wifi_only_becomes_active_when_it_is_the_sole_link_built(self):
        # The bug this exists to fix: no RNode, no FM hardware, WiFi is
        # the only thing that came up at all. LINK_LORA (the default
        # transport's own intended link) never appears in built_links.
        self.assertEqual(
            rns_bridge._startup_active_link(rns_bridge.LINK_LORA,
                                            [rns_bridge.LINK_WIFI]),
            rns_bridge.LINK_WIFI)

    def test_prefers_a_non_wifi_fallback_over_wifi_when_one_also_built(self):
        # LoRa failed to build, but FM-backup and WiFi both came up --
        # WiFi must not become the instant default just because LINK_LORA
        # (the original intended link) is not among the built ones; a
        # non-WiFi alternative existing at all is reason enough to prefer
        # it, exactly as if this were a live LoRa-down/FM-backup failover.
        self.assertEqual(
            rns_bridge._startup_active_link(
                rns_bridge.LINK_LORA,
                [rns_bridge.LINK_WIFI, LINK_BACKUP]),
            LINK_BACKUP)

    def test_nothing_built_returns_none(self):
        self.assertIsNone(rns_bridge._startup_active_link(
            rns_bridge.LINK_LORA, []))

    def test_backup_only_transport_keeps_backup_when_it_built(self):
        self.assertEqual(
            rns_bridge._startup_active_link(LINK_BACKUP,
                                            [LINK_BACKUP]),
            LINK_BACKUP)


class TestSetLinkOverride(unittest.TestCase):

    def setUp(self):
        self._orig_link_ifaces = dict(rns_bridge._link_ifaces)
        self._orig_active_link = rns_bridge._active_link
        self._orig_link_override = rns_bridge._link_override
        rns_bridge._link_ifaces = {
            rns_bridge.LINK_WIFI: _StubLinkIface(),
            rns_bridge.LINK_LORA: _StubLinkIface(),
            LINK_BACKUP: _StubLinkIface(),
        }
        self._orig_log = rns_bridge.RNS.log
        self.logged = []
        rns_bridge.RNS.log = lambda msg, level=None: self.logged.append(msg)

    def tearDown(self):
        rns_bridge._link_ifaces = self._orig_link_ifaces
        rns_bridge._active_link = self._orig_active_link
        rns_bridge._link_override = self._orig_link_override
        rns_bridge.RNS.log = self._orig_log

    def _override_lines(self):
        return [m for m in self.logged if m.startswith("[cot-bridge] link override")]

    def test_forces_each_known_link(self):
        for link in rns_bridge._LINK_PRIORITY:
            with self.subTest(link=link):
                rns_bridge._link_override = None
                rns_bridge.set_link_override(link)
                self.assertEqual(rns_bridge._link_override, link)
                self.assertEqual(rns_bridge._active_link, link)

    def test_auto_clears_the_override(self):
        rns_bridge.set_link_override(LINK_BACKUP)
        rns_bridge.set_link_override("auto")
        self.assertIsNone(rns_bridge._link_override)

    def test_an_unknown_link_is_refused_and_does_not_change_anything(self):
        rns_bridge._link_override = None
        rns_bridge._active_link = rns_bridge.LINK_LORA
        rns_bridge.set_link_override("bluetooth")
        self.assertIsNone(rns_bridge._link_override)
        self.assertEqual(rns_bridge._active_link, rns_bridge.LINK_LORA)

    def test_forcing_an_unbuilt_link_is_refused(self):
        for link in rns_bridge._LINK_PRIORITY:
            with self.subTest(link=link):
                rns_bridge._link_ifaces = {
                    rns_bridge.LINK_WIFI: _StubLinkIface(),
                    rns_bridge.LINK_LORA: _StubLinkIface(),
                    LINK_BACKUP: _StubLinkIface(),
                }
                del rns_bridge._link_ifaces[link]
                rns_bridge._link_override = None
                rns_bridge.set_link_override(link)
                self.assertIsNone(rns_bridge._link_override)

    def test_setting_an_override_logs_the_change(self):
        rns_bridge._link_override = None
        rns_bridge.set_link_override("lora")
        lines = self._override_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("auto -> lora", lines[0])

    def test_clearing_an_override_logs_the_change(self):
        rns_bridge.set_link_override(LINK_BACKUP)
        self.logged.clear()
        rns_bridge.set_link_override("auto")
        lines = self._override_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("%s -> auto" % LINK_BACKUP, lines[0])

    def test_switching_directly_between_two_forced_links_logs_both_endpoints(self):
        rns_bridge.set_link_override("lora")
        self.logged.clear()
        rns_bridge.set_link_override(LINK_BACKUP)
        lines = self._override_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("lora -> %s" % LINK_BACKUP, lines[0])

    def test_reapplying_the_same_override_does_not_log_again(self):
        rns_bridge.set_link_override("lora")
        self.logged.clear()
        rns_bridge.set_link_override("lora")
        self.assertEqual(self._override_lines(), [])

    def test_calling_auto_when_already_auto_does_not_log(self):
        rns_bridge._link_override = None
        rns_bridge.set_link_override("auto")
        self.assertEqual(self._override_lines(), [])

    def test_a_refused_override_does_not_log_a_change(self):
        rns_bridge._link_override = None
        rns_bridge.set_link_override("bluetooth")
        self.assertEqual(self._override_lines(), [])
        del rns_bridge._link_ifaces[LINK_BACKUP]
        rns_bridge.set_link_override(LINK_BACKUP)
        self.assertEqual(self._override_lines(), [])


class _StubFile(object):
    """A file stand-in with only what _record_range_sample uses, so a test
    doesn't need a real path on disk."""
    def __init__(self):
        self.lines = []

    def write(self, s):
        self.lines.append(s)

    def flush(self):
        pass


class TestRecordRangeSample(unittest.TestCase):
    """A range test is only ever run on one link at a time in practice (LoRa
    or FM, not both) -- see rangetest.COLUMNS' own "link" column comment --
    but _record_range_sample() is what actually threads the link through and
    keeps a LoRa-only reading (channel noise floor, off the RNode) from being
    attached to a frame that arrived on a different link entirely."""

    def setUp(self):
        self._orig_rangetest = rns_bridge._rangetest
        self._orig_file = rns_bridge._rangetest_file
        self._orig_lora_iface = rns_bridge._lora_iface
        rns_bridge._rangetest = rangetest.RangeTest(
            distance_fn=lambda *a: 0.0, clock=time.monotonic)
        self.stub_file = _StubFile()
        rns_bridge._rangetest_file = self.stub_file

    def tearDown(self):
        rns_bridge._rangetest = self._orig_rangetest
        rns_bridge._rangetest_file = self._orig_file
        rns_bridge._lora_iface = self._orig_lora_iface

    def _last_row(self):
        line = self.stub_file.lines[-1].rstrip("\n")
        return dict(zip(rangetest.COLUMNS, line.split(",")))

    def test_link_is_written_in_its_own_column(self):
        rns_bridge._lora_iface = None
        rns_bridge._record_range_sample("abc123", None, None, 1, LINK_BACKUP)
        self.assertEqual(self._last_row()["link"], LINK_BACKUP)

    def test_lora_noise_floor_is_attached_for_a_lora_frame(self):
        class Iface(object):
            r_noise_floor = -95.0
            r_interference = None
        rns_bridge._lora_iface = Iface()
        rns_bridge._record_range_sample("abc123", -80, 5.0, 1, rns_bridge.LINK_LORA)
        self.assertEqual(self._last_row()["noise_floor_dbm"], "-95.00")

    def test_lora_noise_floor_is_not_attached_for_a_non_lora_frame(self):
        """Even with LoRa built and reporting a real noise floor, a frame
        that arrived on FM says nothing about LoRa's channel -- attaching it
        anyway would misleadingly label a LoRa reading as FM's."""
        class Iface(object):
            r_noise_floor = -95.0
            r_interference = None
        rns_bridge._lora_iface = Iface()
        rns_bridge._record_range_sample("abc123", None, None, 1, LINK_BACKUP)
        row = self._last_row()
        self.assertEqual(row["noise_floor_dbm"], "")
        self.assertEqual(row["link"], LINK_BACKUP)


class TestRangeTestFixRidesTheBeacon(unittest.TestCase):
    """2026-11-08: while a range test runs, every beacon carries the sender's
    latest fix, and the receiver books it BEFORE writing that beacon's row.
    The drive that day had 4 usable rows of 25 because the only position the
    far end got was the self-SA, rate-limited on air to about one a minute."""

    PEER = "aaaa02"
    LAT, LON = 20.9089800, -140.5829100

    def setUp(self):
        self._orig = (rns_bridge._rangetest, rns_bridge._rangetest_file)
        rns_bridge._rangetest = rangetest.RangeTest(
            distance_fn=tx_policy.haversine_m, clock=time.monotonic,
            wall_clock=time.time)
        self.stub_file = _StubFile()
        rns_bridge._rangetest_file = self.stub_file

    def tearDown(self):
        rns_bridge._rangetest, rns_bridge._rangetest_file = self._orig

    def _beacon(self, taken):
        return cot_codec.encode_beacon(
            self.PEER, 5, fix=cot_codec.pack_beacon_fix(self.LAT, self.LON, taken))

    def test_no_fix_is_sent_outside_a_test(self):
        rns_bridge._rangetest.note_position(rns_bridge._NODE_ID, self.LAT, self.LON)
        rns_bridge._rangetest_file = None
        self.assertIsNone(rns_bridge._range_test_beacon_fix())

    def test_no_fix_is_sent_without_one(self):
        self.assertIsNone(rns_bridge._range_test_beacon_fix())

    def test_our_fix_is_sent_with_the_time_it_was_taken(self):
        rns_bridge._rangetest.note_position(
            rns_bridge._NODE_ID, self.LAT, self.LON, stamp=time.time() - 20)
        fix = cot_codec.unpack_beacon_fix(rns_bridge._range_test_beacon_fix())
        self.assertAlmostEqual(fix.lat, self.LAT, places=6)
        self.assertAlmostEqual(fix.time, time.time() - 20, delta=2)

    def test_a_fix_past_the_backstop_is_not_sent(self):
        book = rns_bridge._rangetest
        book.note_position(rns_bridge._NODE_ID, self.LAT, self.LON,
                           stamp=time.time() - book.max_fix_age_s + 1)
        own = book.fix_for(rns_bridge._NODE_ID)
        book._fixes[rns_bridge._NODE_ID] = own._replace(
            at=own.at - 10)                     # now just past the backstop
        self.assertIsNone(rns_bridge._range_test_beacon_fix())

    def test_the_beacons_row_carries_the_beacons_own_fix(self):
        rns_bridge._rangetest.note_position(rns_bridge._NODE_ID, self.LAT + 0.01,
                                            self.LON)
        rns_bridge._book_beacon_fix(self.PEER, self._beacon(int(time.time()) - 3))
        rns_bridge._record_range_sample(self.PEER, None, None, 5, LINK_BACKUP)
        line = self.stub_file.lines[-1].rstrip("\n")
        row = dict(zip(rangetest.COLUMNS, line.split(",")))
        self.assertAlmostEqual(float(row["tx_lat"]), self.LAT, places=5)
        self.assertLess(float(row["tx_fix_age_s"]), 10.0)
        self.assertAlmostEqual(float(row["distance_m"]), 1112, delta=5)

    def test_the_same_fix_on_the_next_beacon_is_not_a_stale_report(self):
        """The fix rides every beacon until ATAK reports a new one; a repeat
        is not a resend, and the summary must not count it as one."""
        taken = int(time.time()) - 3
        rns_bridge._book_beacon_fix(self.PEER, self._beacon(taken))
        rns_bridge._book_beacon_fix(self.PEER, self._beacon(taken))
        self.assertEqual(rns_bridge._rangetest.stale_refused, 0)
        self.assertIsNotNone(rns_bridge._rangetest.fix_for(self.PEER))

    def test_a_newer_fix_replaces_the_held_one(self):
        now = int(time.time())
        rns_bridge._book_beacon_fix(self.PEER, self._beacon(now - 30))
        rns_bridge._book_beacon_fix(self.PEER, self._beacon(now - 1))
        self.assertEqual(rns_bridge._rangetest.fix_for(self.PEER).stamp, now - 1)

    def test_a_fix_far_from_our_clock_is_ignored(self):
        """Also what keeps a padded beacon's random bytes, read as a fix, out
        of the book."""
        rns_bridge._book_beacon_fix(self.PEER, self._beacon(int(time.time()) - 3600))
        self.assertIsNone(rns_bridge._rangetest.fix_for(self.PEER))

    def test_nothing_is_booked_outside_a_test(self):
        rns_bridge._rangetest_file = None
        rns_bridge._book_beacon_fix(self.PEER, self._beacon(int(time.time())))
        self.assertIsNone(rns_bridge._rangetest.fix_for(self.PEER))

    def test_a_non_beacon_is_left_alone(self):
        rns_bridge._book_beacon_fix(self.PEER, b"\x01\x02\x03")
        self.assertIsNone(rns_bridge._rangetest.fix_for(self.PEER))

    def test_it_is_booked_before_the_row_is_written(self):
        """_note_peer_frame writes the row. Booked after it, every row would
        carry the PREVIOUS beacon's fix -- 30 s stale at drive speed, the
        exact error this exists to remove, and silent."""
        src = inspect.getsource(rns_bridge._on_rns_packet_received)
        self.assertLess(src.index("_book_beacon_fix("),
                        src.index("_note_peer_frame("))

    def test_the_beacon_sender_attaches_it(self):
        src = inspect.getsource(rns_bridge._send_one_beacon)
        self.assertIn("fix=_range_test_beacon_fix()", src)


class TestBeaconSlotOffset(unittest.TestCase):
    """The deterministic per-node beacon slot -- see rns_bridge.py's comment
    above _BEACON_SLOT_S_DEFAULT for why this replaced pure random jitter:
    two independently-jittered nodes could still drift into an occasional
    near-collision by chance, which real FM hardware hit on 2026-09-11."""

    LORA = rns_bridge.LINK_LORA

    def test_it_is_deterministic_for_one_node_and_cycle(self):
        offsets = {rns_bridge._beacon_slot_offset(7, link=self.LORA,
                                                   interval=90)
                   for _ in range(5)}
        self.assertEqual(len(offsets), 1)

    def test_it_stays_inside_one_beacon_interval(self):
        # The whole point is to spread transmissions across ONE beacon
        # interval, not several -- an offset past the interval would mean a
        # slot that never lines up with a real beacon.
        for link in (self.LORA, LINK_BACKUP):
            for interval in (30, 90):
                for cycle in range(12):
                    offset = rns_bridge._beacon_slot_offset(
                        cycle, link=link, interval=interval)
                    self.assertGreaterEqual(offset, 0.0)
                    self.assertLess(offset, interval, (link, interval, cycle))

    def test_it_lands_on_a_whole_slot_boundary(self):
        # Quantised, not continuous: this is what stops two nodes ever
        # sitting a near-miss 0.3s apart, the 2026-09-11 failure.
        slot_s = rns_bridge._beacon_slot_s(LINK_BACKUP)
        for cycle in range(20):
            offset = rns_bridge._beacon_slot_offset(
                cycle, link=LINK_BACKUP, interval=90)
            self.assertAlmostEqual(offset % slot_s, 0.0, places=6)

    def test_different_node_ids_usually_land_in_different_slots(self):
        original = rns_bridge._NODE_ID
        try:
            slots = set()
            for i in range(20):
                rns_bridge._NODE_ID = "%06x" % i
                slots.add(rns_bridge._beacon_slot_offset(
                    0, link=self.LORA, interval=90))
            self.assertGreater(len(slots), 10)
        finally:
            rns_bridge._NODE_ID = original


class TestBeaconSlotCannotCollideForever(unittest.TestCase):
    """The 2026-09-30 regression, stated as the property that actually
    matters: no PAIR of nodes may share a slot indefinitely.

    A fixed per-node slot cannot promise this once the grid is coarse enough
    for FM's burst -- only six slots fit a 90s cycle, so a 1-in-6 pair is
    locked into a permanent mutual blackout. The real bench pair hit exactly
    that: node ids aaaa02 and aaaa01 both hash to slot 3 of 6.
    """

    BACKUP = LINK_BACKUP
    INTERVAL = 90

    def _offsets(self, node_id, cycles):
        original = rns_bridge._NODE_ID
        try:
            rns_bridge._NODE_ID = node_id
            return [rns_bridge._beacon_slot_offset(
                c, link=self.BACKUP, interval=self.INTERVAL) for c in cycles]
        finally:
            rns_bridge._NODE_ID = original

    def test_the_real_bench_pair_does_not_collide_every_cycle(self):
        cycles = range(60)
        a = self._offsets("aaaa02", cycles)
        b = self._offsets("aaaa01", cycles)
        clashes = sum(1 for x, y in zip(a, b) if x == y)
        self.assertLess(clashes, len(list(cycles)),
                        "this pair shares a slot on EVERY cycle -- the exact "
                        "permanent blackout measured on the bench")
        # And they should be apart on most cycles, not merely not-always.
        self.assertLess(clashes, len(list(cycles)) / 2)

    def test_no_pair_of_node_ids_collides_on_every_cycle(self):
        cycles = list(range(40))
        ids = ["%06x" % i for i in range(12)]
        offsets = {nid: self._offsets(nid, cycles) for nid in ids}
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                clashes = sum(1 for x, y in zip(offsets[a], offsets[b])
                              if x == y)
                self.assertLess(clashes, len(cycles), (a, b))

    def test_when_they_differ_they_are_at_least_a_whole_slot_apart(self):
        """Two nodes that land on different slots are separated by at least
        one full slot width -- which is the property that makes the grid
        work, since _beacon_slot_s() sizes that width above the link's own
        burst length (see its comment, and docs/fm-lessons.md on why a grid
        narrower than one burst separates nobody).

        Asserted against the width the link actually reports rather than a
        hardcoded burst length: it was hardcoded to FM's 11.5 s ceiling
        until 2026-10-01, which silently became unsatisfiable the moment FM
        stopped being a link with its own wider grid.
        """
        slot_s = rns_bridge._beacon_slot_s(self.BACKUP)
        cycles = range(40)
        a = self._offsets("aaaa02", cycles)
        b = self._offsets("aaaa01", cycles)
        for x, y in zip(a, b):
            if x == y:
                continue
            gap = abs(x - y)
            gap = min(gap, self.INTERVAL - gap)
            self.assertGreaterEqual(gap, slot_s, (x, y))


class TestNextBeaconAt(unittest.TestCase):
    """Beacon slots are placed on the WALL clock, so every node measures its
    slot from the same reference.

    The regression this pins is the other half of the 2026-09-30 finding:
    the schedule used to ride time.monotonic(), whose epoch is per-process,
    so a fixed per-node offset shifted that one node's sends uniformly and
    separated nothing. Two bench phones sat 3-4s apart on every cycle for a
    whole session despite hashing to slots 10s apart.
    """

    @staticmethod
    def _fixed(offset):
        """A node that draws the same slot every cycle -- the simplest
        stand-in for exercising the scheduling itself."""
        return lambda _cycle: offset

    def test_two_nodes_with_different_slots_are_separated_by_exactly_that(self):
        # The heart of it: both nodes ask at DIFFERENT local moments (as two
        # independently-started processes always would) and still land the
        # full slot distance apart on the air.
        #
        # Modulo the interval, because "the NEXT occurrence" of two slots can
        # straddle a cycle boundary -- node b's upcoming slot may precede
        # node a's even though b sits 30s after a on the shared grid. What
        # matters for collisions is the on-air spacing, which is the modular
        # distance, not the raw difference of the two next times.
        interval = 90.0
        a = rns_bridge._next_beacon_at(1000.0, interval, self._fixed(0.0))
        b = rns_bridge._next_beacon_at(1007.3, interval, self._fixed(30.0))
        self.assertAlmostEqual((b - a) % interval, 30.0)

    def test_the_same_slot_gives_the_same_absolute_time_whenever_asked(self):
        # The property monotonic() could not provide: within one cycle, when
        # a node asks is irrelevant -- only which slot it holds. Both reads
        # here sit before the upcoming slot, so both must name it.
        interval = 90.0
        first = rns_bridge._next_beacon_at(1000.0, interval, self._fixed(45.0))
        later = rns_bridge._next_beacon_at(1030.0, interval, self._fixed(45.0))
        self.assertEqual(first, later)

    def test_it_is_always_strictly_in_the_future(self):
        interval = 90.0
        for now in (0.0, 1.0, 44.9, 45.0, 45.1, 1234567.89):
            for offset in (0.0, 15.0, 45.0):
                due = rns_bridge._next_beacon_at(
                    now, interval, self._fixed(offset))
                self.assertGreater(due, now, (now, offset))

    def test_it_never_schedules_more_than_one_interval_out(self):
        interval = 90.0
        for now in (0.0, 13.7, 89.9, 90.0, 1234567.89):
            due = rns_bridge._next_beacon_at(
                now, interval, self._fixed(15.0))
            self.assertLessEqual(due - now, interval, now)

    def test_a_suspended_process_rejoins_the_grid_instead_of_catching_up(self):
        # Doze: the loop wakes long after its slot passed. It should take the
        # NEXT slot on the shared grid, not fire a backlog -- and the result
        # must still sit on the grid, which is what keeps it separated from
        # the other node after both wake.
        interval = 90.0
        due = rns_bridge._next_beacon_at(
            100000.0, interval, self._fixed(30.0))
        self.assertGreater(due, 100000.0)
        self.assertLessEqual(due - 100000.0, interval)
        self.assertAlmostEqual((due - 30.0) % interval, 0.0)

    def test_every_slot_lands_on_the_shared_grid(self):
        interval = 90.0
        for offset in (0.0, 15.0, 30.0, 75.0):
            due = rns_bridge._next_beacon_at(
                54321.0, interval, self._fixed(offset))
            self.assertAlmostEqual((due - offset) % interval, 0.0, places=6)

    def test_it_asks_for_the_cycle_it_actually_schedules(self):
        # The offset is re-drawn per cycle, so the callable must be asked
        # about the SAME cycle the returned time belongs to -- getting this
        # off by one would put a node on another cycle's slot and silently
        # undo the separation.
        interval = 90.0
        asked = {}

        def offset_for(cycle):
            asked[cycle] = 15.0 * (cycle % 6)
            return asked[cycle]

        due = rns_bridge._next_beacon_at(1000.0, interval, offset_for)
        cycle = math.floor(due / interval)
        self.assertAlmostEqual(due - cycle * interval, asked[cycle])

    def test_feeding_a_slot_back_in_never_returns_that_same_slot(self):
        # The loop's actual pattern: it schedules `due`, fires, then asks
        # again from no earlier than `due`. If that could hand back the same
        # slot, the loop re-fires immediately -- which is exactly what the
        # bench caught on 2026-09-30 (three beacons 150ms apart).
        interval = 90.0
        offset_fn = lambda c: 15.0 * (c % 6)
        due = rns_bridge._next_beacon_at(1000.0, interval, offset_fn)
        again = rns_bridge._next_beacon_at(due, interval, offset_fn)
        self.assertGreater(again, due)

    def test_the_loops_schedule_never_fires_twice_inside_one_burst(self):
        # Simulate the beacon loop's scheduling arithmetic over many cycles,
        # including its non-negative jitter, and assert the node never keys
        # up twice within a burst length of itself.
        interval = 90.0
        offset_fn = lambda c: 15.0 * (c % 6)
        fired = []
        last_due = 0.0
        now = 1000.0
        for _ in range(40):
            now = max(now, last_due)
            due = rns_bridge._next_beacon_at(now, interval, offset_fn)
            # Worst-case jitter, which is where the old symmetric draw broke.
            now = due + rns_bridge._BEACON_SLOT_JITTER_S
            fired.append(due)
            last_due = due
        for earlier, later in zip(fired, fired[1:]):
            self.assertGreater(
                later - earlier, LONG_BURST_S,
                "two beacons scheduled inside one burst of each other")

    def test_a_node_that_redraws_each_cycle_still_gets_a_future_time(self):
        interval = 90.0
        for now in (0.0, 89.999, 90.0, 1000.0, 123456.7):
            due = rns_bridge._next_beacon_at(
                now, interval, lambda c: 15.0 * (c % 6))
            self.assertGreater(due, now, now)
            self.assertLessEqual(due - now, 2 * interval, now)


class TestTheReassemblyWindowCoversEveryRetrySchedule(unittest.TestCase):
    """ONE reassembler serves every link, and the two links' retry schedules are
    built on completely different principles: reliable.py's ARQ retries on a
    timer derived from measured airtime (~105 s worst case), fm_resend's blind
    resends are spaced in wall-clock minutes (~227 s). The buffer has to outlive
    whichever is longer.

    Getting this wrong is the worst kind of bug this codebase has: fragments
    accumulated across attempts are discarded just before the attempt that
    would have completed them, which takes a multi-fragment message from roughly
    60% deliverable to under 2% (docs/open-questions.md:2291) and is visible
    from nowhere except a delivery rate. It looks exactly like a bad radio.
    """

    def test_the_fm_window_outlives_a_whole_blind_resend_schedule(self):
        self.assertGreater(rns_bridge._reassembly_window_s(include_fm=True),
                           fm_resend.span_s())

    def test_it_takes_the_max_rather_than_choosing_a_link(self):
        """Failover is N-way and live: a message's attempt 1 can go out over
        LoRa and attempt 4 over FM after a DEGRADED switch, and fragments from
        both have to land in the same reassembly. The pre-teardown code guarded
        FM out of the resize with an early return, which was right when FM was
        the exclusive transport and is the wrong shape now."""
        both = rns_bridge._reassembly_window_s(include_fm=True)
        lora_only = rns_bridge._reassembly_window_s(include_fm=False)
        self.assertGreaterEqual(both, lora_only)
        self.assertGreater(both, fm_resend.span_s())
        # And it is a max, not a sum -- a window that merely accumulated both
        # schedules would hold slots far longer than anything needs.
        self.assertLess(both, lora_only + fm_resend.span_s())

    def test_the_import_time_value_is_the_safe_floor(self):
        """Built with FM included on purpose. A node whose transport is FM may
        never reach _resize_reassembly at all -- both of that function's callers
        hold an RNode interface -- so the floor has to be safe on its own."""
        self.assertGreaterEqual(rns_bridge._REASSEMBLY_TIMEOUT_S,
                                rns_bridge._reassembly_window_s(include_fm=True))
        self.assertGreater(rns_bridge._REASSEMBLY_TIMEOUT_S,
                           fm_resend.span_s())

    def test_resizing_on_a_non_fm_transport_does_not_pay_for_fm(self):
        """The whole point of tightening it at startup: a LoRa node should not
        hold stalled partials for an FM schedule it will never run."""
        before = rns_bridge._transport
        try:
            rns_bridge._transport = "usb"
            rns_bridge._resize_reassembly(types.SimpleNamespace(bitrate=1074))
            self.assertAlmostEqual(
                rns_bridge._reassembler._timeout,
                rns_bridge._reassembly_window_s(include_fm=False), places=6)
            self.assertLess(rns_bridge._reassembler._timeout,
                            fm_resend.span_s())
        finally:
            rns_bridge._transport = before

    def test_resizing_on_fm_never_shrinks_below_the_blind_schedule(self):
        """The regression this exists to prevent. _resize_reassembly is called
        at startup and on every preset change, and before this fix it would
        shrink an FM node's window to the ARQ span every time."""
        before = rns_bridge._transport
        try:
            rns_bridge._transport = "fm"
            rns_bridge._resize_reassembly(types.SimpleNamespace(bitrate=1074))
            self.assertGreater(rns_bridge._reassembler._timeout,
                               fm_resend.span_s())
        finally:
            rns_bridge._transport = before
            rns_bridge._resize_reassembly(
                types.SimpleNamespace(bitrate=1074))

    def test_a_slower_preset_widens_the_arq_term(self):
        """Picking "Long Slow" takes the link from 1074 bps to 183 bps and every
        ARQ timer stretches with it, so the airtime function has to reach the
        derivation rather than being ignored."""
        fast = rns_bridge._reassembly_window_s(include_fm=False)
        slow = rns_bridge._reassembly_window_s(include_fm=False,
                                               airtime_s=lambda n: n * 0.05)
        self.assertGreater(slow, fast)

    def test_an_fm_backup_node_pays_for_fm_on_a_lora_transport(self):
        """THE CASE THE TRANSPORT TEST ALONE MISSED. In LoRa-primary/FM-backup
        mode _transport is "usb" while FM is built and _escalate_to_fm registers
        real blind schedules on it -- so keying the resize on _transport would
        have excluded FM's span in exactly the configuration that needs it most,
        on the attempts that reach FM only BECAUSE LoRa already failed.
        _resize_reassembly asks whether an FM schedule exists, not which
        transport is selected.
        """
        before_transport = rns_bridge._transport
        before_iface = rns_bridge._fm_iface
        try:
            rns_bridge._transport = "usb"
            rns_bridge._fm_iface = _StubLinkIface()
            rns_bridge._resize_reassembly(types.SimpleNamespace(bitrate=1074))
            self.assertGreater(rns_bridge._reassembler._timeout,
                               fm_resend.span_s())
        finally:
            rns_bridge._transport = before_transport
            rns_bridge._fm_iface = before_iface
            rns_bridge._resize_reassembly(
                types.SimpleNamespace(bitrate=1074))

    def test_the_duplicate_window_covers_the_blind_schedule(self):
        """The OTHER receive-side window, and the one whose failure is visible to
        a user rather than only in a delivery rate: too short and a late blind
        resend is delivered to ATAK again, so a chat message appears twice.
        reliable.py's stock default happens to cover the current span -- pinned
        here so a change to either constant is caught whichever way it goes."""
        self.assertGreaterEqual(reliable.DEFAULT_SEEN_TTL_S, fm_resend.span_s())

    def test_on_a_slow_preset_the_arq_span_can_exceed_fms(self):
        """WHY THIS IS A MAX AND NOT AN EITHER/OR, demonstrated rather than
        argued. It is tempting to assume FM always has the longer schedule and
        simply skip the resize when FM is selected -- which is what the
        pre-teardown code did. On a slow LoRa preset the ARQ span is ~365 s
        against FM's ~284 s, so that assumption inverts, and skipping the
        resize would leave the window too SHORT for the LoRa attempts of the
        very same message.
        """
        slow_airtime = lambda n: n * 0.05
        arq_only = rns_bridge._reassembly_window_s(include_fm=False,
                                                   airtime_s=slow_airtime)
        self.assertGreater(arq_only, fm_resend.span_s() * 1.25)
        both = rns_bridge._reassembly_window_s(include_fm=True,
                                               airtime_s=slow_airtime)
        self.assertAlmostEqual(both, arq_only, places=6)


class TestGatewayIsBuiltButNotWired(unittest.TestCase):
    """Constructed so a second radio is configuration, not new decisions.

    Not called from the receive path: forwarding onto a NAMED link needs
    per-link transmit, and this bridge has one destination and one interface,
    so it would produce decisions nothing could act on.
    """

    def test_the_gateway_exists(self):
        self.assertIsInstance(rns_bridge._gateway, gateway_mod.Gateway)

    def test_it_forwards_nowhere_with_one_radio(self):
        self.assertEqual(
            rns_bridge._gateway.decide("lora", 0x0, 60, hops=3), [])

    def test_it_is_not_called_from_the_receive_path(self):
        # If this starts failing, the comment above it needs deleting too.
        import inspect
        src = inspect.getsource(rns_bridge._consider_relay)
        self.assertNotIn("_gateway", src)


class _StubMesh(object):
    """No real crypto -- _beacon_tick only needs something that accepts
    wrap(frame, hops) and hands back bytes to hand to _transmit_sealed."""
    def wrap(self, frame, hops):
        return frame


class TestTheFmTestHooks(unittest.TestCase):
    """The adb hooks for the 2026-10-12 profile comparison. They must send what
    they say, and the beacon switch must restore."""

    def setUp(self):
        self._beacon_enabled = rns_bridge._beacon_enabled
        self._send = rns_bridge._send_one_beacon
        self._sleep = rns_bridge.time.sleep
        self.sent = []
        rns_bridge._send_one_beacon = lambda *a, **kw: (
            self.sent.append((a, kw)) or (b"f", b"s"))
        rns_bridge.time.sleep = lambda s: None

    def tearDown(self):
        rns_bridge._beacon_enabled = self._beacon_enabled
        rns_bridge._send_one_beacon = self._send
        rns_bridge.time.sleep = self._sleep

    def _join_train(self):
        for t in rns_bridge.threading.enumerate():
            if t.name == "fm-test-train":
                t.join(5)

    def test_a_train_sends_n_numbered_fm_beacons(self):
        self.assertEqual(rns_bridge.fm_test_train(5, 9.0, "A"), 5)
        self._join_train()
        self.assertEqual(len(self.sent), 5)
        seqs = [a[1] for a, _ in self.sent]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertTrue(all(a[0] == rns_bridge.LINK_FM for a, _ in self.sent))
        self.assertTrue(all("fm-test A" in kw["note"] for _, kw in self.sent))

    def test_a_failing_send_does_not_stop_the_train(self):
        calls = []

        def flaky(*a, **kw):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("radio hiccup")
            return (b"f", b"s")
        rns_bridge._send_one_beacon = flaky
        rns_bridge.fm_test_train(4, 1.0, "B")
        self._join_train()
        self.assertEqual(len(calls), 4)

    def test_the_beacon_switch_flips_the_flag(self):
        rns_bridge.fm_test_beacons(False)
        self.assertFalse(rns_bridge._beacon_enabled)
        rns_bridge.fm_test_beacons(True)
        self.assertTrue(rns_bridge._beacon_enabled)

    def test_the_beacon_switch_reads_adb_strings_correctly(self):
        for text, expect in (("0", False), ("false", False), ("off", False),
                             ("1", True), ("on", True), ("TRUE", True)):
            rns_bridge.fm_test_beacons(text)
            self.assertEqual(rns_bridge._beacon_enabled, expect, text)

    def test_the_hook_can_select_the_trial_and_come_back(self):
        self.addCleanup(rns_bridge.fm_test_profile, "fast")
        self.assertEqual(rns_bridge.fm_test_profile("twotone"), "twotone")
        self.assertEqual(rns_bridge.fm_test_profile("fast"), "fast")

    def test_an_unknown_name_falls_back_to_the_default_and_never_raises(self):
        self.addCleanup(rns_bridge.fm_test_profile, "fast")
        self.assertEqual(rns_bridge.fm_test_profile("nope"), "fast")

    def test_the_custom_hook_takes_a_net_code_and_refuses_without_falling_back(self):
        self.addCleanup(rns_bridge.fm_test_profile, "fast")
        code = "4T-1500/300-250Bd-H1024.2048"
        self.assertEqual(rns_bridge.fm_test_custom(code), code)
        # A refused spec leaves the running config alone: a trial that quietly
        # ran the default would be scored as the thing it was not.
        self.assertEqual(rns_bridge.fm_test_custom("4T-1500/100-250Bd-H1024.2048"), "")
        self.assertEqual(fm_link.net_code(), code)


class TestFmReciprocalBeacon(unittest.TestCase):
    """Closes the asymmetry found live 2026-09-26 (see the project memory /
    docs/open-questions.md): a node that fails over to FM stays visible to
    its peer, because its own beacon now arrives over FM -- but nothing
    made the HEALTHY peer beacon back over FM, so IT could go invisible on
    the failed-over node's map for the whole outage. _beacon_tick() must
    also fire a beacon over FM, in addition to the active link's own, the
    moment a connected peer's most recent sighting arrived over FM -- real,
    current evidence that peer cannot otherwise hear us -- and must NOT do
    so unconditionally, which would give up the RX-always-on/TX-only-when-
    needed airtime tradeoff this link's design deliberately made.
    """

    def setUp(self):
        self._orig = dict(
            beacon_enabled=rns_bridge._beacon_enabled,
            destination=rns_bridge._destination,
            sweep_active=rns_bridge._sweep_active,
            mesh=rns_bridge._mesh,
            active_link=rns_bridge._active_link,
            fm_iface=rns_bridge._fm_iface,
            link_ifaces=dict(rns_bridge._link_ifaces),
            peers=rns_bridge._peers,
            beacon_seq=rns_bridge._beacon_seq,
            digest_sent=rns_bridge._digest_sent,
            last_beacon_at=rns_bridge._last_beacon_at,
            last_tx_at=rns_bridge._last_tx_at,
            transmit_sealed=rns_bridge._transmit_sealed,
        )

        rns_bridge._beacon_enabled = True
        rns_bridge._destination = object()  # only needs to be not-None
        rns_bridge._sweep_active = False
        rns_bridge._mesh = _StubMesh()
        rns_bridge._active_link = rns_bridge.LINK_LORA
        rns_bridge._peers = rns_bridge.peers_mod.PeerTable(
            connected_s=rns_bridge._PEER_CONNECTED_S,
            forget_s=rns_bridge._PEER_FORGET_S)
        rns_bridge._beacon_seq = 0
        rns_bridge._digest_sent = None
        rns_bridge._last_beacon_at = 0.0
        rns_bridge._last_tx_at = 0.0

        fm_iface = _StubTxIface()
        rns_bridge._fm_iface = fm_iface
        rns_bridge._link_ifaces = {
            rns_bridge.LINK_LORA: _StubTxIface(),
            rns_bridge.LINK_FM: fm_iface,
        }

        self.sent_links = []

        def _fake_transmit(sealed, link=None):
            self.sent_links.append(link or rns_bridge._active_link)
            return True

        rns_bridge._transmit_sealed = _fake_transmit

    def tearDown(self):
        rns_bridge._beacon_enabled = self._orig["beacon_enabled"]
        rns_bridge._destination = self._orig["destination"]
        rns_bridge._sweep_active = self._orig["sweep_active"]
        rns_bridge._mesh = self._orig["mesh"]
        rns_bridge._active_link = self._orig["active_link"]
        rns_bridge._fm_iface = self._orig["fm_iface"]
        rns_bridge._link_ifaces = self._orig["link_ifaces"]
        rns_bridge._peers = self._orig["peers"]
        rns_bridge._beacon_seq = self._orig["beacon_seq"]
        rns_bridge._digest_sent = self._orig["digest_sent"]
        rns_bridge._last_beacon_at = self._orig["last_beacon_at"]
        rns_bridge._last_tx_at = self._orig["last_tx_at"]
        rns_bridge._transmit_sealed = self._orig["transmit_sealed"]

    def test_no_reciprocal_when_no_peer_needs_it(self):
        rns_bridge._peers.note_frame("peerA", link=rns_bridge.LINK_LORA)
        rns_bridge._beacon_tick()
        self.assertNotIn(rns_bridge.LINK_FM, self.sent_links)

    def test_reciprocal_fires_when_a_connected_peer_was_last_heard_on_fm(self):
        rns_bridge._peers.note_frame("peerA", link=rns_bridge.LINK_FM)
        rns_bridge._beacon_tick()
        self.assertIn(rns_bridge.LINK_FM, self.sent_links)
        # Additive, not a replacement: the primary, digest-carrying beacon
        # on the active link still goes out too.
        self.assertIn(rns_bridge.LINK_LORA, self.sent_links)

    def test_no_reciprocal_while_already_active_on_fm(self):
        rns_bridge._active_link = rns_bridge.LINK_FM
        rns_bridge._peers.note_frame("peerA", link=rns_bridge.LINK_FM)
        rns_bridge._beacon_tick()
        self.assertEqual(self.sent_links, [rns_bridge.LINK_FM])

    def test_no_reciprocal_without_an_fm_interface_built(self):
        rns_bridge._fm_iface = None
        rns_bridge._peers.note_frame("peerA", link=rns_bridge.LINK_FM)
        rns_bridge._beacon_tick()
        self.assertNotIn(rns_bridge.LINK_FM, self.sent_links)

    def test_no_reciprocal_when_the_fm_interface_is_offline(self):
        rns_bridge._fm_iface.online = False
        rns_bridge._peers.note_frame("peerA", link=rns_bridge.LINK_FM)
        rns_bridge._beacon_tick()
        self.assertNotIn(rns_bridge.LINK_FM, self.sent_links)

    def test_a_peer_now_back_on_lora_does_not_trigger_it(self):
        # last_link() is the MOST RECENT radio, not "ever" -- see
        # peers.heard_on()'s own docstring for the distinction. A peer that
        # moved back to LoRa no longer needs a reciprocal FM beacon.
        rns_bridge._peers.note_frame("peerA", link=rns_bridge.LINK_FM)
        rns_bridge._peers.note_frame("peerA", link=rns_bridge.LINK_LORA)
        rns_bridge._beacon_tick()
        self.assertNotIn(rns_bridge.LINK_FM, self.sent_links)

    def test_a_disconnected_peer_last_heard_on_fm_does_not_trigger_it(self):
        clock = [1000.0]
        rns_bridge._peers = rns_bridge.peers_mod.PeerTable(
            connected_s=10, forget_s=600, clock=lambda: clock[0])
        rns_bridge._peers.note_frame("peerA", link=rns_bridge.LINK_FM)
        clock[0] += 11  # past connected_s
        rns_bridge._beacon_tick()
        self.assertNotIn(rns_bridge.LINK_FM, self.sent_links)


class TestTheSequenceGapLineSaysWhetherWeWereTransmitting(unittest.TestCase):
    """A numbered frame that never arrived is a loss with no burst line to
    explain it when this node's own transmitter covered the interval, because
    the receive path is not fed while the radio keys up. The gap line is then
    the ONLY trace, so it has to say so itself (2026-10-09: bursts 14 and 20 of
    a 26-burst run, both under the phone's own beacons)."""

    class _Rx(object):
        def __init__(self, answer):
            self.answer = answer
            self.asked = None

        def tx_within(self, seconds):
            self.asked = seconds
            return self.answer

    def setUp(self):
        import fm_link
        self.fm_link = fm_link
        self.logged = []
        self._real_log = rns_bridge.RNS.log
        rns_bridge.RNS.log = lambda m, level=0, *a, **k: self.logged.append(m)
        self.addCleanup(setattr, rns_bridge.RNS, "log", self._real_log)
        self._real_rx = fm_link._RX[0]
        self.addCleanup(fm_link._RX.__setitem__, 0, self._real_rx)

    def gap(self, since_s=25.0):
        rns_bridge._log_seq_gap(b"\x01\x02\x03", 12, 14, 1, since_s, "bench")
        return self.logged[-1]

    def test_it_names_the_transmit_when_there_was_one(self):
        self.fm_link._RX[0] = self._Rx((2, 7.0))
        line = self.gap()
        self.assertIn("missed 1 frame(s) (seq 12 -> 14", line)
        self.assertIn("transmitted 2 time(s), 7.0 s on air", line)
        self.assertIn("a likely cause", line)

    def test_it_says_when_our_transmitter_does_not_explain_it(self):
        self.fm_link._RX[0] = self._Rx((0, 0.0))
        self.assertIn("did NOT transmit", self.gap())

    def test_it_looks_back_over_the_whole_interval_plus_a_margin(self):
        rx = self._Rx((0, 0.0))
        self.fm_link._RX[0] = rx
        self.gap(since_s=25.0)
        self.assertEqual(rx.asked, 28.0)

    def test_it_stays_silent_about_transmits_without_a_receive_path(self):
        self.fm_link._RX[0] = None
        line = self.gap()
        self.assertIn("missed 1 frame(s)", line)
        self.assertNotIn("transmit", line)

    def test_a_failing_receive_path_never_costs_the_log_line(self):
        class Boom(object):
            def tx_within(self, seconds):
                raise RuntimeError("no")
        self.fm_link._RX[0] = Boom()
        self.assertIn("missed 1 frame(s)", self.gap())


class TestPeerPositionsForTheCoverageMap(unittest.TestCase):
    """peer_positions() is what the Coverage tab draws peers' footprints
    from. It is fed at the same point as the range-test book -- a decoded
    position frame -- and the chain from a frame off the air to that JSON is
    tested whole, because a break in it is silent: the map just shows no
    peers, which is also what an empty mesh looks like."""

    SENDER = "a1b2c3d4e5f6"

    def setUp(self):
        rb = rns_bridge
        self._tables = {n: (getattr(rb, n), type(getattr(rb, n))(getattr(rb, n)))
                        for n in ("_atak_uid_to_node", "_last_marker", "_sa_rx",
                                  "_peer_positions")}
        self._objects = {n: (getattr(rb, n), copy.deepcopy(vars(getattr(rb, n))))
                         for n in ("_tx_policy", "_reliable_rx", "_rangetest")}
        rb._peer_positions.clear()
        rb._sa_rx.pop(self.SENDER, None)
        self._sa_tx = cot_codec.SaTx()      # the sender's own position stream
        self._t = 0

    def tearDown(self):
        for n, (live, saved) in self._tables.items():
            live.clear()
            live.update(saved)
        for n, (live, saved) in self._objects.items():
            vars(live).clear()
            vars(live).update(saved)

    def _deliver(self, frame):
        rns_bridge._handle_frame(frame, None, None, self.SENDER,
                                 link=rns_bridge.LINK_LORA)

    def _arrive(self, lat, lon):
        """A position report, encoded the way the sender's bridge encodes its
        own (through its self-SA session -- see build_packets' _sa_tx)."""
        self._t += 30
        xml = _pli_xml("ANDROID-peer-pos", lat=lat, lon=lon,
                       time_s="2026-07-31T10:%02d:%02dZ" % divmod(self._t, 60))
        frame = cot_codec.encode(xml, cot_codec.RNS_MAX_PAYLOAD - mesh.OVERHEAD,
                                 session=self._sa_tx)
        self._sa_tx.commit()
        self._deliver(frame)

    def _arrive_other(self, xml):
        """A marker or chat from the same sender: no self-SA session."""
        self._deliver(cot_codec.encode(xml, cot_codec.RNS_MAX_PAYLOAD - mesh.OVERHEAD))

    def _positions(self):
        out = json.loads(rns_bridge.peer_positions())
        self.assertIsInstance(out, list, out)
        return out

    def test_a_received_position_reaches_the_coverage_map(self):
        self._arrive(20.9120, -140.5823)
        got = self._positions()
        self.assertEqual(len(got), 1, got)
        self.assertEqual(got[0]["node_id"], self.SENDER)
        self.assertAlmostEqual(got[0]["lat"], 20.9120, places=4)
        self.assertAlmostEqual(got[0]["lon"], -140.5823, places=4)
        self.assertLess(got[0]["age_s"], 5.0)
        self.assertTrue(got[0]["name"])

    def test_a_later_position_moves_the_peer(self):
        self._arrive(20.9120, -140.5823)
        self._arrive(20.9175, -140.5833)
        got = self._positions()
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0]["lat"], 20.9175, places=4)

    def test_a_received_position_teaches_which_node_its_uid_is_on(self):
        """So a direct chat to that contact waits for THAT node's ack. Before
        2026-10-23 only a chat from the contact taught this (the callsign
        route tests <takv>, which the codec strips), and a chat to a contact
        that had never chatted us was settled by whoever answered first --
        on the Stage 3 bench, the relay."""
        self.assertIsNone(rns_bridge._chat_addressee_node(
            "GeoChat.ANDROID-me.ANDROID-peer-pos.6dc8a74c"))
        self._arrive(20.9120, -140.5823)
        self.assertEqual(rns_bridge._atak_uid_to_node.get("ANDROID-peer-pos"), self.SENDER)
        self.assertEqual(rns_bridge._chat_addressee_node(
            "GeoChat.ANDROID-me.ANDROID-peer-pos.6dc8a74c"), self.SENDER)

    def test_a_marker_teaches_no_uid(self):
        import test_cot_codec
        self._arrive_other(test_cot_codec.MARKER)
        self.assertNotIn(self.SENDER, rns_bridge._atak_uid_to_node.values())

    def test_a_marker_or_chat_is_not_a_peer_position(self):
        """A marker carries coordinates, but they are where the PIN is. Both
        arrive as compact frames -- the frame-kind test the range-test book
        uses lets them through -- so this pins the stronger one."""
        import test_cot_codec
        self._arrive_other(test_cot_codec.MARKER)
        self._arrive_other(DIRECT_CHAT_XML)
        self.assertEqual(self._positions(), [])
        # And after the sender's real position is known, a pin it drops
        # 7000 km away must not move it there.
        self._arrive(20.9120, -140.5823)
        self._arrive_other(test_cot_codec.MARKER)
        got = self._positions()
        self.assertAlmostEqual(got[0]["lat"], 20.9120, places=4)
        self.assertAlmostEqual(got[0]["lon"], -140.5823, places=4)

    def test_a_range_test_starting_does_not_empty_the_map(self):
        self._arrive(20.9120, -140.5823)
        rns_bridge._rangetest = rangetest.RangeTest(distance_fn=lambda *a: 0.0,
                                                    clock=time.monotonic)
        try:
            self.assertEqual(len(self._positions()), 1)
        finally:
            rns_bridge._rangetest = self._objects["_rangetest"][0]

    def test_old_reports_are_left_out(self):
        self._arrive(20.9120, -140.5823)
        lat, lon, at = rns_bridge._peer_positions[self.SENDER]
        rns_bridge._peer_positions[self.SENDER] = (
            lat, lon, at - rns_bridge._PEER_POSITION_MAX_AGE_S - 1)
        self.assertEqual(self._positions(), [])

    def test_null_island_and_our_own_id_are_ignored(self):
        rns_bridge._note_peer_position(self.SENDER, 0.0, 0.0)
        rns_bridge._note_peer_position(rns_bridge._NODE_ID, 20.8175, -140.5133)
        self.assertEqual(self._positions(), [])

    def test_newest_first_and_bounded(self):
        for i in range(rns_bridge._PEER_POSITION_MAX + 3):
            rns_bridge._note_peer_position("node%02d" % i, 20.3175 + i * 0.001, -140.4133)
        got = self._positions()
        self.assertEqual(len(got), rns_bridge._PEER_POSITION_MAX)
        self.assertEqual(got[0]["node_id"], "node%02d" % (rns_bridge._PEER_POSITION_MAX + 2))


class TestPeersAwaitingAPosition(TestPeerPositionsForTheCoverageMap):
    """peers_awaiting_position() is what the Coverage tab says for a node it
    hears but cannot draw. A beacon carries no position, so until that
    node's ATAK has a fix it is on the mesh and off the map -- and the map
    alone cannot tell that apart from a node that is not there."""

    def setUp(self):
        super().setUp()
        self._orig_peers = rns_bridge._peers
        rns_bridge._peers = rns_bridge.peers_mod.PeerTable(
            connected_s=rns_bridge._PEER_CONNECTED_S,
            forget_s=rns_bridge._PEER_FORGET_S)

    def tearDown(self):
        rns_bridge._peers = self._orig_peers
        super().tearDown()

    def _awaiting(self):
        out = json.loads(rns_bridge.peers_awaiting_position())
        self.assertIsInstance(out, list, out)
        return [p["node_id"] for p in out]

    def test_a_heard_peer_with_no_position_is_listed_until_one_arrives(self):
        rns_bridge._peers.note_frame(self.SENDER, link=rns_bridge.LINK_LORA)
        self.assertEqual(self._awaiting(), [self.SENDER])
        self._arrive(20.9120, -140.5823)
        self.assertEqual(self._awaiting(), [])

    def test_a_stale_position_counts_as_none(self):
        rns_bridge._peers.note_frame(self.SENDER, link=rns_bridge.LINK_LORA)
        self._arrive(20.9120, -140.5823)
        lat, lon, at = rns_bridge._peer_positions[self.SENDER]
        rns_bridge._peer_positions[self.SENDER] = (
            lat, lon, at - rns_bridge._PEER_POSITION_MAX_AGE_S - 1)
        self.assertEqual(self._awaiting(), [self.SENDER])

    def test_a_peer_not_heard_lately_is_not_waited_for(self):
        rns_bridge._peers.note_frame(self.SENDER, link=rns_bridge.LINK_LORA)
        rns_bridge._peers._peers[self.SENDER]["last_seen"] -= rns_bridge._PEER_CONNECTED_S + 1
        self.assertEqual(self._awaiting(), [])


class TestAPinIsNotWhereItsSenderIs(TestPeerPositionsForTheCoverageMap):
    """The same gate decides three books: the range test's fixes, the marker
    kept for withdrawing a departed peer, and the coverage map's peers. Until
    2026-10-20 the first two used the frame kind, which a placed marker and a
    chat pass too -- so a pin was booked as its sender's position, and was
    the thing withdrawn (deleted from the local map) when that peer went
    quiet. Driven through _handle_frame with the reliable wrapper on, exactly
    as a marker and a chat arrive off the air."""

    def _arrive_reliable(self, xml, msg_id):
        encoded = cot_codec.encode(xml, cot_codec.RNS_MAX_PAYLOAD - mesh.OVERHEAD)
        _, wrapped = cot_codec.wrap_reliable(encoded, self.SENDER, msg_id,
                                            any_ack=False, addressee=None)
        self._deliver(wrapped)

    def _withdrawn(self):
        sent = []
        orig = (rns_bridge._send_to_atak, rns_bridge._peer_snapshot)
        rns_bridge._send_to_atak = lambda xml, what: sent.append(xml) or True
        rns_bridge._peer_snapshot = lambda: []          # everyone has gone quiet
        try:
            rns_bridge._withdraw_departed()
        finally:
            rns_bridge._send_to_atak, rns_bridge._peer_snapshot = orig
        return sent

    def test_the_marker_and_chat_really_are_compact_frames(self):
        """The premise: if these stopped being compact frames, the old gate
        would look correct and these tests would prove nothing."""
        import test_cot_codec
        for xml in (test_cot_codec.MARKER, DIRECT_CHAT_XML):
            frame = cot_codec.encode(xml, cot_codec.RNS_MAX_PAYLOAD - mesh.OVERHEAD)
            self.assertIn(frame[0] & 0x0F, cot_codec.COMPACT_KINDS)

    def test_range_test_keeps_the_senders_position_not_its_pin(self):
        import test_cot_codec
        self._arrive(20.9120, -140.5823)
        self._arrive_reliable(test_cot_codec.MARKER, 41)
        self._arrive_reliable(DIRECT_CHAT_XML, 42)
        fix = rns_bridge._rangetest.fix_for(self.SENDER)
        self.assertAlmostEqual(fix.lat, 20.9120, places=4)
        self.assertAlmostEqual(fix.lon, -140.5823, places=4)

    def test_a_pin_alone_gives_the_range_test_no_position(self):
        import test_cot_codec
        self._arrive_reliable(test_cot_codec.MARKER, 43)
        self.assertIsNone(rns_bridge._rangetest.fix_for(self.SENDER))
        self.assertNotIn(self.SENDER, rns_bridge._last_marker)

    def test_a_departed_peer_withdraws_its_own_dot_not_its_pin(self):
        import test_cot_codec
        self._arrive(20.9120, -140.5823)
        self._arrive_reliable(test_cot_codec.MARKER, 44)
        sent = self._withdrawn()
        self.assertEqual(len(sent), 1)
        self.assertIn(b'uid="ANDROID-peer-pos"', sent[0])
        self.assertNotIn(b"0ef9c3a9-bf54-46e5-9655-29956a0d80ba", sent[0])

    def test_a_peer_that_only_dropped_a_pin_withdraws_nothing(self):
        import test_cot_codec
        self._arrive_reliable(test_cot_codec.MARKER, 45)
        self.assertEqual(self._withdrawn(), [])


class TestCoverageRadioIsTheRadioInUse(unittest.TestCase):
    """coverage_radio() hands the Coverage tab the LoRa numbers of the radio
    actually transmitting: what an RNode REPORTS, what a Meshtastic board is
    set to -- not only the app's saved settings, which a board need not run
    and an RNode can have clamped."""

    SAVED = {"preset": "Short Slow", "frequency_hz": 915000000, "txpower_dbm": 22}

    def setUp(self):
        self._orig = (rns_bridge._lora_iface, rns_bridge._radio_firmware,
                      rns_bridge._load_radio_config)
        rns_bridge._load_radio_config = lambda: dict(self.SAVED)

    def tearDown(self):
        (rns_bridge._lora_iface, rns_bridge._radio_firmware,
         rns_bridge._load_radio_config) = self._orig

    def _ask(self):
        return json.loads(rns_bridge.coverage_radio())

    def test_no_radio_uses_the_app_settings_and_says_so(self):
        rns_bridge._lora_iface = None
        r = self._ask()
        self.assertEqual((r["preset"], r["frequency_mhz"], r["txpower_dbm"]),
                         ("Short Slow", "915.000", 22))
        self.assertIn("not connected", r["source"])

    def test_an_rnode_gives_what_it_reports(self):
        rns_bridge._radio_firmware = rns_bridge.RADIO_RNODE
        rns_bridge._lora_iface = types.SimpleNamespace(
            online=True, r_txpower=17, r_frequency=906875000)
        r = self._ask()
        self.assertEqual((r["txpower_dbm"], r["frequency_mhz"]), (17, "906.875"))
        self.assertEqual(r["preset"], "Short Slow")
        self.assertIn("RNode", r["source"])

    def test_an_rnode_not_yet_reporting_falls_back(self):
        rns_bridge._radio_firmware = rns_bridge.RADIO_RNODE
        rns_bridge._lora_iface = types.SimpleNamespace(online=False, r_txpower=None)
        self.assertIn("not connected", self._ask()["source"])

    def _board(self, **snap):
        # A stand-in mt_config: the real one imports the meshtastic package,
        # which the desktop suite does not have, and only snapshot() -- the
        # board's own settings, already tested in test_mt_config -- is used.
        base = {"tx_power_dbm": 27, "preset": "LONG_FAST", "preset_name": "Long Fast",
                "override_frequency_mhz": 906.875}
        base.update(snap)
        fake = types.ModuleType("mt_config")
        fake.snapshot = lambda node: base
        port = types.SimpleNamespace(online=True, node=lambda: object())
        rns_bridge._radio_firmware = rns_bridge.RADIO_MESHTASTIC
        rns_bridge._lora_iface = types.SimpleNamespace(port=port)
        orig = sys.modules.get("mt_config")
        sys.modules["mt_config"] = fake
        try:
            return self._ask()
        finally:
            if orig is None:
                sys.modules.pop("mt_config", None)
            else:
                sys.modules["mt_config"] = orig

    def test_a_meshtastic_board_gives_its_own_settings(self):
        r = self._board()
        self.assertEqual((r["preset"], r["frequency_mhz"], r["txpower_dbm"]),
                         ("Long Fast", "906.875", 27))
        self.assertEqual(r["source"], "Meshtastic board")

    def test_board_default_power_is_assumed_and_labelled(self):
        r = self._board(tx_power_dbm=0)
        self.assertEqual(r["txpower_dbm"], rns_bridge._MT_DEFAULT_TX_DBM_ASSUMED)
        self.assertIn("assumed", r["source"])

    def test_board_without_an_app_preset_or_override_falls_back_and_says_so(self):
        r = self._board(preset="LONG_TURBO", preset_name=None, override_frequency_mhz=0.0)
        self.assertEqual((r["preset"], r["frequency_mhz"]), ("Short Slow", "915.000"))
        self.assertIn("no app name", r["source"])
        self.assertIn("slot", r["source"])

    def test_the_answer_feeds_coverage(self):
        import coverage
        r = self._board(tx_power_dbm=27)
        radio = coverage.resolve_radio("lora", {
            "lora_preset": r["preset"], "lora_freq_mhz": r["frequency_mhz"],
            "lora_tx_dbm": r["txpower_dbm"], "lora_source": r["source"]})
        self.assertEqual(radio["tx_dbm"], 27.0)
        self.assertEqual(radio["sens_dbm"], -131.5)          # Long Fast


class TestCotDumpBenchFixture(unittest.TestCase):
    """The bench's CoT dump: off unless its file exists, verbatim when on,
    capped, and never in the way of delivery."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (rns_bridge._COT_DUMP_PATH, rns_bridge._COT_DUMP_MAX_B)
        rns_bridge._COT_DUMP_PATH = os.path.join(self._tmp.name, "dump")

    def tearDown(self):
        rns_bridge._COT_DUMP_PATH, rns_bridge._COT_DUMP_MAX_B = self._saved
        self._tmp.cleanup()

    def test_nothing_is_written_without_the_file(self):
        rns_bridge._cot_dump("TO ATAK", b"<event/>")
        self.assertFalse(os.path.exists(rns_bridge._COT_DUMP_PATH))

    def test_both_directions_are_kept_verbatim(self):
        open(rns_bridge._COT_DUMP_PATH, "wb").close()
        rns_bridge._cot_dump("FROM ATAK", b'<event uid="a"/>')
        rns_bridge._cot_dump("TO ATAK", b'<event uid="b"/>')
        with open(rns_bridge._COT_DUMP_PATH, "rb") as f:
            out = f.read()
        self.assertIn(b'FROM ATAK 16 bytes\n<event uid="a"/>\n', out)
        self.assertIn(b'TO ATAK 16 bytes\n<event uid="b"/>\n', out)

    def test_it_stops_at_the_cap(self):
        rns_bridge._COT_DUMP_MAX_B = 10
        with open(rns_bridge._COT_DUMP_PATH, "wb") as f:
            f.write(b"x" * 11)
        rns_bridge._cot_dump("TO ATAK", b"<event/>")
        self.assertEqual(os.path.getsize(rns_bridge._COT_DUMP_PATH), 11)


class TestGaveUpReport(unittest.TestCase):
    """What the log says when the active link's ARQ gives up on a message. On
    2026-10-23 a chat handed to FM was logged "[Error] UNDELIVERED" and then
    arrived over FM 5 s later."""

    def setUp(self):
        self._saved = (rns_bridge._fm_iface, rns_bridge._active_link,
                       rns_bridge._escalate_to_fm, rns_bridge.RNS.log)
        # The stub gives every level the value 0; tell these two apart.
        self._levels = (rns_bridge.RNS.LOG_WARNING, rns_bridge.RNS.LOG_ERROR)
        rns_bridge.RNS.LOG_WARNING, rns_bridge.RNS.LOG_ERROR = "warning", "error"
        self.escalated, self.logged = [], []
        rns_bridge._escalate_to_fm = self.escalated.append
        rns_bridge.RNS.log = lambda msg, level=None: self.logged.append((level, msg))
        rns_bridge._active_link = rns_bridge.LINK_LORA
        self.record = types.SimpleNamespace(
            label="b-t-f GeoChat.x", attempts=4, reason=reliable.NO_ACK,
            missing={"aaaa01"}, msg_id=24307)

    def tearDown(self):
        (rns_bridge._fm_iface, rns_bridge._active_link,
         rns_bridge._escalate_to_fm, rns_bridge.RNS.log) = self._saved
        rns_bridge.RNS.LOG_WARNING, rns_bridge.RNS.LOG_ERROR = self._levels

    def test_handed_to_fm_is_a_warning_not_undelivered(self):
        rns_bridge._fm_iface = object()
        rns_bridge._on_gave_up(self.record)
        self.assertEqual(self.escalated, [self.record])
        (level, msg), = self.logged
        self.assertEqual(level, "warning")
        self.assertNotIn("UNDELIVERED", msg)
        self.assertIn("no ack on lora", msg)
        self.assertIn("after 4 transmission(s)", msg)
        self.assertIn("continuing on FM", msg)

    def test_nowhere_left_to_try_is_still_an_undelivered_error(self):
        rns_bridge._fm_iface = None
        rns_bridge._on_gave_up(self.record)
        self.assertEqual(self.escalated, [])
        (level, msg), = self.logged
        self.assertEqual(level, "error")
        self.assertIn("UNDELIVERED b-t-f GeoChat.x after 4 transmission(s) on lora", msg)
