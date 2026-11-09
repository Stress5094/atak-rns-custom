"""That the Meshtastic link's size, airtime model and Interface agree with each
other and with the layers above them.

What each group is for:

  * THE SIZE LIMIT. 231 B is a measurement off a real board (see mt_link's
    docstring), so these tests pin that it is the number in use and that the
    arithmetic around it closes: 231 + the board's 24 B wrapper is exactly the
    255 B LoRa ceiling.
  * THE AIRTIME MODEL. A formula is only worth anything checked against a figure
    that did not come from this file, so one published Semtech case anchors it.
  * THE INTERFACE. Driven against a fake board. The properties that matter are
    that an oversize frame is refused SYNCHRONOUSLY (the board's own answer is
    an error that arrives after the call has already returned), that an offline
    link refuses, and that signal stats are set before delivery.
  * FRAGMENTATION. The reason this link is worth having is that a message the
    29 B FM link cannot carry at all crosses it. The test runs the real
    fragmenter, the real mesh envelope and the real reassembler across the fake
    board, and checks no sealed frame ever exceeds the limit.
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rns_stub

rns_stub.install()

import airtime
import cot_codec
import mesh
import mt_link
import radio


class FakePort(object):
    """Stands in for a Meshtastic board. `peer` is another FakePort: whatever
    one sends, the other receives, like two boards in range."""

    def __init__(self):
        self.sent = []
        self.peer = None
        self.last_error = None
        self._receiver = None
        self._listener = None
        self.accept = True

    def send(self, payload):
        self.sent.append(payload)
        if self.peer is not None and self.peer._receiver is not None:
            self.peer._receiver(payload, -91, 6.5, 0x1234)
        return self.accept

    def set_receiver(self, fn):
        self._receiver = fn

    def set_state_listener(self, fn):
        self._listener = fn

    def go(self, online):
        self._listener(online)


class Owner(object):
    def inbound(self, data, iface):
        pass


def make_iface(deliver=None, online=True):
    port = FakePort()
    iface = mt_link.MtInterface(Owner(), port, deliver_frame=deliver)
    if online:
        port.go(True)
    return iface, port


class TestSizeLimit(unittest.TestCase):

    def test_the_limit_is_the_measured_231(self):
        self.assertEqual(mt_link.hw_mtu(), 231)

    def test_the_wrapper_arithmetic_closes_on_the_lora_ceiling(self):
        # Not a derivation of 231 -- that was measured -- but a check that the
        # measurement and the stated wrapper cost agree with each other.
        self.assertEqual(mt_link.PAYLOAD_MAX_B + mt_link.WIRE_OVERHEAD_B,
                         mt_link.LORA_PAYLOAD_MAX_B)

    def test_it_is_smaller_than_what_rns_assumes_for_lora(self):
        # This is why the MTU is asked of the link: sizing against 464 B is the
        # mistake that took the FM link offline on its first send.
        self.assertLess(mt_link.hw_mtu(), cot_codec.RNS_MAX_PAYLOAD)

    def test_the_interface_reports_the_same_limit(self):
        iface, _ = make_iface()
        self.assertEqual(iface.HW_MTU, mt_link.hw_mtu())

    def test_the_mesh_envelope_matches_the_one_airtime_assumes(self):
        self.assertEqual(mesh.OVERHEAD, airtime.MESH_ENVELOPE_BYTES)


class TestAirtimeModel(unittest.TestCase):

    def test_formula_matches_a_published_semtech_figure(self):
        # SF7, 125 kHz, CR 4/5, 8-symbol preamble, explicit header, CRC, 10 B
        # payload: the widely quoted 41.2 ms (Semtech's calculator: 41.216 ms).
        got = mt_link._lora_toa_s(10, 7, 125000, 5, 8)
        self.assertAlmostEqual(got, 0.041216, places=5)

    def test_a_full_long_fast_packet_is_about_two_seconds(self):
        t = mt_link.time_on_air_s(mt_link.hw_mtu(), "Long Fast")
        self.assertAlmostEqual(t, 2.157, places=2)

    def test_longer_packets_take_longer(self):
        a = mt_link.time_on_air_s(20)
        b = mt_link.time_on_air_s(120)
        c = mt_link.time_on_air_s(231)
        self.assertLess(a, b)
        self.assertLess(b, c)

    def test_a_slower_preset_takes_longer(self):
        fast = mt_link.time_on_air_s(100, "Short Fast")
        slow = mt_link.time_on_air_s(100, "Long Slow")
        self.assertLess(fast, slow)

    def test_the_airtime_link_charges_a_full_packet_its_modelled_time(self):
        link = mt_link.airtime_link("Long Fast")
        self.assertAlmostEqual(link.frame_s(mt_link.hw_mtu()),
                               mt_link.time_on_air_s(mt_link.hw_mtu(),
                                                     "Long Fast"),
                               places=9)

    def test_every_preset_has_a_model(self):
        for name in radio.PRESETS:
            self.assertGreater(mt_link.bitrate_bps(name), 0, name)

    def test_negative_payload_is_refused(self):
        with self.assertRaises(ValueError):
            mt_link.time_on_air_s(-1)


class TestInterface(unittest.TestCase):

    def test_starts_offline_until_the_port_says_otherwise(self):
        iface, _ = make_iface(online=False)
        self.assertFalse(iface.online)
        self.assertFalse(iface.process_outgoing(b"x"))

    def test_follows_the_port_online_and_offline(self):
        iface, port = make_iface()
        self.assertTrue(iface.online)
        port.go(False)
        self.assertFalse(iface.online)
        self.assertFalse(iface.process_outgoing(b"x"))

    def test_sends_through_the_port_and_counts_bytes(self):
        iface, port = make_iface()
        self.assertTrue(iface.process_outgoing(b"hello"))
        self.assertEqual(port.sent, [b"hello"])
        self.assertEqual(iface.txb, 5)

    def test_a_frame_at_the_limit_goes(self):
        iface, port = make_iface()
        self.assertTrue(iface.process_outgoing(bytes(mt_link.hw_mtu())))

    def test_a_frame_one_over_the_limit_is_refused_and_never_reaches_the_board(self):
        iface, port = make_iface()
        self.assertFalse(iface.process_outgoing(bytes(mt_link.hw_mtu() + 1)))
        self.assertEqual(port.sent, [])
        self.assertEqual(iface.oversize_refused, 1)
        self.assertEqual(iface.txb, 0)

    def test_a_port_that_declines_is_reported_as_a_failure(self):
        iface, port = make_iface()
        port.accept = False
        self.assertFalse(iface.process_outgoing(b"x"))
        self.assertEqual(iface.txb, 0)

    def test_a_port_that_raises_oserror_is_a_failure_not_a_crash(self):
        iface, port = make_iface()

        def boom(payload):
            raise OSError("cable pulled")
        port.send = boom
        self.assertFalse(iface.process_outgoing(b"x"))

    def test_receive_sets_signal_stats_before_delivery(self):
        seen = []

        def deliver(data, iface):
            seen.append((data, iface.r_stat_rssi, iface.r_stat_snr))
        iface, port = make_iface(deliver)
        port._receiver(b"abc", -97, 4.25, 0xBEEF)
        self.assertEqual(seen, [(b"abc", -97, 4.25)])
        self.assertEqual(iface.last_sender, 0xBEEF)
        self.assertEqual(iface.rxb, 3)

    def test_set_preset_changes_the_airtime_model(self):
        iface, _ = make_iface()
        before = iface.bitrate
        iface.set_preset("Short Fast")
        self.assertEqual(iface.preset, "Short Fast")
        self.assertGreater(iface.bitrate, before)

    def test_set_preset_ignores_a_name_it_does_not_know(self):
        iface, _ = make_iface()
        before = (iface.preset, iface.bitrate)
        iface.set_preset("Warp Speed")
        self.assertEqual((iface.preset, iface.bitrate), before)

    def test_empty_receive_is_ignored(self):
        seen = []
        iface, port = make_iface(lambda d, i: seen.append(d))
        port._receiver(b"", -97, 4.25, 1)
        self.assertEqual(seen, [])


class TestFragmentationAcrossTheLink(unittest.TestCase):
    """Real fragmenter + real mesh envelope + real reassembler, over the fake
    board. A message too big for one packet must cross as several, every sealed
    frame within the limit, and come out the far end byte-identical."""

    PASS = "bench passphrase"

    def setUp(self):
        if not mesh.HAVE_AEAD:
            self.skipTest("no AEAD backend")
        key = mesh.derive_key(self.PASS, rounds=1)
        self.tx = mesh.Mesh("aa11bb", key)
        self.rx = mesh.Mesh("cc22dd", key)
        self.budget = mt_link.hw_mtu() - mesh.OVERHEAD

    def _cross(self, frame):
        """Send `frame` end to end, returning (reassembled, sealed_sizes)."""
        a, pa = make_iface()
        got = []
        reasm = cot_codec.Reassembler()

        def deliver(data, iface):
            inbound = self.rx.unwrap(data)
            if inbound is None:
                return
            whole = reasm.offer(inbound.frame)
            if whole is not None:
                got.append(whole)
        b, pb = make_iface(deliver)
        pa.peer = pb
        sizes = []
        for piece in cot_codec.fragment(frame, self.budget):
            sealed = self.tx.wrap(piece)
            sizes.append(len(sealed))
            self.assertTrue(a.process_outgoing(sealed))
        return got, sizes

    def _frame(self, n, seed=1):
        rng = random.Random(seed)
        # Leading 0x00 so Reassembler.offer() cannot mistake a one-packet frame
        # for a fragment header (it looks at the high nibble for the version).
        return b"\x00" + bytes(rng.getrandbits(8) for _ in range(n - 1))

    def test_the_budget_is_the_limit_less_the_envelope(self):
        self.assertEqual(self.budget, 231 - 12)

    def test_a_frame_that_fits_goes_as_one_packet(self):
        got, sizes = self._cross(self._frame(self.budget))
        self.assertEqual(len(sizes), 1)
        self.assertEqual(sizes[0], mt_link.hw_mtu())
        self.assertEqual(len(got), 1)

    def test_a_frame_one_byte_over_becomes_two_and_every_one_fits(self):
        frame = self._frame(self.budget + 1)
        got, sizes = self._cross(frame)
        self.assertEqual(len(sizes), 2)
        self.assertTrue(all(s <= mt_link.hw_mtu() for s in sizes), sizes)
        self.assertEqual(got, [frame])

    def test_a_large_message_survives_the_trip_byte_for_byte(self):
        frame = self._frame(1500, seed=7)
        got, sizes = self._cross(frame)
        self.assertGreater(len(sizes), 5)
        self.assertTrue(all(s <= mt_link.hw_mtu() for s in sizes), sizes)
        self.assertEqual(got, [frame])

    def test_the_most_the_fragment_cap_allows_still_fits_and_one_more_does_not(self):
        per = self.budget - cot_codec.FRAG_HEADER_LEN
        biggest = per * cot_codec.MAX_FRAGMENTS
        got, sizes = self._cross(self._frame(biggest, seed=3))
        self.assertEqual(len(sizes), cot_codec.MAX_FRAGMENTS)
        self.assertEqual(len(got), 1)
        with self.assertRaises(cot_codec.CotEncodeError):
            cot_codec.fragment(self._frame(biggest + 1), self.budget)

    def test_the_whole_message_ceiling_beats_the_fm_links_192_bytes(self):
        per = self.budget - cot_codec.FRAG_HEADER_LEN
        ceiling = per * cot_codec.MAX_FRAGMENTS
        self.assertGreater(ceiling, 3000)
        self.assertGreater(ceiling, 192)

    def test_a_full_fragment_train_costs_what_the_model_says(self):
        # 16 full packets back to back on Long Fast: the airtime a maximal
        # message occupies, which is what the cap is there to bound.
        t = mt_link.train_airtime_s(cot_codec.MAX_FRAGMENTS, "Long Fast")
        self.assertAlmostEqual(t, 16 * 2.157, delta=0.2)


if __name__ == "__main__":
    unittest.main()
