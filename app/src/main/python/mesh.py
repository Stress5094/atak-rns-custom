"""
Milestone 6: authenticated encryption, and the frame identity a mesh needs.

Everything the bridge has sent so far went out on a single RNS destination of
type PLAIN, which is unauthenticated and unencrypted by design. Anyone with an
RNode on the same frequency and the destination hash can read every position
and inject events that ATAK will render as real; `_NODE_ID` is a random value
in a file with nothing binding a frame to it, so milestone 4's acknowledgement
accounting can be forged by anyone in earshot.

PLAIN is also single-hop and always will be -- Transport excludes PLAIN from
the path table and never injects plain broadcasts into transport. The
alternative RNS offers, a SINGLE destination, is routed multi-hop but costs a
fixed ~80 bytes of crypto per message *per recipient*: about 123% duty cycle
for 16 nodes reporting once a minute, which does not fit in the channel. So
this module keeps the cheap PLAIN broadcast -- one transmission reaches every
node in earshot -- and puts our own encryption and our own frame identity on
top of it.

WHAT THIS LAYER ADDS, AND WHAT IT COSTS

    [ctrl 1][origin 3][epoch 1][seq 2][hops 1]  header, sent in clear
    [ ciphertext, same length as the plaintext frame            ]
    [ tag 8                                                     ]

16 bytes on a 61-byte position report. The header is deliberately readable --
duplicate suppression has to work before anything is decrypted, or a relay
would have to trial-decrypt everything it hears -- but all of it except `hops`
is covered by the authentication tag, so `origin` and `seq` cannot be altered.

`hops` is excluded because a relay must be able to decrement it, exactly as
with an IP TTL, and it is therefore the one field an attacker can rewrite. It
is placed last so that the authenticated part is a contiguous prefix, and it is
clamped on the way in, so the worst a forged value can buy is bounded by the
duplicate cache.

WHY THE NONCE IS NOT ON THE WIRE

AES-CCM needs a 12-byte nonce that never repeats under one key. Transmitting
one would cost 12 bytes a frame; instead it is *derived* from header fields
that are already there and already authenticated:

    nonce = origin(3) || epoch(1) || seq(2) || six zero bytes

That is what keeps the crypto overhead at 8 bytes rather than 20. It also
means nonce uniqueness is exactly the question "can one node ever reuse an
(epoch, seq) pair", which is why the sequencing below is written the way it is
and should be changed with care: reusing a nonce under CCM leaks the XOR of
two plaintexts and opens the door to forgery. It is the one bug in this file
that would be genuinely dangerous.

Sequence numbers are monotonic within a process. Across restarts they would
start again from zero, so `epoch` is persisted and incremented at every start
-- one small file write per process, not per frame -- and a wrap of `seq` bumps
it too. See `Mesh.next_frame_id`.

WHY AES-CCM WITH A TRUNCATED TAG

Not RNS's own Token: it spends 48 bytes (a 16-byte IV and a full 32-byte HMAC)
plus PKCS7 padding, which is 79% overhead on a position report. Not AES-GCM
either, because pyca will not truncate a GCM tag and 16 bytes is more than this
channel can spare. CCM with an 8-byte tag is what 802.15.4 and LoRaWAN use for
the same reason, and pyca exposes the tag length directly.

TRUST MODEL, STATED PLAINLY

One key, shared by the whole group, derived from a passphrase. Holding it *is*
membership: a node that cannot decrypt is not a peer, and its beacons are
ignored. That authenticates the GROUP, not the MEMBER -- any node holding the
key can put another node's id in `origin` and be believed. This is the same
model as WPA2-PSK and Meshtastic's channels, and it is the right default for a
squad whose radios are all accounted for, but it is a choice and not an
oversight. Per-node signatures would close it at 64 bytes a frame (Ed25519),
which more than doubles a position report; there is room in the frame type
space to add that later without disturbing this format.

Two further consequences worth stating: a lost radio compromises the group
until the passphrase is changed, and the readable header leaks who is
transmitting and how often, which is traffic analysis that IPsec and WireGuard
also accept.

Deliberately free of RNS and Android imports, with the clock injected, so the
whole thing is testable on a desktop. Same shape as tx_policy.py and
reliable.py.

IMPORTANT: the on-device interpreter is CPython 3.8 (Chaquopy ships
libpython3.8.so). Keep this module 3.8-compatible -- no match statements, no
`X | Y` annotations, no str.removeprefix.
"""

import collections
import hashlib
import os
import struct
import threading
import time

import cot_codec

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESCCM
    from cryptography.exceptions import InvalidTag
    HAVE_AEAD = True
except ImportError:  # pragma: no cover - present on device and in the venv
    AESCCM = None

    class InvalidTag(Exception):
        pass

    HAVE_AEAD = False


class MeshError(Exception):
    pass


# Header layout. `hops` sits last so that the authenticated part -- everything
# before it -- is a contiguous prefix and the nonce is a contiguous slice.
#
#   0      ctrl    (PROTOCOL_VERSION << 4) | T_MESH
#   1-3    origin  raw node id
#   4      epoch   key/counter generation
#   5-6    seq     per-origin, big-endian
#   7      hops    remaining, NOT authenticated
HEADER_LEN = 8
ORIGIN_LEN = 3
AAD_LEN = 7          # ctrl through seq; excludes hops
NONCE_LEN = 12

# Authentication tag. FOUR bytes, not the eight this shipped with.
#
# Every frame on this link pays it, and the frames that matter most are tiny --
# a 6-byte ack rode inside 35 bytes of header and seal, so the seal was more
# than half of what a receipt cost to send. Four bytes is ~2% of a position
# report's airtime and ~8% of an ack's.
#
# What it costs: a forgery succeeds with probability 2^-32 per attempt instead
# of 2^-64. That is the honest downgrade, and it is defensible HERE rather than
# in general, because the channel is the limit on attempts. At SF8/125 kHz a
# forged frame takes ~0.13 s of airtime, so an attacker transmitting flat out
# manages ~460 attempts an hour and needs ~10^6 years for an even chance --
# against a group key that a compromised phone hands over instantly. The tag
# was never the weakest part of this design and shortening it does not make it
# so. On a fast link where an attacker could try billions of times, the same
# arithmetic gives the opposite answer.
#
# Replay is unaffected: SeenCache keys on (origin, epoch, seq) and rejects a
# repeat whatever its tag says.
TAG_LEN = 4
OVERHEAD = HEADER_LEN + TAG_LEN
# A clear frame (cot_codec.T_CLEAR) is the header and the plaintext: no tag.
CLEAR_OVERHEAD = HEADER_LEN

_HOPS_OFFSET = 7
# origin || epoch || seq, i.e. the authenticated header minus its ctrl byte.
_NONCE_SLICE = slice(1, AAD_LEN)
_NONCE_PAD = b"\x00" * (NONCE_LEN - (AAD_LEN - 1))


def _nonce(header):
    """Derived, never transmitted -- the whole reason crypto costs 8 bytes here
    and not 20. Unique exactly as long as one origin never reuses an
    (epoch, seq) pair."""
    return bytes(header[_NONCE_SLICE]) + _NONCE_PAD

# Callers cap this well below the limit; it exists so a forged hop count cannot
# ask the network for more than a bounded amount of work. Three bits' worth,
# leaving room to grow without touching the layout.
MAX_HOPS = 7

# `hops` is the number of FURTHER transmissions a frame may receive, so zero
# means "deliver but do not forward" -- the reach the bridge had before step 3.
# It stays the default because it is the safe answer for anything the caller
# has not classified; relay.hop_cap() is what picks a real value per frame.
DEFAULT_HOPS = 0

KEY_BYTES = 32

# The passphrase is chosen by a human and therefore low-entropy, so it is
# stretched rather than merely hashed. The salt is fixed because every node has
# to derive the SAME key from the SAME passphrase -- this is a group key, not a
# stored credential, so a per-node salt would defeat the purpose. The stretching
# is what makes a weak passphrase expensive to attack; the fixed salt only means
# the work can be done once for a given passphrase rather than once per node.
KDF_ROUNDS = 200000
KDF_SALT = b"atakrns-mesh-v1"

# How long a frame id is remembered. Long enough that a retransmission or a
# relayed copy arriving late is still recognised as a duplicate; short enough
# that memory stays bounded on a phone.
DEFAULT_SEEN_TTL_S = 300.0
DEFAULT_MAX_SEEN = 512

# Consecutive rejections, with nothing decrypting in between, before calling it
# a wrong passphrase rather than noise. One is meaningless -- a foreign network
# on the same frequency produces them all day, and so does corruption the
# radio's CRC let through. Two in a row with nothing in between is a peer whose
# key does not match ours, since anything that decrypts resets the count.
MISMATCH_THRESHOLD = 2

_EPOCH_PATH = os.path.expanduser("~/.atakrns_mesh_epoch")


def derive_key(passphrase, rounds=KDF_ROUNDS, salt=KDF_SALT):
    """Passphrase -> 32-byte group key. Same passphrase, same key, everywhere."""
    if not passphrase:
        raise MeshError("mesh passphrase must not be empty")
    if not isinstance(passphrase, bytes):
        passphrase = passphrase.encode("utf-8")
    return hashlib.pbkdf2_hmac("sha256", passphrase, salt, rounds, dklen=KEY_BYTES)


def key_fingerprint(key):
    """Four hex characters identifying a key, for comparing two devices.

    Provisioning a shared secret by reading it off one screen and typing it
    into another is exactly where a silent typo turns into "the radios are
    broken" -- the symptom of a wrong passphrase is indistinguishable from a
    dead link, because both mean nothing arrives. Two nodes showing the same
    fingerprint have the same key; different fingerprints say so immediately.

    A hash of the derived key, not of the passphrase, so it also catches a
    mismatch in the derivation itself. Sixteen bits is far too short to attack
    a key with and quite enough to spot a typo, which is the whole job.
    """
    return hashlib.sha256(b"atakrns-fingerprint" + key).hexdigest()[:4]


def node_id_bytes(node_id):
    """The bridge carries node ids as 6 hex characters; the wire carries 3 raw
    bytes. Anything else is a programming error rather than bad input, so this
    raises rather than guessing."""
    if isinstance(node_id, (bytes, bytearray)):
        raw = bytes(node_id)
    else:
        try:
            raw = bytes.fromhex(node_id)
        except (ValueError, TypeError):
            raise MeshError("node id %r is not hex" % (node_id,))
    if len(raw) != ORIGIN_LEN:
        raise MeshError("node id must be %d bytes, got %d" % (ORIGIN_LEN, len(raw)))
    return raw


def node_id_hex(raw):
    return raw.hex()


def load_epoch(path=_EPOCH_PATH):
    """Read the persisted counter generation, increment it, and write it back.

    Called once per process. Sequence numbers restart at zero on every launch,
    so without this a restarted node would reuse (epoch, seq) pairs it had
    already used -- which is nonce reuse, and the one failure in this module
    with real consequences.

    If the file cannot be written, falls back to a random epoch rather than
    refusing to start. That trades a 1-in-256 chance of colliding with a
    previously used generation for the app being able to run at all, which is
    the right way round for a device whose storage may be full or read-only --
    but it is a real weakening, so it is logged by the caller rather than
    passed over in silence. Returns (epoch, persisted).
    """
    epoch = 0
    try:
        with open(path) as f:
            epoch = (int(f.read().strip()) + 1) & 0xFF
    except (OSError, ValueError):
        epoch = 0
    if save_epoch(epoch, path):
        return epoch, True
    return struct.unpack("B", os.urandom(1))[0], False


def save_epoch(epoch, path=_EPOCH_PATH):
    """Record the generation now in use, so the next start comes after it.

    Also called when a sequence wrap bumps the epoch mid-run -- that bump has
    the same meaning as a restart and has to survive one. Returns whether it
    stuck; best-effort by design, since refusing to transmit because a counter
    file will not write would be a worse failure than the one it prevents.
    """
    try:
        with open(path, "w") as f:
            f.write(str(epoch & 0xFF))
        return True
    except OSError:
        return False


Inbound = collections.namedtuple("Inbound", "origin epoch seq hops frame duplicate")


class SeenCache(object):
    """Remembers frame ids, so a duplicate is neither relayed nor delivered twice.

    Same shape as reliable.ReliableRx's seen-set and for the same reasons: it
    runs on the RNode serial read thread, where an O(n) scan stalls radio
    reception, so expiry walks from the head of an insertion-ordered dict and
    stops at the first live entry.

    This is also what bounds replay: a recorded frame played back inside the TTL
    is recognised and dropped. Outside the TTL it is not, which is why the
    caller additionally checks the decoded event's timestamp against the clock.
    """

    def __init__(self, ttl_s=DEFAULT_SEEN_TTL_S, max_seen=DEFAULT_MAX_SEEN,
                 clock=time.monotonic):
        self.ttl_s = ttl_s
        self.max_seen = max_seen
        self._clock = clock
        self._seen = collections.OrderedDict()
        self._lock = threading.Lock()

    def check(self, frame_id):
        """True the first time a frame id is offered, False afterwards.

        Never raises: failing open costs a duplicate, failing closed drops a
        real message.
        """
        try:
            with self._lock:
                now = self._clock()
                while self._seen:
                    key, seen_at = next(iter(self._seen.items()))
                    if now - seen_at < self.ttl_s:
                        break
                    self._seen.pop(key)
                if frame_id in self._seen:
                    return False
                self._seen[frame_id] = now
                while len(self._seen) > self.max_seen:
                    self._seen.popitem(last=False)
                return True
        except Exception:
            return True

    def forget(self, frame_id):
        """Drop a frame id, so the next copy of it reads as new again.

        For a receiver that decides, after unwrapping, that it should never
        have heard this frame -- which on a real radio only happens when the
        bench is simulating a node being out of range. Leaving the id recorded
        would make the RELAYED copy of the same frame look like a replay and be
        dropped, which is precisely the thing a multi-hop test needs to see
        arrive. Simulated deafness has to leave no trace to be worth anything.
        """
        try:
            with self._lock:
                self._seen.pop(frame_id, None)
        except Exception:
            pass

    def __len__(self):
        return len(self._seen)


class Mesh(object):
    """Wraps and unwraps frames, and hands out frame ids.

    wrap() runs on the CoT listener and beacon threads, unwrap() on the RNode
    serial read thread. Unlike reliable.py this one does NOT leave locking to
    the caller: sequence allocation is the critical section, and getting it
    wrong means reusing a nonce, which is too dangerous to make somebody else's
    responsibility.
    """

    def __init__(self, node_id, key, epoch=0, on_epoch_change=None,
                 seen=None, clock=time.monotonic):
        if not HAVE_AEAD:
            raise MeshError(
                "no AEAD backend: the cryptography package is required "
                "(it ships with RNS on device; on a desktop use a venv that has it)")
        if len(key) != KEY_BYTES:
            raise MeshError("key must be %d bytes, got %d" % (KEY_BYTES, len(key)))
        self.origin = node_id_bytes(node_id)
        self._aead = AESCCM(key, tag_length=TAG_LEN)
        self._epoch = epoch & 0xFF
        self._seq = 0
        self._on_epoch_change = on_epoch_change
        self._seq_lock = threading.Lock()
        self.seen = seen if seen is not None else SeenCache(clock=clock)
        self._stats = collections.Counter()
        # Rejections since the last frame that verified. The cumulative
        # `unauthenticated` total cannot answer "is the key wrong RIGHT NOW":
        # a node that talked happily for an hour and then had its peer re-key
        # has a large accepted count that hides a total failure. This one
        # returns to zero the instant anything decrypts, so it describes the
        # present rather than the history.
        self._since_accept = 0
        # The version last heard from a peer that is not on ours, so a report
        # can name which build to update rather than only that something is off.
        self._peer_version = None
        # Whether unencrypted frames (cot_codec.T_CLEAR) are accepted. Off by
        # default: a clear frame proves nothing about who sent it, so a node
        # that has not opted in treats them as foreign. A node set to send FM
        # unencrypted turns this on, since its peers will be doing the same.
        self.accept_clear = False

    # -- outbound -----------------------------------------------------------

    def next_frame_id(self):
        """Allocate the (epoch, seq) that makes this frame's nonce unique.

        Wrapping `seq` bumps `epoch`, which is the same thing a restart does,
        so the persisted value stays ahead of anything already used. At one
        frame a minute a wrap is about 45 days away; it is handled anyway
        because the alternative is silent nonce reuse.
        """
        with self._seq_lock:
            # The epoch is snapshotted BEFORE any bump: this frame belongs to
            # the generation it was allocated under, and the wrap takes effect
            # for the next one. Returning the incremented value instead would
            # spend the new generation's last sequence number before its first.
            epoch, seq = self._epoch, self._seq
            self._seq = (seq + 1) & 0xFFFF
            if self._seq == 0:
                self._epoch = (epoch + 1) & 0xFF
                if self._on_epoch_change is not None:
                    try:
                        self._on_epoch_change(self._epoch)
                    except Exception:
                        pass  # persistence is best-effort; the bump still stands
            return epoch, seq

    def wrap(self, frame, hops=DEFAULT_HOPS):
        """One complete inner frame -> one mesh frame ready for the radio.

        The inner frame is whatever cot_codec produced -- compact, zlib'd XML, a
        fragment, a beacon, a reliable wrapper, an ack. This layer is outermost
        and does not care which, so the entire existing frame vocabulary keeps
        working unchanged and every one of them gets encrypted.
        """
        if not frame:
            raise MeshError("cannot wrap an empty frame")
        epoch, seq = self.next_frame_id()
        header = (bytes([cot_codec.CTRL_MESH]) + self.origin
                  + bytes([epoch]) + struct.pack(">H", seq)
                  + bytes([min(max(int(hops), 0), MAX_HOPS)]))
        sealed = self._aead.encrypt(_nonce(header), bytes(frame), header[:AAD_LEN])
        self._stats["wrapped"] += 1
        return header + sealed

    def forward(self, packet):
        """The same packet with one hop spent, ready to go out again.

        A relay does NOT re-wrap. Re-wrapping would re-encrypt under this
        node's own origin and sequence, which would erase the sender the
        receiver needs and break the `(origin, epoch, seq)` identity every
        node's duplicate suppression is keyed on -- the frame would flood
        forever, once per relay. So the ciphertext, the tag and every
        authenticated header byte are passed through untouched and only `hops`
        changes, which is the entire reason that byte is outside the AAD.

        A consequence worth naming: the original sender hears its own frame
        come back, recognises its own origin and drops it. That is free passive
        acknowledgement -- proof the relay happened, at no airtime cost.

        Returns None if there is nothing left to spend.
        """
        clear = is_clear(packet)
        if clear:
            if len(packet) < HEADER_LEN + 1:
                return None
        elif not is_mesh(packet) or len(packet) < HEADER_LEN + TAG_LEN + 1:
            return None
        hops = min(packet[_HOPS_OFFSET], MAX_HOPS)
        if hops <= 0:
            return None
        out = bytearray(packet)
        out[_HOPS_OFFSET] = hops - 1
        self._stats["forwarded"] += 1
        return bytes(out)

    def to_clear(self, packet):
        """A sealed mesh frame -> the same frame unencrypted (T_CLEAR), for
        sending where the content must be readable by any listener.

        Same header (origin, epoch, seq, hops) with only the type changed, so
        duplicate suppression and relaying work exactly as for a sealed frame,
        and the plaintext in place of ciphertext+tag -- 4 bytes SHORTER, so
        anything sized for a sealed frame still fits. A frame that is already
        clear passes through. Returns None for anything that does not decrypt
        under this key (it is not ours to re-send in the clear).
        """
        if is_clear(packet):
            return bytes(packet)
        if not is_mesh(packet) or len(packet) < HEADER_LEN + TAG_LEN + 1:
            return None
        header = bytes(packet[:HEADER_LEN])
        try:
            frame = self._aead.decrypt(_nonce(header), bytes(packet[HEADER_LEN:]),
                                       header[:AAD_LEN])
        except InvalidTag:
            return None
        self._stats["sent_clear"] += 1
        return bytes([cot_codec.CTRL_CLEAR]) + header[1:] + frame

    def overhead(self):
        """Bytes wrap() will add, so a caller can shrink the budget it hands to
        the encoder and keep a single-packet message single-packet. Mirrors
        cot_codec.reliable_overhead()."""
        return OVERHEAD

    # -- inbound ------------------------------------------------------------

    def unwrap(self, packet):
        """One received payload -> an Inbound, or None if it is not for us.

        Returns None -- never raises -- for anything unusable: a frame that is
        not ours, one that fails authentication, or one whose header is
        truncated. A radio hears noise, foreign networks and outright hostile
        traffic, and none of those should be able to interrupt reception.

        `duplicate` is set rather than swallowed because the caller may still
        want to act on a repeat: milestone 4 must acknowledge a duplicate (the
        sender repeating itself means our previous ack is what went missing),
        while a relay must not forward one.
        """
        try:
            if is_clear(packet):
                return self._unwrap_clear(packet)
            if not is_mesh(packet):
                # A frame carrying our envelope's TYPE but a different protocol
                # version is a peer running another build, and it deserves
                # saying so: bumping PROTOCOL_VERSION is exactly what makes a
                # half-finished rollout produce two radios that transmit
                # perfectly and ignore each other. Everything else -- noise,
                # other people's networks -- is genuinely foreign and boring.
                if packet and (packet[0] & 0x0F) == cot_codec.T_MESH:
                    self._stats["wrong_version"] += 1
                    self._peer_version = packet[0] >> 4
                else:
                    self._stats["foreign"] += 1
                return None
            if len(packet) < HEADER_LEN + TAG_LEN + 1:
                self._stats["truncated"] += 1
                return None

            header = packet[:HEADER_LEN]
            origin = header[1:1 + ORIGIN_LEN]
            epoch = header[4]
            (seq,) = struct.unpack(">H", header[5:7])
            hops = min(header[_HOPS_OFFSET], MAX_HOPS)

            try:
                frame = self._aead.decrypt(_nonce(header), packet[HEADER_LEN:],
                                           header[:AAD_LEN])
            except InvalidTag:
                # Either a foreign network sharing the frequency, corruption the
                # radio's own CRC let through, or someone trying it on. All
                # three are the same non-event from here.
                self._stats["unauthenticated"] += 1
                self._since_accept += 1
                return None

            # Reset on ANY frame that verified, duplicates included: a duplicate
            # decrypted, so it proves the key is right, which is the only thing
            # this counter is measuring.
            self._since_accept = 0
            fresh = self.seen.check((origin, epoch, seq))
            self._stats["unwrapped" if fresh else "duplicate"] += 1
            return Inbound(origin, epoch, seq, hops, frame, not fresh)
        except Exception:
            self._stats["error"] += 1
            return None

    def _unwrap_clear(self, packet):
        """unwrap() for an unencrypted frame: accepted only when this node has
        opted in (accept_clear); otherwise the same non-event as a foreign
        network's frame."""
        if not self.accept_clear:
            self._stats["clear_ignored"] += 1
            return None
        if len(packet) < HEADER_LEN + 1:
            self._stats["truncated"] += 1
            return None
        header = packet[:HEADER_LEN]
        origin = bytes(header[1:1 + ORIGIN_LEN])
        epoch = header[4]
        (seq,) = struct.unpack(">H", bytes(header[5:7]))
        hops = min(header[_HOPS_OFFSET], MAX_HOPS)
        fresh = self.seen.check((origin, epoch, seq))
        self._stats["unwrapped_clear" if fresh else "duplicate"] += 1
        return Inbound(origin, epoch, seq, hops, bytes(packet[HEADER_LEN:]), not fresh)

    # -- reporting ----------------------------------------------------------

    def stats(self):
        out = dict(self._stats)
        out["seen"] = len(self.seen)
        out["epoch"] = self._epoch
        out["seq"] = self._seq
        out["since_accept"] = self._since_accept
        out["version"] = cot_codec.PROTOCOL_VERSION
        if self._peer_version is not None:
            out["peer_version"] = self._peer_version
        return out

    def summary_line(self):
        s = self._stats
        line = ("mesh: wrapped=%d unwrapped=%d duplicate=%d unauthenticated=%d "
                "foreign=%d epoch=%d"
                % (s.get("wrapped", 0), s.get("unwrapped", 0),
                   s.get("duplicate", 0), s.get("unauthenticated", 0),
                   s.get("foreign", 0), self._epoch))
        if s.get("sent_clear") or s.get("unwrapped_clear") or s.get("clear_ignored"):
            line += (" clear_sent=%d clear_heard=%d clear_ignored=%d"
                     % (s.get("sent_clear", 0), s.get("unwrapped_clear", 0),
                        s.get("clear_ignored", 0)))
        if s.get("wrong_version"):
            # Checked first: it is a strictly better explanation than the
            # passphrase one when both could apply, and the fix is different.
            line += (" -- HEARD %d FRAMES FROM PROTOCOL v%s WHILE ON v%d, "
                     "UPDATE THE OTHER NODES"
                     % (s["wrong_version"], self._peer_version,
                        cot_codec.PROTOCOL_VERSION))
        elif self._since_accept >= MISMATCH_THRESHOLD:
            line += (" -- NOTHING HAS DECRYPTED IN %d FRAMES, CHECK THE PASSPHRASE"
                     % self._since_accept)
        return line


def is_mesh(packet):
    return bool(packet) and packet[0] == cot_codec.CTRL_MESH


def is_clear(packet):
    return bool(packet) and packet[0] == cot_codec.CTRL_CLEAR
