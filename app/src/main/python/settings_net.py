"""
Settings sharing over a network (docs/settings-sharing.md, steps 3 and 4): a
host says it is there on the local network (or is added by address), phones
connect to it, the host pushes a bundle to each, and each phone reports back
what became of it -- the host's roster.

Pure stdlib plus settings_share's crypto, no RNS and no Android, so the same
file is the host on a phone and on a server (settings_server.py). Android's
one part, which network a socket goes out on, is injected (`prepare_socket`,
`broadcast_addrs`).

WHY NOT RETICULUM, as the plan first said. Every phone here runs Reticulum
with transport enabled, so a settings host announced over it would be
re-announced onto LoRa by every phone that heard it: airtime the plan says
settings traffic must never cost. A phone joined to a server over an RNS TCP
interface would do the same with the server's announces. This uses the plan's
fallback instead, for both.

WHAT THE NETWORK IS TRUSTED WITH: nothing that matters. Bundles are signed
(settings_share.verify), revisions only go up, and a passphrase is sealed to
one phone's key. On top of that each connection is a channel:

- the host signs its connection key with its host key, so a phone knows which
  host it reached (and refuses a server added from its code if another host
  answers at that address);
- the phone proves it holds the key its id is derived from, so nobody can
  take a phone's line on the roster or its place on a server's allow list;
- everything after that is encrypted (AES-GCM, a key per direction, counters
  that only go up), so a settings push crossing the internet does not show
  the network's frequency to whoever is on the path.

Anyone can still pretend to be a host: then it is an UNTRUSTED host, whose
offers wait for this phone's PIN and show its key fingerprint. A host can
require its admin to allow each phone (`admit`, a server's allow list); a
phone not allowed is sent nothing.

THE WIRE

  Discovery, UDP broadcast to DISCOVERY_PORT every BEACON_S:
      b"RTAKH1 " + {"host": <id>, "name": ..., "port": <tcp>}
  Then one TCP connection per phone and host, a JSON object per line:
      host -> phone  {"t": "welcome", "v": 3, "host": <id>, "name", "host_ed",
                      "epk": <b64 X25519, this connection's>, "nonce": <b64>,
                      "ping_s": <int>, "sig": <Ed25519 over the rest>}
      phone -> host  {"t": "hello", "v": 3, "phone": <id>, "x_pub": <b64>,
                      "epk": <b64 X25519, this connection's>,
                      "name", "last_rev": <int>, "proof": <b64 HMAC>}
      The keys come from BOTH one-time keys and the phone's permanent one
      (settings_share.channel_keys): the proof needs the phone's key, and a
      session stays unreadable if that key is stolen later.
      then, encrypted both ways as {"t": "box", "c": <counter>, "ct": <b64>}:
      host -> phone  {"t": "bundle", "text": "RTAKS1...."}
      phone -> host  {"t": "status", "rev": <int>, "state", "detail"}
      either way     {"t": "ping"}, plain (every ping_s; silent for
                     3 x ping_s + 5 = gone)

Phones connect out; nothing listens on a phone. A phone that was away gets
the latest push when it reconnects, if its `last_rev` says it lacks it: that
is the straggler catch-up, and the bundle keeps its original switch time.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import socket
import struct
import threading
import time

import settings_share as ss

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover -- the desktop venv and the phone have it
    AESGCM = None

PROTOCOL_VERSION = 3
DISCOVERY_PORT = 47811
HOST_PORT = 47812
BEACON_MAGIC = b"RTAKH1 "
BEACON_S = 3.0
# A host not heard for this long is forgotten (and not reconnected to).
HOST_FORGET_S = 30.0
# A phone host's keep-alive. A server asks for a slower one (its welcome says
# which), because a phone on mobile data wakes its radio for every ping.
PING_S = 15
PING_MIN_S, PING_MAX_S = 5, 600
MAX_LINE = 64 * 1024
# Connections allowed at once: phones on the roster, and (where the admin
# must allow each phone) phones still waiting, which anyone can make.
MAX_PHONES = 64
MAX_WAITING = 16
# A connection that has not said a valid hello by then is closed: on a public
# port, idle connections are how the phones' slots get used up.
HELLO_S = 10.0
RECONNECT_S = 5.0
# A host added by address may be a server that is down for an hour: retry
# with a doubling delay up to this, not every 5 s on a phone's battery.
RECONNECT_MAX_S = 300.0

# Roster states the host sets itself; the rest are the phone's own words
# (pending / scheduled / applied / switched / rejected / failed / refused /
# undone; "reverted" only from phones built before 2026-10-23, when the
# automatic undo was removed).
NOT_REACHED = "not reached"
SENT = "sent"
WAITING_ADMIN = "waiting for the admin"
PUSH_AGAIN = "push again"

# A server's code (settings_server.py `card`), scanned or typed on a phone.
CARD_PREFIX = "RTAKSRV1."
CARD_VERSION = 1


def _now_s():
    return time.time()


def dead_after(ping_s):
    """Silent this long = gone: three missed pings, plus slack for a slow
    network."""
    return 3 * ping_s + 5


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def _unb64(text):
    return base64.b64decode(str(text).encode("ascii"), validate=True)


def _welcome_signed(msg):
    return json.dumps({k: msg[k] for k in ("t", "v", "host", "name", "host_ed", "epk",
                                           "nonce", "ping_s")},
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def _proof(key, host_id):
    return hmac.new(key, b"rtak-settings-hello" + host_id.encode("ascii"),
                    hashlib.sha256).digest()


class _HostProblem(Exception):
    """Why a host was not accepted, in the operator's words."""


class _Conn(object):
    """One TCP connection: JSON lines in, JSON lines out, never raises.
    Encrypted once `secure()` is given the keys."""

    def __init__(self, sock):
        self.sock = sock
        self._buf = b""
        self._wlock = threading.Lock()
        self.alive = True
        self.last_rx = _now_s()
        self._tx = self._rx = None
        self._tx_n = 0
        self._rx_n = 0

    def secure(self, tx_key, rx_key):
        with self._wlock:
            self._tx, self._rx = AESGCM(tx_key), AESGCM(rx_key)

    def send(self, obj):
        try:
            with self._wlock:
                if self._tx is not None and obj.get("t") != "ping":
                    self._tx_n += 1
                    ct = self._tx.encrypt(struct.pack(">4xQ", self._tx_n),
                                          json.dumps(obj).encode("utf-8"), None)
                    obj = {"t": "box", "c": self._tx_n, "ct": _b64(ct)}
                data = json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"
                self.sock.sendall(data)
            return True
        except OSError:
            self.close()
            return False

    def recv(self):
        """The next message, None on a timeout tick, or raises EOFError."""
        while b"\n" not in self._buf:
            if len(self._buf) > MAX_LINE:
                raise EOFError("line too long")
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return None
            except OSError:
                raise EOFError("closed")
            if not chunk:
                raise EOFError("closed")
            self._buf += chunk
            self.last_rx = _now_s()
        line, self._buf = self._buf.split(b"\n", 1)
        try:
            obj = json.loads(line.decode("utf-8"))
        except ValueError:
            raise EOFError("not JSON")
        if not isinstance(obj, dict):
            raise EOFError("not an object")
        if obj.get("t") == "ping":
            return obj
        if self._rx is None:
            if obj.get("t") == "box":
                raise EOFError("encrypted before the hello")
            return obj
        # Secured: only boxes, each counter above the last (no replays).
        if obj.get("t") != "box":
            raise EOFError("plain after the hello")
        try:
            n = int(obj["c"])
            if n <= self._rx_n:
                raise EOFError("replayed")
            inner = json.loads(self._rx.decrypt(struct.pack(">4xQ", n),
                                                _unb64(obj["ct"]), None).decode("utf-8"))
        except EOFError:
            raise
        except (InvalidTag, KeyError, TypeError, ValueError, struct.error):
            raise EOFError("a box that does not open")
        self._rx_n = n
        if not isinstance(inner, dict):
            raise EOFError("not an object")
        return inner

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass


def _serve(conn, on_message, ping_s, ready=lambda: True, ready_by=None):
    """Read loop shared by both ends: pings while idle, gone when silent for
    dead_after(ping_s()), or when not `ready()` by `ready_by`."""
    conn.sock.settimeout(1.0)
    last_ping = _now_s()
    try:
        while conn.alive:
            msg = conn.recv()
            now = _now_s()
            if msg is not None and msg.get("t") != "ping":
                on_message(msg)
            if now - last_ping >= ping_s():
                conn.send({"t": "ping"})
                last_ping = now
            if now - conn.last_rx > dead_after(ping_s()):
                break
            if ready_by is not None and now > ready_by and not ready():
                break
    except EOFError:
        pass
    finally:
        conn.close()


# -- the host ------------------------------------------------------------------

class HostServer(object):
    """A settings host: the beacon, the phones' connections, pushes, and the
    roster. The same class on a phone and on a server.

    `host` is a settings_share.Host. `admit(phone_id)` -> bool, when given,
    is the admin's allow list: a phone not on it joins the roster as
    WAITING_ADMIN and is sent nothing until `admitted()` is called for it.

    The passphrase of the last push is held in memory only, for phones that
    catch up later. A restarted host has forgotten it, and a phone that still
    lacks such a push is then sent nothing -- the roster says PUSH_AGAIN --
    rather than a bundle without the passphrase, which the phone would decide
    on and could never be sent again at that revision.
    """

    def __init__(self, host, port=HOST_PORT, discovery_port=DISCOVERY_PORT,
                 bind_addr="", beacon=True, broadcast_addrs=None,
                 prepare_socket=None, clock=time.time, log=None,
                 admit=None, ping_s=PING_S, on_change=None):
        self.host = host
        self._port = port
        self._discovery_port = discovery_port
        self._bind_addr = bind_addr
        self._beacon_on = beacon
        self._broadcast_addrs = broadcast_addrs or (lambda: ["255.255.255.255"])
        self._prepare = prepare_socket or (lambda s: None)
        self._clock = clock
        self._log = log or (lambda m: None)
        self._admit = admit
        self._ping_s = int(ping_s)
        self._on_change = on_change or (lambda: None)
        self._lock = threading.Lock()
        self._phones = {}       # phone_id -> roster entry (+ "_conn")
        self._push = None       # rev, settings, passphrase, had_passphrase, switch_at, issued
        self._listener = None
        self._bound_port = None
        self._running = False

    @property
    def port(self):
        return self._bound_port or self._port

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self._bind_addr, self._port))
        s.listen(16)
        s.settimeout(1.0)
        self._listener = s
        self._bound_port = s.getsockname()[1]
        self._running = True
        threading.Thread(target=self._accept_loop, name="settings-host", daemon=True).start()
        if self._beacon_on:
            threading.Thread(target=self._beacon_loop, name="settings-beacon",
                             daemon=True).start()
        self._log("settings host %s listening on port %d" % (self.host.keys.host_id, self.port))

    def stop(self):
        self._running = False
        if self._listener:
            try:
                self._listener.close()
            except OSError:
                pass
        with self._lock:
            for p in self._phones.values():
                if p.get("_conn"):
                    p["_conn"].close()

    # -- what a server keeps across a restart

    def snapshot(self):
        """The roster's allowed phones and the last push, WITHOUT the
        passphrase -- for a server's state file."""
        with self._lock:
            push = None
            if self._push is not None:
                push = {k: v for k, v in self._push.items() if k != "passphrase"}
            phones = {}
            for pid, p in self._phones.items():
                if p.get("state") == WAITING_ADMIN:
                    continue
                phones[pid] = {"name": p.get("name", ""), "x_pub": _b64(p["x_pub"]),
                               "rev": p.get("rev"), "state": p.get("state"),
                               "detail": p.get("detail", ""), "seen": p.get("seen")}
            return {"push": push, "phones": phones}

    def restore(self, snap):
        with self._lock:
            push = (snap or {}).get("push")
            if push:
                self._push = dict(push, passphrase=None)
            for pid, p in ((snap or {}).get("phones") or {}).items():
                self._phones[pid] = {"phone": pid, "name": p.get("name") or pid,
                                     "x_pub": _unb64(p["x_pub"]), "rev": p.get("rev"),
                                     "state": p.get("state"), "detail": p.get("detail", ""),
                                     "seen": p.get("seen"), "connected": False,
                                     "_conn": None}

    # -- discovery

    def beacon_bytes(self):
        return BEACON_MAGIC + json.dumps(
            {"host": self.host.keys.host_id, "name": self.host.name, "port": self.port},
            separators=(",", ":")).encode("utf-8")

    def _beacon_loop(self):
        while self._running:
            data = self.beacon_bytes()
            # Twice where it can be: once on whatever network Android picks
            # and once pinned to WiFi. A duplicate beacon costs nothing; a
            # missing one leaves every phone without a host.
            for pinned in (False, True):
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    if pinned and self._prepare(s) is False:
                        s.close()
                        continue
                    for addr in self._broadcast_addrs():
                        try:
                            s.sendto(data, (addr, self._discovery_port))
                        except OSError:
                            pass
                    s.close()
                except OSError:
                    pass
            time.sleep(BEACON_S)

    # -- connections

    def _accept_loop(self):
        while self._running:
            try:
                sock, addr = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                full = sum(1 for p in self._phones.values() if p.get("_conn")) >= MAX_PHONES
            if full:
                sock.close()
                continue
            threading.Thread(target=self._phone_loop, args=(sock, addr),
                             name="settings-host-phone", daemon=True).start()

    def _phone_loop(self, sock, addr):
        conn = _Conn(sock)
        eph, epk = ss.x25519_keypair()
        nonce = os.urandom(16)
        welcome = {"t": "welcome", "v": PROTOCOL_VERSION, "host": self.host.keys.host_id,
                   "name": self.host.name, "host_ed": _b64(self.host.keys.ed_pub),
                   "epk": _b64(epk), "nonce": _b64(nonce), "ping_s": self._ping_s}
        welcome["sig"] = _b64(self.host.keys.sign(_welcome_signed(welcome)))
        conn.send(welcome)
        state = {"phone": None}

        def on_message(msg):
            t = msg.get("t")
            if t == "hello" and state["phone"] is None:
                state["phone"] = self._hello(conn, addr, msg, eph, epk, nonce)
                if state["phone"] is None:
                    conn.close()
            elif t == "status" and state["phone"] is not None:
                self._status(state["phone"], msg)

        _serve(conn, on_message, lambda: self._ping_s,
               ready=lambda: state["phone"] is not None, ready_by=_now_s() + HELLO_S)
        changed = False
        with self._lock:
            p = self._phones.get(state["phone"])
            if p is not None and p.get("_conn") is conn:
                if p.get("state") == WAITING_ADMIN:
                    # Not on the roster until allowed: anyone can make these.
                    del self._phones[state["phone"]]
                else:
                    p["_conn"] = None
                    p["connected"] = False
                    p["seen"] = self._clock()
                    changed = True
        if changed:
            self._on_change()

    def _hello(self, conn, addr, msg, eph, epk, nonce):
        try:
            x_pub = _unb64(msg.get("x_pub", ""))
            phone_epk = _unb64(msg.get("epk", ""))
            proof = _unb64(msg.get("proof", ""))
        except (ValueError, TypeError):
            return None
        if msg.get("v") != PROTOCOL_VERSION or len(phone_epk) != 32:
            return None
        phone_id = msg.get("phone")
        # The id is a hash of the key, and the proof says the phone holds the
        # key: a phone cannot claim another's line.
        if len(x_pub) != 32 or phone_id != ss.phone_id_for(x_pub):
            return None
        try:
            static_shared = ss.x25519_exchange(eph, x_pub)
            eph_shared = ss.x25519_exchange(eph, phone_epk)
        except Exception:
            return None
        proof_key, to_host, to_phone = ss.channel_keys(static_shared, eph_shared, x_pub,
                                                       phone_epk, epk, nonce)
        if not hmac.compare_digest(proof, _proof(proof_key, self.host.keys.host_id)):
            return None
        conn.secure(to_phone, to_host)
        try:
            last_rev = int(msg.get("last_rev", 0))
        except (TypeError, ValueError):
            last_rev = 0
        allowed = self._admit is None or bool(self._admit(phone_id))
        with self._lock:
            if not allowed and phone_id not in self._phones and sum(
                    1 for p in self._phones.values()
                    if p.get("state") == WAITING_ADMIN) >= MAX_WAITING:
                return None
            p = self._phones.setdefault(phone_id, {"phone": phone_id, "rev": None,
                                                   "state": None, "detail": ""})
            old = p.get("_conn")
            p.update({"name": str(msg.get("name", ""))[:64] or phone_id, "x_pub": x_pub,
                      "addr": addr[0], "connected": True, "seen": self._clock(),
                      "last_rev": last_rev, "_conn": conn})
            if not allowed:
                p.update({"rev": None, "state": WAITING_ADMIN, "detail": ""})
            elif p.get("state") == WAITING_ADMIN:
                p["state"] = None
        if old is not None and old is not conn:
            old.close()
        self._log("settings host: %s (%s) connected from %s%s"
                  % (p["name"], phone_id, addr[0], "" if allowed else ", waiting for the admin"))
        if allowed:
            self._catch_up(phone_id)
            self._on_change()
        return phone_id

    def _catch_up(self, phone_id):
        """Send the last push to a phone that lacks it: a straggler."""
        with self._lock:
            push = self._push
            p = self._phones.get(phone_id)
            if push is None or p is None or p.get("last_rev", 0) >= push["rev"]:
                return
            if push.get("had_passphrase") and not push.get("passphrase"):
                p.update({"rev": push["rev"], "state": PUSH_AGAIN,
                          "detail": "the host restarted and does not keep the passphrase"})
                return
        self._send_bundle(phone_id, push)

    def admitted(self, phone_id):
        """The admin allowed `phone_id`: if it is connected, it gets the last
        push now. False if it is not waiting."""
        with self._lock:
            p = self._phones.get(phone_id)
            if p is None or p.get("state") != WAITING_ADMIN:
                return False
            p["state"] = None
        self._catch_up(phone_id)
        self._on_change()
        return True

    def drop(self, phone_id):
        """The admin revoked `phone_id`: disconnected and off the roster."""
        with self._lock:
            p = self._phones.pop(phone_id, None)
        if p is not None and p.get("_conn"):
            p["_conn"].close()
        self._on_change()
        return p is not None

    def _status(self, phone_id, msg):
        try:
            rev = int(msg.get("rev"))
        except (TypeError, ValueError):
            return
        with self._lock:
            p = self._phones.get(phone_id)
            if p is None or p.get("state") == WAITING_ADMIN or (
                    p["rev"] is not None and rev < p["rev"]):
                return
            p.update({"rev": rev, "state": str(msg.get("state", ""))[:32],
                      "detail": str(msg.get("detail", ""))[:300], "seen": self._clock()})
        self._log("settings roster: %s rev %d %s %s"
                  % (p["name"], rev, p["state"], p["detail"]))
        self._on_change()

    # -- pushing

    def _bundle_for(self, x_pub, push):
        s = dict(push["settings"])
        if push.get("passphrase"):
            s["passphrase"] = ss.seal_passphrase(push["passphrase"], x_pub)
        return ss.make_bundle(self.host.keys, self.host.name, push["rev"], s,
                              push["switch_at"], push["issued"])

    def _send_bundle(self, phone_id, push):
        with self._lock:
            p = self._phones.get(phone_id)
            conn = p and p.get("_conn")
            x_pub = p and p.get("x_pub")
        if not conn:
            return False
        sent = conn.send({"t": "bundle", "text": self._bundle_for(x_pub, push)})
        with self._lock:
            p.update({"rev": push["rev"], "state": SENT if sent else NOT_REACHED,
                      "detail": ""})
        return sent

    def push(self, settings, passphrase=None, switch_in_s=ss.DEFAULT_SWITCH_DELAY_S):
        """Send `settings` to every connected (and allowed) phone. Phones that
        connect later get it then. Returns the revision."""
        if not settings and not passphrase:
            raise ss.Refused("Nothing to push")
        now = int(self._clock())           # as the bundle carries it
        rev = self.host.next_rev()
        push = {"rev": rev, "settings": dict(settings), "passphrase": passphrase or None,
                "had_passphrase": bool(passphrase),
                "switch_at": None if switch_in_s is None else now + int(switch_in_s),
                "issued": now}
        with self._lock:
            self._push = push
            ids = [pid for pid, p in self._phones.items() if p.get("state") != WAITING_ADMIN]
            for pid in ids:
                self._phones[pid].update({"rev": rev, "state": NOT_REACHED, "detail": ""})
        for pid in ids:
            self._send_bundle(pid, push)
        self._log("settings push rev %d to %d phone(s)" % (rev, len(ids)))
        self._on_change()
        return rev

    def last_push(self):
        with self._lock:
            if self._push is None:
                return None
            p = self._push
            return {"rev": p["rev"], "switch_at": p["switch_at"], "issued": p["issued"],
                    "keys": sorted(p["settings"]) + (["passphrase"] if p.get("had_passphrase")
                                                     else []),
                    "passphrase_kept": bool(p.get("passphrase"))}

    def roster(self):
        with self._lock:
            return [{k: v for k, v in p.items() if not k.startswith("_") and k != "x_pub"}
                    for _, p in sorted(self._phones.items(),
                                       key=lambda kv: kv[1].get("name", ""))]


# -- a server's code, for phones ------------------------------------------------

def make_card(keys, name, addr, port=HOST_PORT):
    """What a phone scans (or is typed) to add this server: where it is and
    which host must answer there."""
    addr = (addr or "").strip()
    if not addr or any(c.isspace() for c in addr) or ":" in addr:
        raise ss.Refused("Give the address phones reach this server at: a name or an IP, "
                         "without a port")
    if not 1 <= int(port) <= 65535:
        raise ss.Refused("The port must be 1-65535")
    body = {"v": CARD_VERSION, "host": keys.host_id, "name": name,
            "ed": base64.b64encode(keys.ed_pub).decode("ascii"),
            "addr": addr, "port": int(port)}
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    return CARD_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_card(text):
    """A server's code -> {"host", "name", "ed", "addr", "port"}. Raises
    ss.Refused. Also takes a plain "address" or "address:port" (no host
    check then: whoever answers there is shown by its key on approval)."""
    text = (text or "").strip()
    if not text:
        raise ss.Refused("No server address")
    if text.startswith(CARD_PREFIX):
        data = text[len(CARD_PREFIX):]
        try:
            card = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
            ed = base64.b64decode(card["ed"], validate=True)
            if card.get("v") != CARD_VERSION:
                raise ss.Refused("This server code is from another version of the app")
            if ss.host_id_for(ed) != card["host"]:
                raise ss.Refused("The server code's host id does not match its key")
            port = int(card.get("port", HOST_PORT))
            addr = str(card["addr"]).strip()
        except ss.Refused:
            raise
        except Exception:
            raise ss.Refused("The server code is damaged")
        out = {"host": card["host"], "name": str(card.get("name", ""))[:64],
               "ed": card["ed"], "addr": addr, "port": port}
    else:
        addr, port = text, HOST_PORT
        if text.count(":") == 1:
            addr, p = text.rsplit(":", 1)
            try:
                port = int(p)
            except ValueError:
                raise ss.Refused("The port must be a number")
        out = {"host": None, "name": "", "ed": None, "addr": addr.strip(), "port": port}
    if not out["addr"] or any(c.isspace() for c in out["addr"]):
        raise ss.Refused("Not a server address: %r" % out["addr"])
    if not 1 <= out["port"] <= 65535:
        raise ss.Refused("The port must be 1-65535")
    return out


# -- the phone -------------------------------------------------------------------

def is_local(addr):
    """Whether `addr` is on a local network (pinned to WiFi on a phone) rather
    than the internet or this machine. A hostname is looked up first."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        try:
            ip = ipaddress.ip_address(socket.getaddrinfo(addr, None, socket.AF_INET)[0][4][0])
        except (OSError, IndexError, ValueError):
            return False
    return (ip.is_private or ip.is_link_local) and not ip.is_loopback


class PhoneClient(object):
    """A phone listening for settings hosts and connected to each one heard,
    and to each host added by address (a server).

      hello_fn(host_id)        -> {"phone", "x_pub" (b64), "name", "last_rev"}
      exchange_fn(peer_pub)    -> X25519 shared secret with this phone's key
      on_bundle(host_id, text) -> (rev, state, detail): what became of it
      own_hosts()              -> host ids to ignore: this phone's own, when
                                  it is hosting too, or it would hear its own
                                  beacon and offer itself its own settings
                                  (seen on the OnePlus, 2026-10-21)
      approved(host_id)        -> whether this phone's operator chose to
                                  connect to a host heard on WiFi
      on_found(host_id, name, addr) -> a host heard that is not approved yet
      on_first_key(addr, port, host_id, name) -> a host added by a TYPED
                                  address answered for the first time: from
                                  now on that host, and no other, must answer
                                  there (keep it, or a restart forgets it)

    A HOST HEARD ON WIFI IS NOT CONNECTED TO until its operator approves it
    on this phone (operator, 2026-10-22): before, a phone joined every host it
    heard, and only a server's allow list stood in the way. Anyone on the
    WiFi can beacon, and a phone host and a server with --lan look the same,
    so the rule is for every host heard. The approval is for the host's KEY:
    the connection insists on that host id, as for a server added from its
    code. A host added by address (add_host) was the operator's own doing
    and is approved by that.
    """

    def __init__(self, hello_fn, on_bundle, exchange_fn, discovery_port=DISCOVERY_PORT,
                 listen=True, prepare_socket=None, clock=time.time, log=None,
                 own_hosts=None, approved=None, on_found=None, on_first_key=None):
        self._own_hosts = own_hosts or (lambda: ())
        self._approved = approved or (lambda host_id: False)
        self._on_found = on_found or (lambda host_id, name, addr: None)
        self._on_first_key = on_first_key or (lambda addr, port, host_id, name: None)
        self._hello = hello_fn
        self._exchange = exchange_fn
        self._on_bundle = on_bundle
        self._discovery_port = discovery_port
        self._listen = listen
        self._prepare = prepare_socket or (lambda s: None)
        self._clock = clock
        self._log = log or (lambda m: None)
        self._lock = threading.Lock()
        # One entry per host: keyed by its id once a beacon names it, by
        # "addr:port" for a host added by address. One entry, one connection:
        # a host heard on two addresses would otherwise be connected twice,
        # and each connection would push the other off the host's roster.
        self._hosts = {}        # key -> {"host", "name", "addr", "port", "seen", "manual",
        #                                 "expect", "conn", "problem", "fails"}
        self._unsent = {}       # host_id -> the latest status not yet delivered
        self._running = False
        self._udp = None

    def start(self):
        self._running = True
        if self._listen:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", self._discovery_port))
            s.settimeout(1.0)
            self._udp = s
            threading.Thread(target=self._listen_loop, name="settings-discover",
                             daemon=True).start()

    def stop(self):
        self._running = False
        if self._udp:
            try:
                self._udp.close()
            except OSError:
                pass
        with self._lock:
            for h in self._hosts.values():
                if h.get("conn"):
                    h["conn"].close()

    @staticmethod
    def key_for(addr, port):
        return "%s:%d" % (addr, int(port))

    def add_host(self, addr, port=HOST_PORT, host_id=None, name=""):
        """A host by address (a server). `host_id`, from the server's code,
        is checked against whoever answers there. Kept until removed."""
        self._heard(self.key_for(addr, port), addr, int(port), None, name, manual=True,
                    expect=host_id)

    def remove_host(self, addr, port=HOST_PORT):
        with self._lock:
            h = self._hosts.pop(self.key_for(addr, port), None)
        if h is not None and h.get("conn"):
            h["conn"].close()
        return h is not None

    def hosts(self):
        """The hosts this phone connects to: added by address, or heard and
        approved."""
        with self._lock:
            return [{"host": h["host"], "name": h["name"], "addr": h["addr"],
                     "port": h["port"], "manual": h["manual"], "problem": h["problem"],
                     "connected": bool(h.get("conn") and h["conn"].alive)}
                    for _, h in sorted(self._hosts.items()) if h["approved"]]

    def found(self):
        """Hosts heard on WiFi and waiting for this phone's operator to
        approve them. Nothing is connected to them."""
        with self._lock:
            return [{"host": h["host"], "name": h["name"], "addr": h["addr"], "port": h["port"]}
                    for _, h in sorted(self._hosts.items()) if not h["approved"]]

    def approve_host(self, host_id):
        """The operator approved a host heard on WiFi: connect to it."""
        starts = []
        with self._lock:
            for key, h in self._hosts.items():
                if not h["manual"] and h["host"] == host_id and not h["approved"]:
                    h["approved"] = True
                    starts.append(key)
        for key in starts:
            self._start_loop(key)
        return bool(starts)

    def forget_host(self, host_id):
        """Disconnect from a host heard on WiFi. Heard again, it is asked
        about again (the caller keeps whether it is approved)."""
        with self._lock:
            keys = [k for k, h in self._hosts.items() if not h["manual"] and h["host"] == host_id]
            gone = [self._hosts.pop(k) for k in keys]
        for h in gone:
            if h.get("conn"):
                h["conn"].close()
        return bool(gone)

    def _start_loop(self, key):
        threading.Thread(target=self._host_loop, args=(key,),
                         name="settings-phone", daemon=True).start()

    def _listen_loop(self):
        while self._running:
            try:
                data, (addr, _) = self._udp.recvfrom(2048)
            except socket.timeout:
                self._forget()
                continue
            except OSError:
                break
            if not data.startswith(BEACON_MAGIC):
                continue
            try:
                b = json.loads(data[len(BEACON_MAGIC):].decode("utf-8"))
                host_id = str(b["host"])[:32]
                if host_id and host_id not in self._own_hosts() and not self._listed(host_id):
                    self._heard(host_id, addr, int(b["port"]), host_id, str(b["name"])[:64])
            except (ValueError, KeyError, TypeError):
                continue
            self._forget()

    def _listed(self, host_id):
        """A host already connected by address: its beacon would make a second
        connection to the same host."""
        with self._lock:
            return any(h["manual"] and host_id in (h["host"], h["expect"])
                       for h in self._hosts.values())

    def _forget(self):
        now = self._clock()
        with self._lock:
            for key, h in list(self._hosts.items()):
                if not h["manual"] and now - h["seen"] > HOST_FORGET_S:
                    if h.get("conn"):
                        h["conn"].close()
                    del self._hosts[key]

    def _heard(self, key, addr, port, host_id, name, manual=False, expect=None):
        found = None
        with self._lock:
            h = self._hosts.get(key)
            new = h is None
            if new:
                # Heard on WiFi: the connection must reach THAT host, and only
                # once approved.
                approved = manual or bool(self._approved(host_id))
                h = self._hosts[key] = {"host": host_id, "name": name, "manual": manual,
                                        "expect": expect if manual else host_id,
                                        "addr": addr, "port": port, "approved": approved,
                                        "conn": None, "problem": "", "fails": 0}
                if not approved:
                    found = (host_id, name, addr)
            start = new and h["approved"]
            h["seen"] = self._clock()
            if not (h.get("conn") and h["conn"].alive):
                # Moved (a new DHCP lease, another interface): follow it, but
                # never off an address that is working.
                h["addr"], h["port"] = addr, port
            if host_id:
                h["name"] = name or h["name"]
        if start:
            self._start_loop(key)
        if found:
            self._on_found(*found)

    def _host_loop(self, key):
        """Connected to one host for as long as it is heard (or listed)."""
        while self._running:
            with self._lock:
                h = self._hosts.get(key)
                if h is None:
                    return
                where = (h["addr"], h["port"])
                manual = h["manual"]
            s = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                # A beacon only comes over the local network. A host added by
                # address is pinned to WiFi only if it is on one (an off-grid
                # router has no internet, so Android would route via mobile
                # data), and left to Android's choice for the internet.
                if not manual or is_local(where[0]):
                    self._prepare(s)
                s.settimeout(10.0)
                s.connect(where)
                self._session(key, _Conn(s))
            except OSError as e:
                self._problem(key, "cannot connect: %s" % (e.strerror or e))
                if s is not None:
                    s.close()
            with self._lock:
                h = self._hosts.get(key)
                if h is None:
                    return
                h["fails"] = h["fails"] + 1 if manual else 0
                delay = min(RECONNECT_S * 2 ** max(0, h["fails"] - 1), RECONNECT_MAX_S)
            end = time.time() + delay
            while self._running and time.time() < end and self._present(key):
                time.sleep(min(0.5, delay))

    def _present(self, key):
        with self._lock:
            return key in self._hosts

    def _problem(self, key, text):
        with self._lock:
            h = self._hosts.get(key)
            if h is not None:
                h["problem"] = text

    def _session(self, key, conn):
        state = {"host": None, "ping_s": PING_S}

        def on_message(msg):
            t = msg.get("t")
            if t == "welcome" and state["host"] is None:
                host_id = self._welcome(key, conn, msg)
                if host_id is None:
                    conn.close()
                    return
                state["host"] = host_id
                state["ping_s"] = min(max(int(msg["ping_s"]), PING_MIN_S), PING_MAX_S)
                self._flush(host_id)
                self._log("settings: connected to host %s at %s" % (host_id, key))
            elif t == "bundle" and state["host"] is not None:
                rev, st, detail = self._on_bundle(state["host"], str(msg.get("text", "")))
                if rev is not None:
                    self.report(state["host"], rev, st, detail)

        _serve(conn, on_message, lambda: state["ping_s"],
               ready=lambda: state["host"] is not None, ready_by=_now_s() + HELLO_S)
        with self._lock:
            h = self._hosts.get(key)
            if h is not None and h.get("conn") is conn:
                h["conn"] = None

    def _welcome(self, key, conn, msg):
        """Check a host's welcome and answer it: the host's id, or None."""
        try:
            if msg.get("v") != PROTOCOL_VERSION:
                raise _HostProblem("the host runs another version of the app (protocol %r)"
                                   % msg.get("v"))
            host_ed = _unb64(msg["host_ed"])
            host_id = str(msg["host"])
            if ss.host_id_for(host_ed) != host_id or not ss.ed_verify(
                    host_ed, _unb64(msg["sig"]), _welcome_signed(msg)):
                raise _HostProblem("the host's welcome is not signed by its key")
            epk, nonce = _unb64(msg["epk"]), _unb64(msg["nonce"])
            int(msg["ping_s"])
            with self._lock:
                h = self._hosts.get(key)
                if h is None:
                    return None
                if h.get("expect") and h["expect"] != host_id:
                    raise _HostProblem("a different host answers at this address (%s, not "
                                       "the %s its code named)" % (host_id, h["expect"]))
        except _HostProblem as e:
            self._problem(key, str(e))
            return None
        except (KeyError, TypeError, ValueError):
            self._problem(key, "the host's welcome is damaged")
            return None
        hello = dict(self._hello(host_id))
        x_pub = _unb64(hello["x_pub"])
        mine, phone_epk = ss.x25519_keypair()              # this connection's only
        proof_key, to_host, to_phone = ss.channel_keys(
            self._exchange(epk), ss.x25519_exchange(mine, epk), x_pub, phone_epk, epk, nonce)
        del mine
        hello.update({"t": "hello", "v": PROTOCOL_VERSION, "epk": _b64(phone_epk),
                      "proof": _b64(_proof(proof_key, host_id))})
        conn.send(hello)
        conn.secure(to_host, to_phone)
        first = None
        with self._lock:
            h = self._hosts.get(key)
            if h is None:
                return None
            h.update({"host": host_id, "name": str(msg.get("name", ""))[:64] or h["name"],
                      "conn": conn, "problem": "", "fails": 0})
            if h["manual"] and not h.get("expect"):
                # A typed address had no key to insist on: the first host to
                # answer there is the one from now on (trust on first use),
                # so a different machine later is refused, not taken.
                h["expect"] = host_id
                first = (h["addr"], h["port"], host_id, h["name"])
            # Its beacon may have been heard first: one connection per host,
            # and the one added by address is the one kept.
            twins = [k for k, o in self._hosts.items()
                     if h["manual"] and k != key and not o["manual"] and o["host"] == host_id]
            closing = [self._hosts.pop(k).get("conn") for k in twins]
        for c in closing:
            if c is not None:
                c.close()
        if first:
            self._on_first_key(*first)
        return host_id

    def report(self, host_id, rev, state, detail=""):
        """What became of a bundle from `host_id`. Delivered now if that host
        is connected, otherwise when it next is (only the latest counts)."""
        with self._lock:
            self._unsent[host_id] = {"t": "status", "rev": rev, "state": state,
                                     "detail": detail}
        self._flush(host_id)

    def _flush(self, host_id):
        with self._lock:
            msg = self._unsent.get(host_id)
            conns = [h["conn"] for h in self._hosts.values()
                     if h["host"] == host_id and h.get("conn") and h["conn"].alive]
        if msg and conns and conns[0].send(msg):
            with self._lock:
                if self._unsent.get(host_id) is msg:
                    del self._unsent[host_id]
