"""Carrying a frame from the radio it arrived on to the other one.

An FM handheld and a LoRa node cannot hear each other at all. A node carrying
both radios can hear both, and passing traffic between them is the only thing
that makes the second radio worth its battery: without it an FM-only operator
reaches whoever is in direct FM range and nobody else.

This is forwarding, but it is not relaying, and the difference sets the policy.

Same-link relay has to ask "would this reach anyone new?", and in a single cell
the answer is almost always no -- everybody already heard it. That test is why
the relay budget survives contact with a real mesh, and it is why FM relaying
was dropped: at 1.5 s a minute only 60-byte frames could ever have been
forwarded there anyway.

Cross-link forwarding does not need the test. A frame that arrived on FM has,
by definition, not been heard by anything on LoRa. Coverage is the default
rather than the exception, so the gate here is cheaper: hops remaining, the
destination willing to carry a frame that size, and its budget having room.

What it does still need is suppression between GATEWAYS. Two nodes both
carrying both radios will both forward the same frame, and the receiver's
duplicate check saves the map but not the airtime. So a forward waits a
jittered moment first, and is cancelled if the frame turns up on the
destination link meanwhile -- the same mechanism relaying uses, with one
difference that matters on a slow link: the window is scaled by how long the
destination takes to send the frame. A fixed two seconds is fine on LoRa and
wrong on FM, where the frame itself can take three, and the second gateway
would start transmitting before the first had finished.
"""

import collections
import random
import time

Forward = collections.namedtuple("Forward", "link delay_s reason")

# What share of a channel a node may spend carrying traffic across to it.
#
# Deliberately larger than the relay allowance, and the coverage argument above
# is the reason. A same-link relay usually reaches nobody new, so it is right
# to be stingy: 2.5% and most relays suppressed before they are charged. A
# gateway forward reaches nodes that could not have heard the frame at all, so
# every second buys strictly more than a relay's second does.
#
# Five per cent is a policy choice, not a measurement, and should be replaced
# by one. The LoRa figure it sits beside was derived from an observed 5.8%
# channel occupancy under a 10% ceiling; there is no equivalent reading for FM
# yet, so this is the honest placeholder rather than a number with a bench
# behind it.
#
# What it buys, at the measured frame costs:
#
#     3.0 s a minute   LoRa   a 55-byte position is 0.55 s   ~5 a minute
#                      FM     the same position is 1.39 s    ~2 a minute
#                      FM     a 119-byte marker is 2.55 s    ~1 a minute
#
# One marker a minute across the bridge is thin. It is also what the channel
# has, and a budget that pretended otherwise would just move the failure to
# where it is harder to see.
DEFAULT_GATEWAY_DUTY = 0.05

# The floor on how long a gateway waits before forwarding.
DEFAULT_WINDOW_S = 2.0

# ...and the multiple of the destination's own frame time it must also clear,
# so a second gateway has heard the first before its timer fires.
WINDOW_FRAME_FACTOR = 1.5


def _default_jitter(window_s):
    return random.uniform(0.0, window_s)


class Gateway(object):
    """Which other links a received frame should be lifted onto."""

    def __init__(self, gates, budgets, window_s=DEFAULT_WINDOW_S,
                 jitter=None, clock=time.monotonic):
        missing = [name for name in gates.names() if name not in budgets]
        if missing:
            raise ValueError("no budget for link(s) %s" % ", ".join(missing))
        self.gates = gates
        self.budgets = budgets
        self.window_s = float(window_s)
        self._jitter = jitter if jitter is not None else _default_jitter
        self._clock = clock
        self.stats = collections.Counter()

    def window_for(self, gate, frame_len):
        """How long to wait before forwarding onto this link."""
        return max(self.window_s,
                   WINDOW_FRAME_FACTOR * gate.link.frame_s(frame_len))

    def _verdict(self, arrived_on, gate, kind, frame_len, hops):
        if gate.name == arrived_on:
            # Same-link forwarding is the relay policy's decision, and on FM
            # the answer is a flat no. Answering it here too would either
            # double-charge the budget or quietly overrule that.
            return False, "arrived here"
        if hops <= 0:
            return False, "hop limit"
        allowed = gate.allows(kind, frame_len)
        if not allowed.ok:
            return False, allowed.reason
        return True, "reaches %s, which did not hear it" % gate.name

    def explain(self, arrived_on, kind, frame_len, hops):
        """Every link's answer with its reason, for logging a refusal.

        Deliberately free of side effects: it charges nothing, so it can be
        called to explain a decision without changing one.
        """
        return collections.OrderedDict(
            (gate.name, self._verdict(arrived_on, gate, kind, frame_len, hops))
            for gate in self.gates)

    def decide(self, arrived_on, kind, frame_len, hops):
        """The forwards to make, budget charged for each.

        An empty list is a normal answer: most frames arrive on the only link
        that could carry them.
        """
        if arrived_on not in self.gates:
            raise KeyError("frame arrived on unknown link %r; have %s"
                           % (arrived_on, ", ".join(self.gates.names())))
        out = []
        for gate in self.gates:
            ok, reason = self._verdict(arrived_on, gate, kind, frame_len, hops)
            if not ok:
                if reason != "arrived here":
                    self.stats["refused/" + reason.split(",")[0]] += 1
                continue
            cost = gate.link.frame_s(frame_len)
            if not self.budgets.charge(gate.name, cost):
                self.stats["refused/over budget on %s" % gate.name] += 1
                continue
            window = self.window_for(gate, frame_len)
            self.stats["forwarded/" + gate.name] += 1
            out.append(Forward(gate.name, self._jitter(window), reason))
        return out

    def cancelled(self, forward, frame_len):
        """Somebody else forwarded it first; give the airtime back.

        Worth doing here where it is not for same-link relay. The coverage test
        suppresses most relays before they are ever charged, so a cancelled one
        is rare; a gateway has no such test and cancellation is the normal way
        a second bridge behaves. Leaving those charges on the bucket would
        throttle the link for transmissions it never made.
        """
        gate = self.gates[forward.link]
        self.budgets[forward.link].refund(gate.link.frame_s(frame_len))
        self.stats["forwarded/" + forward.link] -= 1
        self.stats["cancelled/" + forward.link] += 1

    def summary_line(self):
        sent = sum(v for k, v in self.stats.items() if k.startswith("forwarded/"))
        cancelled = sum(v for k, v in self.stats.items()
                        if k.startswith("cancelled/"))
        refused = sum(v for k, v in self.stats.items() if k.startswith("refused/"))
        return ("gateway: forwarded=%d cancelled=%d refused=%d"
                % (sent, cancelled, refused))
