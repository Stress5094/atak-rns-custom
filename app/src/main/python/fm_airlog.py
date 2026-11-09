"""Why a burst was lost: one line per burst this node HEARD, saying how it ended.

WHY THIS EXISTS. 2026-10-09's baseline sent 26 bursts and the phone decoded 21.
The phone could say that 5 were missing (a gap in the sender's sequence
numbers) and nothing about WHY, so the loss could not be told apart from "the
receiver was transmitting at the time" -- and on this link that is most of it
(`docs/open-questions.md`: three of four beacon losses on 2026-10-03 were the
receiver keying up; a half-duplex radio leaves no trace of what it missed).

WHAT CAN AND CANNOT BE KNOWN. A receiver can only describe a burst whose carrier
it saw (fm_carrier). Each carrier episode ends in exactly one of:

    OK            a frame decoded and framed correctly
    REJECTED      a frame passed codec2's CRC and then failed framing --
                  almost always a clipped or mis-levelled signal
    OVER OUR TX   the carrier overlapped a window in which THIS node was
                  transmitting (it hears nothing while it keys up)
    CUT SHORT     the carrier was a fraction of a burst: an impulse, or a burst
                  that began or ended under something else
    LOST          a full-length carrier and nothing decoded: fading,
                  interference or a level problem -- the line says which way
                  the level points, and says it is a guess

A burst whose carrier was NEVER seen (too weak for the detector, or flickered
under its threshold -- see fm_carrier, which misses ~1/3 of mid-burst checks)
leaves no episode at all. That case is covered from the other side: the peer
table logs a SEQUENCE GAP when a numbered frame is skipped. Read the two
together: a gap with no episode near it is a burst that never arrived; a gap
with an episode is a burst that arrived and was not decoded.

ONE CASE THE EPISODES SURFACE THAT NOTHING ELSE DID: a frame that DECODED while
the detector saw no carrier. That is the detector failing, not the channel,
and it is logged as such rather than counted as a clean receive.

Pure (no RNS, no Android), clock injected, bounded history. Same shape as
fm_health. IMPORTANT: on-device CPython 3.8 -- keep this module 3.8-compatible.
"""

import collections
import time

import fm_health

#: A carrier shorter than this fraction of a full burst is "cut short". The
#: detector's own hold (fm_carrier.HOLD_S) pads every episode, so a real burst
#: always reads at least its own length.
SHORT_FRACTION = 0.5

#: How long after the carrier drops a decode may still arrive. codec2 decodes a
#: frame at its END and the carrier statistic lags by about its own window, so a
#: decode lands up to ~0.7 s after the carrier falls (measured 0.68 s).
GRACE_S = 1.5

#: A transmit window is widened by this much at the front before it is compared
#: with a carrier. The receive loop is not fed while this node transmits, and
#: the detector needs a window of audio to notice a burst, so a burst that
#: began under our transmit does not show as a carrier until after it ends.
OVERLAP_LEAD_S = 1.0

#: A "cut short" verdict is HELD this long before it is logged, in case the
#: burst turns out to have decoded. The carrier detector flickers (it misses
#: about a third of mid-burst checks), so one real burst can read as a blip, a
#: gap, and then a decode. Measured 2026-10-09: both "CUT SHORT, carrier 0.0 s"
#: lines in a 26-burst run were bursts that went on to be delivered, the decode
#: following the blip's verdict by ~1.2 s. Cost: a genuine impulse is reported
#: this much later. Risk: a blip followed within this window by a decode of an
#: UNRELATED frame is folded into it and the impulse goes unreported.
MERGE_S = 4.0

MAX_EVENTS = 64


class _Episode(object):
    def __init__(self, start, level, peak):
        self.start = start
        self.last_busy = start
        self.max_level = level
        self.peak = peak
        self._sq = 0.0
        self._n = 0
        self.decoded = 0
        self.rejected = 0
        self.reason = ""
        self.closed_at = None

    def add_audio(self, peak, rms):
        self.peak = max(self.peak, peak)
        self._sq += rms * rms
        self._n += 1

    @property
    def rms(self):
        return (self._sq / self._n) ** 0.5 if self._n else 0.0

    @property
    def duration(self):
        return self.last_busy - self.start


class BurstLedger(object):
    """Turns the receive path's chunk-by-chunk observations into one verdict per
    burst. Fed by fm_rx.FmRx.feed(); read with pop_events()."""

    def __init__(self, burst_s, clock=time.monotonic, grace_s=GRACE_S):
        self.burst_s = float(burst_s)
        self._clock = clock
        self._grace = grace_s
        self._open = None            # the episode in progress
        self._closing = None         # ended, still waiting out the grace
        self._tx_start = None
        self._tx_windows = collections.deque(maxlen=16)   # (start, end)
        self._events = []            # finished, not yet popped
        self._pending_short = None   # (settled_at, text): a blip awaiting a decode
        self.history = collections.deque(maxlen=MAX_EVENTS)
        self.counts = collections.Counter()

    # -- inputs ---------------------------------------------------------------

    def note_tx(self, active):
        """This node started (True) or stopped (False) transmitting. From the
        audio service, which is the only thing that knows when the radio keys."""
        now = self._clock()
        if active:
            if self._tx_start is None:
                self._tx_start = now
        elif self._tx_start is not None:
            self._tx_windows.append((self._tx_start, now))
            self._tx_start = None

    def observe(self, busy, level, peak, rms, decoded=0, rejected=0, reason="",
                strong=None):
        """One chunk of receive audio has been processed.

        `busy` and `level` are the carrier detector's; `peak`/`rms` are of the
        chunk, measured before de-emphasis; `decoded`/`rejected` are the frames
        this chunk completed.

        `strong` is whether the statistic is over the detector's threshold NOW,
        as opposed to `busy`, which stays true for fm_carrier.HOLD_S after a
        burst ends. The hold exists to bridge flicker for listen-before-
        transmit; used here it would pad every episode by a second and pull
        the average level toward idle noise, so duration and level are taken
        only over chunks that are actually strong. Defaults to `busy`."""
        now = self._clock()
        if strong is None:
            strong = busy
        self._expire_short(now)

        if busy:
            if self._open is None:
                # A new carrier while the last one is still waiting out its
                # grace means they were separate bursts: settle the old one.
                self._settle(self._closing)
                self._closing = None
                self._open = _Episode(now, level, peak)
            ep = self._open
            if strong:
                ep.last_busy = now
                ep.max_level = max(ep.max_level, level)
                ep.add_audio(peak, rms)
        elif self._open is not None:
            self._closing, self._open = self._open, None
            self._closing.closed_at = now

        target = self._open or self._closing
        if decoded or rejected:
            if target is None:
                # Decoded with no carrier ever seen: the detector missed a
                # burst the modem heard. Not a channel event.
                self._emit("OK", "frame decoded but the carrier detector never "
                           "saw it (detector miss, not a channel loss)"
                           + self._merged_note())
            else:
                target.decoded += decoded
                target.rejected += rejected
                if reason:
                    target.reason = reason

        if (self._closing is not None
                and now - self._closing.closed_at >= self._grace):
            self._settle(self._closing)
            self._closing = None

    # -- verdicts -------------------------------------------------------------

    def _overlaps_tx(self, ep):
        lo, hi = ep.start - OVERLAP_LEAD_S, ep.last_busy
        windows = list(self._tx_windows)
        if self._tx_start is not None:
            windows.append((self._tx_start, self._clock()))
        return any(s <= hi and e >= lo for s, e in windows)

    def _level_hint(self, ep):
        rms = ep.rms
        if ep.peak >= fm_health.SATURATED_PEAK:
            return ("receive audio hit full scale (peak %.2f): turn the "
                    "receiving volume down" % ep.peak)
        if rms < fm_health.TARGET_RMS_LOW:
            return ("receive level LOW (rms %.2f, aim %.2f-%.2f): weak signal "
                    "or volume too low" % (rms, fm_health.TARGET_RMS_LOW,
                                           fm_health.TARGET_RMS_HIGH))
        if rms > fm_health.TARGET_RMS_HIGH:
            return ("receive level HIGH (rms %.2f, aim %.2f-%.2f)"
                    % (rms, fm_health.TARGET_RMS_LOW, fm_health.TARGET_RMS_HIGH))
        return ("receive level in band (rms %.2f): not a level problem -- "
                "fading or interference mid-burst" % rms)

    def _settle(self, ep):
        if ep is None:
            return
        where = "carrier %.1f s, level %.2f" % (ep.duration, ep.max_level)
        if ep.decoded:
            self._emit("OK", "%s, %d frame(s) decoded, rx rms %.2f%s"
                       % (where, ep.decoded, ep.rms, self._merged_note()))
        elif ep.rejected:
            self._emit("REJECTED", "%s, a frame decoded but was unusable (%s); "
                       "%s" % (where, ep.reason or "no reason recorded",
                               self._level_hint(ep)))
        elif self._overlaps_tx(ep):
            self._emit("OVER OUR TX", "%s, nothing decoded -- this node was "
                       "transmitting around it (half-duplex: it cannot hear "
                       "while it keys up)" % where)
        elif ep.duration < SHORT_FRACTION * self.burst_s:
            # HELD, not emitted: see MERGE_S. Anything already held is older
            # than this blip and has had its chance, so it goes out first.
            self._flush_short()
            self._pending_short = (
                self._clock(),
                "%s of a ~%.1f s burst, nothing decoded -- an impulse, or a "
                "burst that began or ended under something else"
                % (where, self.burst_s))
        else:
            self._emit("LOST", "%s (a full burst), nothing decoded; %s [a guess "
                       "from the level, not a diagnosis]"
                       % (where, self._level_hint(ep)))

    # -- the held blip --------------------------------------------------------

    def _expire_short(self, now):
        if (self._pending_short is not None
                and now - self._pending_short[0] >= MERGE_S):
            self._flush_short()

    def _flush_short(self):
        if self._pending_short is not None:
            _, text = self._pending_short
            self._pending_short = None
            self._emit("CUT SHORT", text)

    def _merged_note(self):
        """A decode arrived while a blip was held: they were one burst, and the
        blip was the detector flickering, not an impulse. Empty if nothing is
        held (or if what is held has aged out)."""
        if self._pending_short is None:
            return ""
        self._expire_short(self._clock())
        if self._pending_short is None:
            return ""
        self._pending_short = None
        return (" [the carrier detector flickered first -- an earlier blip "
                "belongs to this burst and is not counted separately]")

    def tx_within(self, seconds):
        """(times, seconds on air) this node transmitted during the last
        `seconds`, counting one in progress. For the sequence-gap line: a
        numbered frame that never arrived while we were transmitting was
        probably hidden by it, and the receive path has no episode to say so
        because it is not fed audio while the radio keys up."""
        now = self._clock()
        lo = now - seconds
        n, total = 0, 0.0
        windows = list(self._tx_windows)
        if self._tx_start is not None:
            windows.append((self._tx_start, now))
        for a, b in windows:
            if b >= lo:
                n += 1
                total += b - max(a, lo)
        return n, total

    def _emit(self, kind, text):
        self.counts[kind] += 1
        line = "[fm-air] burst %s: %s" % (kind, text)
        self._events.append(line)
        self.history.append(line)

    # -- outputs --------------------------------------------------------------

    def pop_events(self):
        """Verdicts finished since the last call, oldest first."""
        out, self._events = self._events, []
        return out

    def summary(self):
        """One line of running totals, for the status block."""
        if not self.counts:
            return "no bursts heard yet"
        return ", ".join("%s %d" % (k, self.counts[k]) for k in
                         ("OK", "REJECTED", "OVER OUR TX", "CUT SHORT", "LOST")
                         if self.counts[k])
