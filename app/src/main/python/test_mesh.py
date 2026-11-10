"""Desktop tests for mesh.py.

Run the whole suite (from this directory):

    ./run_tests.sh

UNLIKE THE OTHER TEST MODULES, THIS ONE NEEDS AN AEAD BACKEND. The `cryptography`
package ships with RNS on device, but a bare desktop python3 may not have it, in
which case every test here skips -- and a skipped crypto test is a hole, not a
pass. `run_tests.sh` exists to prevent exactly that: it builds a venv with the
package and runs the suite under it. Running

    python3 -m unittest discover -p 'test_*.py' -v

directly is fine for the other modules, but silently skips this one unless the
interpreter already has `cryptography`.

The clock is injected so expiry tests are instant. KDF rounds are turned right
down in tests: stretching is what makes derivation slow, and slowness is the
point in production and pure cost here.
"""

import random
import unittest

import cot_codec as codec
import mesh

ME = "aa11bb"
PEER = "cc22dd"

# A stand-in for whatever cot_codec produced. The mesh layer is deliberately
# indifferent to the inner frame's type, so most tests do not need a real one.
INNER = bytes([codec.CTRL_COMPACT]) + b"position-ish payload" * 3

A_PLI = (
    b'<event version="2.0" uid="ANDROID-1" type="a-f-G-U-C" how="m-g" '
    b'time="2026-07-31T18:00:00Z" start="2026-07-31T18:00:00Z" '
    b'stale="2026-07-31T18:02:00Z">'
    b'<point lat="37.4219" lon="-122.0840" hae="12.0" ce="9.5" le="9999999.0"/>'
    b'<detail><contact callsign="ALPHA"/></detail></event>'
)

# Incompressible detail, so this genuinely fragments at the real MTU rather
# than zlib-ing down into one packet. Seeded rather than os.urandom so a
# failure here is reproducible.
_rng = random.Random(1)
_NOISE = "".join("%02x" % _rng.getrandbits(8) for _ in range(700))
A_DRAWING = (
    '<event version="2.0" uid="drawing-1" type="u-d-f" '
    'time="2026-07-30T18:22:31Z" stale="2026-07-30T19:22:31Z" how="h-e">'
    '<point lat="35.5" lon="-79.5" hae="0.0" ce="1.0" le="1.0"/>'
    '<detail><link point="%s"/><strokeColor value="-1"/></detail></event>' % _NOISE
).encode("utf-8")

skip_without_backend = unittest.skipUnless(
    mesh.HAVE_AEAD, "no AEAD backend: install cryptography or use a venv that has it")


class FakeClock(object):
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def key(passphrase="bench passphrase"):
    return mesh.derive_key(passphrase, rounds=1)


def node(node_id=ME, passphrase="bench passphrase", **kw):
    return mesh.Mesh(node_id, key(passphrase), **kw)


# ---------------------------------------------------------------------------
# Key derivation and node ids
# ---------------------------------------------------------------------------

class TestDeriveKey(unittest.TestCase):
    def test_same_passphrase_gives_the_same_key_everywhere(self):
        # The whole group has to land on one key, so this is a requirement and
        # not an implementation detail.
        self.assertEqual(mesh.derive_key("squad", rounds=1),
                         mesh.derive_key("squad", rounds=1))

    def test_different_passphrases_give_different_keys(self):
        self.assertNotEqual(mesh.derive_key("squad", rounds=1),
                            mesh.derive_key("squad ", rounds=1))

    def test_key_is_the_right_length(self):
        self.assertEqual(len(mesh.derive_key("squad", rounds=1)), mesh.KEY_BYTES)

    def test_empty_passphrase_is_refused(self):
        # Failing closed matters: an empty passphrase would otherwise derive a
        # perfectly valid key that every install shares.
        with self.assertRaises(mesh.MeshError):
            mesh.derive_key("")

    def test_accepts_bytes_and_str_alike(self):
        self.assertEqual(mesh.derive_key("squad", rounds=1),
                         mesh.derive_key(b"squad", rounds=1))


class TestFingerprint(unittest.TestCase):
    def test_the_same_key_always_shows_the_same_four_characters(self):
        k = key()
        self.assertEqual(mesh.key_fingerprint(k), mesh.key_fingerprint(k))

    def test_a_different_key_shows_different_characters(self):
        # The entire point: an operator comparing two screens must see a
        # difference when the passphrases differ by one character.
        self.assertNotEqual(mesh.key_fingerprint(mesh.derive_key("squad", rounds=1)),
                            mesh.key_fingerprint(mesh.derive_key("squab", rounds=1)))

    def test_it_is_four_hex_characters(self):
        fp = mesh.key_fingerprint(key())
        self.assertEqual(len(fp), 4)
        int(fp, 16)

    def test_it_does_not_leak_the_key(self):
        k = key()
        self.assertNotIn(mesh.key_fingerprint(k), k.hex())


class TestNodeId(unittest.TestCase):
    def test_hex_round_trip(self):
        self.assertEqual(mesh.node_id_hex(mesh.node_id_bytes("c0ffee")), "c0ffee")

    def test_wrong_length_is_refused(self):
        with self.assertRaises(mesh.MeshError):
            mesh.node_id_bytes("c0ffeeaa")

    def test_non_hex_is_refused(self):
        with self.assertRaises(mesh.MeshError):
            mesh.node_id_bytes("zzzzzz")


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------

@skip_without_backend
class TestWrapUnwrap(unittest.TestCase):
    def test_round_trip_recovers_the_inner_frame_exactly(self):
        got = node(PEER).unwrap(node(ME).wrap(INNER))
        self.assertEqual(got.frame, INNER)

    def test_overhead_is_what_callers_are_told_it_is(self):
        # _max_payload is shrunk by overhead() so a one-packet message stays a
        # one-packet message; if these disagree, messages silently fragment.
        m = node()
        self.assertEqual(len(m.wrap(INNER)) - len(INNER), m.overhead())
        self.assertEqual(m.overhead(), mesh.OVERHEAD)

    def test_header_carries_the_sender_in_the_clear(self):
        # Readable on purpose: duplicate suppression has to work before
        # anything is decrypted.
        p = node(ME).wrap(INNER)
        self.assertEqual(p[0], codec.CTRL_MESH)
        self.assertTrue(mesh.is_mesh(p))
        self.assertEqual(mesh.node_id_hex(p[1:4]), ME)

    def test_the_inner_frame_is_not_on_the_wire_in_clear(self):
        self.assertNotIn(INNER, node().wrap(INNER))

    def test_wraps_every_frame_type_the_codec_produces(self):
        # This layer is outermost and must not care what it carries -- beacons,
        # acks, fragments and CoT all ride the same envelope.
        m, r = node(ME), node(PEER)
        for inner in (codec.encode_beacon(ME, 1, callsign="ALPHA"),
                      codec.encode_ack(ME, 42, PEER),
                      codec.wrap_reliable(INNER, ME)[1],
                      codec.fragment(INNER * 8, mtu=120)[0]):
            self.assertEqual(r.unwrap(m.wrap(inner)).frame, inner)

    def test_empty_frame_is_refused(self):
        with self.assertRaises(mesh.MeshError):
            node().wrap(b"")

    def test_a_short_key_is_refused(self):
        with self.assertRaises(mesh.MeshError):
            mesh.Mesh(ME, b"too short")


@skip_without_backend
class TestAuthentication(unittest.TestCase):
    def setUp(self):
        self.packet = node(ME).wrap(INNER)
        self.rx = node(PEER)

    def _flip(self, index):
        bad = bytearray(self.packet)
        bad[index] ^= 0x01
        return self.rx.unwrap(bytes(bad))

    def test_a_flipped_ciphertext_byte_is_rejected(self):
        self.assertIsNone(self._flip(mesh.HEADER_LEN + 2))

    def test_a_flipped_tag_byte_is_rejected(self):
        self.assertIsNone(self._flip(len(self.packet) - 1))

    def test_a_forged_origin_is_rejected(self):
        # The point of authenticating the header: spoofing another node's id
        # would otherwise defeat milestone 4's ack accounting.
        self.assertIsNone(self._flip(1))

    def test_a_forged_sequence_number_is_rejected(self):
        self.assertIsNone(self._flip(5))

    def test_the_wrong_key_cannot_read_or_forge(self):
        outsider = node(PEER, passphrase="not the squad passphrase")
        self.assertIsNone(outsider.unwrap(self.packet))

    def test_hops_is_rewritable_by_design(self):
        # Excluded from the AAD so a relay can decrement it, exactly like an IP
        # TTL. The cost is that anyone can rewrite it, which is why it is
        # clamped rather than trusted.
        bad = bytearray(self.packet)
        bad[7] = 200
        got = self.rx.unwrap(bytes(bad))
        self.assertIsNotNone(got)
        self.assertEqual(got.hops, mesh.MAX_HOPS)

    def test_truncated_packets_are_rejected_not_raised(self):
        for n in range(0, mesh.HEADER_LEN + mesh.TAG_LEN + 1):
            self.assertIsNone(self.rx.unwrap(self.packet[:n]))

    def test_foreign_traffic_is_ignored_quietly(self):
        # A radio hears noise and other people's networks; none of it should be
        # able to interrupt reception.
        for junk in (b"", b"\x00", bytes([codec.CTRL_COMPACT]) + b"xyz",
                     codec.encode_beacon(ME, 1), b"\xff" * 200):
            self.assertIsNone(self.rx.unwrap(junk))
        self.assertEqual(self.rx.stats().get("error", 0), 0)


# ---------------------------------------------------------------------------
# Nonce uniqueness -- the one failure here with real consequences
# ---------------------------------------------------------------------------

@skip_without_backend
class TestFrameIds(unittest.TestCase):
    def test_sequence_numbers_do_not_repeat(self):
        m = node()
        ids = [m.next_frame_id() for _ in range(1000)]
        self.assertEqual(len(set(ids)), len(ids))

    def test_a_sequence_wrap_bumps_the_epoch(self):
        m = node(epoch=7)
        m._seq = 0xFFFF
        self.assertEqual(m.next_frame_id(), (7, 0xFFFF))
        self.assertEqual(m.next_frame_id(), (8, 0))

    def test_a_wrap_persists_the_new_epoch(self):
        # Without this a restart just after a wrap would reuse the generation
        # it had already started spending.
        saved = []
        m = node(epoch=7, on_epoch_change=saved.append)
        m._seq = 0xFFFF
        m.next_frame_id()
        m.next_frame_id()
        self.assertEqual(saved, [8])

    def test_a_failing_epoch_store_does_not_lose_the_bump(self):
        # The bump still stands; but a counter that could not be saved means a
        # restart could reuse it, so the NEXT allocation refuses (fix C).
        def explode(_):
            raise OSError("read-only filesystem")
        m = node(epoch=7, on_epoch_change=explode)
        m._seq = 0xFFFF
        m.next_frame_id()
        self.assertEqual(m.stats()["epoch"], 8)
        with self.assertRaises(mesh.MeshError) as cm:
            m.next_frame_id()
        self.assertIn("persist", str(cm.exception))

    def test_no_nonce_repeats_across_a_wrap(self):
        # The property that actually matters: distinct (epoch, seq) must give
        # distinct nonces, including across the wrap boundary.
        # 0x7F, not 0xFF: wrapping the byte is exactly what is now refused.
        m = node(epoch=0x7F)
        m._seq = 0xFFFE
        seen = set()
        for _ in range(4):
            epoch, seq = m.next_frame_id()
            header = b"\x16" + m.origin + bytes([epoch]) + seq.to_bytes(2, "big") + b"\x01"
            seen.add(mesh._nonce(header))
        self.assertEqual(len(seen), 4)

    def test_nonces_are_the_length_the_cipher_wants(self):
        self.assertEqual(len(mesh._nonce(node().wrap(INNER)[:mesh.HEADER_LEN])),
                         mesh.NONCE_LEN)

    def test_two_nodes_with_the_same_counters_still_differ(self):
        # Origin is in the nonce, so two nodes booting together and sending
        # their first frame at the same moment do not collide.
        a, b = node(ME), node(PEER)
        self.assertNotEqual(mesh._nonce(a.wrap(INNER)[:mesh.HEADER_LEN]),
                            mesh._nonce(b.wrap(INNER)[:mesh.HEADER_LEN]))

    def test_allocation_is_thread_safe(self):
        # wrap() is called from the CoT listener and the beacon thread. A torn
        # read here means two frames share a nonce.
        import threading
        m = node()
        out = []
        lock = threading.Lock()

        def spin():
            got = [m.next_frame_id() for _ in range(200)]
            with lock:
                out.extend(got)

        threads = [threading.Thread(target=spin) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(out)), len(out))


class TestLoadEpoch(unittest.TestCase):
    FP = "ab12"

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.path = self.dir + "/epoch"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _stored(self):
        import json
        with open(self.path) as f:
            return json.load(f)

    def test_first_run_starts_at_zero_persists_and_flags_a_lost_file(self):
        c = mesh.load_epoch(self.path, self.FP)
        self.assertEqual((c.counter, c.exhausted), (0, None))
        self.assertTrue(c.counter_reset)
        self.assertEqual(self._stored(), {"fp": self.FP, "counter": 0})

    def test_each_start_under_the_same_key_advances_the_counter(self):
        got = [mesh.load_epoch(self.path, self.FP) for _ in range(4)]
        self.assertEqual([c.counter for c in got], [0, 1, 2, 3])
        self.assertEqual([c.counter_reset for c in got], [True, False, False, False])

    def test_a_bare_int_file_migrates_and_is_adopted_under_the_current_key(self):
        with open(self.path, "w") as f:
            f.write("41")
        c = mesh.load_epoch(self.path, self.FP)
        self.assertEqual((c.counter, c.exhausted, c.counter_reset), (42, None, False))
        self.assertEqual(self._stored(), {"fp": self.FP, "counter": 42})

    def test_a_different_key_resets_the_counter(self):
        mesh.load_epoch(self.path, self.FP)
        mesh.load_epoch(self.path, self.FP)
        c = mesh.load_epoch(self.path, "ffff")
        self.assertEqual((c.counter, c.exhausted, c.counter_reset), (0, None, False))
        self.assertEqual(self._stored()["fp"], "ffff")

    def test_the_same_key_continues_where_it_left_off(self):
        mesh.load_epoch(self.path, "ffff")
        self.assertEqual(mesh.load_epoch(self.path, "ffff").counter, 1)

    def test_a_corrupt_file_restarts_at_zero_and_says_so(self):
        for junk in ("not a number", "{}", '{"fp": "x"}', '{"counter": -3}',
                     '{"counter": "5"}', "true", "[1]", ""):
            with open(self.path, "w") as f:
                f.write(junk)
            c = mesh.load_epoch(self.path, self.FP)
            self.assertEqual((c.counter, c.exhausted, c.counter_reset),
                             (0, None, True), junk)

    def test_the_counter_is_never_masked(self):
        with open(self.path, "w") as f:
            f.write('{"fp": "%s", "counter": 254}' % self.FP)
        self.assertEqual(mesh.load_epoch(self.path, self.FP).counter, 255)
        c = mesh.load_epoch(self.path, self.FP)
        self.assertEqual((c.counter, c.exhausted), (256, mesh.EXHAUSTED_SPACE))
        # Exhaustion writes nothing: the file still says 255.
        self.assertEqual(self._stored()["counter"], 255)

    def test_a_legacy_255_is_exhausted_under_the_current_key(self):
        with open(self.path, "w") as f:
            f.write("255")
        self.assertEqual(mesh.load_epoch(self.path, self.FP).exhausted,
                         mesh.EXHAUSTED_SPACE)

    def test_rekeying_at_exhaustion_clears_it(self):
        with open(self.path, "w") as f:
            f.write('{"fp": "%s", "counter": 255}' % self.FP)
        self.assertEqual(mesh.load_epoch(self.path, self.FP).exhausted,
                         mesh.EXHAUSTED_SPACE)
        c = mesh.load_epoch(self.path, "9999")
        self.assertEqual((c.counter, c.exhausted), (0, None))

    def test_an_unwritable_path_stops_transmit_rather_than_going_random(self):
        c = mesh.load_epoch(self.dir + "/no/such/dir/epoch", self.FP)
        self.assertEqual(c.exhausted, mesh.EXHAUSTED_PERSIST)
        self.assertEqual(c.counter, 0)  # deterministic: no random epoch

    def test_there_is_no_random_epoch_left(self):
        import inspect
        self.assertNotIn("urandom", inspect.getsource(mesh.load_epoch))

    def test_save_epoch_reports_failure(self):
        self.assertFalse(mesh.save_epoch(3, self.FP, self.dir + "/no/such/dir/e"))
        self.assertTrue(mesh.save_epoch(3, self.FP, self.path))


@skip_without_backend
class TestEpochExhaustion(unittest.TestCase):
    def test_counter_255_still_transmits_256_does_not(self):
        m = node(epoch=255)
        self.assertEqual(m.next_frame_id(), (255, 0))
        self.assertIsNone(m.tx_stopped())
        w = node(epoch=256)
        with self.assertRaises(mesh.MeshError) as cm:
            w.wrap(INNER)
        self.assertIn("nonce space exhausted", str(cm.exception))
        self.assertIn("TRANSMIT STOPPED", w.tx_stopped())

    def test_unwrap_still_works_when_exhausted(self):
        sender = node(PEER, epoch=3)
        receiver = node(ME, epoch=256)
        got = receiver.unwrap(sender.wrap(INNER))
        self.assertEqual(got.frame, INNER)

    def test_a_seq_wrap_into_256_stops_the_mesh_after_that_frame(self):
        m = node(epoch=255)
        m._seq = 0xFFFF
        self.assertEqual(m.next_frame_id(), (255, 0xFFFF))
        with self.assertRaises(mesh.MeshError):
            m.next_frame_id()
        self.assertIn("TRANSMIT STOPPED", m.summary_line())

    def test_the_wire_epoch_is_the_low_byte(self):
        m = node(epoch=200)
        self.assertEqual(m.next_frame_id()[0], 200)

    def test_a_failed_save_on_wrap_stops_transmit(self):
        m = node(epoch=5, on_epoch_change=lambda c: False)
        m._seq = 0xFFFF
        m.next_frame_id()
        with self.assertRaises(mesh.MeshError):
            m.wrap(INNER)
        self.assertIn("persist", m.tx_stopped())

    def test_counter_reset_is_reported_once(self):
        m = node(epoch=0, counter_reset=True)
        self.assertIn("COUNTER FILE WAS LOST", m.summary_line())
        self.assertNotIn("COUNTER FILE WAS LOST", m.summary_line())

    def test_stats_carry_the_stop(self):
        self.assertIn("tx_stopped", node(epoch=256).stats())
        self.assertNotIn("tx_stopped", node(epoch=1).stats())


# ---------------------------------------------------------------------------
# Duplicate suppression and replay
# ---------------------------------------------------------------------------

class TestSeenCache(unittest.TestCase):
    def test_first_sighting_is_fresh_and_the_rest_are_not(self):
        c = mesh.SeenCache(clock=FakeClock())
        self.assertTrue(c.check(("a", 0, 1)))
        self.assertFalse(c.check(("a", 0, 1)))
        self.assertFalse(c.check(("a", 0, 1)))

    def test_different_ids_do_not_collide(self):
        c = mesh.SeenCache(clock=FakeClock())
        self.assertTrue(c.check(("a", 0, 1)))
        self.assertTrue(c.check(("b", 0, 1)))
        self.assertTrue(c.check(("a", 1, 1)))
        self.assertTrue(c.check(("a", 0, 2)))

    def test_entries_expire(self):
        clock = FakeClock()
        c = mesh.SeenCache(ttl_s=300.0, clock=clock)
        c.check(("a", 0, 1))
        clock.advance(301.0)
        self.assertTrue(c.check(("a", 0, 1)))

    def test_size_is_bounded_under_flood(self):
        # A hostile or broken peer must not be able to grow memory without
        # limit on the serial read thread.
        c = mesh.SeenCache(max_seen=64, clock=FakeClock())
        for i in range(10000):
            c.check(("a", 0, i))
        self.assertLessEqual(len(c), 64)

    def test_forget_makes_the_next_copy_fresh_again(self):
        # The bench's simulated deafness: a node told to ignore a neighbour has
        # already unwrapped the frame by the time it decides to drop it, so the
        # id must come back out. Otherwise the RELAYED copy -- the one the whole
        # multi-hop test exists to observe -- arrives looking like a replay.
        c = mesh.SeenCache(clock=FakeClock())
        self.assertTrue(c.check(("a", 0, 1)))
        c.forget(("a", 0, 1))
        self.assertTrue(c.check(("a", 0, 1)))

    def test_forget_leaves_everything_else_alone(self):
        c = mesh.SeenCache(clock=FakeClock())
        c.check(("a", 0, 1))
        c.check(("b", 0, 1))
        c.forget(("a", 0, 1))
        self.assertFalse(c.check(("b", 0, 1)))

    def test_forgetting_something_never_seen_is_harmless(self):
        c = mesh.SeenCache(clock=FakeClock())
        c.forget(("a", 0, 1))
        self.assertTrue(c.check(("a", 0, 1)))

    def test_expiry_stops_at_the_first_live_entry(self):
        # Amortised O(1) is a hard requirement here, not a nicety: this runs on
        # the thread that reads the radio.
        clock = FakeClock()
        c = mesh.SeenCache(ttl_s=100.0, clock=clock)
        for i in range(50):
            c.check(("a", 0, i))
        clock.advance(101.0)
        for i in range(50, 60):
            c.check(("a", 0, i))
        self.assertEqual(len(c), 10)


@skip_without_backend
class TestReplay(unittest.TestCase):
    def test_a_replayed_packet_is_flagged_duplicate_not_dropped(self):
        # Flagged rather than swallowed: milestone 4 must still acknowledge a
        # duplicate, because a repeat means our previous ack went missing.
        rx = node(PEER)
        packet = node(ME).wrap(INNER)
        self.assertFalse(rx.unwrap(packet).duplicate)
        again = rx.unwrap(packet)
        self.assertTrue(again.duplicate)
        self.assertEqual(again.frame, INNER)

    def test_replay_past_the_cache_ttl_is_still_not_fresh(self):
        # CHANGED by the replay-window fix: this used to assert that past the
        # TTL the cache forgot the frame and a replay was accepted as fresh
        # (`assertFalse(duplicate)`). The per-origin ReplayWindow now remembers
        # it, so the replay comes back as a duplicate. See TestReplayWindow.
        clock = FakeClock()
        rx = mesh.Mesh(PEER, key(), seen=mesh.SeenCache(ttl_s=300.0, clock=clock))
        packet = node(ME).wrap(INNER)
        rx.unwrap(packet)
        clock.advance(301.0)
        self.assertTrue(rx.unwrap(packet).duplicate)

    def test_rewriting_hops_does_not_evade_duplicate_detection(self):
        # hops is unauthenticated, so it is the obvious thing to change when
        # trying to get a recorded frame accepted twice. It is not in the id.
        rx = node(PEER)
        packet = bytearray(node(ME).wrap(INNER))
        rx.unwrap(bytes(packet))
        packet[7] = 4
        self.assertTrue(rx.unwrap(bytes(packet)).duplicate)

    def test_a_node_hears_its_own_traffic_as_fresh_then_duplicate(self):
        m = node(ME)
        packet = m.wrap(INNER)
        self.assertFalse(m.unwrap(packet).duplicate)
        self.assertTrue(m.unwrap(packet).duplicate)


@skip_without_backend
class TestMismatchDetection(unittest.TestCase):
    """The 2026-08-04 bench found the first version of this wrong.

    It compared cumulative totals, so the phone that did NOT re-key -- which
    had 33 good frames behind it and 2 rejections in front -- stayed silent,
    while the phone that did re-key correctly showed the warning. Exactly
    backwards: the user who needs telling is the one who has just been cut off
    without touching anything.
    """

    def setUp(self):
        self.rx = node(PEER)
        self.stranger = node(ME, passphrase="a different network")

    def _reject(self, n=1):
        for _ in range(n):
            self.rx.unwrap(self.stranger.wrap(INNER))

    def test_a_single_rejection_is_not_called_a_mismatch(self):
        # Foreign networks and corruption produce these all day.
        self._reject()
        self.assertLess(self.rx.stats()["since_accept"], mesh.MISMATCH_THRESHOLD)

    def test_a_run_of_rejections_is_called_a_mismatch(self):
        self._reject(2)
        self.assertGreaterEqual(self.rx.stats()["since_accept"],
                                mesh.MISMATCH_THRESHOLD)
        self.assertIn("CHECK THE PASSPHRASE", self.rx.summary_line())

    def test_a_long_history_of_success_does_not_mask_a_mismatch(self):
        # The exact bench case that broke the first version.
        friend = node(ME)
        for _ in range(33):
            self.rx.unwrap(friend.wrap(INNER))
        self._reject(2)
        self.assertGreaterEqual(self.rx.stats()["since_accept"],
                                mesh.MISMATCH_THRESHOLD)

    def test_one_good_frame_clears_the_warning(self):
        self._reject(5)
        self.rx.unwrap(node(ME).wrap(INNER))
        self.assertEqual(self.rx.stats()["since_accept"], 0)
        self.assertNotIn("CHECK THE PASSPHRASE", self.rx.summary_line())

    def test_a_duplicate_also_clears_it(self):
        # A duplicate decrypted, so it proves the key is right, which is the
        # only thing this counter measures.
        friend = node(ME)
        packet = friend.wrap(INNER)
        self.rx.unwrap(packet)
        self._reject(5)
        self.assertTrue(self.rx.unwrap(packet).duplicate)
        self.assertEqual(self.rx.stats()["since_accept"], 0)

    def test_noise_that_is_not_ours_does_not_count(self):
        # Frames from another protocol never reach the tag check, so they must
        # not be read as a passphrase problem.
        for _ in range(10):
            self.rx.unwrap(b"\x10 not our protocol at all")
        self.assertEqual(self.rx.stats()["since_accept"], 0)


@skip_without_backend
class TestVersionMismatch(unittest.TestCase):
    """PROTOCOL_VERSION 2 changed the envelope's control byte, so a half-done
    rollout produces two radios that transmit perfectly and ignore each other.
    That must not look like noise."""

    def _old_build_frame(self):
        # Same envelope, previous version nibble.
        packet = bytearray(node(ME).wrap(INNER))
        packet[0] = ((codec.PROTOCOL_VERSION - 1) << 4) | codec.T_MESH
        return bytes(packet)

    def test_a_frame_from_another_version_is_named_as_such(self):
        rx = node(PEER)
        self.assertIsNone(rx.unwrap(self._old_build_frame()))
        self.assertEqual(rx.stats()["wrong_version"], 1)
        self.assertEqual(rx.stats().get("foreign", 0), 0)

    def test_the_report_says_which_version_and_what_to_do(self):
        rx = node(PEER)
        rx.unwrap(self._old_build_frame())
        line = rx.summary_line()
        self.assertIn("v%d" % (codec.PROTOCOL_VERSION - 1), line)
        self.assertIn("UPDATE", line)

    def test_a_version_mismatch_outranks_the_passphrase_warning(self):
        # Both could be true at once, but only one is the actual fix.
        rx = node(PEER)
        stranger = node(ME, passphrase="wrong")
        for _ in range(5):
            rx.unwrap(stranger.wrap(INNER))
        rx.unwrap(self._old_build_frame())
        self.assertIn("UPDATE", rx.summary_line())
        self.assertNotIn("PASSPHRASE", rx.summary_line())

    def test_genuine_noise_is_still_just_foreign(self):
        rx = node(PEER)
        for junk in (b"\x10whatever", b"\xff\xff\xff", codec.encode_beacon(ME, 1)):
            rx.unwrap(junk)
        self.assertEqual(rx.stats().get("wrong_version", 0), 0)
        self.assertGreater(rx.stats()["foreign"], 0)

    def test_stats_report_our_own_version(self):
        self.assertEqual(node().stats()["version"], codec.PROTOCOL_VERSION)


@skip_without_backend
class TestReporting(unittest.TestCase):
    def test_summary_line_counts_each_outcome(self):
        rx, tx = node(PEER), node(ME)
        packet = tx.wrap(INNER)
        rx.unwrap(packet)
        rx.unwrap(packet)
        rx.unwrap(b"\x10junk")
        bad = bytearray(packet)
        bad[-1] ^= 1
        rx.unwrap(bytes(bad))
        line = rx.summary_line()
        self.assertIn("unwrapped=1", line)
        self.assertIn("duplicate=1", line)
        self.assertIn("foreign=1", line)
        self.assertIn("unauthenticated=1", line)

    def test_stats_expose_the_counter_state(self):
        m = node(epoch=5)
        m.wrap(INNER)
        s = m.stats()
        self.assertEqual(s["epoch"], 5)
        self.assertEqual(s["seq"], 1)
        self.assertEqual(s["wrapped"], 1)


@skip_without_backend
class TestPipeline(unittest.TestCase):
    """The whole chain the bridge actually runs, without RNS in the way.

    rns_bridge cannot be imported on a desktop (it needs RNS and a serial
    port), so the layering it depends on is pinned here instead: encode ->
    fragment at the inner budget -> wrap each fragment -> unwrap -> reassemble
    -> decode.
    """

    # What start() computes: RNS.Packet.PLAIN_MDU less the envelope.
    OUTER = codec.RNS_MAX_PAYLOAD
    INNER = OUTER - mesh.OVERHEAD

    def _round_trip(self, xml):
        tx, rx = node(ME), node(PEER)
        reassembler = codec.Reassembler()
        out = None
        for packet in codec.build_packets(xml, self.INNER):
            self.assertLessEqual(len(packet), self.INNER)
            on_air = tx.wrap(packet)
            # The point of the inner/outer split: nothing ever exceeds what
            # RNS will pack.
            self.assertLessEqual(len(on_air), self.OUTER)
            inbound = rx.unwrap(on_air)
            self.assertIsNotNone(inbound)
            whole = reassembler.offer(inbound.frame)
            if whole is not None:
                out = codec.decode(whole)
        return out

    def test_a_position_report_survives_the_whole_chain(self):
        xml = self._round_trip(A_PLI)
        self.assertIn(b'uid="ANDROID-1"', xml)
        self.assertIn(b'lat="37.4219000"', xml)
        self.assertIn(b'callsign="ALPHA"', xml)

    def test_a_position_report_still_fits_in_one_packet(self):
        # The budget claim that matters. If the envelope pushed a routine PLI
        # into two fragments, every position would cost two preambles and the
        # airtime arithmetic behind this milestone would be wrong.
        self.assertEqual(len(codec.build_packets(A_PLI, self.INNER)), 1)

    def test_a_multi_fragment_drawing_survives_the_whole_chain(self):
        xml = self._round_trip(A_DRAWING)
        self.assertIn(b'uid="drawing-1"', xml)

    def test_fragments_are_individually_authenticated(self):
        # Wrapping each fragment separately is what lets a relay forward one
        # without reassembling -- and means a forged fragment is rejected
        # before it can reach the reassembler's buffers at all.
        tx, rx = node(ME), node(PEER)
        reassembler = codec.Reassembler()
        packets = codec.build_packets(A_DRAWING, self.INNER)
        self.assertGreater(len(packets), 1)
        for i, packet in enumerate(packets):
            on_air = bytearray(tx.wrap(packet))
            if i == 1:
                on_air[mesh.HEADER_LEN + 1] ^= 0x01
            inbound = rx.unwrap(bytes(on_air))
            if i == 1:
                self.assertIsNone(inbound)
                continue
            self.assertIsNone(reassembler.offer(inbound.frame),
                              "a message must not complete while a fragment is missing")

    def test_an_arq_retry_is_a_new_frame_on_the_wire(self):
        # reliable.py hands the SAME bytes back for a retry. Wrapping happens
        # at the point of transmission, so each attempt gets a fresh sequence
        # number -- which is what stops a relay's duplicate cache from
        # swallowing every retransmission while ARQ reports them going out.
        tx, rx = node(ME), node(PEER)
        _, frame = codec.wrap_reliable(codec.encode(A_PLI, self.INNER), ME)
        first, second = tx.wrap(frame), tx.wrap(frame)
        self.assertNotEqual(first, second)
        self.assertFalse(rx.unwrap(first).duplicate)
        self.assertFalse(rx.unwrap(second).duplicate)

    def test_a_recorded_frame_replayed_verbatim_is_caught(self):
        tx, rx = node(ME), node(PEER)
        on_air = tx.wrap(codec.encode(A_PLI, self.INNER))
        self.assertFalse(rx.unwrap(on_air).duplicate)
        self.assertTrue(rx.unwrap(on_air).duplicate)

    def test_an_outsider_hears_nothing_useful(self):
        # The property the whole milestone exists for.
        on_air = node(ME).wrap(codec.encode(A_PLI, self.INNER))
        self.assertNotIn(b"ANDROID-1", on_air)
        self.assertNotIn(b"37.4219", on_air)
        self.assertIsNone(node(PEER, passphrase="wrong").unwrap(on_air))


@skip_without_backend
class TestReplayWindow(unittest.TestCase):
    """Per-origin replay window: what the 300 s SeenCache cannot do."""

    def setUp(self):
        self.clock = FakeClock()
        self.sender = node(ME, epoch=5)
        self.rx = mesh.Mesh(PEER, key(),
                            seen=mesh.SeenCache(ttl_s=300.0, clock=self.clock))

    def _frames(self, n):
        return [self.sender.wrap(INNER) for _ in range(n)]

    def _not_fresh(self, got):
        return got is None or got.duplicate

    def test_replay_older_than_the_ttl_is_rejected(self):
        packets = self._frames(3)
        for p in packets:
            self.assertFalse(self.rx.unwrap(p).duplicate)
        self.clock.advance(3600.0)
        self.assertTrue(self._not_fresh(self.rx.unwrap(packets[0])))

    def test_a_replay_far_behind_the_window_is_dropped_outright(self):
        packets = self._frames(mesh.REPLAY_WINDOW + 5)
        for p in packets:
            self.rx.unwrap(p)
        self.clock.advance(3600.0)
        self.assertIsNone(self.rx.unwrap(packets[0]))
        self.assertEqual(self.rx.stats()["replayed"], 1)
        self.assertIn("replayed=1", self.rx.summary_line())

    def test_the_edge_of_the_window(self):
        packets = self._frames(mesh.REPLAY_WINDOW + 1)
        self.rx.unwrap(packets[-1])               # high = W
        self.assertIsNone(self.rx.unwrap(packets[0]))   # W behind: out
        self.assertFalse(self.rx.unwrap(packets[1]).duplicate)  # W-1 behind: in

    def test_out_of_order_inside_the_window_is_accepted_once(self):
        packets = self._frames(10)
        self.rx.unwrap(packets[9])
        got = self.rx.unwrap(packets[3])
        self.assertFalse(got.duplicate)
        self.assertEqual(got.frame, INNER)
        self.assertTrue(self.rx.unwrap(packets[3]).duplicate)
        self.assertTrue(self.rx.unwrap(packets[9]).duplicate)

    def test_a_duplicate_is_delivered_as_duplicate_not_none(self):
        # The relay layer's passive cancellation depends on this.
        p = self._frames(1)[0]
        self.rx.unwrap(p)
        again = self.rx.unwrap(p)
        self.assertIsNotNone(again)
        self.assertTrue(again.duplicate)

    def test_a_joining_receiver_accepts_earlier_seqs_inside_the_window(self):
        packets = self._frames(20)
        self.assertFalse(self.rx.unwrap(packets[15]).duplicate)
        self.assertFalse(self.rx.unwrap(packets[2]).duplicate)

    def test_epoch_restart_plus_one_and_plus_five_are_accepted(self):
        for step in (1, 5):
            rx = mesh.Mesh(PEER, key())
            rx.unwrap(node(ME, epoch=5).wrap(INNER))
            first = node(ME, epoch=5 + step)
            got = rx.unwrap(first.wrap(INNER))
            self.assertFalse(got.duplicate)
            self.assertEqual(got.epoch, 5 + step)
            # The window reset: seq 0 of the new epoch is not "behind" anything,
            # and the second frame advances normally.
            self.assertFalse(rx.unwrap(first.wrap(INNER)).duplicate)

    def test_a_retired_epoch_replay_is_rejected(self):
        old = node(ME, epoch=5)
        old_packet = old.wrap(INNER)
        self.rx.unwrap(old_packet)
        self.rx.unwrap(node(ME, epoch=6).wrap(INNER))
        self.assertIsNone(self.rx.unwrap(old.wrap(INNER)))
        self.assertIsNone(self.rx.unwrap(old_packet))
        self.assertEqual(self.rx.stats()["replayed"], 2)

    def test_an_unknown_older_epoch_is_rejected(self):
        self.rx.unwrap(node(ME, epoch=50).wrap(INNER))
        self.assertIsNone(self.rx.unwrap(node(ME, epoch=40).wrap(INNER)))
        self.assertEqual(self.rx.stats()["replayed_unknown_epoch"], 1)
        self.assertIn("replayed=1", self.rx.summary_line())

    def test_serial_comparison_across_the_byte_boundary(self):
        self.rx.unwrap(node(ME, epoch=254).wrap(INNER))
        # Counter 256+ can't transmit, but a PEER'S wire epoch 255 -> 1 (d=2)
        # is "newer" in serial arithmetic and must not be mistaken for old.
        rx = mesh.Mesh(PEER, key())
        rx.unwrap(node(ME, epoch=255).wrap(INNER))
        self.assertFalse(rx.unwrap(node(ME, epoch=1).wrap(INNER)).duplicate)

    def test_unauthenticated_frames_never_touch_the_window(self):
        packet = bytearray(self._frames(1)[0])
        packet[5] = 0xFF  # forged high seq, tag no longer verifies
        packet[6] = 0xFF
        self.assertIsNone(self.rx.unwrap(bytes(packet)))
        self.assertEqual(len(self.rx.replay), 0)

    def test_forget_makes_the_frame_fresh_again(self):
        # The bench's simulated deafness path.
        p = self._frames(1)[0]
        got = self.rx.unwrap(p)
        self.rx.seen.forget((got.origin, got.epoch, got.seq))
        self.assertFalse(self.rx.unwrap(p).duplicate)

    def test_forget_of_a_frame_below_the_window_or_retired_is_a_noop(self):
        packets = self._frames(mesh.REPLAY_WINDOW + 5)
        for p in packets:
            self.rx.unwrap(p)
        first = packets[0]
        self.rx.seen.forget((first[1:4], first[4], int.from_bytes(first[5:7], "big")))
        self.assertIsNone(self.rx.unwrap(first))
        self.rx.replay.forget((b"\x00\x00\x00", 1, 1))  # unknown origin

    def test_origin_lru_eviction_re_learns(self):
        rx = mesh.Mesh(PEER, key(), replay=mesh.ReplayWindow(max_origins=3))
        senders = [node("%06x" % (0x100 + i), epoch=1) for i in range(5)]
        first = senders[0].wrap(INNER)
        rx.unwrap(first)
        for s in senders[1:]:
            rx.unwrap(s.wrap(INNER))
        self.assertEqual(len(rx.replay), 3)
        # Evicted: re-learned as unknown (accepted), no crash.
        self.assertIsNotNone(rx.unwrap(first))
        self.assertEqual(len(rx.replay), 3)

    def test_our_own_echo_is_still_delivered_once(self):
        # Passive acknowledgement: the relay hands our frame back.
        packet = self.sender.wrap(INNER, hops=1)
        got = self.sender.unwrap(packet)
        self.assertIsNotNone(got)
        self.assertEqual(got.origin, self.sender.origin)
        self.assertFalse(got.duplicate)

    def test_clear_frames_are_exempt(self):
        self.rx.accept_clear = True
        clear = self.sender.to_clear(self._frames(1)[0])
        # A forged-high clear frame must not strand real clear traffic.
        forged = bytearray(clear)
        forged[5], forged[6] = 0xFF, 0xF0
        self.assertFalse(self.rx.unwrap(bytes(forged)).duplicate)
        self.assertFalse(self.rx.unwrap(clear).duplicate)
        self.assertEqual(len(self.rx.replay), 0)

    def test_the_window_is_pure_python_int_math(self):
        w = mesh.ReplayWindow(window=4)
        o = b"abc"
        self.assertEqual(w.check(o, 0, 10), mesh.REPLAY_ACCEPT)
        self.assertEqual(w.check(o, 0, 12), mesh.REPLAY_ACCEPT)
        self.assertEqual(w.check(o, 0, 9), mesh.REPLAY_ACCEPT)   # 3 behind, in
        self.assertEqual(w.check(o, 0, 8), mesh.REPLAY_REJECT)   # 4 behind, out
        self.assertEqual(w.check(o, 0, 9), mesh.REPLAY_DUPLICATE)
        self.assertEqual(w.check(o, 0, 11), mesh.REPLAY_ACCEPT)
        self.assertEqual(w.check(o, 0, 12), mesh.REPLAY_DUPLICATE)


if __name__ == "__main__":
    unittest.main()


@skip_without_backend
class TestForward(unittest.TestCase):
    """Relaying passes the original packet through untouched apart from the hop
    count, which is the entire reason that byte sits outside the AAD."""

    def setUp(self):
        self.key = mesh.derive_key("forward test", rounds=1)
        self.sender = mesh.Mesh(b"\x01\x02\x03", self.key)
        self.hop = mesh.Mesh(b"\x0a\x0b\x0c", self.key)
        self.far = mesh.Mesh(b"\x0d\x0e\x0f", self.key)

    @skip_without_backend
    def test_a_relayed_frame_still_authenticates(self):
        packet = self.sender.wrap(b"\x50hello", hops=2)
        relayed = self.hop.forward(packet)
        got = self.far.unwrap(relayed)
        self.assertIsNotNone(got)
        self.assertEqual(got.frame, b"\x50hello")

    @skip_without_backend
    def test_the_original_sender_survives_the_relay(self):
        """If forward() re-wrapped, the receiver would see the RELAY as the
        sender and every node's duplicate suppression would key on a different
        identity each hop -- the frame would circulate forever."""
        packet = self.sender.wrap(b"\x50hello", hops=2)
        got = self.far.unwrap(self.hop.forward(packet))
        self.assertEqual(got.origin, self.sender.origin)

    @skip_without_backend
    def test_the_flood_identity_is_unchanged(self):
        packet = self.sender.wrap(b"\x50hello", hops=3)
        direct = self.far.unwrap(packet)
        relayed = mesh.Mesh(b"\x11\x22\x33", self.key).unwrap(
            self.hop.forward(packet))
        self.assertEqual((direct.origin, direct.epoch, direct.seq),
                         (relayed.origin, relayed.epoch, relayed.seq))

    @skip_without_backend
    def test_a_second_copy_is_recognised_as_a_duplicate(self):
        packet = self.sender.wrap(b"\x50hello", hops=3)
        self.assertFalse(self.far.unwrap(packet).duplicate)
        self.assertTrue(self.far.unwrap(self.hop.forward(packet)).duplicate)

    @skip_without_backend
    def test_each_hop_spends_exactly_one(self):
        packet = self.sender.wrap(b"\x50hello", hops=3)
        self.assertEqual(self.far.unwrap(packet).hops, 3)
        once = self.hop.forward(packet)
        self.assertEqual(self.far.unwrap(once).hops, 2)
        twice = self.hop.forward(once)
        self.assertEqual(self.far.unwrap(twice).hops, 1)

    @skip_without_backend
    def test_a_spent_frame_cannot_be_forwarded(self):
        packet = self.sender.wrap(b"\x50hello", hops=1)
        once = self.hop.forward(packet)
        self.assertIsNotNone(once)
        self.assertIsNone(self.hop.forward(once))

    @skip_without_backend
    def test_a_frame_sent_with_no_hops_is_never_forwarded(self):
        self.assertIsNone(self.hop.forward(self.sender.wrap(b"\x50x", hops=0)))

    @skip_without_backend
    def test_tampering_with_anything_else_still_fails(self):
        """The hop byte is the ONLY thing outside the tag. A relay that altered
        the origin, the sequence or the ciphertext must not verify."""
        packet = bytearray(self.sender.wrap(b"\x50hello", hops=3))
        for offset in (1, 4, 5, 6, len(packet) - 1):
            bad = bytearray(packet)
            bad[offset] ^= 0x01
            self.assertIsNone(self.far.unwrap(bytes(bad)), offset)

    def test_junk_is_refused_rather_than_forwarded(self):
        for junk in (b"", b"\x00", b"not a mesh frame at all"):
            self.assertIsNone(self.hop.forward(junk))

    @skip_without_backend
    def test_default_hops_does_not_relay(self):
        """The safe answer for anything a caller has not classified."""
        self.assertEqual(mesh.DEFAULT_HOPS, 0)
        self.assertIsNone(self.hop.forward(self.sender.wrap(b"\x50x")))

    @skip_without_backend
    def test_a_full_hop_count_is_what_marks_a_frame_as_heard_directly(self):
        """What the bench's simulated deafness rests on.

        `origin` cannot distinguish a frame heard from its sender from one a
        relay forwarded -- forward() deliberately leaves it naming the
        originator at every hop. The hop count is the only thing that can: the
        sender stamps a cap fixed by the frame's class, and each relay spends
        exactly one.
        """
        cap = 2
        packet = self.sender.wrap(b"\x50hello", hops=cap)
        self.assertGreaterEqual(self.far.unwrap(packet).hops, cap)
        rx = mesh.Mesh(b"\x11\x22\x33", self.key)
        self.assertLess(rx.unwrap(self.hop.forward(packet)).hops, cap)

    @skip_without_backend
    def test_a_node_out_of_range_still_receives_the_relayed_copy(self):
        """The whole multi-hop bench in one test.

        The far node is deaf to the sender, so it drops what it hears directly
        -- but on a desk it DOES hear it, and unwrap() has already recorded the
        id by the time that decision is made. Unless the drop also forgets,
        the relayed copy arrives as a replay and the test can never show a
        relay working.
        """
        packet = self.sender.wrap(b"\x50hello", hops=2)

        direct = self.far.unwrap(packet)
        self.assertFalse(direct.duplicate)
        # ... the receiver decides it should not have heard this at all.
        self.far.seen.forget((direct.origin, direct.epoch, direct.seq))

        relayed = self.far.unwrap(self.hop.forward(packet))
        self.assertFalse(relayed.duplicate)
        self.assertEqual(relayed.frame, b"\x50hello")
        self.assertEqual(relayed.origin, self.sender.origin)

    @skip_without_backend
    def test_forgetting_does_not_reopen_the_relayed_copy_to_replay(self):
        """Deafness must not cost the replay defence. Only the dropped frame is
        forgotten; the copy actually accepted is remembered as usual."""
        packet = self.sender.wrap(b"\x50hello", hops=2)
        direct = self.far.unwrap(packet)
        self.far.seen.forget((direct.origin, direct.epoch, direct.seq))
        relayed = self.hop.forward(packet)
        self.assertFalse(self.far.unwrap(relayed).duplicate)
        self.assertTrue(self.far.unwrap(relayed).duplicate)
