"""The FM link as RNS and Kotlin see it: an Interface, and four entry points.

This is the top of the FM stack and the only module in it that imports RNS.
Everything below -- fm_config, fm_framing, fm_modem, fm_burst, fm_health,
fm_rx -- is deliberately free of RNS and Android, so the whole waveform and
receive path is testable on a desktop against the real libcodec2. That boundary
is worth keeping: it is why Phase 0's measurements could be made at all, and the
reason it survives is that exactly one module is allowed to cross it.

TWO HALVES, AND THEY TALK OVER A SOCKET.

  * The module-level functions (`build_tx_burst`, `feed_audio`, `channel_busy`,
    `hw_mtu`) are called straight from Kotlin's FmAudioBridgeService -- from
    the AudioRecord loop, the AudioTrack path and listen-before-transmit. They
    never go through the interface object.
  * `FmInterface` is the RNS-facing half. It owns no audio and no hardware;
    it reads and writes one loopback socket that Kotlin is on the other end of.

So a frame travels: `process_outgoing` -> `pack_frame` -> socket -> Kotlin
unpacks it -> `build_tx_burst` -> AudioTrack. And back: AudioRecord ->
`feed_audio` -> already-packed frames -> socket -> `FrameParser` ->
`process_incoming`.

REGISTERED WITH RNS.Transport, BUT NOT ROUTED THROUGH RNS.Packet, and this is
load-bearing rather than an optimisation. An RNS.Packet costs ~19 B of fixed
framing (destination hash + flags/hops/context) on top of mesh.py's own 12 B
envelope. The code this link runs carries 29 B in total. Routing a beacon
through RNS.Packet pushed its real floor to ~47 B -- larger than the entire
capacity of any code small enough to be worth using here -- which is how the
very first live FM beacon took the whole link offline on its first send. This
link is a single pre-known broadcast destination with no multi-hop RNS routing
and no RNS-native encryption to lose, so rns_bridge's `_transmit_sealed()`
calls `process_outgoing()` directly and this interface delivers inbound frames
by calling `deliver_frame` directly. It is still a real
RNS.Interfaces.Interface registered with Transport: only the per-message wire
format skips RNS.Packet.

THE MTU IS ARITHMETIC, NOT A GUESS, and that distinction was a live outage.
`hw_mtu()` is computed from fm_config, needs no libcodec2 and cannot fail, so
rns_bridge's `_compute_max_packet()` can ASK this link what it carries instead
of assuming RNS.Packet.PLAIN_MDU (which is LoRa's ~464 B and describes a
different interface entirely). Sizing FM against PLAIN_MDU is what let a 42 B
beacon sail past a 30 B code with no complaint from RNS and only fail at the
modem, taking the link down with it.

WHAT THIS MODULE DOES NOT DO. It does not schedule. One burst is one burst;
whether to send it again minutes later is fm_resend's job and rns_bridge's
wiring, because in-burst repetition and time diversity are different
mechanisms against different failure modes (see fm_burst's docstring).
"""

import struct
import threading
import time

import fm_burst
import fm_carrier
import fm_config
import fm_custom
import fm_modem
import fm_rx

from RNS.Interfaces.Interface import Interface as RNSInterface


# ---------------------------------------------------------------------------
# The loopback socket's own framing.
#
# SOCKET-LOCAL, NEVER TRANSMITTED -- unlike fm_framing's 1-byte prefix, which
# is physically on the air inside the frame. This one exists purely because the
# socket to Kotlin is a raw TCP byte stream: one process_outgoing() call has no
# guaranteed relationship to how many bytes arrive in one read() at the far
# end. A physical FSK-LDPC frame is already self-delimited once the code is
# known, so nothing about this belongs on the radio.
#
# Kotlin's FmAudioBridgeService implements the unpacking half of this (it never
# needs to pack -- feed_audio() below returns bytes already in this format, so
# Kotlin forwards them verbatim). Keep the two in sync by hand if this ever
# changes.
# ---------------------------------------------------------------------------

_FRAME_HEADER = struct.Struct(">H")


def pack_frame(payload):
    """Wrap `payload` with its 2-byte length for the loopback socket."""
    if len(payload) > 0xFFFF:
        raise ValueError(
            "frame too large for a 2-byte length prefix: %d bytes"
            % len(payload))
    return _FRAME_HEADER.pack(len(payload)) + payload


class FrameParser(object):
    """Accumulates socket bytes and yields complete frames as they arrive."""

    def __init__(self):
        self._buf = b""

    def feed(self, chunk):
        self._buf += chunk
        out = []
        while len(self._buf) >= _FRAME_HEADER.size:
            (n,) = _FRAME_HEADER.unpack_from(self._buf)
            end = _FRAME_HEADER.size + n
            if len(self._buf) < end:
                break
            out.append(self._buf[_FRAME_HEADER.size:end])
            self._buf = self._buf[end:]
        return out


# ---------------------------------------------------------------------------
# The operating point in force.
#
# ONE LinkConfig, held in one place, read by both directions. A transmitter and
# a receiver that disagree on the code, the symbol rate or the tone placement
# recover NOTHING -- not a degraded link, a dead one -- and no mechanism on
# this link can report the disagreement, because reporting it would require the
# link to work. So both directions read this rather than capturing numbers of
# their own, and `summary_line()` exists so two operators can compare by eye.
# ---------------------------------------------------------------------------

_CONFIG = [fm_config.OPERATING_POINT]
_CONFIG_LOCK = threading.Lock()


def config():
    """The LinkConfig both directions are running on."""
    return _CONFIG[0]


def set_profile(name, vox=False):
    """Choose a tone placement. Returns (profile_name, substituted).

    `vox=True` is for a cable with NO PTT pin (K1 PRO class): the audio keys
    the radio, so the burst is built with the long VOX lead-in
    (fm_config.VOX_CABLE_LEAD_IN_S) and Kotlin is told keying is "vox", which
    makes it skip the CM108 claim instead of falling back from it. Off, the
    operating point's own CM108 keying and 0.10 s lead-in are untouched --
    that is the Digirig Lite path and this flag must never alter it.

    DOES NOT RAISE ON AN UNKNOWN NAME, because the name arrives from Kotlin as
    a bare SharedPreferences string and a stale preference must not stop the
    bridge starting -- fm_config.resolve_link_config() substitutes the measured
    default and says that it did, which the caller logs.

    RESETS THE RECEIVE PATH. The modem holds its tone placement from
    construction, so without this a profile change under a running process
    would leave the receiver listening on the old tones and simply hearing
    nothing, with nothing in the logs to say why.

    The placement is a per-NET decision: every node on an FM net must be on the
    same one. Nothing on the link itself can detect a mismatch, so the only
    check that exists is an operator comparing `summary_line()` at both ends.
    """
    over = {}
    if vox:
        over = dict(keying=fm_config.KEYING_VOX,
                    lead_in_s=fm_config.VOX_CABLE_LEAD_IN_S,
                    deemphasis_us=fm_config.VOX_CABLE_DEEMPHASIS_US)
    cfg, substituted = fm_config.resolve_link_config(name, **over)
    with _CONFIG_LOCK:
        _CONFIG[0] = cfg
    reset_rx()
    return cfg.profile.name, substituted


def set_trial_profile(name):
    """Switch to a TRIAL placement (fm_config.TRIAL_PROFILES) for an over-the-air
    comparison. Same effect as set_profile -- the config is replaced and the
    receive path reset -- but it RAISES on an unknown name instead of
    substituting, because a trial that quietly ran the default would be scored
    as the thing it was not. Not persistent: nothing is written to preferences."""
    cfg = fm_config.trial_link_config(name)
    with _CONFIG_LOCK:
        _CONFIG[0] = cfg
    reset_rx()
    return cfg.profile.name


def set_custom(spec_json, vox=False):
    """Run on operator-entered settings (fm_custom). Returns
    (profile_name, error): error is "" when the spec was applied.

    A spec that cannot be built FALLS BACK to the measured default preset
    (with the same `vox` choice) rather than raising, for set_profile's
    reason: it arrives from SharedPreferences, and a stale or hand-damaged
    preference must not stop the bridge starting. The error comes back so
    the caller can log it at ERROR -- a node silently on the default is on a
    different net from the one its operator chose.

    Resets the receive path, as set_profile does.
    """
    try:
        cfg = fm_custom.link_config(fm_custom.CustomSpec.from_json(spec_json),
                                    vox=bool(vox))
        error = ""
    except fm_config.ProfileError as e:
        name, _ = set_profile(fm_config.DEFAULT_PROFILE, vox=vox)
        return name, str(e) or "unusable custom FM settings"
    with _CONFIG_LOCK:
        _CONFIG[0] = cfg
    reset_rx()
    return cfg.profile.name, error


def net_code():
    """The settings every node on the net must share, as one short line
    (fm_custom.net_code). Presets have one too, so a custom node and a preset
    node can be compared directly."""
    return fm_custom.net_code(config())


def profile_name():
    return config().profile.name


def profile_is_default():
    return config().profile.name == fm_config.DEFAULT_PROFILE


def summary_line():
    """Every number that has to match at the far end, on one line."""
    return config().summary_line()


def evidence_line():
    """What this placement has actually been measured on -- which, for a
    handheld operator running the desktop-measured default, is the one thing
    the status screen can honestly tell them."""
    cfg = config()
    if cfg.profile.name == fm_custom.CUSTOM_NAME:
        return fm_custom.evidence_text(cfg)
    return cfg.evidence_line()


def budget_lines():
    """Where the airtime goes, itemised. The 4.4x the rebuild recovered came
    from the code size, the repeat count and the lead-in rather than from the
    waveform, and that is invisible while the burst is one number."""
    return config().budget_lines()


def burst_duration_s():
    """How long one physical transmission runs, lead-in included.

    Read by the beacon slot grid: two nodes in adjacent slots must not
    transmit over each other, so the grid is sized from this rather than from
    a constant that silently becomes wrong when the operating point moves.
    """
    return config().burst_duration_s()


# ---------------------------------------------------------------------------
# What Kotlin's audio service has to agree with.
#
# ONE CALL, ANSWERED FROM THE LIVE CONFIG, because every number below used to
# be a second copy in Kotlin and each one of them is silent when it disagrees:
# a sample rate that does not match the profile produces a burst at the wrong
# pitch (nothing decodes, nothing logs), a keying method that does not match
# the cable keys nothing or keys it twice, and a listen-before-transmit bound
# sized against the wrong burst either cuts into a real transmission or holds
# a message for no reason. Phase 4 made the MTU arithmetic for exactly this
# reason; this is the same move for the audio half.
# ---------------------------------------------------------------------------

# Slack on the listen-before-transmit bound, over the burst it is waiting out.
# Covers the poll interval and the chunk cadence on the Kotlin side -- the
# reading can only be as fresh as the last chunk fed to the decoder -- rather
# than any property of the channel.
CHANNEL_BUSY_WAIT_MARGIN_S = 1.0


# The longest random wait a node takes after it has had to DEFER, before it
# listens again and, if the channel is still clear, transmits.
#
# WHY IT EXISTS. Listening only helps if it makes the node wait, and once it
# does, every node that queued behind the same burst is released at the same
# instant -- the end of it -- and keys up together. That swaps "keyed into a
# burst in progress" for "keyed at the same moment as the other waiter", which
# no listener can hear. Spreading the release over a window means the later
# node finds the earlier one already on the air, and the detector hears that
# ~70 ms into its burst.
#
# SIZED AGAINST THE DETECTOR, not against the burst. Two waiters collide only
# if their draws land within the time it takes one to hear the other. MEASURED
# ON TWO PHONES 2026-10-06: 0.68 s from one phone's key-up to the other's
# reading going busy (PTT key-up + lead-in + radio + audio buffering + the
# detector's own ~0.27 s). So for two nodes
#     P(collide) = 1 - (1 - 0.68 / W)^2  = 56% at W = 2 s, 31% at W = 4 s,
# against ~100% with no back-off. (The first version of this comment used 0.3 s,
# the detector's own latency alone, and said 28% -- wrong by half.) Longer is
# better for that and worse for how long a beacon waits. 2 s is a first guess
# that the measurement says is too small; it is left until a longer run shows
# whether start-of-slot overlaps are actually a problem.
BACKOFF_MAX_S = 2.0

# The least this node leaves between the END of one of its bursts and the START
# of its next, plus a random 0..BACKOFF_MAX_S. 2026-10-08, ten minutes of two-way
# traffic: when this phone's resend and beacon timers coincided it sent 2-3
# bursts back to back, runs up to ~7 s, and a node that waited them out (the
# other's hold is 1.0 s, then its own random back-off) found the next burst
# already starting. 1.5 s is longer than the other node's hold, so a polite
# peer gets a turn. A first value, not a measurement.
INTER_BURST_GAP_S = 1.5


def channel_busy_wait_s():
    """How long to defer for a channel that reads busy, before sending anyway.

    DERIVED FROM THE LIVE BURST, not from the duration ceiling. The figure
    this replaces was `MAX_BURST_S + margin` -- the longest burst any
    configuration is ALLOWED, which at this operating point is nearly four
    times the one actually on the air -- and the same substitution is what
    Phase 4 had to make for the beacon slot grid and for
    eligibility.FM_MAX_FRAME_S. Every node on an FM net runs the same
    configuration (a disagreement recovers nothing at all), so the burst this
    node builds is also the longest one it can hear.

    THE RELEASE TERM IS NOT PADDING. The carrier reading averages over its
    last fm_carrier.WINDOW_S, so it goes idle up to that long after the burst
    ends, and a bound of one burst alone would expire while a burst that has
    just finished still reads busy -- and "transmitted anyway" would be logged
    for a channel that was in fact clear. (This was fm_modem.SYNC_DECAY_S,
    0.6 s, for the modem's sync bitmask. That reading is no longer used: it
    went busy only AFTER a burst, held for ~2.6 s, and let a node key into one
    in progress.)

    THE BACK-OFF IS INSIDE THE BOUND. A node that defers waits up to
    BACKOFF_MAX_S more after the channel clears, so the bound has to leave room
    for it or a node would give up and "transmit anyway" in the middle of its
    own politeness.

    IT CAN STILL EXPIRE INSIDE A BACK-TO-BACK FRAGMENT TRAIN, and that is
    deliberate rather than overlooked. A 16-fragment message is 16 bursts, and
    a bound long enough to wait that out would hold an outgoing message for
    most of a minute. The rule this implements is the one worth implementing
    -- never key up into a burst that is audible right now, because that
    destroys it -- and a collision in the gap between two bursts of a train is
    what the in-burst copies and fm_resend's time diversity exist to absorb.
    """
    return (burst_duration_s() + fm_carrier.RELEASE_S
            + CHANNEL_BUSY_WAIT_MARGIN_S + BACKOFF_MAX_S)


def audio_plan():
    """Everything Kotlin's FmAudioBridgeService needs, as a JSON string.

    A JSON STRING rather than a dict, matching rns_bridge.get_status(): a
    single string crossing the Chaquopy boundary is trivially debuggable from
    either side, where nested conversions are not.

    ASKED ONCE PER LINK OPEN, not per burst -- nothing in here changes without
    a profile change, and a profile change restarts the process (see
    RadioLink.pendingRestart() for why that is deliberate rather than lazy).
    """
    import json
    cfg = config()
    return json.dumps({
        "sample_rate_hz": cfg.profile.sample_rate_hz,
        # "cm108" or "vox". The service keys GPIO for the first and plays into
        # a silent transmitter for the second, so getting this from the config
        # is what keeps the lead-in and the keying from disagreeing -- they are
        # one decision in fm_config (see KEYING_LEAD_IN_S) and were two
        # constants in two languages before this.
        "keying": cfg.keying,
        "lead_in_s": cfg.lead_in_s,
        "burst_s": cfg.burst_duration_s(),
        # What to wait after asserting PTT before playing, for the keying
        # method in force. Zero under VOX, where the audio IS the keying and
        # waiting would transmit nothing.
        "ptt_keyup_s": _ptt_keyup_s(cfg),
        "channel_busy_wait_s": channel_busy_wait_s(),
        "backoff_max_s": BACKOFF_MAX_S,
        "inter_burst_gap_s": INTER_BURST_GAP_S,
        "capacity_bytes": cfg.capacity_bytes(),
        "profile": cfg.profile.name,
        "net_code": fm_custom.net_code(cfg),
        "summary": cfg.summary_line(),
    })


def _ptt_keyup_s(cfg):
    """How long after asserting PTT the carrier is actually up.

    CHARGED AS A WAIT BEFORE THE AUDIO STARTS, not absorbed by the lead-in,
    and the bench is why: tools/fm_bench_audio puts LEAD_SILENCE_S (0.30 s) of
    digital silence in front of every burst it plays precisely because
    "anything transmitted before then is simply gone", and the 178/180 figure
    the operating point rests on was measured that way. Playing the instant
    PTT is asserted would instead eat 0.055 s of a 0.10 s lead-in tone,
    leaving the receiver's AGC half the settling time the configuration
    budgets for it.

    IT MAKES THE AIRTIME MODEL OPTIMISTIC BY THAT MUCH. `bitrate_bps()` is
    solved so a full frame costs exactly `burst_duration_s()`, so a real
    transmission is burst + key-up -- about 2% over the model at this
    operating point, in the same direction as (and far smaller than) the ~2x
    the flat-cost note in `bitrate_bps` already states.

    ZERO UNDER VOX. There is no PTT to assert: the audio trips the
    transmitter, which is what the much longer VOX lead-in buys, and waiting
    before playing would simply delay the thing doing the keying.
    """
    import airtime
    if cfg.keying == fm_config.KEYING_VOX:
        return 0.0
    return airtime.FM_PTT_KEYUP_S


def hw_mtu():
    """Real content one burst carries -- this link's MTU.

    PURE ARITHMETIC. No modem, no libcodec2, no native_lib_dir, so this cannot
    fail and can be asked at any point in startup. That is what lets
    `_compute_max_packet()` ask the link instead of assuming LoRa's budget.
    """
    return config().capacity_bytes()


def bitrate_bps():
    """What to charge a full frame at, for `airtime.fm()` and for RNS's own
    bookkeeping.

    DERIVED SO THAT A FULL-CAPACITY FRAME COSTS THE REAL BURST. airtime.Link
    charges `per_tx_s + (payload + 19) * 8 / bitrate`, so solving that for the
    measured burst duration at full capacity is the one point where the
    payload-proportional model and this link agree exactly.

    AND IT IS A MODEL, NOT THE TRUTH, in a direction worth stating. On this
    link airtime is FLAT: every frame is padded to the code's full payload
    before modulation, so a 6 B frame and a 29 B frame both cost the same
    2.35 s burst. The proportional model therefore UNDER-charges short frames
    -- by roughly 2x at the shortest -- which means an airtime budget built on
    it believes there is more channel left than there is. Tolerable because
    nothing on FM relays or fragments (see eligibility.fm_gate) and the traffic
    is nearly all full-size beacons, but a budget that ever starts gating real
    traffic here wants a flat-cost model instead of a bigger number.
    """
    import airtime
    cfg = config()
    bits = (cfg.capacity_bytes() + airtime.RNS_HEADER_BYTES) * 8.0
    carrying = cfg.burst_duration_s() - airtime.FM_PTT_KEYUP_S
    if carrying <= 0:
        # Degenerate only for a configuration whose whole burst is shorter
        # than one key-up, which the duration ceiling has no reason to refuse.
        # Charge the whole burst to the bits rather than return something
        # non-positive, which airtime.Link rejects outright.
        carrying = cfg.burst_duration_s()
    return bits / carrying


# ---------------------------------------------------------------------------
# The native library.
# ---------------------------------------------------------------------------

def _prepare_native(native_lib_dir):
    """Point fm_modem at the APK's own libcodec2 copy.

    On Android the bundled arm64 .so sits in the app's nativeLibraryDir, which
    nothing find_library() tries will resolve through the app's linker
    namespace. On a desktop `native_lib_dir` is empty and this is a no-op, so
    the same call site works on both.
    """
    if not native_lib_dir:
        return
    import os
    os.environ["RTAK_CODEC2_LIB"] = os.path.join(native_lib_dir,
                                                 "libcodec2.so")


# ---------------------------------------------------------------------------
# Transmit.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# IS THE AUDIO SIDE ACTUALLY THERE?
#
# The loopback socket says only that Kotlin's service is running, not that it
# has a sound card. On 2026-10-23 the Pixel's Digirig half-enumerated (serial
# chip, no CM108) for a whole 15-minute range test: Python connected, wrote a
# beacon, the service dropped the connection for want of a link, and Python
# reconnected 5 s later -- so the interface read online, and 22 beacons were
# logged "sent" with 0 key-ups. Audio arriving here is the direct evidence: the
# recorder feeds feed_audio() continuously while the card is open, and pauses
# only while this node transmits, when build_tx_burst() is called instead.
# ---------------------------------------------------------------------------

AUDIO_ALIVE_S = 10.0
_last_audio = [None]


def _note_audio():
    _last_audio[0] = time.monotonic()


def audio_alive(max_age_s=AUDIO_ALIVE_S, now=None):
    """True while Kotlin has passed us received audio, or a burst to send,
    within the last `max_age_s` seconds."""
    last = _last_audio[0]
    now = time.monotonic() if now is None else now
    return last is not None and now - last <= max_age_s


def build_tx_burst(native_lib_dir, payload):
    """One payload -> raw PCM16LE mono samples for AudioTrack.

    Called from Kotlin once it has pulled one complete frame off the socket.

    `bytes(payload)` FIRST, AND IT IS NOT DEFENSIVE. Chaquopy hands a
    Kotlin-originated argument over as a raw jarray('B') proxy rather than a
    Python bytes object, and jarray's own `__radd__` boxes each element with
    Java's SIGNED byte semantics -- so concatenating it produces a list of ints
    where every byte >= 0x80 has gone negative. The failure surfaces far
    downstream as "byte must be in range(0, 256)", and only ever for real
    traffic: every test and bench payload is built in Python and already
    correct. Going through the buffer protocol here is zero-copy and genuinely
    unsigned.

    A MODEM PER BURST, deliberately, unlike the receive path's one persistent
    instance. There is no state to carry between bursts on this side -- each
    modulation is self-contained -- and a cached transmit handle would be a
    native object held for the life of the process for no gain.
    """
    _prepare_native(native_lib_dir)
    payload = bytes(payload)
    cfg = config()
    _note_audio()
    with fm_modem.Codec2Fsk(cfg.profile, codename=cfg.codename) as modem:
        samples = fm_burst.build(modem, payload, cfg)
    return fm_burst.to_pcm16(samples)


# ---------------------------------------------------------------------------
# Receive.
#
# ONE fm_rx.FmRx FOR THE LIFE OF THE PROCESS, not one per chunk. codec2's
# demodulator carries its own sync state, the de-emphasis filter carries its
# accumulator, and fm_health carries the level history -- rebuilding any of
# them per chunk of audio gives a link that never acquires sync at all, which
# presents as dead rather than slow. Module-level rather than owned by
# FmInterface because Kotlin's audio loop calls feed_audio() directly and
# never touches the interface object.
# ---------------------------------------------------------------------------

_RX = [None]
_RX_LOCK = threading.Lock()
# Held for the whole of a feed_audio() call and around the close in reset_rx().
# Without it a profile switch frees the native modem (freedv_close) while the
# audio thread is still inside it -- a use-after-free that took the Pixel's app
# down with SIGSEGV in libcodec2 one second after a test switched profiles
# (2026-10-12, thread fm-audio-rx). set_profile() at startup is not ordered
# against the audio service either, so the same race was possible there.
_FEED_LOCK = threading.RLock()


def _rx(native_lib_dir):
    with _RX_LOCK:
        if _RX[0] is None:
            _prepare_native(native_lib_dir)
            cfg = config()
            modem = fm_modem.Codec2Fsk(cfg.profile, codename=cfg.codename)
            _RX[0] = fm_rx.FmRx(modem, cfg)
        return _RX[0]


def reset_rx():
    """Drop the receive path so the next feed_audio() builds a fresh one.

    Called when the operating point changes under a running process. Closing is
    best-effort: a modem that cannot be closed still has to be dropped, or the
    link stays mistuned for the life of the process -- a worse outcome than a
    leaked native handle.
    """
    with _RX_LOCK:
        rx, _RX[0] = _RX[0], None
    if rx is not None:
        # Waits for a feed_audio() already inside the old modem to finish; the
        # next call builds a fresh one.
        with _FEED_LOCK:
            try:
                rx.modem.close()
            except Exception:
                pass


def feed_audio(native_lib_dir, pcm_bytes):
    """Called from Kotlin's continuously-running AudioRecord loop.

    Returns zero or more decoded payloads, each already wrapped with
    pack_frame(), concatenated -- so Kotlin forwards the bytes verbatim to
    deliverFromLink() with no framing logic of its own on this side.

    Returning nothing is the NORMAL case: a chunk is milliseconds and a burst
    is seconds.
    """
    _note_audio()
    _tune_feed(pcm_bytes)
    with _FEED_LOCK:
        rx = _rx(native_lib_dir)
        out = b"".join(pack_frame(c) for c in rx.feed_pcm(pcm_bytes))
        _log_burst_verdicts(rx)
    return out


# ---------------------------------------------------------------------------
# The tone sweep (fm_tune): one phone sends it, the other listens.
#
# SENT THROUGH THE BENCH PLAY HOOK (FmAudioBridgeService.playBenchRequests):
# the same keying, key-up wait, AudioTrack and cable as a real burst, with no
# audio code of its own. LISTENED TO here, in feed_audio's stream, which is
# exactly what the decoder hears -- after Android's capture, before anything
# else. Analysed on a thread so the audio loop never waits on it.
# ---------------------------------------------------------------------------

SWEEP_FILE = "fm_tune_sweep.raw"
PLAY_REQUEST = "fm_play_req"
_TUNE = {"state": "idle", "want": 0, "buf": bytearray(), "result": None,
         "started": 0.0, "tx_before": None}
_TUNE_LOCK = threading.Lock()


def tune_send(home=None):
    """Queue the sweep for the audio service to play, keyed. Returns its
    length in seconds. `home` is the app's files directory (Chaquopy's ~)."""
    import os
    import fm_tune
    home = home or os.path.expanduser("~")
    with open(os.path.join(home, SWEEP_FILE), "wb") as fh:
        fh.write(fm_tune.sweep_pcm16())
    # Appended, not overwritten: a request already queued is not lost.
    with open(os.path.join(home, PLAY_REQUEST), "a") as fh:
        fh.write("%s %d 0\n" % (SWEEP_FILE, fm_tune.FS))
    return fm_tune.SWEEP_S


def tune_listen(seconds):
    """Start recording what this phone hears, for `seconds`, then analyse."""
    import fm_tune
    seconds = max(float(seconds), fm_tune.SWEEP_S + 1.0)
    with _TUNE_LOCK:
        _TUNE.update(state="listening", buf=bytearray(), result=None,
                     want=int(seconds * fm_tune.FS) * 2, started=time.time(),
                     tx_before=_tx_count())
    return seconds


def _tx_count():
    got = recent_tx(3600.0)
    return None if got is None else got[0]


def _tune_feed(pcm_bytes):
    """Append a chunk to a listen in progress.

    bytes() FIRST: from Kotlin this is a Chaquopy jarray of SIGNED Java bytes,
    and appending it directly copies -128..127 as ints, which fails only when
    the recording is complete (2026-10-25, OnePlus: "bytes must be in
    range(0, 256)", the listen stuck in "analysing"). bytes() is how
    fm_burst.from_pcm16 already reads the same object.

    NEVER RAISES into feed_audio: that failure also cost the decoder a chunk.
    A listen that breaks ends with its error; the receive path carries on.
    """
    if _TUNE["state"] != "listening":
        return
    try:
        chunk = bytes(pcm_bytes)
        with _TUNE_LOCK:
            if _TUNE["state"] != "listening":
                return
            _TUNE["buf"] += chunk
            if len(_TUNE["buf"]) < _TUNE["want"]:
                return
            _TUNE["state"] = "analysing"
            pcm = bytes(_TUNE["buf"])
        threading.Thread(target=_tune_analyse, args=(pcm,), daemon=True,
                         name="fm-tune").start()
    except Exception as e:
        with _TUNE_LOCK:
            _TUNE.update(state="done", buf=bytearray(),
                         result={"found": False,
                                 "error": "The recording failed: %s: %s"
                                          % (type(e).__name__, e)})


def _tune_analyse(pcm):
    import fm_tune
    result = fm_tune.analyse_and_recommend(pcm, keying=config().keying)
    # Holes: the receive loop is not fed while this phone transmits, so its
    # own burst during the recording would splice the audio and misplace
    # every tone after it. Said, not hidden.
    after = _tx_count()
    if (_TUNE["tx_before"] is not None and after is not None
            and after > _TUNE["tx_before"]):
        result.setdefault("notes", []).insert(
            0, "This phone transmitted during the recording, which leaves a "
               "hole in it; measure again if the result looks wrong.")
    with _TUNE_LOCK:
        _TUNE.update(state="done", result=result, buf=bytearray())


def tune_status():
    """JSON: state (idle/listening/analysing/done), seconds heard so far, and
    the result once done."""
    import json
    import fm_tune
    with _TUNE_LOCK:
        heard = len(_TUNE["buf"]) / 2.0 / fm_tune.FS
        return json.dumps({"state": _TUNE["state"],
                           "heard_s": round(heard, 1),
                           "want_s": round(_TUNE["want"] / 2.0 / fm_tune.FS, 1),
                           "result": _TUNE["result"]})


def tune_cancel():
    with _TUNE_LOCK:
        _TUNE.update(state="idle", buf=bytearray(), result=None)


def _log_burst_verdicts(rx):
    """One NOTICE line per burst heard, saying how it ended (see fm_airlog).

    A logging failure must never cost the audio path a chunk, and RNS may not be
    importable under a test, so every way this can fail is swallowed here."""
    try:
        lines = rx.pop_events()
        if not lines:
            return
        import RNS
        for line in lines:
            RNS.log(line, RNS.LOG_NOTICE)
    except Exception:
        pass


def recent_tx(window_s):
    """(times, seconds on air) this node transmitted in the last `window_s`, or
    None if no receive path exists yet. Takes no native_lib_dir because it only
    READS the existing instance -- it must never build one."""
    rx = _RX[0]
    if rx is None:
        return None
    try:
        return rx.tx_within(window_s)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Station ID (fm_callsign): the callsign as audible Morse, sent with NO network
# key and NO modem, so any listener can read it. Kotlin's audio service asks
# for it on its own timer and plays it like a burst (LBT, PTT, key-up wait).
# ---------------------------------------------------------------------------

# Steady tone ahead of the Morse when the radio is keyed by VOX: the radio
# switches on 0.3-0.7 s after audio starts (see fm_config.VOX_CABLE_LEAD_IN_S's
# notes), and a second covers that with margin without costing much airtime.
CALLSIGN_VOX_LEAD_S = 1.0


def callsign_id_pcm(callsign):
    """Mono PCM16LE of `callsign` in Morse at the link's sample rate, or None
    when the callsign is blank or not valid (fm_callsign.normalize)."""
    import fm_callsign
    call = fm_callsign.normalize(callsign)
    if call is None:
        return None
    cfg = config()
    lead = CALLSIGN_VOX_LEAD_S if cfg.keying == fm_config.KEYING_VOX else 0.0
    return fm_callsign.pcm16(call, cfg.profile.sample_rate_hz, lead_s=lead)


def callsign_valid(callsign):
    """For the settings field: True when `callsign` would be sent."""
    import fm_callsign
    return fm_callsign.normalize(callsign) is not None


def note_tx_state(native_lib_dir, active):
    """Kotlin reports that this node started or stopped transmitting.

    The receive loop is deliberately not fed while the radio keys up, so from
    the receive path a burst that arrived during our own transmit looks
    identical to one the channel lost. This is what tells them apart."""
    _rx(native_lib_dir).note_tx(bool(active))


def carrier_level(native_lib_dir):
    """The carrier detector's current statistic, for a log line. Idle audio
    should read ~0.52-0.63 and a burst 0.97+; see fm_carrier. Exists so the
    phone's own idle noise can be compared with the desktop capture the
    threshold was set from."""
    return float(_rx(native_lib_dir).carrier.level)


def carrier_detail(native_lib_dir):
    """The detector's level, tracked idle floor and busy threshold as one log
    fragment. Offline field logs need all three: a level alone cannot say
    whether a "clear" at key-up was a quiet channel or a threshold that had
    drifted above a weak burst."""
    c = _rx(native_lib_dir).carrier
    return "level=%.3f floor=%.3f thr=%.3f" % (c.level, c.floor, c.threshold)


def channel_busy(native_lib_dir):
    """Is the demodulator currently seeing real FSK traffic?

    For listen-before-transmit, reading the SAME persistent instance
    feed_audio() has been running -- no second detector to keep in sync with
    the real one, and no extra audio processing.

    NOT AN ENERGY CHECK, and that is measured rather than stylistic: a raw
    discriminator tap on this hardware sits at RMS 0.65-0.75 with zero
    traffic, so no threshold can discriminate. See fm_modem.Modem.has_sync.
    """
    return _rx(native_lib_dir).channel_busy()


def health_snapshot():
    """The receive path's observations, JSON-ready, or None if no audio has
    ever been fed. None rather than a zeroed reading, so a status screen can
    tell "nothing has arrived" from "no audio path exists"."""
    rx = _RX[0]
    return rx.snapshot() if rx is not None else None


def status_lines():
    """What to show an operator, most actionable first. The level line leads,
    because the volume knob is the gain stage on an FM audio link and is the
    one thing on the screen they can act on."""
    rx = _RX[0]
    lines = list(rx.summary_lines()) if rx is not None else []
    lines.append(summary_line())
    lines.append(evidence_line())
    return lines


# ---------------------------------------------------------------------------
# The RNS-facing half.
# ---------------------------------------------------------------------------

class FmInterface(RNSInterface):
    """Owns one loopback socket to Kotlin's FmAudioBridgeService, and nothing
    about audio or hardware.

    Modelled on RNodeInterface's process_incoming/process_outgoing shape (see
    rns_bridge's `_build_rnode_interface`), simplified: there is no KISS byte
    stream and no radio-config handshake on this link, which is exactly why
    subclassing RNodeInterface would inherit machinery that cannot apply.

    RECONNECTS ON ITS OWN if the socket dies, mirroring RNodeInterface's
    open_port()/reconnect_port() pattern: a write OSError, or the read loop
    seeing the socket close, flips `online` False and starts a background retry
    loop that asks `reconnect` for a fresh connected socket, swaps it in,
    resets the frame parser and restarts the reader. `reconnect` is optional --
    None for tests that build this against a bare socket pair with no Kotlin
    bridge behind it.
    """

    RECONNECT_WAIT_S = 5.0
    _audio_alive = None   # class default: RNS's __init__ sets online first

    def __init__(self, owner, sock, native_lib_dir, deliver_frame=None,
                 reconnect=None, audio_alive=None):
        super().__init__()
        # `online` is the socket's state AND, when given, whether the audio
        # side is really there (see audio_alive above). Optional so a test can
        # drive this over a bare socket pair with no recorder behind it.
        self._audio_alive = audio_alive
        self.owner = owner
        self.sock = sock
        self.native_lib_dir = native_lib_dir
        self._reconnect = reconnect
        self._reconnecting = False
        self._reconnect_lock = threading.Lock()
        self.name = "FmAudioInterface"
        # Snapshotted rather than read live, because RNS reads it as a plain
        # attribute. Safe because the operating point is chosen by
        # set_profile() before start(), which is before this exists -- and if
        # that ever stops being true, the live answer is hw_mtu().
        self.HW_MTU = hw_mtu()
        self.bitrate = bitrate_bps()
        self.online = True

        # Defaults to the normal RNS-routed path (owner.inbound, i.e.
        # RNS.Transport.inbound -> Packet.unpack -> destination callback), but
        # rns_bridge overrides it with _on_rns_packet_received directly -- see
        # this module's header on the ~19 B that skips.
        self._deliver = deliver_frame or owner.inbound

        self._parser = FrameParser()
        self._reader = threading.Thread(
            target=self._read_loop, name="fm-link-read", daemon=True)
        self._reader.start()

    @property
    def online(self):
        return bool(getattr(self, "_sock_online", False)) and (
            self._audio_alive is None or self._audio_alive())

    @online.setter
    def online(self, value):
        self._sock_online = value

    def process_incoming(self, data):
        if not data:
            return
        self.rxb += len(data)
        self._deliver(data, self)

    def process_outgoing(self, data):
        """Returns True on success, False otherwise -- mirroring
        RNS.Packet.send()'s own convention, so rns_bridge's bypass call sites
        can check the result the way they would check a normal send."""
        if not self.online:
            return False
        my_sock = self.sock
        try:
            my_sock.sendall(pack_frame(data))
            self.txb += len(data)
            return True
        except OSError as e:
            import RNS
            RNS.log("%s write failed: %s -- reconnecting" % (self, e),
                    RNS.LOG_ERROR)
            # ONLY THIS SOCKET'S OWN FAILURE takes the link offline. If a
            # reconnect already swapped in a fresh self.sock between the
            # attempt above and this check, my_sock is a stale, superseded
            # connection and its failure says nothing about the new one.
            if self.sock is my_sock:
                self.online = False
                self._start_reconnect()
            return False

    def _read_loop(self):
        import RNS
        my_sock = self.sock
        try:
            while True:
                chunk = my_sock.recv(4096)
                if not chunk:
                    break
                for frame in self._parser.feed(chunk):
                    self.process_incoming(frame)
        except OSError as e:
            RNS.log("%s read loop ended: %s -- reconnecting" % (self, e),
                    RNS.LOG_WARNING)
        finally:
            # Same stale-socket guard as process_outgoing: a reader thread that
            # outlives its own socket (blocked in recv() on a connection a
            # write failure already replaced) must not undo the reconnect that
            # already happened out from under it.
            if self.sock is my_sock:
                self.online = False
                self._start_reconnect()

    def _start_reconnect(self):
        """Start the retry loop, unless one is already running (a write failure
        and the read loop dying from the same drop both get here) or this
        instance has no reconnect callable at all."""
        if self._reconnect is None:
            return
        with self._reconnect_lock:
            if self._reconnecting:
                return
            self._reconnecting = True
        threading.Thread(target=self._reconnect_loop,
                         name="fm-link-reconnect", daemon=True).start()

    def _reconnect_loop(self):
        import RNS
        while True:
            time.sleep(self.RECONNECT_WAIT_S)
            try:
                new_sock = self._reconnect()
            except Exception as e:
                RNS.log("%s reconnect attempt failed: %s" % (self, e),
                        RNS.LOG_WARNING)
                continue
            old_sock = self.sock
            try:
                old_sock.close()
            except Exception:
                pass
            self.sock = new_sock
            self._parser = FrameParser()
            with self._reconnect_lock:
                self._reconnecting = False
            self.online = True
            RNS.log("%s reconnected" % self, RNS.LOG_NOTICE)
            self._reader = threading.Thread(
                target=self._read_loop, name="fm-link-read", daemon=True)
            self._reader.start()
            return

    def __str__(self):
        return "FmAudioInterface"
