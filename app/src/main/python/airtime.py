"""What one frame costs, on a link that charges for more than its bytes.

Every airtime figure in this bridge came from one function that divided bytes
by a bitrate:

    ((payload_len + 19) * 8.0) / bitrate

That is right for LoRa. A frame costs exactly what it weighs, halving the
payload halves the cost, and a single global function is fine because there was
a single radio.

FM is not like that, in two ways that matter.

First, keying costs 55 ms before a bit moves -- measured on an MD-UV390 through
a Digirig Lite, and the same figure whether the frame is six bytes or six
hundred. Second, there are now two radios at once, with the LoRa side running
at a bitrate the RNode reports live and the FM side at whatever the modem is
configured for. One global function cannot answer for both.

What the fixed costs do is change what optimising for airtime MEANS. On LoRa
the way to spend less is to send fewer bytes. On FM it is to send fewer FRAMES,
and those are not the same instruction.

Two things are charged per frame no matter how small it is: the 55 ms key-up,
and the 19-byte RNS header. At 442 bps that header is 344 ms -- six times the
keying -- so the larger fixed cost is the one FM did not introduce.

                    LoRa 1074 bps          FM 442 bps + 55 ms key
    payload    total   carrying nothing    total   carrying nothing
       6 B     186 ms        76%           508 ms        79%
      60 B     588 ms        24%          1485 ms        27%
     200 B    1631 ms         9%          4019 ms        10%

Which is not the result this module was written expecting. Small frames are
dreadful on BOTH links, and for the same reason: 19 bytes of header against 6
bytes of payload. FM's key-up moves the figure by three points; it is not what
makes small frames expensive.

So bundling is worth roughly as much on LoRa as on FM, and always was. The
place FM genuinely differs is that everything costs about 2.5x more, which
means the same careless traffic that LoRa absorbs will saturate FM. See
`bundle_saving_s` for what merging frames actually buys on a given link.
"""

# RNS HEADER_1. Charged on every frame on every link, so it belongs here
# rather than being open-coded at each call site the way it used to be.
#
# Nineteen and not thirty-one, though cot_codec correctly says a transmission
# costs 31 bytes of header it cannot avoid. The other twelve are the mesh
# envelope, and every length handed to this module is measured AFTER
# mesh.wrap() -- the TX loop seals at the choke point, and _consider_relay
# measures the sealed packet it received. So the envelope is already inside
# `payload_len` and adding it here would charge it twice.
RNS_HEADER_BYTES = 19
MESH_ENVELOPE_BYTES = 12          # mesh.OVERHEAD, already inside payload_len

# Measured on the bench, 2026-09-05: MD-UV390 on GMRS1, keyed by a Digirig
# Lite's CM108 GPIO, carrier timed against the assertion on a receiving UV-5R
# with the squelch open. See tools/ptt_test.py.
FM_PTT_KEYUP_S = 0.055

# The same radio and channel on VOX, keyed by tone through the Digirig's own
# cable. Nearly four times the cost, and that is the GOOD case -- through a
# BTECH APRS-K1 the same measurement gives 3.2 s, because the cable somehow
# holds the transmitter up long after the audio stops. Recorded so nobody
# reaches for VOX thinking it is close to free.
FM_VOX_KEYUP_S = 0.215
FM_VOX_APRS_K1_KEYUP_S = 3.2

# LoRa keys per packet too, but the RNode does it in the low milliseconds and
# RNS's own bitrate figure already smears the preamble across the frame. Below
# the resolution of anything this module decides, so it is zero rather than a
# guess dressed up as a measurement.
LORA_KEYUP_S = 0.0


class Link(object):
    """One transport's airtime model: a bitrate and a fixed cost per key-up.

    `bitrate_bps` may be a number or a zero-argument callable. The callable
    form is how the bridge feeds in an RNode's live `.bitrate`, so pacing
    self-tunes when the radio parameters change -- but a link that is down
    reports 0, and dividing by that produces an infinity that propagates into
    a sleep() and hangs the sender. `fallback_bps` is what gets used instead.
    """

    def __init__(self, name, bitrate_bps, per_tx_s=0.0,
                 header_bytes=RNS_HEADER_BYTES, fallback_bps=None):
        self.name = name
        self._bitrate = bitrate_bps
        if fallback_bps is None:
            if callable(bitrate_bps):
                raise ValueError(
                    "%s: a live bitrate needs a fallback_bps, because an "
                    "interface that is down reports 0" % name)
            fallback_bps = bitrate_bps
        if not fallback_bps or fallback_bps <= 0:
            raise ValueError("%s: fallback_bps must be positive, not %r"
                             % (name, fallback_bps))
        if per_tx_s < 0:
            raise ValueError("%s: per_tx_s cannot be negative" % name)
        self.fallback_bps = float(fallback_bps)
        self.per_tx_s = float(per_tx_s)
        self.header_bytes = int(header_bytes)

    def bitrate(self):
        """The bitrate to charge at right now, never zero or negative."""
        value = self._bitrate() if callable(self._bitrate) else self._bitrate
        try:
            value = float(value)
        except (TypeError, ValueError):
            return self.fallback_bps
        return value if value > 0 else self.fallback_bps

    def frame_s(self, payload_len):
        """Airtime for one frame of `payload_len` bytes, key-up included."""
        if payload_len < 0:
            raise ValueError("payload_len cannot be negative")
        bits = (payload_len + self.header_bytes) * 8.0
        return self.per_tx_s + bits / self.bitrate()

    def fixed_s(self):
        """Airtime charged per frame regardless of payload: key-up + header.

        Both are per-frame and neither carries a byte the application asked
        for, so they belong together. At FM bitrates the header is the larger
        of the two by a factor of six, which is not what you would guess from
        the fact that only one of them is called overhead anywhere else.
        """
        return self.per_tx_s + (self.header_bytes * 8.0) / self.bitrate()

    def overhead_fraction(self, payload_len):
        """How much of a frame's airtime carries no payload.

        Dominated by the header, not the key-up: a 6-byte payload is 76%
        overhead on LoRa and 79% on FM. Reach for a bigger payload before
        reaching for a cheaper link.
        """
        total = self.frame_s(payload_len)
        return 0.0 if total <= 0 else self.fixed_s() / total

    def frames_per_minute(self, payload_len, duty=1.0):
        """How many such frames fit in `duty` of a minute.

        The number the whole airtime plan is trying to protect: how much a
        channel actually carries, once key-up and header are paid for.
        """
        if not 0.0 < duty <= 1.0:
            raise ValueError("duty must be in (0, 1], not %r" % (duty,))
        each = self.frame_s(payload_len)
        return 0.0 if each <= 0 else (60.0 * duty) / each

    def bundle_saving_s(self, payload_lens):
        """What sending these payloads as one frame saves over sending each.

        On a link with no fixed cost this is the header bytes and nothing else,
        which is real but small. On FM it is a key-up per frame avoided, and
        that is the number that decides whether holding traffic to bundle it is
        worth the latency it costs.
        """
        lens = list(payload_lens)
        if len(lens) < 2:
            return 0.0
        apart = sum(self.frame_s(n) for n in lens)
        together = self.frame_s(sum(lens))
        return apart - together

    def __repr__(self):
        return ("Link(%r, %.0f bps, per_tx=%.0f ms)"
                % (self.name, self.bitrate(), self.per_tx_s * 1000))


def lora(bitrate_bps, fallback_bps):
    """The RNode link. `bitrate_bps` is normally a callable reading .bitrate."""
    return Link("lora", bitrate_bps, per_tx_s=LORA_KEYUP_S,
                fallback_bps=fallback_bps)


def fm(bitrate_bps, per_tx_s=FM_PTT_KEYUP_S, fallback_bps=None):
    """The FM link. Defaults to PTT keying, because VOX is four times the cost
    at best and forty times it through the wrong cable."""
    return Link("fm", bitrate_bps, per_tx_s=per_tx_s,
                fallback_bps=fallback_bps or bitrate_bps)


# AutoInterface's own throughput guess (RNS.Interfaces.AutoInterface.
# BITRATE_GUESS) -- reused rather than re-estimated, so this module's number
# for the link matches what the interface itself would report if asked. No
# key-up: there is no PTT/preamble cost on WiFi the way there is on LoRa/FM,
# so per_tx_s is 0 rather than merely small.
WIFI_KEYUP_S = 0.0
WIFI_BITRATE_GUESS = 10 * 1000 * 1000


def wifi(bitrate_bps=WIFI_BITRATE_GUESS, per_tx_s=WIFI_KEYUP_S, fallback_bps=None):
    """The WiFi link. A fixed, generous bitrate rather than a live reading --
    there is no interface-reported figure to poll the way RNode's is, and the
    airtime model only needs a number that keeps this link from ever looking
    like the bottleneck."""
    return Link("wifi", bitrate_bps, per_tx_s=per_tx_s,
                fallback_bps=fallback_bps or bitrate_bps)
