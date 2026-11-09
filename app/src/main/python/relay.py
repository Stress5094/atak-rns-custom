"""
Milestone 6 step 3: decide which received frames are worth relaying.

Steps 1 and 2 built an authenticated mesh and then made it cheap enough to
afford relaying: 13.7% duty at 16 nodes down to 5.8%, measured on the bench.
This module spends that headroom, and the whole design is shaped by how little
of it there is. With one position per node per minute at 39 bytes on air:

    | 16 nodes, all in one radio cell   | duty  |
    |-----------------------------------|-------|
    | no relaying                       |  5.8% |
    | one relay per frame               | 11.0% |
    | every node relays everything      | 83.8% |

Flooding misses the channel by a factor of fourteen. But the middle row is the
one that shapes this module: even ONE relay per frame does not fit -- and in a
squad standing together it is entirely wasted, because every node already heard
the original. **The default has to be zero relays, and each one has to earn its
airtime.**

Four mechanisms, in the order they matter:

1. DUPLICATE SUPPRESSION. A frame seen before is never relayed. This already
   exists as mesh.SeenCache, built for replay defence, keyed on exactly the
   `(origin, epoch, seq)` that flood identity needs.

2. COVERAGE. A node relays only if it can reach somebody the sender could not.
   Every node advertises a small Bloom filter of the neighbours it has heard
   from; a receiver relays only when it knows a neighbour missing from that
   filter. In a single cell nobody can extend coverage, so nothing relays and
   the cost collapses to the first row of the table -- flooding costs nothing
   until the squad actually splits. This is MPR selection, as OLSR does it.

   A Bloom false positive reads as "you already cover that node", i.e. a
   MISSED relay, which the next position report repairs. The error direction
   is the safe one, which is why a filter is worth its false positives here.

3. DELAY WITH PASSIVE CANCELLATION, weighted by signal strength. A relay is
   scheduled rather than sent, and cancelled if the same frame is heard from
   anyone else first. The delay is shortest for the node that heard the
   original WEAKEST, because that node is furthest away and its relay reaches
   the most new ground -- Meshtastic's trick, and it approximates the coverage
   test on its own when the digests are stale.

4. A TOKEN BUCKET. A hard ceiling on relay airtime per node, so a stale
   neighbour table or a forged hop count cannot spend the channel. Positions
   are what it drops first, because a lost position is replaced within the
   minute and a lost marker is lost for good.

Deliberately free of RNS and Android imports, with the clock injected, so the
whole thing is testable on a desktop. Same shape as tx_policy.py, reliable.py
and mesh.py.

IMPORTANT: the on-device interpreter is CPython 3.8 (Chaquopy ships
libpython3.8.so). Keep this module 3.8-compatible.
"""

import collections
import hashlib
import struct
import time

# ---------------------------------------------------------------------------
# Hop caps, by traffic class
#
# `hops` is the number of further transmissions a frame is allowed, and it is
# the one header field outside the authentication tag so that a relay can
# decrement it -- exactly an IP TTL.
# ---------------------------------------------------------------------------

# A beacon is NEVER relayed, whatever its hop field says. The beacon is what
# defines the one-hop neighbour set that mechanism 2 runs on; flooding it makes
# every node look adjacent to every other and destroys the coverage test the
# entire budget depends on. This is a floor enforced on receipt, not just a
# value chosen when sending, because a hop count is attacker-controlled.
HOPS_BEACON = 0

# A dropped position is replaced within the minute, so it gets the smaller cap
# and is the first thing the token bucket sacrifices.
HOPS_POSITION = 2

# Chat, markers, drawings and routes are one-shot: there is no later event to
# supersede a dropped one, which is the same reasoning that motivated
# reliable.py. They get the larger cap and priority in the bucket.
#
# Acknowledgements get this cap too, and must. They were originally given
# HOPS_BEACON on the reasoning that an ack answers a node that just transmitted
# to us and is therefore one hop away -- true before relaying existed, false
# after it. The 2026-08-12 bench measured the cost of the old value: every
# multi-hop chat arrived, was acknowledged, and was still reported UNDELIVERED,
# with the retries spending the airtime this whole module exists to protect.
# Only one-shot traffic is ever acknowledged, so this is exactly enough to
# retrace any path the message could have taken and never more.
HOPS_ONESHOT = 3

# ---------------------------------------------------------------------------

# Relay airtime a node may spend per minute. 1.5 s against the ~2.5 s of spare
# the measured 5.8% leaves under a 10% ceiling -- deliberately short of it, so
# that relaying cannot itself be what pushes the channel over.
DEFAULT_BUDGET_S_PER_MIN = 1.5

# The same allowance as a fraction of the channel, which is what it always
# meant: 1.5 s a minute is 2.5% duty. Worth naming, because seconds only look
# portable between links. They are not. The identical 1.5 s buys
#
#     LoRa at 1074 bps   a 60-byte relay costs 0.588 s   ~2.5 relays a minute
#     FM   at  442 bps   the same relay costs 1.485 s    ~1.0 relays a minute
#
# so a budget that reads generous on the shipped radio is close to one relay
# per minute on FM. Nothing is wrong with that -- it is the channel being
# honest -- but it has to be visible, and a bare "1.5" hides it.
DEFAULT_RELAY_DUTY = DEFAULT_BUDGET_S_PER_MIN / 60.0

# How much relay airtime the bucket HOLDS, as distinct from how fast it refills
# (the 1.5 s/min above, which is the duty and is unchanged). Was equal to the
# refill until 2026-10-14, which made a full bucket about two relays: the hour
# Meshtastic run spent 0.92 s/min against the 1.5 s/min cap and still had 5 of
# 81 relays refused, each straight after 2-3 others, and the same night's
# three-node line refused 4 of ~10 chat/ack relays the same way
# (results/2026-10-14_multihop_meshtastic/). Bursts, not the average, were what
# it cut. 2.0 s is the operator's figure, not a measured optimum: it buys about
# one more relay per burst (~0.7 s each on Meshtastic Long Fast) and lifts the
# largest relayable frame (Gate.relayable_bytes) by 0.5 s of airtime, while the
# long-run share of the channel stays at DEFAULT_RELAY_DUTY.
DEFAULT_RELAY_BURST_S = 2.0

# The window a relay is scheduled inside. Long enough that a nearer node's
# transmission arrives in time to cancel a further one, short enough not to add
# visible latency to a chat message.
DEFAULT_WINDOW_S = 2.0

# Signal range mapped across that window. A frame heard at or below SNR_FLOOR
# is treated as maximally distant and goes first; at or above SNR_CEILING it
# waits the whole window and is almost always cancelled.
SNR_FLOOR = -10.0
SNR_CEILING = 10.0

# Enough jitter to separate two nodes reporting identical SNR, which is common
# -- RNodes report SNR in quarter-dB steps, so ties are frequent.
TIE_JITTER_S = 0.25

DEFAULT_MSG_TTL_S = 60.0
DEFAULT_MAX_MSGS = 128

# Bloom filter over node ids. 8 bytes and two hashes puts the false-positive
# rate near 15% at 16 neighbours, and a false positive costs one missed relay
# that the next position repairs.
DIGEST_BYTES = 8
DIGEST_HASHES = 2

Decision = collections.namedtuple("Decision", "relay delay_s reason")

_NO = lambda reason: Decision(False, 0.0, reason)  # noqa: E731


def digest_build(node_ids):
    """A Bloom filter of the node ids this node has heard from directly."""
    bits = bytearray(DIGEST_BYTES)
    for node_id in node_ids:
        for bit in _digest_bits(node_id):
            bits[bit // 8] |= 1 << (bit % 8)
    return bytes(bits)


def digest_contains(digest, node_id):
    """True if `node_id` may be in the filter. False is certain; True is not --
    see the note on error direction in the module docstring."""
    if not digest or len(digest) != DIGEST_BYTES:
        # No digest, or one from a build that sized it differently. Claiming
        # the sender covers everybody would silence relaying altogether, so an
        # unusable digest means "covers nothing" and the coverage test falls
        # back to the delay and the token bucket.
        return False
    for bit in _digest_bits(node_id):
        if not digest[bit // 8] & (1 << (bit % 8)):
            return False
    return True


def covers(digest, neighbours):
    """The coverage test: does this sender already reach everyone we do?

    True means relaying would reach nobody new, so do not. That is the ordinary
    answer for a squad standing together, and it is what keeps flooding free
    until the group actually splits.

    Note the deliberate asymmetry with digest_contains(): a MISSING digest
    reads as "covers everything" here, and as "contains nothing" there. The
    pure Bloom test must not let absence mean membership. But at this level the
    question is whether to spend airtime, and a node whose beacon has not
    arrived yet would otherwise have every frame it sends relayed by every
    node that hears it -- which is the one thing the budget cannot absorb.
    Bounded by how long its first beacon takes.
    """
    if digest is None or not neighbours:
        return True
    return all(digest_contains(digest, n) for n in neighbours)


def covers_target(digest, target, neighbours):
    """Coverage for a frame that only has to reach ONE node.

    An acknowledgement is the only traffic here with a single destination, and
    it is named in the frame: an ack carries the origin of the message it
    answers, which is precisely the node it has to get back to. Everything else
    is a broadcast where "who needs this?" is genuinely everybody.

    The general coverage test asks "do I reach anyone the sender does not?",
    which for an ack is the wrong question -- it will happily forward a receipt
    down a branch of the mesh nobody involved in the conversation is on. This
    asks the narrower one, and is the cheap realisation of what MeshCore gets
    from source-routed paths. It needs no path on the wire, because the
    destination is already there.

    Returns True to suppress, False to relay, and **None for no opinion** --
    the target is a stranger, so this node cannot tell whether it sits on the
    way there. The caller falls back to the general test, which is what keeps
    a three-hop path working: the middle node has never heard of either end.
    """
    if not target:
        return None
    if digest is not None and digest_contains(digest, target):
        # Whoever sent this can already reach the node it is addressed to.
        return True
    if target in neighbours:
        # ...and we can. This is the hop that actually delivers it.
        return False
    return None


def _digest_bits(node_id):
    """The bit positions one node id sets.

    Derived from a single SHA-256 rather than k independent hashes: the digest
    is rebuilt on every beacon, so this runs often enough that one hash beats
    two, and taking disjoint slices of a 256-bit output is the standard way to
    get independent-enough bits.
    """
    if not isinstance(node_id, bytes):
        node_id = str(node_id).encode("utf-8", "replace")
    out = hashlib.sha256(b"atakrns-neighbour" + node_id).digest()
    total = DIGEST_BYTES * 8
    return [struct.unpack(">H", out[i * 2:i * 2 + 2])[0] % total
            for i in range(DIGEST_HASHES)]


class RelayBudget(object):
    """A token bucket, in seconds of airtime per minute.

    Continuous refill rather than per-interval reset: a bucket that refills on
    the minute lets a node spend its whole allowance in one burst at the top of
    every minute, which is precisely the correlated key-up the beacon jitter
    exists to prevent.

    `capacity` is what the bucket holds (the most it can spend in one burst,
    and so the largest single charge it can ever accept); `per_minute_s` is the
    refill. `burst_s` left as None makes them equal, which is what every
    budget was before DEFAULT_RELAY_BURST_S existed.
    """

    @classmethod
    def from_duty(cls, duty, clock=time.monotonic, burst_s=None):
        """A budget stated as a fraction of the channel rather than seconds.

        The honest way to express it once there is more than one link: duty
        means the same thing on both, where seconds do not.
        """
        if not 0.0 < duty <= 1.0:
            raise ValueError("duty must be in (0, 1], not %r" % (duty,))
        return cls(per_minute_s=60.0 * duty, clock=clock, burst_s=burst_s)

    def __init__(self, per_minute_s=DEFAULT_BUDGET_S_PER_MIN, clock=time.monotonic,
                 burst_s=None):
        if per_minute_s <= 0:
            raise ValueError("a budget of %r buys nothing" % (per_minute_s,))
        if burst_s is not None and burst_s <= 0:
            raise ValueError("a burst of %r holds nothing" % (burst_s,))
        self.per_minute_s = float(per_minute_s)
        self.capacity = float(per_minute_s if burst_s is None else burst_s)
        self._clock = clock
        self._tokens = self.capacity
        self._last = clock()
        self.spent = 0.0
        self.refused = 0

    def _refill(self):
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.capacity,
                           self._tokens + elapsed * self.per_minute_s / 60.0)

    def charge(self, airtime_s):
        """Take `airtime_s` from the bucket. False if it will not fit."""
        self._refill()
        if airtime_s > self._tokens:
            self.refused += 1
            return False
        self._tokens -= airtime_s
        self.spent += airtime_s
        return True

    def refund(self, airtime_s):
        """Give back airtime that was charged for a transmission never made.

        A relay is usually suppressed by the coverage test long before it is
        charged, so this barely matters there. A cross-link forward has no
        coverage test -- a frame from FM has by definition not been heard on
        LoRa -- so a second gateway being beaten to it is the NORMAL case, and
        leaving those charges on the bucket would throttle a link for
        transmissions it never made.

        Capped at capacity, because a refund that overfilled the bucket would
        hand back airtime the minute never contained.
        """
        self._refill()
        given = min(float(airtime_s), self.capacity - self._tokens)
        if given <= 0:
            return 0.0
        self._tokens += given
        self.spent -= given
        return given

    def tokens(self):
        self._refill()
        return self._tokens

    def duty(self):
        """The share of the channel this budget allows."""
        return self.per_minute_s / 60.0

    def relays_per_minute(self, frame_s):
        """What the budget actually buys, given what one relay costs here.

        The number to look at when setting a budget. Seconds per minute is the
        unit it is stored in; relays per minute is the thing anyone cares
        about, and the two diverge by 2.5x between the links in this bridge.
        """
        return 0.0 if frame_s <= 0 else self.per_minute_s / float(frame_s)


class NoRelay(object):
    """A link that forwards nothing, shaped like a RelayPolicy so callers do
    not branch.

    FM is the case. At a 1.5 s/min budget only frames of 60 bytes or less could
    ever have been forwarded there, which is barely a relay at all -- and a
    frame that big is a position report, so the feature would have covered
    exactly one kind of traffic while costing the airtime accounting on
    everything.

    Not the same as FM being cut off. A node carrying both radios still bridges
    between them; what is switched off here is FM retransmitting onto FM.

    Absence would have expressed this too -- simply never constructing a policy
    -- but then "this link does not relay" and "somebody forgot to wire up the
    relay" look identical in a log, and one of those is a bug.
    """

    def __init__(self, name):
        self.name = name
        self.budget = None
        self.stats = collections.Counter()

    def decide(self, hops, kind, packet_len, snr, covered, msg_id=None,
               fragments=1):
        self.stats["refused/link does not relay"] += 1
        return Decision(False, 0.0, "%s does not relay" % self.name)

    def summary_line(self):
        return ("relay[%s]: off -- originates only, no forwarding (%d frames "
                "declined)"
                % (self.name, self.stats.get("refused/link does not relay", 0)))


class UnmeteredBudget(object):
    """A budget that never refuses -- shaped like RelayBudget so callers
    (RelayPolicy, Gateway, LinkBudgets) need no branch, the same reason
    NoRelay above is shaped like a RelayPolicy.

    For a link whose real constraint is not airtime scarcity at all --
    WiFi, unlike a half-duplex, regulator-constrained radio, is not made
    safer by metering it as a fraction of a channel it barely occupies.
    Before this class existed, WiFi's budget was only EFFECTIVELY
    unlimited, as an accidental side effect of its airtime model assuming
    a ~10,000x faster bitrate than LoRa's -- the same DEFAULT_RELAY_DUTY
    that buys ~2.5 relays/minute on LoRa buys thousands on WiFi, so the
    metering never actually bound in practice. That was never a decision
    anyone made, just a number that happened not to matter -- change the
    bitrate assumption later (to model a congested or degraded WiFi link
    more honestly, say) and metering would silently reappear with it.
    This makes "WiFi's relay/gateway traffic is unmetered" an explicit
    choice instead, the same way eligibility.wifi_gate()'s
    WIFI_MAX_FRAME_S = None already makes "WiFi's frame size is uncapped"
    an explicit one.

    Losing something real in exchange: even a budget too generous to ever
    bind in today's bench traffic is still SOME ceiling on how much this
    node will relay/forward, which is what stands between "a large or
    busy mesh offers more relay-eligible traffic than usual" and "this
    node spends unbounded CPU/queue space trying to forward all of it" --
    a cost paid on THIS phone regardless of how fast the link itself is.
    Coverage suppression (relay.py) and the destination-gate check
    (gateway.py) both cut real traffic before it reaches this budget, but
    neither is a hard cap the way a token bucket is.
    """

    def __init__(self):
        self.capacity = float("inf")
        self.spent = 0.0
        self.refused = 0

    def charge(self, airtime_s):
        """Always succeeds -- there is nothing to run out of."""
        self.spent += airtime_s
        return True

    def refund(self, airtime_s):
        """Never actually withheld anything, so there is nothing to give
        back beyond undoing the bookkeeping charge() did."""
        self.spent = max(0.0, self.spent - airtime_s)
        return airtime_s

    def tokens(self):
        return float("inf")

    def duty(self):
        """No self-imposed ceiling: as much of the channel as the traffic
        offers, same meaning `1.0` has for RelayBudget.duty()."""
        return 1.0

    def relays_per_minute(self, frame_s):
        return float("inf")


class LinkBudgets(object):
    """One relay budget per link, because two radios are two channels.

    Airtime spent on LoRa does not occupy FM's channel, and vice versa. A
    single bucket shared between them would have to be set either for the worse
    link -- throttling the better one for no reason -- or for the better one,
    which overruns the worse. Neither is a compromise worth making when the
    channels are genuinely independent.

    Stated as duty rather than seconds, so the same number means the same thing
    on both links even though it buys 2.5x fewer relays on one of them.

    `unmetered` names the links that get an UnmeteredBudget instead of a
    duty-metered one -- a link whose real constraint is not airtime
    scarcity (see UnmeteredBudget's own docstring for why WiFi is the
    case this exists for), rather than one this class would otherwise
    have to guess about from its bitrate alone.
    """

    def __init__(self, names, duty=DEFAULT_RELAY_DUTY, clock=time.monotonic,
                 unmetered=(), burst_s=None):
        names = list(names)
        if not names:
            raise ValueError("a budget per link needs at least one link")
        if len(set(names)) != len(names):
            raise ValueError("duplicate link names: %r" % (names,))
        self.duty = duty
        unmetered = frozenset(unmetered)
        self._budgets = collections.OrderedDict(
            (name, UnmeteredBudget() if name in unmetered
             else RelayBudget.from_duty(duty, clock=clock, burst_s=burst_s))
            for name in names)

    def __getitem__(self, name):
        try:
            return self._budgets[name]
        except KeyError:
            # Loudly. A mistyped link name that quietly refused every relay
            # would look exactly like a congested channel.
            raise KeyError("no budget for link %r; have %s"
                           % (name, ", ".join(self._budgets)))

    def __contains__(self, name):
        return name in self._budgets

    def __iter__(self):
        return iter(self._budgets)

    def __len__(self):
        return len(self._budgets)

    def names(self):
        return list(self._budgets)

    def charge(self, name, airtime_s):
        """Spend from one link's bucket. False if it will not fit there."""
        return self[name].charge(airtime_s)

    def summary_line(self):
        return "relay budget: " + "  ".join(
            "%s=%.1f/%.1fs" % (name, b.tokens(), b.capacity)
            for name, b in self._budgets.items())


class RelayPolicy(object):
    """Decides whether a received frame is relayed, and when.

    `airtime_fn` maps a packet length to seconds on the air, injected for the
    same reason the clock is: the real one lives in rns_bridge and depends on
    the radio's configured bitrate.
    """

    @classmethod
    def for_link(cls, link, budget=None, **kw):
        """A policy for one `airtime.Link`, costed and named by it.

        One policy per link rather than one policy answering for several: the
        budgets are separate, the per-message memory should be separate (a
        frame refused on the slow link may still be worth relaying on the fast
        one), and the summary line then says which channel it is describing.
        """
        return cls(airtime_fn=link.frame_s, budget=budget, name=link.name, **kw)

    def __init__(self, airtime_fn, budget=None, window_s=DEFAULT_WINDOW_S,
                 clock=time.monotonic, jitter=None,
                 msg_ttl_s=DEFAULT_MSG_TTL_S, max_msgs=DEFAULT_MAX_MSGS,
                 name=None):
        self.name = name
        self.airtime_fn = airtime_fn
        self.budget = (budget if budget is not None
                       else RelayBudget(clock=clock, burst_s=DEFAULT_RELAY_BURST_S))
        self.window_s = window_s
        self._clock = clock
        self._jitter = jitter if jitter is not None else _default_jitter
        self._msg_ttl_s = msg_ttl_s
        self._max_msgs = max_msgs
        # msg_id -> (decided_at, relay?) so every fragment of one message gets
        # the same answer. Half a relayed drawing is pure waste: the receiver
        # cannot reassemble it and the airtime is spent regardless.
        self._msgs = collections.OrderedDict()
        self.stats = collections.Counter()

    # -- the decision -------------------------------------------------------

    def decide(self, hops, kind, packet_len, snr, covered, msg_id=None,
               fragments=1):
        """Relay this frame?

        `covered` is the coverage test's answer: True when the sender's own
        neighbour digest already accounts for every neighbour we know, i.e. we
        would reach nobody new. `kind` is the inner frame's type nibble.
        """
        if kind == _T_BEACON:
            # Enforced here and not only at the sender: a hop count is not
            # covered by the authentication tag, so a beacon arriving with hops
            # set is either a bug or somebody probing for exactly this.
            return self._no("beacon")
        if hops <= 0:
            return self._no("hop limit")
        if covered:
            # The common case, and the one the whole budget rests on: in a
            # single cell no node can extend coverage, so nothing relays.
            return self._no("no new coverage")

        decided = self._recall(msg_id)
        if decided is not None:
            if not decided:
                return self._no("message already refused")
            # Charged as a whole when its first fragment was decided.
            return self._yes(snr, "relaying fragment")

        cost = self.airtime_fn(packet_len) * max(1, fragments)
        if not self.budget.charge(cost):
            self._remember(msg_id, False)
            return self._no("over budget")
        self._remember(msg_id, True)
        return self._yes(snr, "extends coverage")

    def _yes(self, snr, reason):
        self.stats["relayed"] += 1
        return Decision(True, self._delay_for(snr), reason)

    def _no(self, reason):
        self.stats["refused/" + reason] += 1
        return Decision(False, 0.0, reason)

    def _delay_for(self, snr):
        """Weakest signal goes first, because that node is furthest from the
        sender and its relay reaches the most ground the original did not."""
        if snr is None:
            fraction = 0.5
        else:
            span = SNR_CEILING - SNR_FLOOR
            fraction = min(1.0, max(0.0, (float(snr) - SNR_FLOOR) / span))
        return max(0.0, fraction * self.window_s + self._jitter())

    # -- per-message memory -------------------------------------------------

    def _recall(self, msg_id):
        if msg_id is None:
            return None
        entry = self._msgs.get(msg_id)
        if entry is None:
            return None
        decided_at, verdict = entry
        if self._clock() - decided_at > self._msg_ttl_s:
            self._msgs.pop(msg_id, None)
            return None
        return verdict

    def _remember(self, msg_id, verdict):
        if msg_id is None:
            return
        self._msgs[msg_id] = (self._clock(), verdict)
        while len(self._msgs) > self._max_msgs:
            self._msgs.popitem(last=False)

    def summary_line(self):
        refused = sum(v for k, v in self.stats.items() if k.startswith("refused/"))
        parts = " ".join(
            "%s=%d" % (k.split("/", 1)[1].replace(" ", "_"), v)
            for k, v in sorted(self.stats.items()) if k.startswith("refused/"))
        return ("relay%s: sent=%d refused=%d [%s] budget=%.1f/%.1fs spent=%.1fs"
                % ("" if self.name is None else "[%s]" % self.name,
                   self.stats.get("relayed", 0), refused, parts or "none",
                   self.budget.tokens(), self.budget.capacity,
                   self.budget.spent))


def _default_jitter():
    import random
    return random.random() * TIE_JITTER_S


# Imported lazily rather than at module scope so relay.py keeps no dependency
# on the codec's frame vocabulary beyond the one value it needs.
_T_BEACON = 0x3


def hop_cap(is_position, is_beacon):
    """What a SENDER should stamp on a frame of this class."""
    if is_beacon:
        return HOPS_BEACON
    return HOPS_POSITION if is_position else HOPS_ONESHOT
