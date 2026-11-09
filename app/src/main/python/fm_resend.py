"""Make one-shot traffic survive the FM link's variability, without ARQ.

RESTORED 2026-10-02 (rebuild Phase 2a) from `0355f92^`, where it shipped as
milestone 8 and keyed real hardware. The scheduling logic is unchanged -- it
was already free of RNS and Android imports with the clock injected, which is
what made it portable intact -- and what changed is the documentation, which
had drifted from its own code. See "WHAT CANCELS A SCHEDULE" below.

NOTHING CALLS note_heard() YET. The beacon that carries the tags is wired up in
rns_bridge.py, which cannot happen before the FM interface exists again, so
that is Phase 4 and the first hardware measurement is after Phase 5. What
exists today is this module and its tests, which is deliberate: the design
wanted settling before integration rather than during it.

WHY THIS EXISTS RATHER THAN reliable.py. The FM link carries CoT payload
only, and acknowledgement traffic costs a share of the airtime that scales
with the mesh. An asymmetric link -- A hears B but not the reverse, which this
bench has measured repeatedly -- cannot converge a request/response loop at
all. So `reliable.py`'s ARQ engine is never attached to this interface, and
nothing here ever sends, expects, or waits for an acknowledgement.

THE AIRTIME IS WORSE THAN THE RAW RATE SUGGESTS, which is why "just ack it"
keeps sounding affordable and is not. At the 2026-10-02 operating point the
raw rate is 800 bps and the DELIVERED rate is ~99 bps: 29 B of content per
2.35 s burst, lead-in and both in-burst copies included. An earlier version of
this docstring said "~500 bps", which was the raw figure of the slower
configuration -- exactly the mistake fm_config.raw_bps() now carries a warning
about. One round trip of ack traffic is a sizeable fraction of a beacon
interval.

That leaves the question this module answers: with no feedback of any kind,
how does a chat message survive a link that delivers somewhere between 20%
and 100% of bursts depending on nothing anyone has been able to identify?

THE ANSWER IS TIME DIVERSITY, AND THE DISTINCTION FROM THE OBVIOUS ANSWER
IS THE WHOLE POINT. The obvious reading of "unreliable link" is "send more
copies", which this project already tried as an in-burst repeat and measured
as worthless at the rate that matters -- see fm_config.DEFAULT_REPEATS, which
carries the measurements, and fm_burst's module docstring, which is where the
"repetition is not reliability" argument now lives. Copies inside one burst
share one preamble and land within a few
seconds of each other, so they are perfectly CORRELATED in channel state:
when this link is in a bad patch it stays bad for minutes to tens of
minutes, and three copies 3 seconds apart are three samples of the same
bad patch. Worse, they lengthen the burst, and burst duration is the one
thing this channel has consistently punished.

The measured structure of the failure is what makes spacing work. From
docs/open-questions.md's FM sections: batches run back-to-back scored 4/4
and 24/24 at settings that scored 2/10 and 4/10 when the same trials were
spread across an hour, and the failures arrive in CLUSTERS (19 straight
failures in one 9.6-minute stretch; 3 straight passes in the next 8
minutes) rather than scattered independently. A retry seconds later is a
wasted transmission because it samples the same cluster. A retry minutes
later is a genuinely independent sample.

2026-09-20: DEFAULT_INTERVAL_S was deliberately shortened from 240s to 60s
on an explicit user request, trading away FULL coverage of the worst
clusters above for faster typical-case delivery -- see DEFAULT_INTERVAL_S's
own comment below and docs/open-questions.md for the exact numbers. This is
a known, chosen gap, not an oversight: at 60s a schedule can finish inside
one of the longer recorded clusters.

WHAT CANCELS A SCHEDULE: THE HEARD-DIGEST, and nothing else ever will.
note_heard() below is that path. It is the only completion signal this link
has, and it costs no transmission of its own -- the tags ride a beacon that
was going out anyway, which is what keeps it inside the no-acknowledgements
constraint rather than a violation of it.

TWO PROPERTIES MAKE IT SAFE, and both are load-bearing rather than tidy:

  * It can only ever CANCEL a schedule, never start one. Nothing in this
    module triggers a transmission. A digest that arrives corrupted, late, or
    from a node talking about someone else's traffic can waste airtime at
    worst; it can never cause any.
  * A tag is a 2-byte hash of (origin, msg_id) rather than the id itself,
    because ids are unique per SENDER only -- node A's message 7 and node B's
    message 7 are the same number. A false positive stops a blind schedule
    early and silently abandons a message that never arrived, which is
    precisely the failure the resends exist to prevent. See
    cot_codec.heard_tag() for the collision reasoning.

THE SIZING WAS RE-DERIVED, NOT INHERITED, and an earlier version of this
docstring claimed the opposite -- that a heard-digest "does not fit today,
because the beacon is already at its exact byte ceiling". It said so while
note_heard() sat 250 lines below it, already working and already shipped: the
header was simply never updated after the digest landed. Measured against the
code the rebuild actually chose (H_256_768_22, 29 B of capacity):

    beacon floor (name 2 B, callsign 0)         10 B plaintext, 22 B on wire
    + 3 heard tags, no neighbour digest         17 B plaintext, 29 B on wire
    + the 8 B neighbour digest alone            18 B plaintext, 30 B -- OVER
    + 3 heard tags AND the neighbour digest     25 B plaintext, 37 B -- OVER

So three tags fit EXACTLY, with zero bytes spare, and only by giving up the
8-byte neighbour digest -- which serves a LoRa-only relay coverage test, so
FM can drop it. That is where _FM_HEARD_MAX = 3 comes from, and it is a
ceiling with no slack in it: every 2 bytes given back to the node name costs
one tag. test_fm_resend.py asserts this against cot_codec so the budget is
enforced rather than remembered.

WHAT IS STILL UNMEASURED, and it gates the Reed-Solomon experiment: whether a
17 B beacon survives often enough for a confirmation to be worth anything. The
carriage is hardware-proven but has only ever flown with heard=[]. If beacons
are lost at the data rate then confirmations are rare and schedules run to
completion regardless -- which is the no-cancel behaviour described above, and
is safe, just wasteful.

CUMULATIVE REASSEMBLY IS LOAD-BEARING, NOT AN OPTIMISATION. FM's payload
budget is 17 B (29 B of frame capacity less mesh.OVERHEAD), so a fragment
carries 12 B after cot_codec's 5 B fragment header, and any real chat message
is many fragments -- a 60 B message is five of them. Two regimes, at a
per-burst delivery p and F fragments over N attempts:

    fragments accumulate across attempts:  (1 - (1-p)^N) ** F
    each attempt must land whole:          1 - (1 - p**F) ** N

At p=0.5, F=8, N=4 that is 60% against 1.6%. The difference is entirely
whether the receiver's reassembler still holds attempt 1's fragments when
attempt 4 arrives, which is why callers MUST size both the reassembly
timeout and the receive-side duplicate window against span_s() below
rather than against their own defaults. cot_codec.fragment_reliable()
already gives every attempt the same msg_id, which is what makes the
fragments combinable at all; this module's job is not to outlive it.

Deliberately free of RNS/Android imports, with the clock injected, so the
whole thing is testable on a desktop without sleeping. Same shape as
reliable.py and tx_policy.py.
"""

import collections
import random
import time

# == cot_codec.MSG_ID_MASK; test_fm_resend asserts they agree. Not imported so
# this module stays free of the codec.
MSG_ID_MASK = 0x7FFF


# Total transmissions of a message, the first included. Five rather than
# more because each one costs a full burst of airtime and the returns fall
# off fast once the spacing already crosses a typical bad cluster: at a
# per-burst 50%, going 3 -> 4 attempts moves a single-fragment message from
# 87% to 94%, and 4 -> 5 only reaches 97%. Five also keeps the total span
# (see span_s()) inside something a user might plausibly still care about
# the answer to -- a chat message delivered 40 minutes late is arguably
# worse than one reported undelivered.
#
# One of these five is the fast attempt-2 (see DEFAULT_FIRST_RETRY_S below),
# which is a DIFFERENT mechanism from the other four and does not really
# count toward the reliability math above -- it is spaced far too close to
# attempt 1 to be an independent sample of this link's minutes-long failure
# clusters, the same correlation problem the module docstring already
# raises about in-burst repeats. It exists for a narrower, real failure
# mode: a single missed burst caused by something SHORT-lived (a collision
# with another node's simultaneous send, the kind this session found on
# real hardware and fixed with _stagger_window() -- staggering cuts the
# odds of that happening but does not zero them out), not a multi-minute
# bad patch. The count went 4 -> 5, not 4 total with one swapped out, so
# that the original four widely-spaced samples -- the ones actually doing
# the cluster-escaping work -- are undisturbed; span_s() below reflects
# this explicitly.
DEFAULT_ATTEMPTS = 5

# Gap between attempts (2->3, 3->4, 4->5 -- see DEFAULT_FIRST_RETRY_S for
# the different gap before attempt 2). The failure clusters this bench has
# recorded run several minutes to tens of minutes (a 19-trial dead stretch
# across 9.6 minutes; 4 straight failures across 10 minutes, then 3 straight
# passes across 8) -- a gap shorter than that samples the same cluster twice
# and buys nothing against THOSE clusters specifically.
#
# WAS 240s (which put three post-front-load gaps across ~12-14 minutes,
# comfortably outlasting every recorded cluster). SHORTENED TO 60s on an
# explicit user request, 2026-09-20, after being shown that trade
# specifically: at 60s the whole 5-attempt schedule spans ~230s worst-case
# (see span_s()), which is SHORTER than the worst recorded cluster (576s) --
# a schedule can now finish entirely inside one of the longer bad patches,
# with no ack to ever reveal that it happened. Chosen anyway because most
# observed failures are shorter than the worst case, and 60s recovers those
# roughly 4x faster than 240s did. If chat delivery starts looking
# unreliable again in a way staggering/the fast first retry don't explain,
# this is the first constant to look at -- re-widen it, or fix DEFAULT_
# ATTEMPTS accordingly, rather than assuming a new bug. Not a measured
# optimum either way: nobody has characterised the cluster length
# distribution properly (that needs the multi-hour passive monitoring run
# docs/open-questions.md has called for twice).
DEFAULT_INTERVAL_S = 60.0

# The gap before attempt 2 ONLY -- every later gap (2->3, 3->4, 4->5) still
# uses DEFAULT_INTERVAL_S above, unchanged. ~10-20s (15s +/- 5s jitter) is
# short enough to recover a single missed burst before a user notices, and
# deliberately much shorter than any measured failure cluster (minutes to
# tens of minutes -- see the module docstring), so it is NOT a bet that the
# channel has recovered by then. It is a bet that the miss was something
# else entirely: a collision with another node's own simultaneous send
# (observed on real hardware -- two phones transmitting about a second
# apart), a one-off audio hiccup, anything whose bad state does not persist
# the way this link's RF clusters do. If the miss WAS a cluster, this fast
# retry samples the same bad state again and is simply wasted -- cheap
# enough at one extra burst on an otherwise-idle channel (see the module
# docstring's own affordability argument) that guessing wrong here costs
# little, while guessing right recovers a message ~15x faster than waiting
# out the full interval.
DEFAULT_FIRST_RETRY_S = 15.0
DEFAULT_FIRST_RETRY_JITTER_S = 5.0

# Fraction of the interval used as random jitter, so two nodes that happen
# to send at the same moment do not then stay in lockstep for every
# subsequent attempt -- on a half-duplex channel a repeated collision would
# destroy both messages every time rather than once. Same reasoning as the
# beacon's own slot jitter in rns_bridge.py.
DEFAULT_JITTER_FRAC = 0.15

# Bounded so a burst of one-shot traffic cannot grow this without limit.
# Far above any realistic number in flight at once on a link this slow --
# at five attempts each (see span_s()), 16 live messages is already more
# one-shot traffic than this channel can physically carry.
DEFAULT_MAX_INFLIGHT = 16

# Kept for the status screen after a schedule finishes, so "what happened
# to that message" has an answer for a while.
DEFAULT_COMPLETED_KEPT = 8


def span_s(attempts=DEFAULT_ATTEMPTS, interval_s=DEFAULT_INTERVAL_S,
           jitter_frac=DEFAULT_JITTER_FRAC,
           first_retry_s=DEFAULT_FIRST_RETRY_S,
           first_retry_jitter_s=DEFAULT_FIRST_RETRY_JITTER_S):
    """Worst-case wall-clock time from a message's first transmission to its
    last, jitter included.

    EXPORTED BECAUSE TWO OTHER THINGS MUST BE SIZED AGAINST IT, and sizing
    them by hand is how they drift apart:

      * the receiver's duplicate window (reliable.ReliableRx's ttl_s). A
        resend arriving after the window has forgotten the message is
        delivered to ATAK a second time -- a chat message rendered twice,
        or a marker that reappears after being cleared. reliable.py's own
        default is 300 s, sized for ARQ finishing in ~16 s, and is far too
        short for this schedule.
      * the receiver's reassembly timeout (cot_codec.Reassembler). See the
        module docstring: fragments accumulating across attempts is what
        makes a multi-fragment message deliverable at all here, and it
        stops working the moment a partial is timed out between attempts.

    Both want a margin on top of this, not this exactly.

    NOT a uniform (attempts - 1) * interval any more: the gap before
    attempt 2 is the short, front-loaded one (see DEFAULT_FIRST_RETRY_S),
    every gap after that is still the full interval. Formula mirrors
    _first_delay()/_delay() exactly -- if those change, this must too.
    """
    if attempts <= 1:
        return 0.0
    first_gap = first_retry_s + first_retry_jitter_s
    later_gaps = max(0, attempts - 2)
    return first_gap + later_gaps * interval_s * (1.0 + jitter_frac)


class _Scheduled(object):
    __slots__ = ("msg_id", "packets", "label", "attempts", "next_try",
                 "registered_at", "tag")

    def __init__(self, msg_id, packets, label, now, tag=None):
        self.msg_id = msg_id
        self.packets = packets
        self.label = label
        self.attempts = 1          # the caller transmits the first one
        self.next_try = None
        self.registered_at = now
        # The opaque identifier a peer will echo back if it receives this
        # message (cot_codec.heard_tag). None means this schedule can only
        # ever run to completion.
        self.tag = tag


class FmResend(object):
    """Schedules blind, spaced retransmissions of one-shot FM traffic.

    Never sends anything itself: tick() returns what is due and the caller
    enqueues it, the same division reliable.ReliableTx already uses, so the
    scheduling is testable without a radio, a socket or a clock that moves.
    """

    def __init__(self, attempts=DEFAULT_ATTEMPTS,
                 interval_s=DEFAULT_INTERVAL_S,
                 jitter_frac=DEFAULT_JITTER_FRAC,
                 first_retry_s=DEFAULT_FIRST_RETRY_S,
                 first_retry_jitter_s=DEFAULT_FIRST_RETRY_JITTER_S,
                 max_inflight=DEFAULT_MAX_INFLIGHT,
                 completed_kept=DEFAULT_COMPLETED_KEPT,
                 clock=time.monotonic, rng=None):
        self.attempts = max(1, attempts)
        self.interval_s = interval_s
        self.jitter_frac = jitter_frac
        self.first_retry_s = first_retry_s
        self.first_retry_jitter_s = first_retry_jitter_s
        self.max_inflight = max_inflight
        self._clock = clock
        self._rng = rng or random.Random()
        self._pending = collections.OrderedDict()   # msg_id -> _Scheduled
        self._completed = collections.deque(maxlen=completed_kept)
        self._stats = collections.Counter()
        self._next_msg_id = self._rng.randrange(MSG_ID_MASK + 1)

    def next_msg_id(self):
        """A message id for cot_codec.wrap_reliable().

        Owned here rather than by the caller because the id is what makes
        resends combinable -- the receiver's duplicate check and its
        reassembler both key on it, so every attempt of one message must
        carry the same one, and two different messages must not collide
        while either is still in flight. Wraps at 15 bits: the field is 16
        wide but its top bit is the any-ack flag (cot_codec.ANY_ACK_BIT), so
        wrap_reliable() masks the id to 15. An id handed out above that would
        reach the receiver as a DIFFERENT number, and the heard-tag the
        sender expects (hashed from this id) could never match the one the
        receiver reports (hashed from the masked one): half of all messages
        would never be confirmed and would burn every blind resend. Seen live
        2026-10-12 as ids 55473..55476 against 22705..22708. With at most
        max_inflight live at once and a span measured in minutes, reuse
        cannot overlap.
        """
        msg_id = self._next_msg_id
        self._next_msg_id = (self._next_msg_id + 1) & MSG_ID_MASK
        return msg_id

    def _delay(self):
        jitter = self.interval_s * self.jitter_frac
        return self.interval_s + self._rng.uniform(-jitter, jitter)

    def _first_delay(self):
        """The gap before attempt 2 only -- see DEFAULT_FIRST_RETRY_S."""
        return self.first_retry_s + self._rng.uniform(
            -self.first_retry_jitter_s, self.first_retry_jitter_s)

    def register(self, msg_id, packets, label="", tag=None):
        """Record that `packets` were just transmitted once, and schedule
        the remaining attempts.

        The caller has already sent attempt 1 by the time this is called --
        this only arranges the repeats, so a message with attempts=1 is
        registered, completed and never resent.

        `tag` is what a peer echoes back on its beacon if this message
        reaches it (see note_heard). Supplied by the caller rather than
        computed here so that the choice of what a tag IS -- and the
        collision reasoning behind it -- lives in one place,
        cot_codec.heard_tag().
        """
        now = self._clock()
        entry = _Scheduled(msg_id, list(packets), label, now, tag=tag)
        self._stats["registered"] += 1
        if self.attempts <= 1:
            self._stats["completed"] += 1
            self._completed.append((msg_id, label, 1))
            return
        # The FIRST scheduled resend (attempt 2) uses the short, front-loaded
        # gap -- every later reschedule, done in tick() below, uses the full
        # interval. See DEFAULT_FIRST_RETRY_S.
        entry.next_try = now + self._first_delay()
        self._pending[msg_id] = entry
        while len(self._pending) > self.max_inflight:
            # Oldest first: it has had the most chances already, so it is
            # the one whose remaining attempts are worth least. Counted
            # rather than silent -- a link shedding schedules is a link
            # being asked for more than it can carry, which is worth
            # seeing on the status screen.
            _, oldest = self._pending.popitem(last=False)
            self._stats["displaced"] += 1
            self._completed.append((oldest.msg_id, oldest.label,
                                    oldest.attempts))

    def forget(self, msg_id):
        """Drop a schedule -- for a message whose transmission failed
        outright, so the repeats would fail identically."""
        if self._pending.pop(msg_id, None) is not None:
            self._stats["forgotten"] += 1

    def note_heard(self, tags):
        """A peer has reported receiving these messages. Stop resending them.

        THE ONLY COMPLETION SIGNAL THIS LINK HAS, and it costs no
        transmission of its own: the tags ride a beacon that was going out
        anyway (see rns_bridge's beacon loop). That is what keeps it inside
        the link's no-acknowledgements constraint rather than a violation of
        it -- there is no per-message response, no timeout waiting on one,
        and nothing here ever TRIGGERS a transmission. It can only cancel.

        Cancelling early on a false report would silently abandon a message
        that never arrived, which is the failure the resends exist to
        prevent, so the tag is built to make that a 1-in-65536 hash accident
        rather than a systematic confusion between two senders' identical
        message ids -- see cot_codec.heard_tag().

        Returns the labels cancelled, for the caller to log: on a link that
        cannot otherwise confirm anything, this is the one moment worth
        telling the operator about.
        """
        wanted = set(tags or ())
        if not wanted:
            return []
        done = []
        for msg_id, entry in list(self._pending.items()):
            if entry.tag is not None and entry.tag in wanted:
                self._pending.pop(msg_id, None)
                self._stats["confirmed"] += 1
                self._completed.append((msg_id, entry.label, entry.attempts))
                done.append(entry.label)
        return done

    def tick(self):
        """Returns [(msg_id, packets, label, attempt)] due for resending now.

        Each returned entry has already had its attempt counted, so a
        caller that drops one on the floor costs that message a chance --
        the same contract reliable.ReliableTx.tick() has, and the same
        reason: the alternative is a message that can never finish.
        """
        now = self._clock()
        due = []
        finished = []
        # list() rather than iterating live: register() runs on the CoT
        # listener thread while this runs on the retry thread, so a message
        # sent mid-tick would otherwise raise "dictionary changed size
        # during iteration" -- which lands in _reliable_loop's broad except
        # and silently skips that tick for LoRa's ARQ retries too, a much
        # wider blast radius than the FM schedule it came from. Callers are
        # expected to hold their own lock as well (rns_bridge uses
        # _reliable_lock); this is belt-and-braces on the cheaper side.
        for msg_id, entry in list(self._pending.items()):
            if entry.next_try is not None and now >= entry.next_try:
                entry.attempts += 1
                due.append((msg_id, list(entry.packets), entry.label,
                            entry.attempts))
                self._stats["resent"] += 1
                if entry.attempts >= self.attempts:
                    finished.append(msg_id)
                else:
                    entry.next_try = now + self._delay()
        for msg_id in finished:
            entry = self._pending.pop(msg_id)
            self._stats["completed"] += 1
            self._completed.append((msg_id, entry.label, entry.attempts))
        return due

    def pending_tags(self):
        """The cancel-tag of every schedule still running, as {tag: label}.

        FOR DIAGNOSIS, not for the cancel path -- note_heard() above does the
        matching and is the only thing that may mutate anything.

        It exists because on 2026-10-03 the first two-phone hardware run had
        beacons carrying real tags, received by the peer, cancelling nothing,
        and no way to tell whether the digest was empty or simply did not
        match what was pending. One side of that comparison was unreachable
        from outside this class, so the question could not be asked at all.

        Schedules registered without a tag are omitted: they can never be
        cancelled by a digest, so listing them beside tags that can would
        invite exactly the wrong conclusion.
        """
        return {e.tag: e.label for e in self._pending.values()
                if e.tag is not None}

    def snapshot(self):
        """For get_status(). Plain data only, JSON-ready."""
        now = self._clock()
        return {
            "inflight": [
                {
                    "msg_id": e.msg_id,
                    "label": e.label,
                    "attempts": e.attempts,
                    "of": self.attempts,
                    "next_in_s": (None if e.next_try is None
                                  else round(max(0.0, e.next_try - now), 1)),
                }
                for e in self._pending.values()
            ],
            "completed": [
                {"msg_id": m, "label": l, "attempts": a}
                for m, l, a in self._completed
            ],
        }

    def stats(self):
        return dict(self._stats)

    def summary_line(self):
        s = self._stats
        return ("fm-resend: %d scheduled, %d resent, %d completed, "
                "%d in flight%s"
                % (s["registered"], s["resent"], s["completed"],
                   len(self._pending),
                   (", %d displaced" % s["displaced"]) if s["displaced"] else ""))
