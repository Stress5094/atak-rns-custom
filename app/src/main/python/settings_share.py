"""
Settings sharing, the core: a host signs a bundle of settings, a phone checks
it, shows what would change, and applies it only with the phone's own PIN or
because the operator chose to trust that host. Plan: docs/settings-sharing.md.

Free of RNS and Android imports, with the clock and the radio injected, so the
same file runs in the app (Chaquopy), on a server (the host side), and in the
desktop tests with REAL crypto -- the same reason mesh.py is written this way.
Needs `cryptography` (Ed25519, X25519, AES-GCM): 3.4.8 on the phone.

WHAT A BUNDLE IS

    {"body": {"v": 1, "host": <id>, "host_name", "host_ed": <b64>,
              "rev": <int>, "switch_at": <unix s or null>, "issued": <unix s>,
              "settings": {...}},
     "sig": <b64 Ed25519 over the canonical JSON of body>}

carried as "RTAKS1." + base64url(zlib(json)) so one string serves a QR code
and a network message alike. `host` is derived from `host_ed`, so a bundle
cannot claim one host's id under another's key.

WHAT A PHONE ENFORCES, whoever sent the bundle

- the signature, and the key pinned for that host id once it is trusted;
- `rev` strictly above the last revision decided from that host: no replay,
  no rollback to an older bundle;
- only SETTING_KEYS. The PIN, the trust list and this phone's own
  "accept the passphrase over the network" choice are not settings at all, so
  no bundle -- trusted or not -- can reach them;
- a passphrase arriving over the network only SEALED to this phone, and only
  if this phone opted in. Otherwise it is dropped from the offer, and the
  offer says so. QR (in person) may carry it plainly.

APPROVAL

Accept = this phone's PIN (salted PBKDF2, never the PIN itself; 5 wrong tries
lock it for 60 s, doubling with each further run of 5). A TRUSTED host's
bundle applies without asking. Resetting a forgotten PIN clears every trusted
host, or the reset would be a way round the PIN.

SWITCHING AND THE UNDO

A bundle carries `switch_at`; an approved bundle applies then, or at once if
it is approved late (that is how a straggler catches up). An applied change
STAYS applied: only the operator's Undo puts the previous settings back.

There was an automatic undo (hearing no node for 300 s after a change that can
split the network put the old settings back). Removed 2026-10-23 at the
operator's request. It counted a node heard on ANY link, so a push that left
LoRa deaf was never undone while FM failover or WiFi still heard someone, and
a phone that does hear nobody may be alone for reasons unrelated to the push.
"""

import base64
import hashlib
import hmac
import json
import os
import time
import zlib

try:
    from cryptography.exceptions import InvalidSignature, InvalidTag
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey)
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey, X25519PublicKey)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
except ImportError:  # pragma: no cover -- the desktop venv and the phone have it
    Ed25519PrivateKey = None

BUNDLE_VERSION = 1
PREFIX = "RTAKS1."

# What a bundle may change. Anything else in a bundle refuses the bundle:
# a host sending a key this phone does not know is a version mismatch, and
# applying half of it would leave a phone on settings nobody chose.
SETTING_KEYS = ("lora_preset", "lora_freq_mhz", "lora_tx_dbm", "fm_profile",
                "fm_custom", "fm_clear", "coverage", "passphrase")
COVERAGE_KEYS = ("fm_mhz", "fm_w", "tx_height_m")
# Settings that, changed on some phones and not others, split the network.
NETWORK_KEYS = ("lora_preset", "lora_freq_mhz", "fm_profile", "fm_custom",
                "fm_clear", "passphrase")
# fm_custom is the NET CODE of a custom FM setting (fm_custom.net_code), sent
# beside fm_profile = "custom". Only what every node must share travels;
# keying, lead-in, filter and copies are each phone's own and stay put.
# fm_clear (bool) is "send FM unencrypted": a network key in the same sense,
# because a phone with it off ignores the clear frames a phone with it on sends.

ROUTE_QR = "qr"
ROUTE_NETWORK = "network"

DEFAULT_SWITCH_DELAY_S = 120

PIN_MIN_DIGITS = 4
PIN_MAX_DIGITS = 12
PIN_ITERATIONS = 200000
PIN_TRIES_PER_LOCK = 5
PIN_LOCK_S = 60

# A bundle issued further ahead than this is refused: a host clock that far
# out is broken, and switch_at from it would mean nothing.
MAX_CLOCK_SKEW_S = 24 * 3600


class Refused(Exception):
    """A bundle this phone will not offer. The message is for the operator."""


class PinError(Exception):
    """Wrong PIN, no PIN set yet, or locked. The message is for the operator."""


# -- encoding ---------------------------------------------------------------

def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def _unb64(text):
    return base64.b64decode(text.encode("ascii"), validate=True)


def _canonical(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def encode(bundle):
    raw = zlib.compress(json.dumps(bundle, separators=(",", ":")).encode("utf-8"), 9)
    return PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode(text):
    text = (text or "").strip()
    if not text.startswith(PREFIX):
        raise Refused("Not a settings bundle")
    data = text[len(PREFIX):]
    try:
        raw = zlib.decompress(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
        bundle = json.loads(raw.decode("utf-8"))
    except Exception:
        raise Refused("The settings bundle is damaged")
    if not isinstance(bundle, dict) or not isinstance(bundle.get("body"), dict):
        raise Refused("The settings bundle is damaged")
    return bundle


def host_id_for(ed_pub_raw):
    return hashlib.sha256(b"rtak-host" + ed_pub_raw).hexdigest()[:16]


def phone_id_for(x_pub_raw):
    return hashlib.sha256(b"rtak-phone" + x_pub_raw).hexdigest()[:16]


# -- keys -------------------------------------------------------------------

def _raw_private(key):
    return key.private_bytes(serialization.Encoding.Raw,
                             serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())


def _raw_public(key):
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _write_json(path, obj):
    """Atomic: a half-written state file would lose the PIN and the trust
    list together."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class HostKeys(object):
    """A host's signing key. On a server this file IS the host: lose it and
    every phone that trusted the host has to trust it again."""

    def __init__(self, ed_private):
        self._ed = ed_private
        self.ed_pub = _raw_public(ed_private.public_key())
        self.host_id = host_id_for(self.ed_pub)

    @classmethod
    def generate(cls):
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def load_or_create(cls, path):
        if os.path.exists(path):
            with open(path) as f:
                return cls(Ed25519PrivateKey.from_private_bytes(_unb64(json.load(f)["ed"])))
        keys = cls.generate()
        _write_json(path, {"ed": _b64(_raw_private(keys._ed))})
        return keys

    def sign(self, data):
        return self._ed.sign(data)


def seal_passphrase(passphrase, phone_x_pub_raw):
    """Encrypt the passphrase to ONE phone: X25519 with a fresh key, HKDF,
    AES-GCM. Another device on the same network cannot read it."""
    eph = X25519PrivateKey.generate()
    eph_pub = _raw_public(eph.public_key())
    shared = eph.exchange(X25519PublicKey.from_public_bytes(phone_x_pub_raw))
    key = HKDF(hashes.SHA256(), 32, None,
               b"rtak-settings-passphrase-v1" + phone_x_pub_raw + eph_pub).derive(shared)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, passphrase.encode("utf-8"), None)
    return {"to": phone_id_for(phone_x_pub_raw), "epk": _b64(eph_pub),
            "n": _b64(nonce), "ct": _b64(ct)}


def _open_passphrase(sealed, phone_x_private):
    my_pub = _raw_public(phone_x_private.public_key())
    eph_pub = _unb64(sealed["epk"])
    shared = phone_x_private.exchange(X25519PublicKey.from_public_bytes(eph_pub))
    key = HKDF(hashes.SHA256(), 32, None,
               b"rtak-settings-passphrase-v1" + my_pub + eph_pub).derive(shared)
    return AESGCM(key).decrypt(_unb64(sealed["n"]), _unb64(sealed["ct"]), None).decode("utf-8")


def x25519_keypair():
    """(private key, raw public): a host's key for one network connection."""
    k = X25519PrivateKey.generate()
    return k, _raw_public(k.public_key())


def x25519_exchange(private, peer_pub_raw):
    return private.exchange(X25519PublicKey.from_public_bytes(peer_pub_raw))


def channel_keys(static_shared, eph_shared, phone_x_pub, phone_epk, epk, nonce):
    """A settings connection's keys (settings_net): one to prove the phone
    holds its key, one per direction to encrypt with. Bound to every public
    key and the host's nonce, so nothing carries over to another connection.

    Two secrets go in. `static_shared` is the host's one-time key with the
    phone's PERMANENT key: only the real phone can compute it, which is what
    the proof proves. `eph_shared` is the host's one-time key with the
    phone's one-time key: both are thrown away after the connection, so a
    phone key stolen later cannot unlock a recorded session (forward
    secrecy). With only the first, it could (found reviewing, 2026-10-22)."""
    okm = HKDF(hashes.SHA256(), 96, None,
               b"rtak-settings-channel-v3" + phone_x_pub + phone_epk + epk + nonce
               ).derive(static_shared + eph_shared)
    return okm[:32], okm[32:64], okm[64:]


def ed_verify(ed_pub_raw, sig, data):
    """True if `sig` is `ed_pub_raw`'s signature over `data`."""
    try:
        Ed25519PublicKey.from_public_bytes(ed_pub_raw).verify(sig, data)
        return True
    except (InvalidSignature, ValueError):
        return False


def make_bundle(keys, host_name, rev, settings, switch_at=None, issued=None):
    """Sign `settings` as revision `rev` of this host. The passphrase, when
    present, is either a plain string (QR only -- a phone refuses it over the
    network) or seal_passphrase()'s dict for one phone."""
    body = {"v": BUNDLE_VERSION, "host": keys.host_id, "host_name": host_name,
            "host_ed": _b64(keys.ed_pub), "rev": int(rev),
            "switch_at": None if switch_at is None else int(switch_at),
            "issued": int(time.time() if issued is None else issued),
            "settings": settings}
    return encode({"body": body, "sig": _b64(keys.sign(_canonical(body)))})


# -- checking a bundle ------------------------------------------------------

def verify(text):
    """The bundle's body if it is well formed and signed by the key it names.
    Raises Refused. Says nothing about whether that host is TRUSTED."""
    bundle = decode(text)
    body = bundle["body"]
    try:
        if body.get("v") != BUNDLE_VERSION:
            raise Refused("This bundle is from a newer or older version of the app "
                          "(format %r)" % body.get("v"))
        ed_pub = _unb64(body["host_ed"])
        if host_id_for(ed_pub) != body["host"]:
            raise Refused("The bundle's host id does not match its key")
        Ed25519PublicKey.from_public_bytes(ed_pub).verify(
            _unb64(bundle["sig"]), _canonical(body))
    except Refused:
        raise
    except InvalidSignature:
        raise Refused("The bundle's signature is not valid -- it was altered "
                      "or not signed by the host it names")
    except Exception:
        raise Refused("The settings bundle is damaged")
    if not isinstance(body.get("rev"), int) or body["rev"] < 1:
        raise Refused("The bundle has no valid revision number")
    settings = body.get("settings")
    if not isinstance(settings, dict) or not settings:
        raise Refused("The bundle carries no settings")
    unknown = sorted(set(settings) - set(SETTING_KEYS))
    if unknown:
        raise Refused("The bundle sets things this app does not know (%s) -- "
                      "update the app" % ", ".join(unknown))
    for k in ("fm_profile", "fm_custom"):
        if k in settings and not isinstance(settings[k], str):
            raise Refused("The bundle's FM settings are not understood")
    if "fm_clear" in settings and not isinstance(settings["fm_clear"], bool):
        raise Refused("The bundle's FM settings are not understood")
    if "fm_custom" in settings and settings.get("fm_profile") != "custom":
        # A net code with nothing selecting it would be applied to nothing.
        raise Refused("The bundle's FM settings are not understood")
    cov = settings.get("coverage")
    if cov is not None and (not isinstance(cov, dict)
                            or set(cov) - set(COVERAGE_KEYS)):
        raise Refused("The bundle's coverage settings are not understood")
    return body


def _flatten(settings):
    """One level, for the before/after list: coverage.fm_mhz etc."""
    out = {}
    for k, v in settings.items():
        if k == "coverage" and isinstance(v, dict):
            for ck, cv in v.items():
                out["coverage." + ck] = cv
        else:
            out[k] = v
    return out


LABELS = {"lora_preset": "LoRa preset", "lora_freq_mhz": "LoRa frequency (MHz)",
          "lora_tx_dbm": "LoRa power (dBm)", "fm_profile": "FM tone profile",
          "fm_custom": "FM custom net code",
          "fm_clear": "FM sent unencrypted",
          "coverage.fm_mhz": "Coverage: FM frequency (MHz)",
          "coverage.fm_w": "Coverage: FM power (W)",
          "coverage.tx_height_m": "Coverage: antenna height (m)",
          "passphrase": "Network passphrase"}


def diff(current, proposed):
    """[{key, label, old, new, network}] for what would CHANGE. The
    passphrase is never shown, only that it changes."""
    cur = _flatten(current or {})
    out = []
    for k, new in sorted(_flatten(proposed).items()):
        old = cur.get(k)
        if str(old) == str(new):
            continue
        secret = k == "passphrase"
        out.append({"key": k, "label": LABELS.get(k, k),
                    "old": "(hidden)" if secret else old,
                    "new": "(new passphrase)" if secret else new,
                    "network": k.split(".")[0] in NETWORK_KEYS})
    return out


def _subset(current, settings):
    out = {}
    for k, v in settings.items():
        if k == "coverage" and isinstance(v, dict):
            cur = current.get("coverage") or {}
            out["coverage"] = {ck: cur.get(ck) for ck in v}
        else:
            out[k] = current.get(k)
    return out


# -- the phone's side -------------------------------------------------------

class Receiver(object):
    """Everything a phone remembers about settings sharing, in one file:
    its PIN hash, its trusted hosts, the last revision decided per host, its
    passphrase-over-network choice, offers waiting, and what to undo.

    The radio is injected:
      current_fn()          -> this phone's settings, in bundle form
      apply_fn(settings)    -> [] or the problems; applies what it can
    """

    def __init__(self, path, current_fn, apply_fn,
                 clock=time.time, pin_iterations=PIN_ITERATIONS, unsupported=()):
        self._path = path
        # Settings this build cannot apply (e.g. the FM tone profile, which
        # needs an app restart): dropped from an offer with a note, so the
        # rest of the bundle still applies.
        self._unsupported = tuple(unsupported)
        self._current = current_fn
        self._apply = apply_fn
        self._clock = clock
        self._iterations = pin_iterations
        self._s = self._load()

    # -- state --------------------------------------------------------------

    def _load(self):
        s = {}
        if os.path.exists(self._path):
            with open(self._path) as f:
                s = json.load(f)
        s.setdefault("pin", None)
        s.setdefault("pin_fails", 0)
        s.setdefault("pin_locked_until", 0)
        s.setdefault("trusted", {})            # host id -> {"ed", "name"}
        s.setdefault("last_rev", {})           # host id -> rev decided
        s.setdefault("accept_passphrase_over_network", False)
        s.setdefault("pending", {})            # host id -> offer
        s.setdefault("scheduled", [])          # approved, waiting for switch_at
        s.setdefault("undo", None)             # {"settings", "applied_at", "host", ...}
        if "x" not in s:
            s["x"] = _b64(_raw_private(X25519PrivateKey.generate()))
        return s

    def _save(self):
        _write_json(self._path, self._s)

    def _x_private(self):
        return X25519PrivateKey.from_private_bytes(_unb64(self._s["x"]))

    @property
    def x_pub(self):
        """This phone's public key, which a host seals the passphrase to."""
        return _raw_public(self._x_private().public_key())

    @property
    def phone_id(self):
        return phone_id_for(self.x_pub)

    def exchange(self, peer_pub_raw):
        """X25519 with this phone's key: how it proves to a settings host
        that it holds the key its id is derived from (settings_net)."""
        return x25519_exchange(self._x_private(), peer_pub_raw)

    # -- the PIN ------------------------------------------------------------

    def has_pin(self):
        return self._s["pin"] is not None

    def _hash(self, pin, salt, iterations):
        return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)

    @staticmethod
    def _check_shape(pin):
        if not (isinstance(pin, str) and pin.isdigit()
                and PIN_MIN_DIGITS <= len(pin) <= PIN_MAX_DIGITS):
            raise PinError("A PIN is %d to %d digits" % (PIN_MIN_DIGITS, PIN_MAX_DIGITS))

    def set_pin(self, new, old=None):
        """First PIN: no `old`. Changing it: the right `old`."""
        self._check_shape(new)
        if self.has_pin():
            self.check_pin(old or "")
        salt = os.urandom(16)
        self._s["pin"] = {"salt": _b64(salt), "it": self._iterations,
                          "hash": _b64(self._hash(new, salt, self._iterations))}
        self._s["pin_fails"] = 0
        self._s["pin_locked_until"] = 0
        self._save()

    def pin_locked_for(self):
        return max(0, int(self._s["pin_locked_until"] - self._clock() + 0.999))

    def check_pin(self, pin):
        """Raises PinError unless right. Counts wrong tries and locks."""
        if not self.has_pin():
            raise PinError("Set a PIN on this phone first")
        wait = self.pin_locked_for()
        if wait:
            raise PinError("Too many wrong PINs -- try again in %d s" % wait)
        p = self._s["pin"]
        good = hmac.compare_digest(
            self._hash(pin or "", _unb64(p["salt"]), p["it"]), _unb64(p["hash"]))
        if good:
            if self._s["pin_fails"]:
                self._s["pin_fails"] = 0
                self._save()
            return
        self._s["pin_fails"] += 1
        fails = self._s["pin_fails"]
        msg = "Wrong PIN"
        if fails % PIN_TRIES_PER_LOCK == 0:
            lock = PIN_LOCK_S * 2 ** (fails // PIN_TRIES_PER_LOCK - 1)
            self._s["pin_locked_until"] = self._clock() + lock
            msg = "Wrong PIN -- locked for %d s" % lock
        self._save()
        raise PinError(msg)

    def reset_pin(self):
        """A forgotten PIN: clears it AND every trusted host, and drops
        anything approved but not yet applied."""
        self._s["pin"] = None
        self._s["pin_fails"] = 0
        self._s["pin_locked_until"] = 0
        self._s["trusted"] = {}
        self._s["scheduled"] = []
        self._save()

    # -- trust, and this phone's own choices (PIN-guarded, never a setting) --

    def trusted_hosts(self):
        return dict(self._s["trusted"])

    def trust_host(self, host_id, host_ed_b64, name, pin):
        self.check_pin(pin)
        if host_id_for(_unb64(host_ed_b64)) != host_id:
            raise Refused("That host's id does not match its key")
        self._s["trusted"][host_id] = {"ed": host_ed_b64, "name": name}
        self._save()

    def untrust_host(self, host_id, pin):
        self.check_pin(pin)
        self._s["trusted"].pop(host_id, None)
        self._save()

    def set_accept_passphrase_over_network(self, on, pin):
        self.check_pin(pin)
        self._s["accept_passphrase_over_network"] = bool(on)
        self._save()

    def accepts_passphrase_over_network(self):
        return self._s["accept_passphrase_over_network"]

    # -- receiving ----------------------------------------------------------

    def receive(self, text, route):
        """Check a bundle and turn it into an offer. Returns the offer, which
        is already APPLIED or SCHEDULED when the host is trusted (see
        offer["outcome"]). Raises Refused."""
        if route not in (ROUTE_QR, ROUTE_NETWORK):
            raise ValueError("route must be qr or network")
        body = verify(text)
        now = self._clock()
        if body["issued"] > now + MAX_CLOCK_SKEW_S:
            raise Refused("The bundle is dated in the future -- check the host's clock")
        host = body["host"]
        trusted = self._s["trusted"].get(host)
        if trusted and trusted["ed"] != body["host_ed"]:
            # Cannot happen while host ids are hashes of keys; kept as the
            # rule a reader can see.
            raise Refused("This host's key has changed since you trusted it")
        last = self._s["last_rev"].get(host, 0)
        if body["rev"] <= last:
            raise Refused("Already decided: revision %d from %s (this phone is "
                          "at %d)" % (body["rev"], body["host_name"], last))

        settings = dict(body["settings"])
        notes = []
        for k in self._unsupported:
            if k in settings:
                del settings[k]
                notes.append("%s was left out: this version of the app cannot "
                              "apply it from a shared bundle" % LABELS.get(k, k))
        if "passphrase" in settings:
            pp = settings.pop("passphrase")
            if isinstance(pp, str):
                if route == ROUTE_QR:
                    settings["passphrase"] = pp
                else:
                    notes.append("The network passphrase was left out: it was sent "
                                 "over the network unprotected")
            elif isinstance(pp, dict) and pp.get("to") == self.phone_id:
                if route == ROUTE_NETWORK and not self.accepts_passphrase_over_network():
                    notes.append("The network passphrase was left out: this phone "
                                 "does not accept it over the network")
                else:
                    try:
                        settings["passphrase"] = _open_passphrase(pp, self._x_private())
                    except (InvalidTag, Exception):
                        notes.append("The network passphrase was left out: it could "
                                     "not be decrypted")
            else:
                notes.append("The network passphrase was left out: it was "
                             "sealed for another phone")
        if not settings:
            raise Refused("Nothing in this bundle can be applied here. " + " ".join(notes))

        offer = {"host": host, "host_name": body["host_name"], "host_ed": body["host_ed"],
                 "rev": body["rev"], "switch_at": body["switch_at"],
                 "settings": settings, "notes": notes,
                 "changes": diff(self._current(), settings),
                 "trusted": bool(trusted), "received": now, "route": route}
        if trusted:
            offer["outcome"] = self._approve(offer)
        else:
            offer["outcome"] = "pending"
            # A newer revision from the same host replaces the one waiting.
            self._s["pending"][host] = offer
            self._save()
        return offer

    def last_rev(self, host):
        """The last revision decided from `host` (0: none), so a host can
        tell what this phone still lacks."""
        return self._s["last_rev"].get(host, 0)

    def pending(self):
        return list(self._s["pending"].values())

    def approve(self, host, pin):
        """Accept the offer waiting from `host` with this phone's PIN.
        Returns "applied", "scheduled" or "failed: ..."."""
        offer = self._s["pending"].get(host)
        if offer is None:
            raise Refused("Nothing is waiting from that host")
        self.check_pin(pin)
        del self._s["pending"][host]
        return self._approve(offer)

    def reject(self, host):
        """No PIN: refusing a change is always allowed."""
        offer = self._s["pending"].pop(host, None)
        if offer is not None:
            self._s["last_rev"][host] = max(self._s["last_rev"].get(host, 0), offer["rev"])
        self._save()

    def _approve(self, offer):
        self._s["last_rev"][offer["host"]] = max(
            self._s["last_rev"].get(offer["host"], 0), offer["rev"])
        # One change waits per host: a newer approval replaces an older one
        # that has not switched yet.
        self._s["scheduled"] = [o for o in self._s["scheduled"]
                                if o["host"] != offer["host"]]
        if offer["switch_at"] is not None and offer["switch_at"] > self._clock():
            self._s["scheduled"].append(offer)
            self._save()
            return "scheduled"
        # Late or immediate: apply now. A straggler catching up lands here.
        return self._apply_offer(offer)

    def _apply_offer(self, offer):
        now = self._clock()
        # Only what this change touches: Undo puts back THESE settings, not a
        # snapshot of everything (which would also undo unrelated changes
        # made by hand since).
        before = _subset(self._current(), offer["settings"])
        problems = self._apply(offer["settings"])
        if problems:
            # Put back whatever part did apply.
            self._apply(before)
            self._s["undo"] = None
            self._save()
            return "failed: " + "; ".join(problems)
        self._s["undo"] = {"settings": before, "applied_at": now,
                           "host": offer["host"], "host_name": offer["host_name"],
                           "rev": offer["rev"]}
        self._save()
        return "applied"

    def undo(self):
        """The Undo button: back to the settings before the last change.
        Returns [] or the problems."""
        u = self._s["undo"]
        if u is None:
            return ["Nothing to undo"]
        problems = self._apply(u["settings"])
        self._s["undo"] = None
        self._save()
        return problems

    def tick(self):
        """Call every few seconds. Applies scheduled changes whose time has
        come; nothing is ever undone from here (see SWITCHING AND THE UNDO).
        Returns what it did, as
        [(event, detail, host, rev)] for the log, the screen and the host's
        roster."""
        now = self._clock()
        events = []
        due = [o for o in self._s["scheduled"] if o["switch_at"] <= now]
        if due:
            self._s["scheduled"] = [o for o in self._s["scheduled"] if o["switch_at"] > now]
            self._save()
            for o in due:
                result = self._apply_offer(o)
                events.append(("switched", "%s rev %d" % (o["host_name"], o["rev"]),
                               o["host"], o["rev"]) if result == "applied" else
                              ("failed", "%s rev %d: %s" % (o["host_name"], o["rev"], result),
                               o["host"], o["rev"]))
        return events


class Host(object):
    """The host's side, for one host identity: revision numbers that only go
    up, and a bundle per phone (the passphrase is sealed per phone). The
    roster of phones is the network layer's (step 3)."""

    def __init__(self, keys, name, rev_path, clock=time.time):
        self.keys = keys
        self.name = name
        self._rev_path = rev_path
        self._clock = clock
        self._rev = 0
        if os.path.exists(rev_path):
            with open(rev_path) as f:
                self._rev = int(json.load(f)["rev"])

    def next_rev(self):
        """Persisted BEFORE use: a host that restarted and reused a revision
        would have its next push refused by every phone as already decided."""
        self._rev += 1
        _write_json(self._rev_path, {"rev": self._rev})
        return self._rev

    def bundles(self, settings, phones=(), passphrase=None,
                switch_in_s=DEFAULT_SWITCH_DELAY_S, rev=None):
        """One push. Returns (rev, {phone_id or "qr": bundle text}).
        `phones` is [(phone_id, x_pub_raw)]; the passphrase, when given, is
        sealed to each. The "qr" bundle carries it plainly -- in person only."""
        rev = self.next_rev() if rev is None else rev
        now = self._clock()
        switch_at = None if switch_in_s is None else now + switch_in_s
        out = {}
        for phone_id, x_pub in phones:
            s = dict(settings)
            if passphrase:
                s["passphrase"] = seal_passphrase(passphrase, x_pub)
            out[phone_id] = make_bundle(self.keys, self.name, rev, s, switch_at, now)
        s = dict(settings)
        if passphrase:
            s["passphrase"] = passphrase
        out["qr"] = make_bundle(self.keys, self.name, rev, s, switch_at, now)
        return rev, out
