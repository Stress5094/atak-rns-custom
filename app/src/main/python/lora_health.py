"""Whether LoRa's transmit path is actually working, not just attached.

Built for LoRa-primary/FM-backup routing: something has to decide which
link broadcast/periodic traffic (beacons, positions, heartbeats -- none of
which carry an ack) currently defaults to. Two signals feed that decision,
of very different character:

    HARD -- is the RNode attached and responding at all? Already a live,
    honest boolean elsewhere in this app (RNode.online, and
    RadioBridgeService's own USB-reconnect handling exists specifically to
    keep it honest rather than stuck reporting a dead port as healthy).
    This module does not re-derive it: the caller reads it and passes it
    in fresh at decision time, the same way a `clock` is injected
    elsewhere in this codebase rather than read globally.

    SOFT -- how often a one-shot message's LoRa ARQ schedule has recently
    exhausted with no acknowledgement at all (reliable.NO_ACK specifically
    -- never DISPLACED/CONGESTED, which mean the channel is already busy,
    not broken; escalating onto FM in response to congestion would be the
    wrong reaction). A rolling rate over a window, gated by a minimum
    sample count. This is the opposite caution from fm_health.py's: FM's
    problem is a short burst of correlated samples overestimating a bad
    link, LoRa's problem is too FEW samples -- one unlucky message must
    not read as "LoRa is down".

Deliberately free of RNS/Android imports, with the clock injected, so it
is testable on a desktop. Same shape as fm_health.py, reliable.py,
tx_policy.py and peers.py.
"""

import collections
import time

DEFAULT_WINDOW_S = 300.0

# How many ARQ outcomes (delivered or escalated) must land in the window
# before the escalation rate is trusted enough to affect the state. Below
# this, a single unlucky message would read as trouble from a sample of one.
DEFAULT_MIN_SAMPLES = 3

# Escalation rate at or above this, with enough samples, reads as DEGRADED.
# LoRa's own ARQ already tolerates real loss -- four attempts and backoff
# exist for exactly that -- so this is meant to catch a radio that is
# attached but not actually getting RF out, not ordinary channel loss. A
# placeholder honestly stated as one, the same way
# eligibility.DEFAULT_GATEWAY_DUTY is, pending real bench data.
DEFAULT_DEGRADED_RATE = 0.5

UP = "up"
DEGRADED = "degraded"
DOWN = "down"


class LoraHealth(object):
    """Observations from LoRa's one-shot delivery outcomes, summarised
    honestly, combined with the interface's own attachment state.

    Fed from rns_bridge.py's reliable-ARQ completion handling
    (note_delivered/note_escalated) and read at decision time with the
    interface's current `online` value passed in -- not stored, so a
    caller always supplies a fresh read rather than this module holding a
    reference that could go stale.
    """

    def __init__(self, window_s=DEFAULT_WINDOW_S,
                 min_samples=DEFAULT_MIN_SAMPLES,
                 degraded_rate=DEFAULT_DEGRADED_RATE,
                 clock=time.monotonic):
        self.window_s = window_s
        self.min_samples = min_samples
        self.degraded_rate = degraded_rate
        self._clock = clock
        self._outcomes = collections.deque()  # (timestamp, escalated: bool)

    def note_delivered(self):
        """A one-shot message got an ack on LoRa -- no escalation needed."""
        self._record(False)

    def note_escalated(self):
        """A one-shot message's LoRa ARQ schedule fully exhausted with no
        ack (reliable.NO_ACK) and had to fall back to FM."""
        self._record(True)

    def _record(self, escalated):
        now = self._clock()
        self._outcomes.append((now, escalated))
        self._prune(now)

    def _prune(self, now):
        cutoff = now - self.window_s
        while self._outcomes and self._outcomes[0][0] < cutoff:
            self._outcomes.popleft()

    def escalation_rate(self):
        """None when there are not yet enough samples to have an opinion,
        rather than a misleadingly confident number from too few tries."""
        now = self._clock()
        self._prune(now)
        if len(self._outcomes) < self.min_samples:
            return None
        escalated = sum(1 for _, e in self._outcomes if e)
        return escalated / len(self._outcomes)

    def state(self, online):
        if not online:
            return DOWN
        rate = self.escalation_rate()
        if rate is not None and rate >= self.degraded_rate:
            return DEGRADED
        return UP

    def summary(self, online):
        state = self.state(online)
        if state == DOWN:
            return "RNode not attached or not responding"
        if state == DEGRADED:
            rate = self.escalation_rate()
            return ("RNode attached, but %.0f%% of recent one-shot "
                    "messages needed FM to deliver" % (rate * 100.0))
        return "RNode attached, no sign of trouble"

    def snapshot(self, online):
        now = self._clock()
        self._prune(now)
        rate = self.escalation_rate()
        return {
            "state": self.state(online),
            "summary": self.summary(online),
            "online": bool(online),
            "escalation_rate": None if rate is None else round(rate, 3),
            "samples_in_window": len(self._outcomes),
            "window_s": self.window_s,
        }
