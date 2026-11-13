"""settings_net: a host and phones over real sockets on this machine, with the
real settings_share rules on both ends (real crypto, real Receiver)."""

import base64
import json
import os
import socket
import tempfile
import threading
import time
import unittest

import settings_net as sn
import settings_share as ss

PIN = "4821"


def wait_until(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Phone(object):
    """A phone's settings and its client, wired as settings_link wires them."""

    def __init__(self, d, name, discovery_port, listen=True, approve=True):
        self.settings = {"lora_preset": "Short Slow", "lora_freq_mhz": "915.000",
                         "lora_tx_dbm": 22, "passphrase": "old pass"}
        self.applied = []
        self.rx = ss.Receiver(os.path.join(d, name + ".json"), lambda: dict(self.settings),
                              self._apply, pin_iterations=1000)
        self.name = name
        self.bundles = []
        self.found = []
        self.approved = set()
        self.client = sn.PhoneClient(self._hello, self._on_bundle, self.rx.exchange,
                                     discovery_port=discovery_port, listen=listen,
                                     approved=lambda h: approve or h in self.approved,
                                     on_found=lambda *f: self.found.append(f))

    def _apply(self, settings):
        self.applied.append(dict(settings))
        self.settings.update(settings)
        return []

    def _hello(self, host_id):
        return {"phone": self.rx.phone_id,
                "x_pub": base64.b64encode(self.rx.x_pub).decode("ascii"),
                "name": self.name, "last_rev": self.rx.last_rev(host_id)}

    def _on_bundle(self, host_id, text):
        self.bundles.append(text)
        try:
            offer = self.rx.receive(text, ss.ROUTE_NETWORK)
            return offer["rev"], offer["outcome"], ""
        except ss.Refused as e:
            return ss.decode(text)["body"].get("rev"), "refused", str(e)


class Tap(object):
    """A relay between a phone and the host that keeps every byte: what
    someone on the path between them sees."""

    def __init__(self, target_port):
        self.seen = b""
        self._target = target_port
        self._l = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._l.bind(("127.0.0.1", 0))
        self._l.listen(4)
        self.port = self._l.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                a, _ = self._l.accept()
            except OSError:
                return
            b = socket.create_connection(("127.0.0.1", self._target))
            for x, y in ((a, b), (b, a)):
                threading.Thread(target=self._pipe, args=(x, y), daemon=True).start()

    def _pipe(self, x, y):
        try:
            while True:
                d = x.recv(4096)
                if not d:
                    break
                self.seen += d
                y.sendall(d)
        except OSError:
            pass
        for z in (x, y):
            try:
                z.close()
            except OSError:
                pass

    def close(self):
        self._l.close()


class SettingsNetTest(unittest.TestCase):
    def setUp(self):
        self._saved = {n: getattr(sn, n) for n in ("BEACON_S", "RECONNECT_S", "HELLO_S",
                                                   "RECONNECT_MAX_S")}
        sn.BEACON_S = 0.05
        sn.RECONNECT_S = 0.05
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name
        self.disc = free_udp_port()
        self.host = ss.Host(ss.HostKeys.generate(), "Host-OnePlus",
                            os.path.join(self.d, "rev.json"))
        self.server = sn.HostServer(self.host, port=0, discovery_port=self.disc,
                                    bind_addr="127.0.0.1",
                                    broadcast_addrs=lambda: ["127.0.0.1"])
        self.phones = []

    def tearDown(self):
        for p in self.phones:
            p.client.stop()
        self.server.stop()
        for n, v in self._saved.items():
            setattr(sn, n, v)
        self._tmp.cleanup()

    def phone(self, name="Pixel 7", listen=False, start=True):
        """A phone that reaches the host by address. Only one test listens
        for the beacon: phones sharing one machine share the discovery port,
        and a beacon sent to 127.0.0.1 reaches only one of them."""
        p = Phone(self.d, name, self.disc, listen=listen)
        self.phones.append(p)
        if start:
            p.client.start()
            if not listen and self.server.port:
                p.client.add_host("127.0.0.1", self.server.port)
        return p

    def entry(self, phone):
        for e in self.server.roster():
            if e["phone"] == phone.rx.phone_id:
                return e
        return None

    def state_is(self, phone, state):
        return lambda: (self.entry(phone) or {}).get("state") == state

    def test_a_phone_finds_the_host_by_its_beacon_and_joins_the_roster(self):
        self.server.start()
        p = self.phone(listen=True)
        self.assertTrue(wait_until(lambda: self.entry(p) is not None))
        e = self.entry(p)
        self.assertEqual((e["name"], e["connected"]), ("Pixel 7", True))
        self.assertTrue(wait_until(lambda: p.client.hosts()[0]["connected"]))
        self.assertEqual(p.client.hosts()[0]["name"], "Host-OnePlus")

    def test_a_host_heard_on_wifi_is_not_joined_until_this_phone_approves_it(self):
        self.server.start()
        p = Phone(self.d, "Pixel 7", self.disc, listen=True, approve=False)
        self.phones.append(p)
        p.client.start()
        self.assertTrue(wait_until(lambda: p.client.found()))
        time.sleep(0.5)                                  # ten beacons
        self.assertEqual((self.server.roster(), p.client.hosts()), ([], []))
        self.assertEqual([f[0] for f in p.found], [self.host.keys.host_id])   # asked once
        self.assertEqual(p.client.found()[0]["name"], "Host-OnePlus")
        self.assertTrue(p.client.approve_host(self.host.keys.host_id))
        self.assertTrue(wait_until(lambda: self.entry(p) is not None))
        self.assertEqual(p.client.found(), [])
        self.assertTrue(p.client.forget_host(self.host.keys.host_id))
        self.assertTrue(wait_until(lambda: not self.entry(p)["connected"]))

    def test_an_approval_is_for_the_hosts_key_not_its_name_or_address(self):
        impostor = sn.HostServer(ss.Host(ss.HostKeys.generate(), "Host-OnePlus",
                                         os.path.join(self.d, "imp.json")),
                                 port=0, bind_addr="127.0.0.1", beacon=False)
        impostor.start()
        try:
            p = Phone(self.d, "Pixel 7", self.disc, listen=True, approve=False)
            p.approved.add(self.host.keys.host_id)        # the REAL host was approved
            self.phones.append(p)
            p.client.start()
            # A beacon claiming the approved host's id, pointing at the impostor.
            fake = sn.BEACON_MAGIC + json.dumps({"host": self.host.keys.host_id,
                                                 "name": "Host-OnePlus",
                                                 "port": impostor.port}).encode()
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.sendto(fake, ("127.0.0.1", self.disc))
            u.close()
            self.assertTrue(wait_until(lambda: "different host" in
                                       (p.client.hosts() or [{}])[0].get("problem", "")))
            self.assertEqual(impostor.roster(), [])
        finally:
            impostor.stop()

    def test_a_phone_ignores_its_own_hosts_beacon(self):
        self.server.start()
        p = Phone(self.d, "OnePlus", self.disc, listen=True)
        p.client._own_hosts = lambda: (self.host.keys.host_id,)
        self.phones.append(p)
        p.client.start()
        time.sleep(0.5)                       # ten beacons' worth
        self.assertEqual((p.client.hosts(), self.server.roster()), ([], []))

    def test_push_offer_approve_and_the_roster_follows(self):
        self.server.start()
        p = self.phone()
        self.assertTrue(wait_until(lambda: self.entry(p) is not None))
        rev = self.server.push({"lora_freq_mhz": "906.875"}, switch_in_s=None)
        self.assertTrue(wait_until(self.state_is(p, "pending")), self.server.roster())
        self.assertEqual(p.applied, [])                   # nothing without the PIN
        p.rx.set_pin(PIN)
        outcome = p.rx.approve(self.host.keys.host_id, PIN)
        p.client.report(self.host.keys.host_id, rev, outcome)
        self.assertTrue(wait_until(self.state_is(p, "applied")))
        self.assertEqual(p.settings["lora_freq_mhz"], "906.875")

    def test_a_trusted_host_applies_and_the_roster_says_so(self):
        self.server.start()
        p = self.phone()
        p.rx.set_pin(PIN)
        p.rx.trust_host(self.host.keys.host_id,
                        base64.b64encode(self.host.keys.ed_pub).decode("ascii"),
                        "Host-OnePlus", PIN)
        self.assertTrue(wait_until(lambda: self.entry(p) is not None))
        self.server.push({"lora_tx_dbm": 17}, switch_in_s=None)
        self.assertTrue(wait_until(self.state_is(p, "applied")), self.server.roster())
        self.assertEqual(p.settings["lora_tx_dbm"], 17)

    def test_a_straggler_gets_the_push_with_its_original_switch_time(self):
        self.server.start()
        early = self.phone("OnePlus")
        self.assertTrue(wait_until(lambda: self.entry(early) is not None))
        rev = self.server.push({"lora_freq_mhz": "906.875"}, switch_in_s=120)
        switch_at = self.server.last_push()["switch_at"]
        late = self.phone("Pixel 7")
        self.assertTrue(wait_until(self.state_is(late, "pending")), self.server.roster())
        body = ss.decode(late.bundles[0])["body"]
        self.assertEqual((body["rev"], body["switch_at"]), (rev, switch_at))

    def test_a_phone_that_already_decided_is_not_sent_it_again(self):
        self.server.start()
        p = self.phone()
        self.assertTrue(wait_until(lambda: self.entry(p) is not None))
        self.server.push({"lora_tx_dbm": 17}, switch_in_s=None)
        self.assertTrue(wait_until(self.state_is(p, "pending")))
        p.rx.reject(self.host.keys.host_id)
        p.client.stop()
        self.assertTrue(wait_until(lambda: not self.entry(p)["connected"]))
        again = Phone(self.d, "Pixel 7", self.disc)      # same state file: same phone
        self.phones.append(again)
        again.client.start()
        self.assertTrue(wait_until(lambda: (self.entry(again) or {}).get("connected")))
        time.sleep(0.3)
        self.assertEqual(again.bundles, [])

    def test_the_passphrase_travels_sealed_and_only_to_a_phone_that_opted_in(self):
        self.server.start()
        yes, no = self.phone("Yes"), self.phone("No")
        yes.rx.set_pin(PIN)
        yes.rx.set_accept_passphrase_over_network(True, PIN)
        self.assertTrue(wait_until(lambda: self.entry(yes) is not None and self.entry(no) is not None))
        self.server.push({"lora_tx_dbm": 17}, passphrase="team pass", switch_in_s=None)
        self.assertTrue(wait_until(self.state_is(yes, "pending")))
        self.assertTrue(wait_until(self.state_is(no, "pending")))
        for p in (yes, no):
            sealed = ss.decode(p.bundles[0])["body"]["settings"]["passphrase"]
            self.assertIsInstance(sealed, dict)
            self.assertNotIn("team pass", json.dumps(sealed))
        self.assertEqual(yes.rx.pending()[0]["settings"]["passphrase"], "team pass")
        self.assertNotIn("passphrase", no.rx.pending()[0]["settings"])
        self.assertIn("does not accept it over the network", no.rx.pending()[0]["notes"][0])

    def test_a_hello_claiming_another_phones_id_is_dropped(self):
        self.server.start()
        s = socket.create_connection(("127.0.0.1", self.server.port))
        s.recv(4096)                                              # welcome
        x_pub = base64.b64encode(b"\x01" * 32).decode("ascii")
        s.sendall(json.dumps({"t": "hello", "phone": "0123456789abcdef", "x_pub": x_pub,
                              "name": "liar", "last_rev": 0}).encode() + b"\n")
        time.sleep(0.3)
        self.assertEqual(self.server.roster(), [])
        s.close()

    def test_garbage_closes_that_connection_and_the_host_carries_on(self):
        self.server.start()
        s = socket.create_connection(("127.0.0.1", self.server.port))
        s.sendall(b"\xff\xfe not json\n")
        s.settimeout(2)
        s.recv(4096)                                              # welcome
        self.assertEqual(s.recv(4096), b"")                      # then closed
        s.close()
        p = self.phone()
        self.assertTrue(wait_until(lambda: self.entry(p) is not None))

    def test_an_older_status_does_not_overwrite_a_newer_one(self):
        self.server.start()
        p = self.phone()
        self.assertTrue(wait_until(lambda: self.entry(p) is not None))
        self.server.push({"lora_tx_dbm": 17}, switch_in_s=None)
        rev2 = self.server.push({"lora_tx_dbm": 18}, switch_in_s=None)
        self.assertTrue(wait_until(lambda: (self.entry(p) or {}).get("rev") == rev2
                                   and self.entry(p)["state"] == "pending"))
        p.client.report(self.host.keys.host_id, rev2 - 1, "rejected")
        time.sleep(0.3)
        self.assertEqual((self.entry(p)["rev"], self.entry(p)["state"]), (rev2, "pending"))

    def test_a_report_made_while_away_arrives_on_reconnect(self):
        self.server.start()
        p = self.phone()
        self.assertTrue(wait_until(lambda: self.entry(p) is not None))
        rev = self.server.push({"lora_tx_dbm": 17}, switch_in_s=None)
        self.assertTrue(wait_until(self.state_is(p, "pending")))
        self.server.stop()
        self.assertTrue(wait_until(lambda: not p.client.hosts()[0]["connected"]))
        p.client.report(self.host.keys.host_id, rev, "switched")   # host is down
        server2 = sn.HostServer(self.host, port=self.server.port, discovery_port=self.disc,
                                bind_addr="127.0.0.1", broadcast_addrs=lambda: ["127.0.0.1"])
        self.server = server2
        server2.start()
        self.assertTrue(wait_until(self.state_is(p, "switched"), timeout=8),
                        server2.roster())

    def test_a_host_added_by_address_needs_no_beacon(self):
        server = sn.HostServer(self.host, port=0, bind_addr="127.0.0.1", beacon=False)
        server.start()
        try:
            p = self.phone(listen=False, start=False)
            p.client.start()
            p.client.add_host("127.0.0.1", server.port)
            self.assertTrue(wait_until(lambda: any(e["phone"] == p.rx.phone_id
                                                   for e in server.roster())))
            self.assertEqual(p.client.hosts()[0]["host"], self.host.keys.host_id)
        finally:
            server.stop()

    def test_nothing_to_push_is_refused(self):
        with self.assertRaises(ss.Refused):
            self.server.push({}, switch_in_s=None)

    # -- the channel (step 4) ---------------------------------------------------

    def test_after_the_hello_nothing_on_the_wire_is_readable(self):
        self.server.start()
        tap = Tap(self.server.port)
        try:
            p = self.phone(start=False)
            p.client.start()
            p.client.add_host("127.0.0.1", tap.port)
            self.assertTrue(wait_until(lambda: self.entry(p) is not None))
            self.server.push({"lora_freq_mhz": "906.875", "lora_preset": "Medium Slow"},
                             switch_in_s=None)
            self.assertTrue(wait_until(self.state_is(p, "pending")))
            self.assertEqual(p.rx.pending()[0]["settings"]["lora_freq_mhz"], "906.875")
            for secret in (b"906.875", b"Medium Slow", b"RTAKS1", b"pending"):
                self.assertNotIn(secret, tap.seen)
            self.assertIn(b'"t":"box"', tap.seen)
        finally:
            tap.close()

    def test_a_recorded_session_stays_unreadable_with_the_phones_key_stolen_later(self):
        self.server.start()
        tap = Tap(self.server.port)
        try:
            p = self.phone(start=False)
            p.client.start()
            p.client.add_host("127.0.0.1", tap.port)
            self.assertTrue(wait_until(lambda: self.entry(p) is not None))
            self.server.push({"lora_freq_mhz": "906.875"}, switch_in_s=None)
            self.assertTrue(wait_until(self.state_is(p, "pending")))
        finally:
            tap.close()
        msgs = [json.loads(line) for line in tap.seen.split(b"\n") if line.strip()]
        welcome = next(m for m in msgs if m.get("t") == "welcome")
        hello = next(m for m in msgs if m.get("t") == "hello")
        box = next(m for m in msgs if m.get("t") == "box")
        epk, nonce = base64.b64decode(welcome["epk"]), base64.b64decode(welcome["nonce"])
        x_pub, phone_epk = base64.b64decode(hello["x_pub"]), base64.b64decode(hello["epk"])
        stolen = p.rx._x_private()                       # the phone's permanent key
        static = ss.x25519_exchange(stolen, epk)
        # Every key the attacker can compute from the stolen key and the record.
        for guess in (static, ss.x25519_exchange(stolen, phone_epk)):
            _, to_host, to_phone = ss.channel_keys(static, guess, x_pub, phone_epk, epk, nonce)
            for k in (to_host, to_phone):
                with self.assertRaises(Exception):
                    sn.AESGCM(k).decrypt(sn.struct.pack(">4xQ", box["c"]),
                                         base64.b64decode(box["ct"]), None)
        self.assertNotEqual(phone_epk, x_pub)            # a fresh key, not the permanent one

    def test_a_hello_with_another_phones_public_key_but_no_proof_is_dropped(self):
        self.server.start()
        real = self.phone("Real")
        self.assertTrue(wait_until(lambda: self.entry(real) is not None))
        real.client.stop()
        self.assertTrue(wait_until(lambda: not self.entry(real)["connected"]))
        s = socket.create_connection(("127.0.0.1", self.server.port))
        s.recv(4096)                                              # welcome
        hello = real._hello(self.host.keys.host_id)               # all public
        _, thief_epk = ss.x25519_keypair()                # well formed: only the proof is wrong
        hello.update({"t": "hello", "v": sn.PROTOCOL_VERSION, "name": "thief",
                      "epk": base64.b64encode(thief_epk).decode("ascii"),
                      "proof": base64.b64encode(b"\x00" * 32).decode("ascii")})
        s.sendall(json.dumps(hello).encode() + b"\n")
        time.sleep(0.3)
        self.assertEqual((self.entry(real)["name"], self.entry(real)["connected"]),
                         ("Real", False))
        s.close()

    def test_a_replayed_or_altered_box_closes_the_connection(self):
        a, b = socket.socketpair()
        ca, cb = sn._Conn(a), sn._Conn(b)
        k1, k2 = b"\x01" * 32, b"\x02" * 32
        ca.secure(k1, k2)
        cb.secure(k2, k1)
        ca.send({"t": "status", "rev": 1})
        line = b.recv(4096)
        cb._buf = line
        self.assertEqual(cb.recv(), {"t": "status", "rev": 1})
        cb._buf = line                                            # the same box again
        with self.assertRaises(EOFError):
            cb.recv()
        box = json.loads(line)
        box["c"] += 5
        cb._buf = json.dumps(box).encode() + b"\n"               # moved to a new counter
        with self.assertRaises(EOFError):
            cb.recv()
        cb._buf = b'{"t":"status","rev":9}\n'                    # plain after the hello
        with self.assertRaises(EOFError):
            cb.recv()
        a.close()
        b.close()

    def test_a_connection_that_never_says_hello_is_closed(self):
        sn.HELLO_S = 0.3
        self.server.start()
        s = socket.create_connection(("127.0.0.1", self.server.port))
        s.settimeout(3)
        s.recv(4096)                                              # welcome
        self.assertEqual(s.recv(4096), b"")                      # then closed
        s.close()

    # -- servers by address -------------------------------------------------------

    def test_a_server_added_from_its_code_refuses_another_host_at_that_address(self):
        self.server.start()
        p = self.phone(start=False)
        p.client.start()
        p.client.add_host("127.0.0.1", self.server.port, host_id="0123456789abcdef")
        self.assertTrue(wait_until(lambda: "different host" in p.client.hosts()[0]["problem"]))
        self.assertEqual(self.server.roster(), [])
        p.client.remove_host("127.0.0.1", self.server.port)
        p.client.add_host("127.0.0.1", self.server.port, host_id=self.host.keys.host_id)
        self.assertTrue(wait_until(lambda: self.entry(p) is not None))
        self.assertEqual(p.client.hosts()[0]["problem"], "")

    def test_a_typed_address_keeps_the_first_hosts_key_and_refuses_another(self):
        self.server.start()
        port = self.server.port
        p = self.phone(start=False)
        firsts = []
        p.client._on_first_key = lambda *a: firsts.append(a)
        p.client.start()
        p.client.add_host("127.0.0.1", port)                  # typed: no key known
        self.assertTrue(wait_until(lambda: self.entry(p) is not None))
        self.assertEqual(firsts, [("127.0.0.1", port, self.host.keys.host_id, "Host-OnePlus")])
        # The server is replaced at that address by another machine.
        self.server.stop()
        self.assertTrue(wait_until(lambda: not p.client.hosts()[0]["connected"]))
        impostor = sn.HostServer(ss.Host(ss.HostKeys.generate(), "Host-OnePlus",
                                         os.path.join(self.d, "imp.json")),
                                 port=port, bind_addr="127.0.0.1", beacon=False)
        impostor.start()
        try:
            self.assertTrue(wait_until(lambda: "different host" in p.client.hosts()[0]["problem"],
                                       timeout=8))
            self.assertEqual((impostor.roster(), len(firsts)), ([], 1))
        finally:
            impostor.stop()

    def test_a_server_that_is_down_is_retried_ever_more_slowly(self):
        sn.RECONNECT_MAX_S = 0.4
        dead = free_udp_port()                       # nothing listens on it over TCP
        p = self.phone(start=False)
        p.client.start()
        p.client.add_host("127.0.0.1", dead)
        self.assertTrue(wait_until(lambda: "cannot connect" in p.client.hosts()[0]["problem"]))
        self.assertTrue(wait_until(lambda: p.client._hosts["127.0.0.1:%d" % dead]["fails"] >= 4))
        # 0.05, 0.1, 0.2, then capped at 0.4: four tries take at least 0.35 s,
        # where a fixed 0.05 s would have made eight.
        t0, n0 = time.time(), p.client._hosts["127.0.0.1:%d" % dead]["fails"]
        time.sleep(1.0)
        self.assertLessEqual(p.client._hosts["127.0.0.1:%d" % dead]["fails"] - n0, 4)

    def test_a_host_added_by_address_is_not_joined_twice_by_its_beacon(self):
        self.server.start()
        p = self.phone(listen=True, start=False)
        p.client.start()
        p.client.add_host("127.0.0.1", self.server.port, host_id=self.host.keys.host_id)
        self.assertTrue(wait_until(lambda: self.entry(p) is not None))
        time.sleep(0.5)                              # ten beacons
        self.assertEqual(len(p.client.hosts()), 1)

    def test_a_card_round_trips_and_a_forged_one_is_refused(self):
        card = sn.make_card(self.host.keys, "Team server", "rtak.example.org", 4000)
        c = sn.parse_card(card)
        self.assertEqual((c["host"], c["name"], c["addr"], c["port"]),
                         (self.host.keys.host_id, "Team server", "rtak.example.org", 4000))
        other = ss.HostKeys.generate()
        body = json.loads(base64.urlsafe_b64decode(card[len(sn.CARD_PREFIX):] + "=="))
        body["host"] = other.host_id                 # someone else's id, this key
        forged = sn.CARD_PREFIX + base64.urlsafe_b64encode(
            json.dumps(body).encode()).decode().rstrip("=")
        with self.assertRaises(ss.Refused):
            sn.parse_card(forged)
        for bad in ("", "  ", "two words", "host:4000"):
            with self.assertRaises(ss.Refused):
                sn.make_card(self.host.keys, "Team server", bad)
        self.assertEqual(sn.parse_card("10.0.0.5")["port"], sn.HOST_PORT)
        self.assertEqual(sn.parse_card(" 10.0.0.5:4000 ")["port"], 4000)
        for bad in ("", "10.0.0.5:x", "10.0.0.5:0", "two words"):
            with self.assertRaises(ss.Refused):
                sn.parse_card(bad)

    def test_which_addresses_are_pinned_to_wifi(self):
        for addr, local in (("192.168.77.113", True), ("10.1.2.3", True),
                            ("172.16.0.9", True), ("169.254.1.1", True),
                            ("127.0.0.1", False), ("8.8.8.8", False)):
            self.assertEqual(sn.is_local(addr), local, addr)

    # -- the allow list and a server's restart --------------------------------------

    def server_with_allow_list(self, allow):
        self.server.stop()
        self.server = sn.HostServer(self.host, port=0, bind_addr="127.0.0.1", beacon=False,
                                    admit=lambda pid: pid in allow)
        self.server.start()

    def test_a_phone_not_allowed_waits_and_is_sent_nothing_until_allowed(self):
        allow = set()
        self.server_with_allow_list(allow)
        p = self.phone()
        self.assertTrue(wait_until(self.state_is(p, sn.WAITING_ADMIN)))
        self.server.push({"lora_tx_dbm": 17}, switch_in_s=None)
        p.client.report(self.host.keys.host_id, 1, "applied")   # a waiting phone's word
        time.sleep(0.3)
        self.assertEqual((p.bundles, self.entry(p)["state"]), ([], sn.WAITING_ADMIN))
        allow.add(p.rx.phone_id)
        self.assertTrue(self.server.admitted(p.rx.phone_id))
        self.assertTrue(wait_until(self.state_is(p, "pending")))
        self.assertEqual(len(p.bundles), 1)

    def test_a_waiting_phone_that_leaves_is_off_the_roster_and_revoke_disconnects(self):
        allow = set()
        self.server_with_allow_list(allow)
        p = self.phone()
        self.assertTrue(wait_until(self.state_is(p, sn.WAITING_ADMIN)))
        p.client.stop()
        self.assertTrue(wait_until(lambda: self.entry(p) is None))
        q = self.phone("Allowed")
        allow.add(q.rx.phone_id)
        self.assertTrue(wait_until(lambda: (self.entry(q) or {}).get("connected")))
        self.assertTrue(self.server.drop(q.rx.phone_id))
        allow.discard(q.rx.phone_id)
        self.assertTrue(wait_until(lambda: not q.client.hosts()[0]["connected"]))

    def test_a_restarted_server_catches_up_stragglers_but_not_with_a_forgotten_passphrase(self):
        self.server.start()
        early = self.phone("Early")
        self.assertTrue(wait_until(lambda: self.entry(early) is not None))
        rev = self.server.push({"lora_tx_dbm": 17}, switch_in_s=120)
        switch_at = self.server.last_push()["switch_at"]
        snap = json.loads(json.dumps(self.server.snapshot()))    # through a file, as saved
        self.server.stop()
        restarted = sn.HostServer(self.host, port=0, bind_addr="127.0.0.1", beacon=False)
        restarted.restore(snap)
        self.server = restarted
        restarted.start()
        self.assertEqual(self.entry(early)["connected"], False)  # on the roster, away
        late = self.phone("Late")
        self.assertTrue(wait_until(self.state_is(late, "pending")))
        body = ss.decode(late.bundles[0])["body"]
        self.assertEqual((body["rev"], body["switch_at"]), (rev, switch_at))
        # Now a push WITH the passphrase, and a restart: nobody else gets it.
        self.server.push({"lora_tx_dbm": 18}, passphrase="team pass", switch_in_s=None)
        snap = self.server.snapshot()
        self.assertNotIn("team pass", json.dumps(snap))
        self.server.stop()
        again = sn.HostServer(self.host, port=0, bind_addr="127.0.0.1", beacon=False)
        again.restore(snap)
        self.server = again
        again.start()
        newcomer = self.phone("Newcomer")
        self.assertTrue(wait_until(self.state_is(newcomer, sn.PUSH_AGAIN)))
        self.assertEqual(newcomer.bundles, [])
        self.assertFalse(again.last_push()["passphrase_kept"])


if __name__ == "__main__":
    unittest.main()
