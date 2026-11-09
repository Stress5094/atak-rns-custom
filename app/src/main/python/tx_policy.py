"""
Milestone 3: decide which CoT events are worth LoRa airtime.

Milestone 2 made events small (~63 bytes for a position report). It did not
decide how OFTEN to send them, and at SF8/125 kHz the channel is only
3125 bps -- roughly 0.2 s of airtime per position report. The first two-phone
bench run produced five events in eight seconds, i.e. ~12% channel utilisation
with **two** nodes. That scales linearly with node count, so five or six nodes
saturates the link and everything starts queueing behind everything else.

The naive fix -- one report per uid per N seconds -- is wrong, because it
delays exactly the updates that matter (a unit that just started moving, or
changed team/role). This module instead throttles on *significance*: a report
is sent when something actually changed, and otherwise at a slow heartbeat.
That is the same shape as dynamic reporting in other SA systems.

MILESTONE 5 amends that for scale. Sending on every 25 m of travel is a report
every ~18 s at walking pace, which at 16 nodes is ~17% duty on positions alone
-- the two-node bench this was sized against hid that. Sustained movement is
now capped to DEFAULT_MOVING_INTERVAL_S, but the FIRST move after a node has
been still is still sent immediately, so the "just started moving" update the
paragraph above is about keeps its latency. The cost is that a marker already
in motion can lag by up to one interval (~85 m at walking pace).

Deliberately free of RNS/Android imports so it is testable on a desktop, and
the clock is injected so rate-limit tests run instantly instead of sleeping.

WHAT IS NEVER THROTTLED: anything that is not a periodic position report.
Chat, drawings, routes and waypoints are user-initiated and one-shot -- there
is no later event to supersede a dropped one, so dropping one loses
information permanently. Only CoT "atoms" (`a-*` types carrying a position)
are eligible, because for those a newer report always supersedes an older one.
"""

import collections
import math
import time

# A position report is sent at least this often even when nothing changes.
#
# It used to be 60 s "so a stationary unit doesn't go stale on other maps",
# staying well inside ATAK's ~2 minute default. That reason is gone: the
# receiving bridge now extends the stale time itself and withdraws the marker
# when its own peer table says the node has stopped answering, so nothing on
# the map depends on this any more.
#
# What still does is a node that arrives LATE. A phone that joins after a
# stationary peer's last report has no way to learn where that peer is -- there
# is no catch-up mechanism and deliberately none planned (see the marker-digest
# note in open-questions). So this is not the map's refresh rate; it is the
# worst case for how long a newcomer sees an incomplete map, and that is what
# it should be chosen against. Matched to the beacon interval so a stationary
# node makes ONE transmission per interval rather than two -- change one of
# these and change the other.
#
# NO LONGER quite one-per-interval on FM: rns_bridge.py's _fm_position_resend
# (2026-09-20) gives every position/heartbeat one quick extra try ~15-20s
# later if this link's real per-burst loss rate is high enough to matter, so
# a stationary FM node can make two position-ish transmissions per interval,
# not one. Left this constant and its "one transmission" reasoning as-is --
# the resend is a bet against loss, not a second independent report, and
# does not change how OFTEN a fresh position is due -- but a reader sizing
# airtime budgets from this comment alone would now be undercounting FM.
DEFAULT_HEARTBEAT_S = 90.0

# ...and immediately whenever it moves more than this. 25 m is about the point
# where a marker visibly jumps at typical tactical zoom levels, and it is
# comfortably outside consumer GPS jitter (~5-10 m), so a stationary device
# does not retrigger on noise alone.
DEFAULT_MIN_DISTANCE_M = 25.0

# Once a node IS moving, further reports are capped to this interval.
#
# Milestone 3 sent on every 25 m of travel, which at walking pace is a report
# every ~18 s. That was sized against a two-node bench. At 16 nodes and ~0.2 s
# of airtime per report it works out near 17% duty on position traffic alone,
# before beacons, chat or acks -- well past what an uncoordinated channel
# absorbs without losing more to collisions than it carries.
#
# What is deliberately NOT capped is the FIRST move: see SEND_ONSET below.
# The expensive part of milestone 3's rule was the steady stream during
# sustained movement, not the one report that says movement began.
DEFAULT_MOVING_INTERVAL_S = 60.0

# How long after its last qualifying move a node is still considered "moving".
#
# This cannot be inferred from distance alone. Between two sends of a walking
# node, reports legitimately show less than min_distance_m of travel simply
# because little time has passed -- "under the threshold" means "not yet", not
# "stopped". So movement is remembered for a period instead, and the next
# qualifying move after it lapses counts as a fresh onset.
#
# MUST be longer than DEFAULT_MOVING_INTERVAL_S. Set equal, the two expire in
# the same instant: a node held quiet for a full interval has its motion lapse
# exactly as the cap releases, so the send that follows is misread as a fresh
# onset and every capped report becomes an onset forever after. The margin is
# what makes "still moving when the cap lifts" the answer instead.
DEFAULT_SETTLE_S = 90.0

# Events injected into local ATAK are remembered this long so that, if ATAK
# ever reflects one back to us, it is recognised and not re-transmitted.
DEFAULT_ECHO_TTL_S = 120.0

# Bound on tracked state. Far more than any realistic bench or squad, and it
# exists only so a flood of distinct uids cannot grow memory without limit.
DEFAULT_MAX_TRACKED = 256

# The bridge's own keepalive, written toward ATAK by
# CotTakServerBridgeService.buildKeepaliveEvent(). It exists purely to stop
# ATAK's connection going idle and carries no information, so it must never
# reach the radio if ATAK reflects it back.
KEEPALIVE_UID = "rns-bridge-ping"

# ...and ATAK sends a keepalive of its OWN, which the uid rule above does not
# catch: uid "ANDROID-<device hex>-ping", type t-x-c-t, roughly every five
# seconds while a streaming server connection is up.
#
# FOUND ON HARDWARE, 2026-08-12, on the addressed fork's first two-phone bench
# -- but the rule below was identical here, so this milestone relayed them too.
# It went unnoticed on these benches because the channel was never quiet: with
# beacons, relayed positions and ARQ traffic in the log, a 47-byte ping every
# few seconds reads as noise. On a silent channel it was all there was.
#
# Matched on TYPE, unlike the chat receipts below, and that difference is
# deliberate. t-x-c-t is CoT's generic control/ping convention -- it says
# "this connection is alive" to the server on the other end of a TCP socket.
# It is meaningless once relayed: the receiving ATAK learns that somebody
# else's local socket is healthy. There is no subtype of it worth the airtime
# on a 3125 bps channel shared by a squad.
KEEPALIVE_TYPE = "t-x-c-t"

# ATAK answers a DIRECT chat with two further CoT events, each carrying the
# original message's guid as its uid: a delivery receipt and a read receipt.
# Room messages ("All Chat Rooms") produce none -- measured on the bench, not
# assumed.
#
# They are expensive. At SF8 the two receipts are ~1.54 s of airtime against
# the message's own 0.74-0.86 s, so they are roughly 60% of what a direct chat
# costs, and they are the reason a two-node chat exchange measured 15.3% duty
# against a 0.9-2.2% idle baseline.
#
# They are also close to redundant here. The bridge already establishes that a
# message arrived, using a 17-byte ack, and says so: `DELIVERED ... after N
# transmission(s)` in the log and the MESSAGES section on the node screen.
# Spending 281 bytes on air to repeat that is a bad trade on a 3125 bps
# channel shared by a squad.
#
# THE COST OF DROPPING THEM IS VISIBLE, so know it before relying on this:
# ATAK's own chat UI will not show delivered/read ticks, and a message that
# arrived perfectly well will keep looking unconfirmed *in ATAK*. The delivery
# information still exists, just on the bridge screen rather than in the chat
# thread. Set drop_chat_receipts=False to trade the airtime back for the ticks.
#
# Matched by exact type, not by a `b-t-f-*` prefix: an unrecognised chat
# subtype is relayed rather than dropped, because a bigger packet is a much
# cheaper mistake than silently losing an event nobody anticipated.
CHAT_RECEIPT_TYPES = frozenset(("b-t-f-d", "b-t-f-r"))
DEFAULT_DROP_CHAT_RECEIPTS = True

# Beyond this, a "move" between consecutive reports is not travel -- nothing
# on foot or in a vehicle covers 100 km between two position reports. It means
# the fix itself changed nature (GPS acquired, or a null/last-known position
# replaced by a real one), so the log prints both coordinates rather than a
# bare distance nobody can interpret.
_IMPLAUSIBLE_MOVE_M = 100000.0

# Latitude and longitude both zero is not a position, it is the absence of one.
# ATAK emits it while GPS is reacquiring, and the point it names -- open ocean
# in the Gulf of Guinea, about 600 km south of Ghana, which mapmakers call Null
# Island -- is somewhere no user of this bridge has ever been.
#
# Transmitting it costs twice over. Every other device draws the marker in the
# Atlantic until the next update arrives, and the bridge itself sees a ~12,000
# km jump out and another back, each qualifying as a movement onset, which is
# the one category that is never rate limited. Two of the most expensive sends
# available, to say something false.
#
# The tolerance is about a centimetre, which is there to catch the exact zeros
# ATAK actually emits rather than to carve out a region. Anyone genuinely at
# 0,0 is in the ocean and has bigger problems.
_NULL_ISLAND_EPSILON = 1e-7

# `reason` is prose for the log and its wording is not load-bearing. `kind` is
# the stable identifier callers may branch on -- rns_bridge uses it to pick out
# onsets for jitter, and matching that on the reason string instead would break
# the moment someone improved the wording.
Decision = collections.namedtuple("Decision", "send reason kind")

_SEND_NEW = "new contact"
# The two halves of what milestone 3 called simply "moved". Separated because
# they cost very different amounts and answer different questions on the
# bench: onsets should be rare and are worth their airtime, while a high
# `moving` count means the rate limit is doing the work it was added for.
#
# Public, unlike its siblings: it crosses into rns_bridge as a Decision.kind.
SEND_ONSET = "started moving"
_SEND_MOVING = "moving"
_SEND_CHANGED = "attributes changed"
_SEND_HEARTBEAT = "heartbeat"
_SEND_NOT_POSITION = "not a position report"
_DROP_RATE = "rate limited"
_DROP_ECHO = "echo of injected event"
_DROP_KEEPALIVE = "bridge keepalive"
_DROP_RECEIPT = "chat receipt"
_DROP_NO_FIX = "no GPS fix"


def jitter_window(peer_count, slot_s, max_s):
    """How wide a random send window should be for `peer_count` peers.

    One slot per peer, so peers land in adjacent slots rather than on top of
    each other, capped so the delay stays bounded as a mesh grows.

    The floor of one slot is what stops this returning zero, but the more
    important half is that it SCALES: a fixed window was measured doing real
    harm on a two-node bench, spending jitter guarding against a collision
    that cannot occur when there is only one other radio. Lives here rather
    than in rns_bridge so it can be tested without RNS installed.
    """
    return min(max_s, max(1, peer_count) * slot_s)


def is_null_island(lat, lon):
    """True for the placeholder ATAK emits when it has no fix. See
    _NULL_ISLAND_EPSILON."""
    if lat is None or lon is None:
        return False
    return abs(lat) < _NULL_ISLAND_EPSILON and abs(lon) < _NULL_ISLAND_EPSILON


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres.

    Haversine rather than a flat-earth approximation: the cheaper version is
    fine near the equator and degrades with latitude, and getting a distance
    threshold silently wrong at high latitude would be an unpleasant bug to
    chase.
    """
    r = 6371008.8  # mean Earth radius, metres
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class TxPolicy(object):
    """Decides whether a captured CoT event earns airtime.

    Not thread-safe by contract -- it is called only from the CoT listener
    thread (decide) and the RNode read thread (note_injected), so those two
    share a lock internally.
    """

    def __init__(self, heartbeat_s=DEFAULT_HEARTBEAT_S,
                 min_distance_m=DEFAULT_MIN_DISTANCE_M,
                 moving_interval_s=DEFAULT_MOVING_INTERVAL_S,
                 settle_s=DEFAULT_SETTLE_S,
                 echo_ttl_s=DEFAULT_ECHO_TTL_S,
                 max_tracked=DEFAULT_MAX_TRACKED,
                 drop_chat_receipts=DEFAULT_DROP_CHAT_RECEIPTS,
                 clock=time.monotonic):
        self.heartbeat_s = heartbeat_s
        self.min_distance_m = min_distance_m
        self.moving_interval_s = moving_interval_s
        self.settle_s = settle_s
        self.echo_ttl_s = echo_ttl_s
        self.max_tracked = max_tracked
        self.drop_chat_receipts = drop_chat_receipts
        self._clock = clock
        # uid -> (sent_at, summary, moving_until). moving_until is the time the
        # node stops counting as in motion if it registers no further travel;
        # it is kept in this same dict rather than a parallel one so it
        # inherits the max_tracked eviction below and cannot outlive its uid.
        self._last_sent = collections.OrderedDict()
        # (uid, time) -> injected_at
        self._injected = collections.OrderedDict()
        self._stats = collections.Counter()

    # -- outbound -----------------------------------------------------------

    def decide(self, summary):
        """summary is a cot_codec.EventSummary. Never raises."""
        try:
            return self._decide(summary)
        except Exception:
            # A policy bug must not stop traffic. Failing open costs airtime;
            # failing closed would silently take the bridge off the air.
            self._stats["error"] += 1
            return Decision(True, "policy error, failing open", "error")

    def _decide(self, summary):
        now = self._clock()
        self._expire(now)

        if summary.uid == KEEPALIVE_UID or summary.type == KEEPALIVE_TYPE:
            return self._drop(_DROP_KEEPALIVE, _DROP_KEEPALIVE)

        if (summary.uid, summary.time) in self._injected:
            # We put this exact event into local ATAK moments ago and it has
            # come back to us. Retransmitting it would feed a loop that grows
            # with every node on the mesh.
            return self._drop(_DROP_ECHO, _DROP_ECHO)

        if self.drop_chat_receipts and summary.type in CHAT_RECEIPT_TYPES:
            # ~60% of a direct chat's airtime, restating what the bridge's own
            # acknowledgement already established. See CHAT_RECEIPT_TYPES.
            return self._drop(_DROP_RECEIPT, _DROP_RECEIPT)

        if not summary.is_position:
            # Chat, drawings, routes, waypoints: one-shot and user-initiated.
            return self._accept(None, now, _SEND_NOT_POSITION, _SEND_NOT_POSITION)

        if is_null_island(summary.lat, summary.lon):
            # Dropped BEFORE the last-sent lookup, and deliberately without
            # recording it. That is what fixes the return trip as well: leave
            # the previous real fix in place and the position that arrives when
            # GPS recovers is a few metres from it, so it passes as an ordinary
            # heartbeat. Record the 0,0 instead and coming back would look like
            # another 12,000 km onset -- fixing one spurious send and keeping
            # the other.
            return self._drop(_DROP_NO_FIX, _DROP_NO_FIX)

        previous = self._last_sent.get(summary.uid)
        if previous is None:
            return self._accept(summary, now, _SEND_NEW, _SEND_NEW)

        sent_at, before, moving_until = previous

        if (before.type != summary.type
                or before.callsign != summary.callsign
                or before.team != summary.team
                or before.role != summary.role):
            return self._accept(summary, now, _SEND_CHANGED, _SEND_CHANGED,
                                moving_until)

        moved = haversine_m(before.lat, before.lon, summary.lat, summary.lon)
        elapsed = now - sent_at

        if moved >= self.min_distance_m:
            # Read the state BEFORE this observation updates it: the whole
            # point is to distinguish the first qualifying move from the
            # hundredth.
            was_moving = now < moving_until
            moving_until = now + self.settle_s

            if not was_moving:
                # A unit that has just started moving is the single most
                # informative position report there is, and the one a fixed
                # interval delays worst. It is never rate limited.
                return self._accept(summary, now, self._move_detail(
                    SEND_ONSET, moved, before, summary), SEND_ONSET,
                    moving_until)

            if elapsed >= self.moving_interval_s:
                return self._accept(summary, now, self._move_detail(
                    _SEND_MOVING, moved, before, summary), _SEND_MOVING,
                    moving_until)

            # Moving, but too soon to spend airtime on it. The motion still has
            # to be recorded or the node would look stationary the moment its
            # reports start being suppressed, and every send would then be
            # misfiled as a fresh onset.
            self._touch(summary.uid, sent_at, before, moving_until)
            return self._drop(
                _DROP_RATE,
                "%s (%.0fs ago, moving, moved %.0fm)" % (_DROP_RATE, elapsed, moved),
            )

        if elapsed >= self.heartbeat_s:
            return self._accept(summary, now, _SEND_HEARTBEAT, _SEND_HEARTBEAT,
                                moving_until)

        return self._drop(
            _DROP_RATE,
            "%s (%.0fs ago, moved %.0fm)" % (_DROP_RATE, elapsed, moved),
        )

    def _move_detail(self, prefix, moved, before, summary):
        # Include both fixes for anything implausibly large. A ~12,000 km
        # "move" was seen on the bench, which is a GPS acquisition (a null or
        # last-known position replaced by a real fix), not travel -- but the
        # log said only the distance, so confirming that meant guessing. Print
        # the coordinates and it explains itself.
        if moved >= _IMPLAUSIBLE_MOVE_M:
            return ("%s %.0fkm (%.5f,%.5f -> %.5f,%.5f -- GPS acquisition?)"
                    % (prefix, moved / 1000.0,
                       before.lat, before.lon, summary.lat, summary.lon))
        return "%s %.0fm" % (prefix, moved)

    def _touch(self, uid, sent_at, summary, moving_until):
        """Update tracked state without counting as a send."""
        self._last_sent[uid] = (sent_at, summary, moving_until)
        self._last_sent.move_to_end(uid)

    def _accept(self, summary, now, reason, stat_key, moving_until=0.0):
        # summary is None for traffic that isn't throttled at all -- there is
        # no point tracking state for events that can never be suppressed.
        if summary is not None:
            self._last_sent[summary.uid] = (now, summary, moving_until)
            self._last_sent.move_to_end(summary.uid)
            while len(self._last_sent) > self.max_tracked:
                self._last_sent.popitem(last=False)
        self._stats["sent"] += 1
        self._stats[stat_key] += 1
        return Decision(True, reason, stat_key)

    def _drop(self, stat_key, reason):
        self._stats["suppressed"] += 1
        self._stats[stat_key] += 1
        return Decision(False, reason, stat_key)

    # -- inbound ------------------------------------------------------------

    def note_injected(self, summary):
        """Record an event handed to local ATAK, so a reflection of it can be
        recognised. Never raises."""
        try:
            now = self._clock()
            self._injected[(summary.uid, summary.time)] = now
            self._injected.move_to_end((summary.uid, summary.time))
            while len(self._injected) > self.max_tracked:
                self._injected.popitem(last=False)
        except Exception:
            self._stats["error"] += 1

    # -- housekeeping -------------------------------------------------------

    def _expire(self, now):
        # Insertion-ordered and never reordered on read, so walking from the
        # head and stopping at the first live entry is amortized O(1).
        while self._injected:
            key, seen = next(iter(self._injected.items()))
            if now - seen < self.echo_ttl_s:
                break
            self._injected.pop(key)

    def stats(self):
        out = dict(self._stats)
        out["tracked_uids"] = len(self._last_sent)
        out["echo_cache"] = len(self._injected)
        return out

    def self_position_pending(self):
        """True once ATAK's own self-marker has been caught at null island at
        least once and no real (non-null-island) position has gone out since.

        This is the state a cold GPS at exactly the moment ATAK's streaming
        connection came up leaves a node in: ATAK sends its self-SA CoT once
        on connect, and does not appear to resend it again on any short
        interval afterwards (observed on the bench: 40+ minutes with a real
        GPS fix flowing to ATAK the whole time, and this policy never seeing
        anything but the original null-island event). A fresh connection
        picks up whatever fix ATAK has AT THAT MOMENT -- see
        CotTakServerBridgeService's forced-reconnect action, which this flag
        exists to drive. Cheap to compute from the same counters
        summary_line() already prints, so no extra state to keep in step.
        """
        real_sent = sum(self._stats.get(k, 0) for k in
                         (_SEND_NEW, SEND_ONSET, _SEND_CHANGED, _SEND_MOVING,
                          _SEND_HEARTBEAT))
        return self._stats.get(_DROP_NO_FIX, 0) > 0 and real_sent == 0

    def summary_line(self):
        """Compact one-liner for the periodic status log. The suppression
        percentage is the number to watch: it is the airtime this policy is
        buying back."""
        s = self._stats
        sent, dropped = s.get("sent", 0), s.get("suppressed", 0)
        total = sent + dropped
        pct = (100.0 * dropped / total) if total else 0.0
        return ("sent=%d suppressed=%d (%.0f%%) [rate=%d echo=%d keepalive=%d "
                "receipt=%d nofix=%d] [onset=%d moving=%d heartbeat=%d]"
                % (sent, dropped, pct, s.get(_DROP_RATE, 0),
                   s.get(_DROP_ECHO, 0), s.get(_DROP_KEEPALIVE, 0),
                   s.get(_DROP_RECEIPT, 0), s.get(_DROP_NO_FIX, 0),
                   s.get(SEND_ONSET, 0), s.get(_SEND_MOVING, 0),
                   s.get(_SEND_HEARTBEAT, 0)))
