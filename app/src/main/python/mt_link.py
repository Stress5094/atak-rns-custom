"""The Meshtastic link as RNS sees it: a size limit, an airtime model, and an
Interface that sits on a port.

WHAT THIS IS. A Meshtastic board used as the radio, in place of an RNode. The
app hands it its own already-sealed mesh frames inside a Meshtastic "private
app" packet, and the board puts them on the air. This is NOT interoperability
with other people's Meshtastic traffic -- see radio.py's header, which says the
same about the presets.

THE SAME SHAPE AS fm_link, ON PURPOSE. Like FM, this is a from-scratch
RNS Interface rather than a copy of RNodeInterface (there is no KISS stream and
no radio-config handshake here), it bypasses RNS.Packet (the board is a single
pre-known broadcast channel, so Reticulum's ~19 B header buys nothing), and the
MTU is ASKED of the link rather than assumed from LoRa's 464 B. The rest of the
app's N-way failover then treats it as one more link.

THE SEAM. `MtInterface` never touches a cable or a protocol. It owns one `port`
and nothing about how bytes reach the board:

    port.send(payload) -> bool            put one packet on the air
    port.set_receiver(fn)                 fn(payload, rssi, snr, sender)
    port.set_state_listener(fn)           fn(online: bool)

The thing that implements that port for a real board -- Meshtastic's protobuf
serial protocol, the 0x94 0xC3 stream framing, the want-config handshake -- is
`mt_port.MeshtasticPort`. It rides the OFFICIAL `meshtastic` package rather than
hand-writing the wire format (see mt_port's header for why). Both halves are
checked against a fake board: `MtInterface` against a fake port here in
test_mt_link, and the real `MeshtasticPort` against a fake board that speaks the
real 0x94 0xC3 wire format in test_mt_port.

THE SIZE LIMIT IS MEASURED. 231 B of payload crosses a RAK4631 running
Meshtastic 2.7.15 as a broadcast PRIVATE_APP packet; 232 B comes back
TOO_LARGE (2026-10-13, sizes 226-233, the board's own routing error). That
agrees with the arithmetic -- 255 B of LoRa payload, less a 16 B Meshtastic
header, less ~8 B of protobuf around our bytes -- but the number is the
measurement, not the arithmetic. It was taken for a broadcast with hop limit 0
on a private channel; a direct message with Meshtastic's public-key encryption
spends another 12 B and would carry less. This link does not send those.
Another firmware version may differ, and nothing in the protocol says so.
"""

import math
import threading

import airtime
import radio

from RNS.Interfaces.Interface import Interface as RNSInterface


# Meshtastic's PRIVATE_APP port number: the one reserved for payloads the
# firmware should carry and not interpret.
PORTNUM_PRIVATE_APP = 256

# MEASURED, see the module docstring.
PAYLOAD_MAX_B = 231

# What the board adds around our bytes on the air: a 16 B Meshtastic header plus
# the protobuf wrapper. 255 B is the LoRa payload ceiling, so a full packet is
# exactly 255 B -- which is what makes 231 the limit rather than a coincidence.
# (A payload under 128 B spends one byte less on its length field; charging 24
# for those is a ~1 byte over-estimate, in the safe direction.)
WIRE_OVERHEAD_B = 24
LORA_PAYLOAD_MAX_B = 255

# Meshtastic's preamble length, in symbols. From its firmware defaults, not
# measured here.
PREAMBLE_SYMBOLS = 16

# The board's FIXED delay per leg, on top of airtime -- what reliable.py adds
# (twice: message out, ack back) to its retry timer for this link.
#
# MEASURED 2026-10-14 (tools/mt_latency.py, results/2026-10-14_meshtastic_phase2):
# two boards on one host clock (RAK4631 and Seeed Tracker L1, 2.7.15, Long Fast,
# desk), 320 packets of 16-231 B, 319 delivered. Hand-off-to-delivery fits
# 1.01 s + 1.04 x time_on_air_s(): the airtime formula's per-byte cost is right
# and the board adds about a second -- queueing, its listen-before-talk
# back-off, decryption -- with a long tail (per-leg excess p90 ~1.3-2.5 s by
# direction, worst 4.4 s; the two directions differ).
#
# WHY 1.5. Simulating round trips from those measurements (message one way,
# ack the other; rtt_simulation.txt), the timer as it was -- 2 x airtime + 1 s
# -- fired before the ack in 92% of 30 B round trips and 30% of 231 B ones, so
# almost every short reliable message was sent at least twice on a perfect
# link. At 1.5 s per leg that is 9% (30 B) to 0.5% (231 B); 2.0 s would buy a
# few points more at the cost of slower repair of real loss. Measured on a
# quiet desk channel: a busy one lengthens the board's back-off, so this is a
# floor, not a ceiling.
LINK_LATENCY_S = 1.5


# ---------------------------------------------------------------------------
# Size and airtime. Pure arithmetic, no board and no RNS needed.
# ---------------------------------------------------------------------------

def hw_mtu():
    """The most one packet carries, in bytes of OUR data.

    This is the whole sealed mesh frame. rns_bridge subtracts the mesh envelope
    (mesh.OVERHEAD, 12 B) from whatever a link reports, so the budget for the
    codec is `hw_mtu() - 12`.
    """
    return PAYLOAD_MAX_B


def time_on_air_s(payload_bytes, preset=radio.DEFAULT_PRESET):
    """Seconds one packet carrying `payload_bytes` of ours occupies the channel.

    A MODEL, NOT A MEASUREMENT. The standard Semtech LoRa time-on-air formula
    over the packet the board really builds (our bytes + WIRE_OVERHEAD_B), with
    Meshtastic's 16-symbol preamble, explicit header and CRC on. The formula is
    checked against a published figure in the tests; this board's own airtime
    counter was not read, so the first thing to compare it to is
    `airUtilTx` on the device.
    """
    if payload_bytes < 0:
        raise ValueError("payload_bytes cannot be negative")
    sf, bw_hz, cr_den = radio.PRESETS[preset]
    return _lora_toa_s(payload_bytes + WIRE_OVERHEAD_B, sf, bw_hz, cr_den,
                       PREAMBLE_SYMBOLS)


def _lora_toa_s(total_bytes, sf, bw_hz, cr_den, preamble_symbols,
                explicit_header=True, crc=True):
    t_sym = (2 ** sf) / float(bw_hz)
    # Low-data-rate optimisation turns on once a symbol outlasts 16 ms.
    de = 1 if t_sym > 0.016 else 0
    h = 0 if explicit_header else 1
    cr = cr_den - 4
    numerator = 8 * total_bytes - 4 * sf + 28 + (16 if crc else 0) - 20 * h
    payload_symbols = 8 + max(
        math.ceil(numerator / float(4 * (sf - 2 * de))) * (cr + 4), 0)
    return (preamble_symbols + 4.25 + payload_symbols) * t_sym


def bitrate_bps(preset=radio.DEFAULT_PRESET):
    """What to charge a full packet at, for `airtime.Link` and RNS bookkeeping.

    DERIVED so that airtime.Link(header_bytes=WIRE_OVERHEAD_B) charges a
    full-size packet exactly its modelled time on air -- the same move fm_link
    makes. Unlike FM, airtime here is close to proportional to length (LoRa has
    no fixed padding), so short packets are charged about right; the error is
    the model's, not the shape.
    """
    full_s = time_on_air_s(hw_mtu(), preset)
    return (hw_mtu() + WIRE_OVERHEAD_B) * 8.0 / full_s


def airtime_link(preset=radio.DEFAULT_PRESET):
    """This link's `airtime.Link`. No key-up cost: the board handles its own."""
    return airtime.Link("mesh", bitrate_bps(preset), per_tx_s=0.0,
                        header_bytes=WIRE_OVERHEAD_B)


def train_airtime_s(fragments, preset=radio.DEFAULT_PRESET):
    """Channel time for `fragments` back-to-back full packets -- the real cost
    of a multi-fragment message, which is what the fragment cap is protecting."""
    return fragments * time_on_air_s(hw_mtu(), preset)


# ---------------------------------------------------------------------------
# The RNS-facing half.
# ---------------------------------------------------------------------------

class MtInterface(RNSInterface):
    """Owns one port to a Meshtastic board and nothing about how it is reached.

    process_outgoing() REFUSES an oversize frame itself rather than passing it
    on, because the board's answer to one is an asynchronous TOO_LARGE routing
    error that arrives after the call has already returned success. A refusal
    here is synchronous and counted, so the caller finds out on the call that
    caused it.

    SIGNAL STATS follow the RNode's convention: `r_stat_rssi` / `r_stat_snr` are
    set just before a frame is delivered, which is where rns_bridge reads them.
    Meshtastic reports both per packet, so this link can supply what FM cannot.
    """

    def __init__(self, owner, port, deliver_frame=None, preset=radio.DEFAULT_PRESET):
        super().__init__()
        self.owner = owner
        self.port = port
        self.preset = preset
        self.name = "MeshtasticInterface"
        # Snapshotted: RNS reads these as plain attributes.
        self.HW_MTU = hw_mtu()
        self.bitrate = bitrate_bps(preset)
        self.online = False

        self.r_stat_rssi = None
        self.r_stat_snr = None
        self.last_sender = None
        self.oversize_refused = 0
        self._lock = threading.Lock()

        self._deliver = deliver_frame or owner.inbound
        port.set_state_listener(self._set_online)
        port.set_receiver(self._on_receive)

    # CHANNEL CONDITIONS, under the RNode's attribute names, so the bridge's
    # _channel_conditions() reads a Meshtastic board exactly as it reads an
    # RNode. From the board's own telemetry (mt_port.telemetry), None when it
    # has said nothing for 3 minutes. The windows are the board's, not the
    # RNode's 15 s / 1 h: channel utilisation is Meshtastic's short-term
    # figure, our transmit airtime its last hour. A board with no noise-floor
    # measurement (the RAK4631 and Tracker L1 on 2.7.15) reports None, never 0.

    def _telemetry(self):
        fn = getattr(self.port, "telemetry", None)
        try:
            return fn() if fn is not None else {}
        except Exception:
            return {}

    @property
    def r_channel_load_short(self):
        return self._telemetry().get("channel_util_pct")

    @property
    def r_channel_load_long(self):
        return None

    @property
    def r_airtime_short(self):
        return None

    @property
    def r_airtime_long(self):
        return self._telemetry().get("air_util_tx_pct")

    @property
    def r_noise_floor(self):
        return self._telemetry().get("noise_floor_dbm")

    def set_preset(self, preset):
        """Tell the airtime model which modulation the board is really on.

        The preset lives on the board, not in this app, so it is read off the
        board once connected (MeshtasticPort.board_preset) and passed here. An
        unknown name is ignored rather than raised: a wrong airtime figure is
        a modelling error, and not a reason to take the link down.
        """
        if preset in radio.PRESETS:
            self.preset = preset
            self.bitrate = bitrate_bps(preset)

    def _set_online(self, online):
        self.online = bool(online)

    def _on_receive(self, payload, rssi=None, snr=None, sender=None):
        if not payload:
            return
        with self._lock:
            self.r_stat_rssi = rssi
            self.r_stat_snr = snr
            self.last_sender = sender
        self.process_incoming(payload)

    def process_incoming(self, data):
        if not data:
            return
        self.rxb += len(data)
        self._deliver(data, self)

    def process_outgoing(self, data):
        """True if the port accepted the packet, False otherwise -- the same
        convention as RNS.Packet.send(), so bypass call sites can check it."""
        if not self.online:
            return False
        if len(data) > self.HW_MTU:
            import RNS
            with self._lock:
                self.oversize_refused += 1
            RNS.log("%s refused a %d B frame: the link carries %d B"
                    % (self, len(data), self.HW_MTU), RNS.LOG_ERROR)
            return False
        try:
            ok = self.port.send(bytes(data))
        except OSError as e:
            import RNS
            RNS.log("%s send failed: %s" % (self, e), RNS.LOG_ERROR)
            return False
        if ok:
            self.txb += len(data)
        else:
            import RNS
            RNS.log("%s did not send a %d B frame: %s"
                    % (self, len(data), self.port.last_error), RNS.LOG_NOTICE)
        return bool(ok)

    def __str__(self):
        return "MeshtasticInterface"
