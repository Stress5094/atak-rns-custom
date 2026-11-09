"""Whether WiFi's transmit path is actually working, not just attached.

Same two-signal shape as lora_health.py, and for the same reason: WiFi, like
the RNode, exposes a real bidirectional socket with an honest `online` flag
and rides the same reliable.py ARQ (real acks) once it carries one-shot
traffic -- it does not share FM's problem (an ambiguous, one-way audio
channel with no interface-level ack signal at all), which is what
fm_health.py's different, window-stickiness shape exists to work around. So
WiFi gets LoRa's template, not FM's:

    HARD -- is the interface attached and reporting connectivity at all?
    Read fresh at decision time, the same way lora_health.py takes it,
    rather than this module re-deriving or caching it.

    SOFT -- how often a one-shot message's WiFi ARQ schedule has recently
    exhausted with no acknowledgement at all (reliable.NO_ACK specifically
    -- never DISPLACED/CONGESTED). A rolling rate over a window, gated by a
    minimum sample count, so one unlucky message never reads as "WiFi is
    down".

Deliberately free of RNS/Android imports, with the clock injected, so it is
testable on a desktop. Same shape as lora_health.py, fm_health.py,
reliable.py, tx_policy.py and peers.py.
"""

import collections
import time

DEFAULT_WINDOW_S = 300.0

# Same starting point as lora_health.DEFAULT_MIN_SAMPLES, for the same
# reason: below this, a single unlucky message would read as trouble from a
# sample of one.
DEFAULT_MIN_SAMPLES = 3

# Same starting point as lora_health.DEFAULT_DEGRADED_RATE -- a placeholder
# honestly stated as one, pending real field data for WiFi specifically.
DEFAULT_DEGRADED_RATE = 0.5

UP = "up"
DEGRADED = "degraded"
DOWN = "down"


class WifiHealth(object):
    """Observations from WiFi's one-shot delivery outcomes, summarised
    honestly, combined with the interface's own attachment state.

    Fed from rns_bridge.py's reliable-ARQ completion handling
    (note_delivered/note_escalated) and read at decision time with the
    interface's current `online` value passed in -- not stored, so a caller
    always supplies a fresh read rather than this module holding a
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
        """A one-shot message got an ack on WiFi -- no escalation needed."""
        self._record(False)

    def note_escalated(self):
        """A one-shot message's WiFi ARQ schedule fully exhausted with no
        ack (reliable.NO_ACK) and had to fall back to another link."""
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
            return "WiFi interface not attached or not reporting"
        if state == DEGRADED:
            rate = self.escalation_rate()
            return ("WiFi attached, but %.0f%% of recent one-shot "
                    "messages needed another link to deliver" % (rate * 100.0))
        return "WiFi attached, no sign of trouble"

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
