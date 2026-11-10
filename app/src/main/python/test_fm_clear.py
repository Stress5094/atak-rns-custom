"""Tests for "send FM unencrypted": the T_CLEAR envelope (mesh.Mesh.to_clear /
unwrap) and its one hook in rns_bridge._transmit_sealed.

What must hold: a clear frame carries the inner frame byte for byte with no
key involved; it keeps the sealed frame's identity (origin, epoch, seq, hops)
so duplicate suppression and relaying are unchanged; a node that has not opted
in ignores clear frames; and only the FM link is ever stripped.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rns_stub

rns_stub.install()

import cot_codec
import mesh
import rns_bridge

A = b"\x01\x02\x03"
B = b"\x0a\x0b\x0c"
INNER = bytes([cot_codec.CTRL_BEACON]) + b"position report body"


def key(passphrase="bench passphrase"):
    return mesh.derive_key(passphrase, rounds=1)


class TestClearEnvelope(unittest.TestCase):

    def test_plaintext_is_on_the_wire(self):
        a = mesh.Mesh(A, key())
        sealed = a.wrap(INNER, hops=2)
        self.assertNotIn(b"position report body", sealed)
        clear = a.to_clear(sealed)
        self.assertTrue(mesh.is_clear(clear))
        self.assertFalse(mesh.is_mesh(clear))
        self.assertEqual(clear[mesh.HEADER_LEN:], INNER)
        # Same identity: origin, epoch, seq, hops untouched.
        self.assertEqual(clear[1:mesh.HEADER_LEN], sealed[1:mesh.HEADER_LEN])
        self.assertEqual(len(clear), len(sealed) - mesh.TAG_LEN)

    def test_readable_with_no_key_at_all(self):
        a = mesh.Mesh(A, key())
        clear = a.to_clear(a.wrap(INNER))
        # Any other key -- or none -- gets the frame, once it opts in.
        b = mesh.Mesh(B, key("a completely different passphrase"))
        b.accept_clear = True
        got = b.unwrap(clear)
        self.assertIsNotNone(got)
        self.assertEqual(got.frame, INNER)
        self.assertEqual(got.origin, A)
        self.assertFalse(got.duplicate)
        self.assertTrue(b.unwrap(clear).duplicate)

    def test_ignored_unless_opted_in(self):
        a = mesh.Mesh(A, key())
        clear = a.to_clear(a.wrap(INNER))
        b = mesh.Mesh(B, key())
        self.assertIsNone(b.unwrap(clear))
        self.assertEqual(b.stats().get("clear_ignored"), 1)

    def test_already_clear_passes_through(self):
        a = mesh.Mesh(A, key())
        clear = a.to_clear(a.wrap(INNER))
        self.assertEqual(a.to_clear(clear), clear)

    def test_foreign_key_is_not_re_sent(self):
        a = mesh.Mesh(A, key())
        other = mesh.Mesh(B, key("other net"))
        self.assertIsNone(a.to_clear(other.wrap(INNER)))
        self.assertIsNone(a.to_clear(b"junk"))

    def test_relay_spends_a_hop_on_a_clear_frame(self):
        a = mesh.Mesh(A, key())
        clear = a.to_clear(a.wrap(INNER, hops=2))
        fwd = mesh.Mesh(B, key()).forward(clear)
        self.assertEqual(fwd[7], 1)
        self.assertEqual(fwd[mesh.HEADER_LEN:], INNER)
        self.assertTrue(mesh.is_clear(fwd))

    def test_sealed_still_works_alongside(self):
        a = mesh.Mesh(A, key())
        b = mesh.Mesh(B, key())
        b.accept_clear = True
        self.assertEqual(b.unwrap(a.wrap(INNER)).frame, INNER)


class FakeIface(object):
    def __init__(self):
        self.sent = []

    def process_outgoing(self, data):
        self.sent.append(data)
        return True


class TestBridgeHook(unittest.TestCase):

    NAMES = ("_fm_clear", "_mesh", "_fm_iface", "_lora_iface", "_radio_firmware")

    def setUp(self):
        self._saved = {n: getattr(rns_bridge, n) for n in self.NAMES}
        rns_bridge._mesh = mesh.Mesh(A, key())
        rns_bridge._fm_iface = FakeIface()
        rns_bridge._lora_iface = FakeIface()

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(rns_bridge, n, v)

    def test_off_sends_sealed(self):
        rns_bridge.set_fm_clear(False)
        sealed = rns_bridge._mesh.wrap(INNER)
        self.assertTrue(rns_bridge._transmit_sealed(sealed, link=rns_bridge.LINK_FM))
        self.assertEqual(rns_bridge._fm_iface.sent, [sealed])

    def test_on_strips_fm_only(self):
        rns_bridge.set_fm_clear(True)
        self.assertTrue(rns_bridge._mesh.accept_clear)
        sealed = rns_bridge._mesh.wrap(INNER)
        self.assertTrue(rns_bridge._transmit_sealed(sealed, link=rns_bridge.LINK_FM))
        (sent,) = rns_bridge._fm_iface.sent
        self.assertTrue(mesh.is_clear(sent))
        self.assertEqual(sent[mesh.HEADER_LEN:], INNER)
        # LoRa (Meshtastic path, which also bypasses RNS.Packet) stays sealed.
        rns_bridge.set_radio_firmware("meshtastic")
        rns_bridge._transmit_sealed(sealed, link=rns_bridge.LINK_LORA)
        self.assertEqual(rns_bridge._lora_iface.sent, [sealed])

    def test_on_refuses_what_it_cannot_decrypt(self):
        rns_bridge.set_fm_clear(True)
        foreign = mesh.Mesh(B, key("other net")).wrap(INNER)
        self.assertFalse(rns_bridge._transmit_sealed(foreign, link=rns_bridge.LINK_FM))
        self.assertEqual(rns_bridge._fm_iface.sent, [])

    def test_mesh_built_later_inherits_the_setting(self):
        # No epoch file: _build_mesh would otherwise persist one in ~.
        saved = mesh.load_epoch
        mesh.load_epoch = lambda *a, **k: (7, True)
        self.addCleanup(setattr, mesh, "load_epoch", saved)
        rns_bridge.set_fm_clear(True)
        self.assertTrue(rns_bridge._build_mesh("bench passphrase").accept_clear)
        rns_bridge.set_fm_clear(False)
        self.assertFalse(rns_bridge._build_mesh("bench passphrase").accept_clear)


if __name__ == "__main__":
    unittest.main()
