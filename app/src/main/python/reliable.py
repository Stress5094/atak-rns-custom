"""
Milestone 4: make one-shot CoT traffic survive packet loss.

Position reports are safe on a lossy link because they self-heal -- ATAK emits
another one seconds later, so a dropped packet costs nothing. Chat, drawings,
routes and waypoints have no such property: they are user-initiated and
one-shot, no later event supersedes them, and today a single lost packet loses
the message permanently while telling neither user. That silence is the actual
defect; a message that fails to arrive is bad, but a message that fails to
arrive and *looks sent* is worse.

RNS's own acknowledgement machinery is unavailable here for the same structural
reason Resource and Link are: Packet.prove() needs destination.identity.prv,
and a PLAIN destination cannot hold an identity. So this is app-layer ARQ on
the same broadcast destination everything else uses.

WHY BROADCAST-PLUS-ACK RATHER THAN A LINK PER PEER: a Link is point-to-point,
so the same chat to N peers costs N times the payload airtime, plus handshakes,
identity announces and per-link keepalives, on a shared 3125 bps channel.
Broadcasting the payload once and collecting small acknowledgements costs it
once no matter who is listening -- ~0.78 s for a 284-byte chat plus ~0.09 s per
ack, against ~1.8 s of payload alone for two peers over Links. The gap widens
with every node added.

Deliberately free of RNS/Android imports, with the clock and the airtime
estimator injected, so the whole thing is testable on a desktop without
sleeping. Same shape as tx_policy.py.
"""

import collections
import os
import random
import struct
import time

import cot_codec
import relay

# How many of the expected peers must acknowledge before a message counts as
# delivered. ONE, deliberately.
#
# Requiring all of them is the stricter guarantee and it does not survive
# contact with a mesh. A broadcast marker goes out in a single transmission
# that everyone hears; the confirmations come back one at a time, so at 16
# nodes one marker costs 15 acks -- modelled at ~30% of the channel on the
# slow presets, from a couple of messages a minute.
#
# It is also the wrong question. "Did anybody get it?" is what the sender
# actually needs to know, and it is the question whose answer is actionable:
# nobody acking means the radio link is down, while 14 of 15 acking means one
# distant node is having a bad minute and no amount of retransmitting from
# here will change that.
#
# The cost is real and worth stating: a message can now be reported DELIVERED
# while a specific peer never received it. Milestone 4's stronger promise is
# still available by raising this.
#
# MEASURED 2026-10-14 in a split mesh (results/2026-10-14_meshtastic_phase2,
# sections 13-15): the only relay refused a room chat over budget, acked it
# itself as a recipient, and the node beyond it never got it -- 1 of 30 in an
# hour. A rule requiring one ack from beyond the relay removed that case but
# cost ~9 s of confirmation time at normal load and 47 resends per 20 messages
# under overload, and was REVERTED by the operator. What changed instead is the
# wording: the bridge's DELIVERED line names who acknowledged, whether it heard
# us directly or through a relay, and who is NOT confirmed
# (rns_bridge._delivered_line).
DEFAULT_REQUIRED_ACKS = 1

# Four transmissions total (the original plus three retries), which with the
# interval below means giving up ~16 s after the send to a one-hop peer. Long
# enough to ride out a burst of interference, short enough that the user learns
# about the failure while they still remember sending the message.
DEFAULT_MAX_ATTEMPTS = 4

# Floor on the retry interval. The round trip for a chat is about 0.9 s (0.78 s
# of payload plus a 0.09 s ack), so this is roughly 2x that. The floor exists
# because the channel is shared -- an ack can legitimately sit behind a
# position report or a beacon, and retransmitting into that is what turns
# congestion into more congestion.
DEFAULT_MIN_TIMEOUT_S = 2.0
DEFAULT_BACKOFF = 1.5

# What one RELAYED hop adds to a round trip.
#
# Every figure above was measured against peers one hop away, because when this
# module was written that was the only kind there were. A relay does not
# forward on arrival: it schedules, waits out its cancellation window, and
# sends only if nobody else did -- so each relayed hop adds that window, not
# just its airtime. Two of those sit in a two-hop round trip, one carrying the
# message out and one carrying the ack back.
#
# Taken from relay.py rather than restated, because the two numbers colliding
# is exactly the bug: the 2026-08-12 bench had a 2.0 s retry floor against a
# 2.0 s relay window on each leg, so the first retry fired before the message
# had even reached the far node. Every multi-hop chat cost three transmissions
# to deliver once, and the wasted relays put the relay budget under strain for
# the first time on any bench.
DEFAULT_RELAY_DELAY_S = relay.DEFAULT_WINDOW_S + relay.TIE_JITTER_S

# Spread retries so two nodes that lost each other's messages at the same
# instant do not retransmit in lockstep forever.
DEFAULT_JITTER_S = 1.0

# Bounds. Far above any realistic burst of one-shot traffic; they exist so a
# misbehaving peer or a stuck radio cannot grow memory without limit.
DEFAULT_MAX_INFLIGHT = 32

# Retries stop while more than this many messages are awaiting acknowledgement.
#
# ARQ is what turns a busy channel into a dead one: every retry needs airtime
# on a channel that has already run out, so more is lost, so more is retried.
# The 2026-08-16 stress bench measured the whole curve on four real nodes --
# every HEALTHY step, 8 through 80 messages/min at 100% delivery, peaked at
# ONE message in flight, while every collapsed step sat at 21-32. There is no
# overlap between the two regimes, so the threshold only has to land in the
# gap. Four is the earliest value that cannot fire on traffic the mesh was
# carrying comfortably.
#
# In flight rather than channel load, which was measured and rejected: at
# 80/min -- perfect delivery -- 38% of channel-load samples already exceeded
# 25%, overlapping the collapsed steps, because the window is short and a node
# is deaf to its own transmissions. Depth is also the only one of the two that
# needs no retuning per preset, and Long Fast is 20x slower than the preset
# this was measured on.
#
# SWEPT 2026-08-16 and 4 survived every comparison. 2/4/6 are level on a clean
# link and 10 collapses (it reaches DEFAULT_MAX_INFLIGHT, after which
# displacement decides everything). Under steady 25% loss 2 falls to 90%
# against 4's 95%, because it suppresses retries that were repairing real loss
# -- the clean-link case cannot see that cost, so do not lower this on
# clean-link evidence. Under bursty loss with the loss matched, 4 and 8 both
# deliver 100% and 4 spends ~10% fewer retries.
#
# ALL OF THAT USED INJECTED LOSS ON RADIOS A METRE APART. TX power cannot make
# a bad link at that range -- ~26 dB of path loss against ~85 dB of margin --
# so every figure above comes from frames discarded in software on an otherwise
# perfect channel. Real distance brings things the fixture does not model:
# asymmetric links where A hears B but not the reverse, loss correlated with
# the SNR the coverage test also depends on, and fades longer than the ~4 s
# tested. THE RANGE TEST IS THE FIRST TIME THIS VALUE MEETS ANY OF THAT.
#
# What would say 4 is wrong, in the log, during a range walk:
#   * `suppressed=N` climbing while the channel is measurably idle -- backing
#     off from congestion that is not there. Too low; try 6.
#   * delivery poor with `retries=N` near zero and few `channel too busy`
#     give-ups -- repair being withheld when it would have worked. Too low.
#   * `displaced by newer traffic` appearing at all at squad message rates --
#     the queue is deciding instead of this, and DEFAULT_MAX_INFLIGHT is the
#     constant to look at, not this one.
# Anything else is not evidence about this value.
DEFAULT_CONGESTION_INFLIGHT = 4

# Multiple of its own retry schedule after which a message is given up on
# regardless of attempts.
#
# Suspending retries means attempts stop being consumed, so max_attempts can no
# longer end a message and it would wait forever -- until the in-flight cap
# shed it silently, trading a retry storm for exactly the quiet loss the same
# bench found. Expressed against the schedule the message WOULD have run, so
# the deadline can never fire before its retries would have finished anyway.
DEFAULT_DEADLINE_FACTOR = 2.0
DEFAULT_UNDELIVERED_KEPT = 8
DEFAULT_SEEN_TTL_S = 300.0
DEFAULT_MAX_SEEN = 128

# Matches rns_bridge._airtime_s: +19 bytes for the RNS HEADER_1, at what RNS
# computes for SF8 / 125 kHz / CR5. Only a fallback -- the bridge injects a
# version that reads the live interface bitrate, so pacing self-tunes if the
# radio parameters change.
_FALLBACK_BITRATE = 3125


def default_airtime_s(payload_len):
    return ((payload_len + 19) * 8.0) / _FALLBACK_BITRATE


def _latency(link_latency_s):
    """`link_latency_s` may be a number or a zero-argument callable (the bridge
    passes one, so a failover between radios is followed live). Anything
    unusable counts as 0 -- the RNode figure -- rather than stopping a retry."""
    try:
        value = link_latency_s() if callable(link_latency_s) else link_latency_s
        return max(0.0, float(value or 0.0))
    except Exception:
        return 0.0


def _base_timeout_s(frame_len, airtime_s, min_timeout_s, link_latency_s):
    """The first retry interval for a one-hop round trip.

    `2 x airtime + 1 s` is the RNode's round trip: the radio starts sending
    almost as soon as it is handed a frame, so the frame and its ack are nearly
    all there is. A Meshtastic board is NOT like that: it queues, waits out its
    own listen-before-talk back-off, and only then transmits; the receiving
    board decrypts before passing anything up. That fixed delay is per LEG and
    the round trip has two (message out, ack back), so it is charged twice.
    """
    return max(min_timeout_s,
               airtime_s(frame_len) * 2.0 + 1.0 + 2.0 * _latency(link_latency_s))


def max_retry_span_s(hops=1, frame_len=0, airtime_s=default_airtime_s,
                     max_attempts=DEFAULT_MAX_ATTEMPTS,
                     min_timeout_s=DEFAULT_MIN_TIMEOUT_S,
                     backoff=DEFAULT_BACKOFF, jitter_s=DEFAULT_JITTER_S,
                     relay_delay_s=DEFAULT_RELAY_DELAY_S,
                     link_latency_s=0.0):
    """How long after the first transmission the LAST retry can still arrive.

    Exists so that anything holding state for a message in flight can be sized
    to outlive it rather than guess. The receiver's reassembly buffer is the
    case that matters: fragments accumulated across attempts are worthless if
    they are discarded before the attempt that would have completed them, and
    that is the exact airtime the per-fragment bitmap exists to save.

    The relationship was silently broken on 2026-08-12 by the multi-hop
    timeout fix earlier the same day. Before it every base was 2.0 s and the
    schedule finished at 9.5 s, well inside the reassembler's fixed 30 s; the
    relay allowance took a two-hop message to 30.9 s and a three-hop one to
    52 s, so the receiver began throwing away partial messages moments before
    the sender's last attempt to complete them. Nothing connected the two
    numbers, which is what let one move past the other unnoticed.
    """
    base = _base_timeout_s(frame_len, airtime_s, min_timeout_s, link_latency_s)
    base += 2.0 * max(0, hops - 1) * relay_delay_s
    span = 0.0
    for attempt in range(1, max_attempts):
        span += base * (backoff ** (attempt - 1)) + jitter_s
    return span


def owes_ack(addressee, node_id):
    """Should this node acknowledge a reliable frame it just received?

    The rule is one line and the default is deliberately "yes": a frame that
    names nobody is owed an ack by everyone who hears it, which is what a room
    message or a marker wants -- receivers then talk each other out of it by
    passive cancellation, so the mesh pays for one ack rather than N.

    A frame that DOES name somebody is owed an ack by that node alone. Those
    acks cannot be cancelled -- letting a bystander silence the real addressee
    would report the message undelivered while it sat on the recipient's
    screen -- so if bystanders send them at all, the mesh pays for all N. That
    is roughly twice the airtime of the message itself at 16 nodes, spent
    saying something the sender does not count.

    Lives here rather than in rns_bridge because rns_bridge cannot be tested on
    a desktop, and a rule whose failure mode is "a chat is reported undelivered
    while sitting on the recipient's screen" should not be the untested kind.
    """
    return addressee is None or addressee == node_id


# Why a message was retired, in the sender's terms.
NO_ACK = "no acknowledgement"
DISPLACED = "displaced by newer traffic"
CONGESTED = "channel too busy to retry"

# `reason` distinguishes a message that used up its retries from one displaced
# by the in-flight cap, which are different failures and want different words:
# the first was answered by nobody, the second was never given the chance.
Undelivered = collections.namedtuple(
    "Undelivered", "msg_id label missing attempts age_s reason frame",
    defaults=(NO_ACK, None))


class _Pending(object):
    __slots__ = ("msg_id", "origin", "frame", "label", "expected", "acked",
                 "attempts", "next_try", "created", "held", "frag_count",
                 "required", "deadline")

    def __init__(self, msg_id, origin, frame, label, expected, now,
                 required_acks=1):
        self.msg_id = msg_id
        self.origin = origin
        self.frame = frame
        self.label = label
        self.expected = expected
        # How many of `expected` have to answer before this counts as
        # delivered. Never more than there are peers to ask, so a lone node
        # is not permanently undeliverable.
        self.required = max(1, min(required_acks, len(expected))) if expected else 0
        self.acked = set()
        self.attempts = 1  # register() is itself the first transmission
        self.next_try = now
        self.created = now
        # Set by register(), which knows the frame length and hop count the
        # schedule depends on. Never None in practice; the far-future default
        # keeps a hand-built _Pending in a test from expiring instantly.
        self.deadline = float("inf")
        # peer -> the fragment indices it has told us it holds. Absent means
        # "no idea", which is treated as holding nothing.
        self.held = {}
        self.frag_count = None

    def missing(self):
        return sorted(self.expected - self.acked)

    def confirmed(self):
        """Expected peers that have acknowledged. Only expected ones count: on
        a broadcast destination we hear acks addressed to other senders too,
        and a stranger's ack says nothing about whether OUR recipients got it."""
        return self.acked & self.expected

    def needed(self):
        """Fragment indices still worth transmitting, or None for "all of it".

        The union over every peer that has not fully acknowledged, because one
        broadcast serves all of them and a fragment only one peer is missing
        still has to go out. Intersecting instead would quietly strand whoever
        was missing the most.

        None whenever the answer is not known well enough to narrow safely: no
        fragment count yet, or a peer that has said nothing about what it holds.
        Sending everything is the honest fallback and is what always happened
        before the bitmap existed.
        """
        if self.frag_count is None or self.frag_count <= 1:
            return None
        want = set()
        for peer in self.expected - self.acked:
            got = self.held.get(peer)
            if got is None:
                return None
            want |= set(range(self.frag_count)) - got
        return want or None


class ReliableTx(object):
    """Tracks messages awaiting acknowledgement and decides when to retry.

    Not thread-safe by contract; rns_bridge holds a lock around it because
    register() runs on the CoT listener thread, on_ack() on the RNode read
    thread, and tick() on the retry thread.
    """

    def __init__(self, max_attempts=DEFAULT_MAX_ATTEMPTS,
                 min_timeout_s=DEFAULT_MIN_TIMEOUT_S,
                 backoff=DEFAULT_BACKOFF,
                 jitter_s=DEFAULT_JITTER_S,
                 max_inflight=DEFAULT_MAX_INFLIGHT,
                 undelivered_kept=DEFAULT_UNDELIVERED_KEPT,
                 airtime_s=default_airtime_s,
                 clock=time.monotonic,
                 rng=random.random,
                 hops_away=None,
                 relay_delay_s=DEFAULT_RELAY_DELAY_S,
                 required_acks=DEFAULT_REQUIRED_ACKS,
                 congestion_inflight=DEFAULT_CONGESTION_INFLIGHT,
                 deadline_factor=DEFAULT_DEADLINE_FACTOR,
                 link_latency_s=0.0):
        # Per-leg fixed delay of the radio, on top of airtime: 0 for an RNode,
        # measured for a Meshtastic board (see _base_timeout_s). A number or a
        # callable, so the bridge can follow whichever radio is live.
        self.link_latency_s = link_latency_s
        self.congestion_inflight = congestion_inflight
        self.deadline_factor = deadline_factor
        self.required_acks = required_acks
        self.max_attempts = max_attempts
        self.min_timeout_s = min_timeout_s
        self.backoff = backoff
        self.jitter_s = jitter_s
        self.max_inflight = max_inflight
        self.airtime_s = airtime_s
        # How far away a peer is, in transmissions. Injected for the same
        # reason the clock and the airtime estimator are: the answer lives in
        # the bridge's peer table, and this module stays free of it. Defaults
        # to "everyone is adjacent", which is what it assumed before there was
        # any such thing as a relay.
        self._hops_away = hops_away or (lambda node_id: 1)
        self.relay_delay_s = relay_delay_s
        self._clock = clock
        self._rng = rng
        self._pending = collections.OrderedDict()  # msg_id -> _Pending
        self._undelivered = collections.deque(maxlen=undelivered_kept)
        # Retired by register() rather than tick(), and handed out on the next
        # tick. register() cannot report them itself: its caller is holding a
        # frame to transmit and has nowhere to put a failure, so before this
        # existed a displaced message reached the snapshot and nothing else --
        # no log line, and only as long as it stayed inside the last
        # `undelivered_kept`. Under congestion displacement is the COMMON way a
        # message dies, which made the quietest failure the most frequent one.
        self._shed = []
        self._stats = collections.Counter()

    # -- outbound -----------------------------------------------------------

    def register(self, frame, origin, peers, label="", addressee=None):
        """Wrap a frame for acknowledged delivery.

        Returns (msg_id, wrapped_frame). The caller transmits the wrapped
        frame; this only records that it is owed an acknowledgement.

        `peers` is who is expected to acknowledge -- the peers heard from
        recently, snapshotted now. Nodes that appear mid-flight are not added:
        a message is delivered to the group that existed when it was sent, and
        adding late arrivals would mean a message can never complete on a
        network anyone is joining.

        WITH NO PEERS, NOTHING IS TRACKED. Retrying into a room known to be
        empty is pure airtime waste, and running solo is the common case on a
        bench. The message still goes out once -- somebody unheard may be
        listening -- it just isn't retried or reported as undelivered, because
        "nobody acknowledged" is not news when nobody is there.

        `addressee` is the one node that owes the answer, when the caller knows
        it -- a direct chat, whose recipient is named in its uid. It goes on the
        wire so that every OTHER receiver stays silent: without it they all ack
        a message none of them was asked about, which at 16 nodes costs about
        twice the airtime of the message itself.
        """
        expected = frozenset(p for p in (peers or ()) if p and p != origin)

        # An addressee we are not actually expecting an ack from is a caller
        # bug, and a quiet one: the named node would be the only one to answer
        # and its answer would not count, so the message would be reported
        # undelivered every time. Drop the claim rather than the delivery.
        if addressee is not None and addressee not in expected:
            addressee = None

        # Tell receivers whether one ack from anybody will do, so the ones who
        # hear somebody else answer first can stay quiet. True only when there
        # is genuinely a choice of ackers AND we will settle for one of them:
        # a direct chat narrows `expected` to the single node that owes the
        # answer, and letting a bystander's ack silence that node would report
        # the message undelivered while it sat on the recipient's screen.
        any_ack = len(expected) > 1 and self.required_acks <= 1

        msg_id, wrapped = cot_codec.wrap_reliable(frame, origin,
                                                  self._pick_msg_id(),
                                                  any_ack=any_ack,
                                                  addressee=addressee)
        if not expected:
            self._stats["sent_unwitnessed"] += 1
            return msg_id, wrapped

        now = self._clock()
        p = _Pending(msg_id, origin, wrapped, label, expected, now,
                     required_acks=self.required_acks)
        # Captured rather than recomputed: _timeout() draws fresh jitter, so
        # calling it twice would deadline this message against a schedule it is
        # not actually on. Built from _timeout rather than max_retry_span_s so
        # it inherits the relay allowance -- and that function's tolerance of a
        # broken distance lookup, which must never be able to stop a retry.
        # Every interval is counted, not just the retries: at max_attempts=1
        # the retry span is zero, and a deadline of "now" would retire a
        # message before its one transmission could be answered.
        first = self._timeout(p)
        p.next_try = now + first
        schedule = sum(first * (self.backoff ** k)
                       for k in range(max(1, self.max_attempts)))
        p.deadline = now + self.deadline_factor * schedule
        self._pending[msg_id] = p
        self._stats["sent"] += 1
        while len(self._pending) > self.max_inflight:
            _, oldest = self._pending.popitem(last=False)
            self._shed.append(self._retire(oldest, DISPLACED))
        return msg_id, wrapped

    def forget(self, msg_id):
        """Drop a registration without reporting it undelivered.

        For the case where registering succeeded but transmitting never
        happened at all -- there is nothing to retry and nothing to report,
        and leaving it in flight would announce a delivery failure for a
        message that was never sent.
        """
        try:
            if self._pending.pop(msg_id, None) is not None:
                self._stats["forgotten"] += 1
        except Exception:
            self._stats["error"] += 1

    def _pick_msg_id(self):
        # wrap_reliable() would pick one at random; do it here instead so an
        # id already in flight is never reused, which would misroute acks.
        # Deliberately not drawn from self._rng -- that one is injected by
        # tests to make retry timing deterministic, and a fixed rng would
        # hand out the same msg_id every time.
        for _ in range(64):
            # Masked to 15 bits: the top one is the any-ack flag, and an id
            # that set it would be reported back by wrap_reliable with the bit
            # stripped, so this table and the wire would disagree.
            candidate = (struct.unpack(">H", os.urandom(2))[0]
                         & cot_codec.MSG_ID_MASK)
            if candidate not in self._pending:
                return candidate
        return None  # let wrap_reliable choose; astronomically rare

    def _relay_allowance(self, pending):
        """How long the relays in the path will sit on this message and its ack.

        Sized on the FARTHEST peer still owed an acknowledgement, not the
        nearest: the message is not delivered until the last of them answers,
        so retrying on the nearest one's schedule retransmits to everybody in
        order to chase the one node that was always going to be slowest.

        Counted per relayed leg -- a peer one hop away has none, and adds
        nothing, which is what keeps the single-cell case exactly as it was.
        Doubled because the round trip crosses those relays twice, once
        carrying the message out and once carrying the ack back.
        """
        try:
            farthest = max(self._hops_away(p) for p in pending.expected)
        except Exception:
            farthest = 1                 # never let bookkeeping stop a retry
        return 2.0 * max(0, int(farthest) - 1) * self.relay_delay_s

    def _timeout(self, pending):
        base = _base_timeout_s(len(pending.frame), self.airtime_s,
                               self.min_timeout_s, self.link_latency_s)
        base += self._relay_allowance(pending)
        return (base * (self.backoff ** (pending.attempts - 1))
                + self._rng() * self.jitter_s)

    def on_ack(self, ack):
        """Record an acknowledgement. Returns the completed _Pending if this
        ack was the last one outstanding, else None. Never raises."""
        try:
            pending = self._pending.get(ack.msg_id)
            if pending is None or pending.origin != ack.origin:
                # Not ours: either an ack for a message we already completed
                # (a peer retrying its ack) or one addressed to another sender,
                # which on a broadcast destination we hear anyway.
                self._stats["ack_unknown"] += 1
                return None
            pending.acked.add(ack.acker)
            self._stats["ack"] += 1
            if len(pending.confirmed()) >= pending.required:
                del self._pending[ack.msg_id]
                self._stats["delivered"] += 1
                return pending
            return None
        except Exception:
            self._stats["error"] += 1
            return None

    def on_partial(self, partial):
        """Record which fragments a peer says it holds. Never raises.

        Does NOT count as an acknowledgement and never completes a message --
        the peer is telling us what is missing, which is the opposite claim.
        Keeping them as separate frame types is what makes that hard to get
        wrong; keeping them as separate methods is the other half.

        Nor does it reset the retry timer. A report of a stalled reassembly is
        news about WHAT to send, not evidence that anything is still moving,
        and treating it as liveness would let a peer stuck on one lost fragment
        hold a message in flight indefinitely.
        """
        try:
            pending = self._pending.get(partial.msg_id)
            if pending is None or pending.origin != partial.origin:
                self._stats["partial_unknown"] += 1
                return
            if pending.frag_count is None:
                pending.frag_count = partial.count
            elif pending.frag_count != partial.count:
                # The report is indexed against a different split than the one
                # we sent. Trusting it would resend the wrong pieces, so drop
                # it and let the retry send everything.
                self._stats["partial_mismatch"] += 1
                return
            pending.held[partial.acker] = set(partial.have)
            self._stats["partial"] += 1
        except Exception:
            self._stats["error"] += 1

    def note_fragment_count(self, msg_id, count):
        """How many fragments this message actually went out as.

        Told rather than derived: this module deliberately knows nothing about
        the MTU or the codec, which is what keeps it testable without either.
        """
        try:
            pending = self._pending.get(msg_id)
            if pending is not None and count:
                pending.frag_count = count
        except Exception:
            self._stats["error"] += 1

    def tick(self):
        """Advance timers.

        Returns (retries, gave_up): frames to put back on the radio, and
        Undelivered records to report. Never raises.
        """
        retries = []
        # Drained before anything that can fail, so a message displaced by the
        # in-flight cap is reported even on a tick that errors out.
        gave_up = self._shed
        self._shed = []
        try:
            now = self._clock()
            # Decided once for the whole tick, so a pass cannot start retrying
            # partway through and undo its own relief.
            congested = len(self._pending) > self.congestion_inflight
            for msg_id in list(self._pending.keys()):
                pending = self._pending.get(msg_id)
                if pending is None:
                    continue
                # Only while congested, and before next_try so a message whose
                # retries are suspended still ends rather than waiting on a
                # timer that stopped moving. Gated because attempts advance per
                # TICK while this advances per wall clock: on an uncongested
                # channel whose retry loop has been starved -- a backgrounded
                # app, a blocked thread -- an ungated deadline would give up on
                # messages that had not yet had their four transmissions.
                if congested and now >= pending.deadline:
                    del self._pending[msg_id]
                    gave_up.append(self._retire(pending, CONGESTED))
                    continue
                if now < pending.next_try:
                    continue
                if pending.attempts >= self.max_attempts:
                    del self._pending[msg_id]
                    gave_up.append(self._retire(pending, NO_ACK))
                    continue
                if congested:
                    # No attempt consumed, for the reason the caller's own
                    # backpressure skips a whole tick: a message must never
                    # spend one of its four tries on a transmission that never
                    # went out. The deadline above is what ends it instead.
                    self._stats["retry_suppressed"] += 1
                    continue
                pending.attempts += 1
                pending.next_try = now + self._timeout(pending)
                self._stats["retry"] += 1
                needed = pending.needed()
                if needed is not None:
                    self._stats["retry_partial"] += 1
                retries.append((msg_id, pending.frame, pending.label,
                                pending.attempts, pending.missing(), needed))
        except Exception:
            self._stats["error"] += 1
        return retries, gave_up

    def _retire(self, pending, reason):
        self._stats["undelivered"] += 1
        record = Undelivered(pending.msg_id, pending.label, pending.missing(),
                             pending.attempts,
                             round(self._clock() - pending.created, 1), reason,
                             pending.frame)
        self._undelivered.append(record)
        return record

    # -- reporting ----------------------------------------------------------

    def snapshot(self):
        """For get_status(). Never raises."""
        try:
            now = self._clock()
            return {
                "inflight": [
                    {
                        "msg_id": p.msg_id,
                        "label": p.label,
                        "attempts": p.attempts,
                        "waiting_on": p.missing(),
                        "age_s": round(now - p.created, 1),
                    }
                    for p in self._pending.values()
                ],
                "undelivered": [
                    {
                        "msg_id": u.msg_id,
                        "label": u.label,
                        "missing": u.missing,
                        "attempts": u.attempts,
                        "age_s": u.age_s,
                        "reason": u.reason,
                    }
                    for u in reversed(self._undelivered)
                ],
            }
        except Exception:
            return {"inflight": [], "undelivered": []}

    def stats(self):
        out = dict(self._stats)
        out["pending"] = len(self._pending)
        return out

    def summary_line(self):
        s = self._stats
        return ("reliable: sent=%d delivered=%d undelivered=%d retries=%d "
                "inflight=%d solo=%d suppressed=%d"
                % (s.get("sent", 0), s.get("delivered", 0),
                   s.get("undelivered", 0), s.get("retry", 0),
                   len(self._pending), s.get("sent_unwitnessed", 0),
                   s.get("retry_suppressed", 0)))


class ReliableRx(object):
    """Remembers which acknowledged messages have already been delivered to
    local ATAK, so a retransmission is acknowledged again but not injected
    twice."""

    def __init__(self, ttl_s=DEFAULT_SEEN_TTL_S, max_seen=DEFAULT_MAX_SEEN,
                 clock=time.monotonic):
        self.ttl_s = ttl_s
        self.max_seen = max_seen
        self._clock = clock
        self._seen = collections.OrderedDict()  # (origin, msg_id) -> seen_at
        self._stats = collections.Counter()

    def should_deliver(self, origin, msg_id):
        """True the first time a message is offered, False afterwards.

        THE CALLER MUST ACKNOWLEDGE EITHER WAY. A duplicate means the sender
        never heard our previous ack, so staying silent guarantees it keeps
        retransmitting -- the ack is exactly what was lost.

        Never raises: failing open (delivering twice) is a duplicate marker on
        a map, while failing closed would drop a real message.
        """
        try:
            now = self._clock()
            self._expire(now)
            key = (origin, msg_id)
            if key in self._seen:
                self._stats["duplicate"] += 1
                return False
            self._seen[key] = now
            self._stats["delivered"] += 1
            while len(self._seen) > self.max_seen:
                self._seen.popitem(last=False)
            return True
        except Exception:
            self._stats["error"] += 1
            return True

    def _expire(self, now):
        # Insertion-ordered and never reordered on read, so walking from the
        # head and stopping at the first live entry is amortized O(1). This
        # runs on the RNode serial read thread, where an O(n) scan stalls
        # radio reads.
        while self._seen:
            key, seen_at = next(iter(self._seen.items()))
            if now - seen_at < self.ttl_s:
                break
            self._seen.pop(key)

    def stats(self):
        out = dict(self._stats)
        out["seen"] = len(self._seen)
        return out
