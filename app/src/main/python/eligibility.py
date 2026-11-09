"""Which links a frame is worth putting on.

Both radios can carry anything. That is not the same as both radios SHOULD, and
the difference is not a matter of taste -- it is arithmetic that only becomes
visible once each link has its own airtime model.

Reticulum's KISS interfaces advertise a 564-byte MTU. At the shipped LoRa
bitrate one full-size frame is a 4.3 s transmission; on FM at 442 bps it is
10.6 s. Ten seconds is not a packet, it is an outage: the channel is unusable
by anyone else for the duration, any fade anywhere inside the window destroys
the whole frame, and there is no partial recovery below the fragment level. The
MTU is a protocol limit and was never a statement about what is sensible to
send.

So the gate's main job is to cap a transmission in TIME rather than in bytes,
because time is what collisions and channel-hogging are measured in, and then
to say what byte budget that implies on each link:

    a 2.0 s cap allows   249 bytes on LoRa at 1074 bps
                          88 bytes on FM   at  442 bps

Same rule, different limits, both derived rather than chosen -- which is the
point. A byte limit that is right for one link is silently wrong for the other.

The second job is refusing traffic classes outright, and here the gate is more
limited than it looks. The type nibble distinguishes a beacon from an ack from
a compact frame, but every position, marker, drawing and route is T_COMPACT, so
"drop drawings on FM, keep positions" cannot be decided here. It has to happen
upstream where the CoT is still parsed. What CAN be decided here is the
fallback path: a frame that reached T_ZXML is one the compact encoder could not
shrink, and those are exactly the frames a slow link should not be carrying.
"""

import collections

Verdict = collections.namedtuple("Verdict", "ok reason")

# The longest single transmission a link should make.
#
# Two seconds rather than a rounder number because of what sits either side of
# it. Below about a second the cap starts fragmenting ordinary traffic, and
# each extra fragment pays the fixed cost again. Above two seconds a frame
# stops being relayable at all on the slow link: the relay budget is 1.5 s a
# minute, so a 2 s frame already cannot be forwarded even once, and anything
# longer is originate-only by construction.
DEFAULT_MAX_FRAME_S = 2.0

# Per link, because the reason for the cap differs between them.
#
# LoRa relays, so its frames want to stay short enough to be forwarded and
# short enough not to block the mesh. Two seconds.
#
# FM does not relay -- decided 2026-09-05, and the measurements support it: at
# a 1.5 s/min relay budget only frames of 60 bytes or less could ever have been
# forwarded there, so relaying on FM was barely functional. Removing it also
# removes the relayability argument for a tight cap, which is what lets FM
# carry markers at all. Three seconds is chosen against what it admits:
#
#     3.0 s = 143 bytes    position 55, waypoint 46, chat 64,
#                          marker 119, rectangle 117, circle 132   all fit
#                          route 231, drawing 476                  do not
# LoRa is UNCAPPED, and the two-second figure that used to be here was wrong.
#
# It was written in a module with no caller and never checked against what the
# bridge actually sends. It sends up to 464 bytes sealed -- 3.60 s at the
# shipped Long Fast preset -- so a 2 s cap would have started refusing traffic
# that has worked on the radio since milestone 1. Capping LoRa may well be
# right, but it is a change to shipped behaviour and belongs in a decision of
# its own, not smuggled in with the FM work.
#
# FM has no legacy to preserve: nothing has ever gone out on it.
#
# FM'S CAP IS NOW PASSED BY THE CALLER, 2026-10-02, and this constant is only
# the default for a caller that does not know the configuration. The "3.0 s =
# 143 bytes" derivation above assumed airtime is bytes over a bitrate, which is
# right for LoRa and wrong for FM: every FM frame is padded to the code's full
# payload before modulation, so cost is FLAT -- a 6 B frame and a 113 B frame
# are the same burst. The cap's real question is "how long is one burst", which
# is a property of the chosen code, symbol rate, copy count and lead-in, so
# rns_bridge derives it from fm_config's live operating point. Left here at 3.0
# rather than removed, because a Gate built without one would have no cap at
# all, and a wrong cap that refuses traffic is easier to notice than none.
LORA_MAX_FRAME_S = None
FM_MAX_FRAME_S = 3.0

# WiFi is not airtime-constrained the way a half-duplex serial radio is --
# there is no shared channel being hogged by one long transmission, so the
# whole "cap in TIME" rationale above does not apply here. Uncapped, like
# LoRa, and for a different reason: LoRa is uncapped because capping it would
# be a change to shipped behaviour; WiFi is uncapped because the byte-time
# tradeoff the cap exists to manage barely exists on this link at all.
WIFI_MAX_FRAME_S = None

_OK = Verdict(True, "")

# Stands in for "no byte limit at all" on an uncapped link with no MTU given.
_NO_LIMIT = 1 << 30


class Gate(object):
    """One link's answer to "may this frame go out here?"."""

    def __init__(self, link, max_frame_s=DEFAULT_MAX_FRAME_S, deny_kinds=(),
                 fragments=True, relays=True, mtu=None):
        if max_frame_s is not None and max_frame_s <= 0:
            raise ValueError("%s: max_frame_s must be positive or None"
                             % link.name)
        self.link = link
        self.name = link.name
        self.max_frame_s = None if max_frame_s is None else float(max_frame_s)
        self.mtu = mtu
        self.deny_kinds = frozenset(deny_kinds)
        self.fragments = bool(fragments)
        self.relays = bool(relays)

    def max_frame_bytes(self):
        """Largest frame that fits the time cap, measured the way the airtime
        model measures: the sealed frame, mesh envelope included, before the
        RNS header is added.

        Zero means the cap cannot be met at all -- the link's own per-frame
        overhead already exceeds it. That is a configuration error rather than
        a busy channel, and `allows` says so in those words.
        """
        if self.max_frame_s is None:
            # No time cap: the protocol's own limit is the only one.
            return self.mtu if self.mtu is not None else _NO_LIMIT
        spare = self.max_frame_s - self.link.fixed_s()
        if spare <= 0:
            return 0
        return int(spare * self.link.bitrate() / 8.0)

    def allows(self, kind, frame_len):
        if kind in self.deny_kinds:
            return Verdict(False, "kind 0x%x is not carried on %s"
                                  % (kind, self.name))
        budget = self.max_frame_bytes()
        if budget <= 0:
            return Verdict(
                False,
                "%s cannot meet a %.1f s cap: its own per-frame overhead is "
                "%.2f s, so no payload fits. Raise max_frame_s or the bitrate."
                % (self.name, self.max_frame_s, self.link.fixed_s()))
        if frame_len > budget:
            if self.max_frame_s is None:
                return Verdict(False, "%d bytes is over %s's %d-byte limit"
                                      % (frame_len, self.name, budget))
            return Verdict(False, "%d bytes is %.1f s on %s, over the %.1f s "
                                  "cap (%d bytes)"
                                  % (frame_len, self.link.frame_s(frame_len),
                                     self.name, self.max_frame_s, budget))
        return _OK

    def frames_needed(self, message_len):
        """How many transmissions a whole message of this size becomes here."""
        budget = self.max_frame_bytes()
        if budget <= 0:
            return 0
        return (int(message_len) + budget - 1) // budget

    def allows_message(self, message_len):
        """May a whole message of this size go out here, fragments included?

        The frame gate alone cannot answer this. It sees frames, and the
        fragments of a large message are each individually small -- so a
        231-byte route sails through a 143-byte gate as two frames, each paying
        the fixed cost again, and a 476-byte drawing as four. On a link that
        does not fragment, the honest place to refuse it is here, once, rather
        than letting it arrive as a burst.
        """
        if self.max_frame_bytes() <= 0:
            return self.allows(0, 0)
        needed = self.frames_needed(message_len)
        if needed > 1 and not self.fragments:
            return Verdict(False,
                           "%d bytes needs %d transmissions on %s (%.1f s "
                           "total) and %s does not fragment"
                           % (message_len, needed, self.name,
                              needed * self.max_frame_s, self.name))
        return _OK

    def relayable_bytes(self, budget):
        """Largest frame the relay budget could ever forward on this link.

        A frame above this can be originated but never relayed, which is worth
        knowing before wondering why multi-hop stopped working on the slow
        link.
        """
        spare = budget.capacity - self.link.fixed_s()
        if spare <= 0:
            return 0
        return int(spare * self.link.bitrate() / 8.0)

    def __repr__(self):
        return ("Gate(%r, %s, <=%dB%s%s)"
                % (self.name,
                   "uncapped" if self.max_frame_s is None
                   else "<=%.1fs" % self.max_frame_s,
                   self.max_frame_bytes(),
                   "" if self.fragments else ", no-frag",
                   "" if self.relays else ", no-relay"))


class Gates(object):
    """The gates for every link, asked together.

    `links_for` is the question the sender actually has: not "may this go on
    FM" but "where does this go". An empty answer is a real one -- a frame too
    big for every link has to be fragmented or dropped, and saying so here is
    better than discovering it in the radio thread.
    """

    def __init__(self, gates):
        gates = list(gates)
        if not gates:
            raise ValueError("a gate set needs at least one link")
        names = [g.name for g in gates]
        if len(set(names)) != len(names):
            raise ValueError("duplicate link names: %r" % (names,))
        self._gates = collections.OrderedDict((g.name, g) for g in gates)

    def __getitem__(self, name):
        try:
            return self._gates[name]
        except KeyError:
            raise KeyError("no gate for link %r; have %s"
                           % (name, ", ".join(self._gates)))

    def __contains__(self, name):
        return name in self._gates

    def __iter__(self):
        return iter(self._gates.values())

    def __len__(self):
        return len(self._gates)

    def names(self):
        return list(self._gates)

    def links_for(self, kind, frame_len):
        """The links this frame may go out on, in configured order."""
        return [name for name, gate in self._gates.items()
                if gate.allows(kind, frame_len).ok]

    def links_for_message(self, message_len):
        """The links a whole message may go out on, fragments counted.

        Asked before encoding rather than per frame, because that is the only
        point at which a link that does not fragment can refuse the whole
        thing instead of receiving it as a burst of small ones.
        """
        return [name for name, gate in self._gates.items()
                if gate.allows_message(message_len).ok]

    def verdicts(self, kind, frame_len):
        """Every link's answer with its reason, for logging a refusal."""
        return collections.OrderedDict(
            (name, gate.allows(kind, frame_len))
            for name, gate in self._gates.items())

    def summary_line(self):
        return "gates: " + "  ".join(
            "%s<=%dB" % (name, gate.max_frame_bytes())
            for name, gate in self._gates.items())


def lora_gate(link, **kw):
    """The LoRa gate: relays, fragments, two-second frames."""
    kw.setdefault("max_frame_s", LORA_MAX_FRAME_S)
    return Gate(link, **kw)


def fm_gate(link, **kw):
    """The FM gate: originates only.

    No relaying, because at a 1.5 s/min budget FM could only ever have
    forwarded frames of 60 bytes or less. No fragmenting, because a message
    that needs two transmissions here costs six seconds of a channel nobody
    else can use while it happens -- and the frame gate cannot see that coming,
    since each fragment is individually small.

    A node carrying both radios still bridges between them. FM not relaying
    onto FM is a different thing from FM being cut off: the gateway is what
    lets an FM-only handheld reach the LoRa mesh at all.
    """
    kw.setdefault("max_frame_s", FM_MAX_FRAME_S)
    kw.setdefault("fragments", False)
    kw.setdefault("relays", False)
    return Gate(link, **kw)


def wifi_gate(link, **kw):
    """The WiFi gate: full-duplex, high-bitrate, relays and fragments freely.

    Unlike LoRa/FM, nothing here is a hand-picked tradeoff -- WiFi just gets
    the same treatment LoRa's frames would get if LoRa had no airtime cost at
    all: no time cap, and the MTU (passed in by the caller, same as LoRa's
    `mtu=cot_codec.RNS_MAX_PAYLOAD`) is the only real limit.
    """
    kw.setdefault("max_frame_s", WIFI_MAX_FRAME_S)
    return Gate(link, **kw)
