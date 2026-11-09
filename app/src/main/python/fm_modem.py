"""Bytes to audio and back, behind an interface the waveform can be swapped
under.

WHY THERE IS AN INTERFACE HERE AT ALL. The waveform question is open. The one
thing measured to deliver byte-perfect frames over these radios is codec2's
FSK-LDPC raw-data mode, and `Codec2Fsk` below is that. But the bench record
also contains a from-scratch Bell 202 AFSK prototype that was built, debugged
and tested, and the only reason judging it was hard is that it was a separate
program rather than a drop-in -- there was no way to run the real framing, the
real burst shape and the real duration ceiling against a different modulator.
So: one small interface, and the rest of the transport written against it.

WHAT A MODEM MUST SUPPLY. Not just modulation. codec2's framed mode provides
four things, and a replacement needs all four or it is not a substitute:

    1. a unique word, to find the start of a burst in noise
    2. forward error correction
    3. a CRC, so a corrupt frame is dropped rather than delivered
    4. preamble/postamble generation

The AFSK prototype had none of 2-4, which is most of why "structurally
different demodulator" did not beat codec2 on the weak leg. codec2's own LOW
level entry points (fsk_mod/fsk_demod) hand over loose bits and leave all four
as an exercise; using them would throw away the entire advantage.

THE LESSONS THAT ARE CODE, NOT COMMENTS. Four things in here were each paid
for with real bench time, and each looks like a modem that does not work:

  * The lead-in tone is built in the modulator's own integer scale, not in
    floats. `modulate_burst` normalises the whole burst once at the end, so a
    float lead-in gets divided by the integer peak along with everything else
    and arrives at 1/16384 amplitude -- present in the samples, inaudible to
    VOX, indistinguishable from having no lead-in. That cost a full sweep that
    looked like a modem failure.
  * Repeated frames share ONE preamble. Concatenating complete bursts loses
    frames from the MIDDLE of the sequence however much silence separates
    them: the sync state machine was not built to re-acquire between two
    bursts it was never told were related.
  * `demodulate` feeds silence after the burst. The demodulator accumulates
    internally and emits a frame only once fed past the end of it, so a burst
    handed over exactly is a burst whose last frame never comes out. Short
    bursts decode while long ones silently do not, which looks exactly like a
    bad configuration. It costs no airtime -- this is silence fed to a
    decoder, not transmitted -- but it does mean a frame surfaces about one
    burst-length after the burst ends, which is real latency.
  * `has_sync` reads the rx-status bitmask, not the obvious sync call, which
    returns 0 throughout for this API path and is very likely voice-mode-only
    in this build. Measured directly against a real burst at the instant it
    decoded.

See docs/fm-lessons.md for the measurements behind all of it.

LOADED LAZILY, deliberately. The previous modem resolved libcodec2 at module
import, so merely importing it required the native library -- which meant
every test module that touched the transport needed either the library or a
stub, and the stubs drifted. Here the library is opened on first construction,
so fm_config/fm_framing/fm_burst stay importable and testable anywhere.
"""

import ctypes
import ctypes.util
import math
import os

import fm_config



# HOP_SEGMENT_S: how long a HOPPING lead-in holds each tone. Measured 2026-10-08
# (Pixel 7 receiving, K1 PRO cable + Baofeng Mini both ends, VOX): with the
# plain steady tone the received level fell from ~0.19 to ~0.02 about 0.75 s
# into the lead-in, stayed down ~1 s, and came back ~0.25 s late on the first
# data -- swallowing the start of the preamble the receiver locks on to. 0 of 3
# bursts decoded off the phone's own raw recording. The same sender with a
# lead-in hopping through all four tones decoded 3 of 3 from the recording. A
# steady tone is the kind of signal a leveling/noise-gate stage turns down;
# a changing one is not. 40 ms was a first guess, not tuned.
HOP_SEGMENT_S = 0.04

class ModemError(RuntimeError):
    """The modem could not be built or driven."""


class Modem(object):
    """What the rest of the FM transport is allowed to assume about a
    waveform.

    A replacement implements these; nothing above this file should reach past
    them. Deliberately small -- every method here is one the transport
    genuinely cannot do without, and anything waveform-specific (tone
    placement, code rate, interleaving) belongs in the implementation and its
    Profile.
    """

    #: Bytes of payload one physical frame carries, EXCLUDING any trailing
    #: CRC the waveform appends for itself. This is the number fm_framing
    #: sizes a frame to; getting the CRC in or out of it is an off-by-two
    #: that works for small payloads and corrupts large ones.
    frame_payload_bytes = 0

    #: Audio sample rate, Hz.
    sample_rate_hz = 0

    def modulate_burst(self, frames, lead_in_s=0.0, hop_lead_in=False):
        """One physical transmission carrying `frames`, sharing a single
        preamble/postamble. Returns float samples scaled to +/-1.0."""
        raise NotImplementedError

    def feed(self, samples):
        """Push float samples from a continuous receive stream. Returns
        whatever frames completed during this call -- usually []."""
        raise NotImplementedError

    def demodulate(self, samples):
        """Decode one finite, already-captured burst. Returns a list of
        frames, possibly empty. Empty is a NORMAL result: it is what a
        survival curve is made of."""
        raise NotImplementedError

    def has_sync(self):
        """True if the demodulator currently has, or very recently had, sync
        on real traffic.

        For listen-before-transmit. Must NOT be an energy measurement: this
        project's receive audio sits at a constantly loud RMS (~0.65-0.75)
        with zero real traffic, so no threshold can discriminate. Asking the
        decoder works because discrimination is its whole job.
        """
        raise NotImplementedError

    def burst_duration_s(self, frames, lead_in_s=0.0):
        """How long a burst of `frames` frames will run, without building it.

        Needed so the duration ceiling (fm_config.MAX_BURST_S) can be checked
        when CHOOSING a configuration, not only after modulating one. A
        default implementation could modulate and measure; implementations
        that can compute it should, because the bench sweeps call this far
        more often than they transmit.
        """
        raise NotImplementedError

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------------
# codec2 FSK-LDPC
# ---------------------------------------------------------------------------

FREEDV_MODE_FSK_LDPC = 9

# Peak sample value the modulator produces, from freedv_api.h. Used to convert
# between codec2's int16 world and the float samples everything above here
# works in. Getting it wrong silently scales the signal and every SNR figure
# with it.
FREEDV_PEAK = 16384.0

# How long the modem's rx-status stays non-zero after a frame decodes.
#
# NO LONGER READ BY LISTEN-BEFORE-TRANSMIT, and 0.6 s is only right for short
# frames. Measured 2026-10-03 on the 0.5-0.6 s of a burst-sized frame, then
# re-measured 2026-10-06 against the operating point's 2.95 s burst: the status
# is idle for the whole burst, goes busy when the 2.6 s frame decodes, and holds
# ~2.6 s -- about one frame length. A listener built on it hears nothing while a
# burst is in progress. fm_carrier.CarrierDetector is what answers "is anyone
# transmitting right now"; this stays for what it was measured as, a property of
# the modem's own sync signal.
SYNC_DECAY_S = 0.6

# The code table, the CRC convention and the default code all live in
# fm_config now, re-exported here because this module's callers think in terms
# of a modem.
#
# WHY THEY MOVED. Burst duration is the binding constraint on this link, and it
# is a joint function of the code, the symbol rate, the tone count, the repeat
# count and the lead-in. While the code table lived here, the only way to ask
# "does this configuration fit under the ceiling" was to construct a native
# modem -- which meant the question could not be asked when a configuration was
# CHOSEN, only after one had been built. fm_config.LinkConfig now answers it in
# pure arithmetic, and this module's native answer is the cross-check (see
# burst_duration_s below, and the test that asserts the two agree).
CODES = fm_config.CODES
CRC_BYTES = fm_config.CRC_BYTES
DEFAULT_CODE = fm_config.DEFAULT_CODE


def payload_bytes_for(codename):
    """Usable payload for a code, without constructing a modem or needing the
    native library. Cheap enough for the bridge's MTU calculation."""
    try:
        return fm_config.code_bytes(codename) - CRC_BYTES
    except fm_config.ProfileError as e:
        # Re-raised as a ModemError because every caller of this function is
        # holding a modem-shaped problem, and because the previous signature
        # promised one.
        raise ModemError(str(e))


class _Advanced(ctypes.Structure):
    _fields_ = [
        ("interleave_frames", ctypes.c_int),
        ("M", ctypes.c_int),
        ("Rs", ctypes.c_int),
        ("Fs", ctypes.c_int),
        ("first_tone", ctypes.c_int),
        ("tone_spacing", ctypes.c_int),
        ("codename", ctypes.c_char_p),
    ]


_LIB = [None]


def _lib():
    """Open libcodec2 once, on first use.

    RTAK_CODEC2_LIB overrides the search entirely -- needed on Android, where
    the bundled arm64 .so sits in the app's native lib directory rather than
    anywhere find_library() or a bare "libcodec2.so" resolves through the
    app's linker namespace. Unset on a desktop, where this is a no-op.
    """
    if _LIB[0] is not None:
        return _LIB[0]
    path = (os.environ.get("RTAK_CODEC2_LIB")
            or ctypes.util.find_library("codec2") or "libcodec2.so")
    try:
        lib = ctypes.CDLL(path)
    except OSError as e:
        raise ModemError(
            "could not load libcodec2 from %r: %s. On Android set "
            "RTAK_CODEC2_LIB to the APK's own nativeLibraryDir copy; on a "
            "desktop bench install libcodec2." % (path, e))

    lib.freedv_open_advanced.restype = ctypes.c_void_p
    lib.freedv_open_advanced.argtypes = [ctypes.c_int,
                                         ctypes.POINTER(_Advanced)]
    lib.freedv_close.argtypes = [ctypes.c_void_p]
    for name in ("freedv_nin", "freedv_get_bits_per_modem_frame",
                 "freedv_get_n_max_modem_samples",
                 "freedv_get_n_tx_modem_samples",
                 "freedv_get_n_tx_preamble_modem_samples",
                 "freedv_get_n_tx_postamble_modem_samples",
                 "freedv_get_modem_sample_rate",
                 "freedv_get_n_nom_modem_samples",
                 "freedv_get_modem_symbol_rate", "freedv_get_rx_status",
                 "freedv_get_total_packets", "freedv_get_total_packet_errors"):
        getattr(lib, name).restype = ctypes.c_int
        getattr(lib, name).argtypes = [ctypes.c_void_p]
    lib.freedv_rawdatatx.argtypes = [ctypes.c_void_p,
                                     ctypes.POINTER(ctypes.c_short),
                                     ctypes.POINTER(ctypes.c_ubyte)]
    lib.freedv_rawdatarx.restype = ctypes.c_int
    lib.freedv_rawdatarx.argtypes = [ctypes.c_void_p,
                                     ctypes.POINTER(ctypes.c_ubyte),
                                     ctypes.POINTER(ctypes.c_short)]
    for name in ("freedv_rawdatapreambletx", "freedv_rawdatapostambletx"):
        getattr(lib, name).restype = ctypes.c_int
        getattr(lib, name).argtypes = [ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_short)]
    lib.freedv_gen_crc16.restype = ctypes.c_ushort
    lib.freedv_gen_crc16.argtypes = [ctypes.POINTER(ctypes.c_ubyte),
                                     ctypes.c_int]
    lib.freedv_set_frames_per_burst.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.freedv_set_verbose.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _LIB[0] = lib
    return lib


def available():
    """Whether libcodec2 can be loaded here. For bench tools and tests that
    should skip rather than fail on a machine without it."""
    try:
        _lib()
        return True
    except ModemError:
        return False


class Codec2Fsk(Modem):
    """codec2's FSK-LDPC raw-data mode, driven through ctypes.

    Takes an fm_config.Profile rather than loose numbers, so the hard
    constraints (orthogonality, the symbol-rate ceiling, samples per symbol)
    have already been checked before anything native is touched. codec2
    asserts two of those internally and ABORTS THE PROCESS when they fail --
    a core dump where a message belongs -- which is exactly why validation
    lives in the profile and not here.
    """

    def __init__(self, profile, codename=DEFAULT_CODE):
        if codename not in CODES:
            raise ModemError("unknown code %r; have %s"
                             % (codename, ", ".join(sorted(CODES))))
        lib = _lib()
        self.profile = profile
        self.codename = codename
        self.sample_rate_hz = profile.sample_rate_hz

        adv = _Advanced(0, profile.tones, profile.symbol_rate_bd,
                        profile.sample_rate_hz, profile.first_tone_hz,
                        profile.tone_spacing_hz, codename.encode("ascii"))
        self._adv = adv      # keep alive: codec2 holds the codename pointer
        self._f = lib.freedv_open_advanced(FREEDV_MODE_FSK_LDPC,
                                           ctypes.byref(adv))
        if not self._f:
            raise ModemError(
                "freedv_open_advanced failed for code %r at %s"
                % (codename, profile))
        lib.freedv_set_verbose(self._f, 0)

        self._bytes_per_frame = (
            lib.freedv_get_bits_per_modem_frame(self._f) // 8)
        self.frame_payload_bytes = self._bytes_per_frame - CRC_BYTES
        self._feed_buf = []

    # -- lifecycle ----------------------------------------------------------

    def close(self):
        if getattr(self, "_f", None):
            _lib().freedv_close(self._f)
            self._f = None

    def reset_rx_state(self):
        """Drop the demodulator's carried-over sync state, keeping the object.

        WHY THIS IS NEEDED AT ALL, and it is an upstream practice rather than
        our idea: FreeDV's own GUI resets its receivers at the end of every
        burst, because "the receive path does not run while transmitting, and a
        demodulator left 'in sync' from before keying would miss the next
        burst". This transport has exactly that gap -- the Kotlin audio service
        stops feeding the decoder while its own burst plays -- and has never
        reset anything.

        BY REOPENING, NOT BY freedv_set_sync(). That call is the obvious API
        and it is documented as driving the OFDM modem state machine; this mode
        is FSK-LDPC, where `freedv_get_sync()` was already measured returning 0
        throughout (see has_sync). An API that may silently no-op is no use for
        a reset whose whole purpose is to change hidden state, so this rebuilds
        the native handle, which cannot no-op.

        NOT THE SAME AS REBUILDING PER CHUNK, which is a known way to get a
        link that never acquires sync at all -- see fm_rx.FmRx's docstring.
        This is for the boundary after a transmission, where the state being
        dropped is stale by construction.

        Everything derived from the handle is re-derived and asserted to match,
        because a reopen that quietly produced a different frame size would
        corrupt every length calculation above it.
        """
        lib = _lib()
        if getattr(self, "_f", None):
            lib.freedv_close(self._f)
            self._f = None
        self._f = lib.freedv_open_advanced(FREEDV_MODE_FSK_LDPC,
                                           ctypes.byref(self._adv))
        if not self._f:
            raise ModemError(
                "freedv_open_advanced failed reopening code %r at %s"
                % (self.codename, self.profile))
        lib.freedv_set_verbose(self._f, 0)
        again = lib.freedv_get_bits_per_modem_frame(self._f) // 8
        if again != self._bytes_per_frame:
            raise ModemError(
                "reopening the modem changed the frame size from %d B to "
                "%d B -- every length above this would now be wrong"
                % (self._bytes_per_frame, again))
        self._feed_buf = []

    # -- rates --------------------------------------------------------------

    def raw_bps(self):
        return self.profile.raw_bps()

    def payload_bps(self):
        """What actually gets delivered, which is the only rate worth
        quoting. The raw symbol rate describes a link roughly five times
        faster than this one is."""
        lib = _lib()
        bits = lib.freedv_get_bits_per_modem_frame(self._f)
        frames_per_s = self.raw_bps() / (bits / fm_config.code_rate(self.codename))
        return frames_per_s * self.frame_payload_bytes * 8

    # -- transmit -----------------------------------------------------------

    def lead_in(self, seconds, hop=False):
        """A tone to trip VOX before the preamble starts. `hop=True` makes it
        hop through every tone (HOP_SEGMENT_S each) instead of holding the
        first one -- see HOP_SEGMENT_S for why a VOX link wants that.

        NOT optional on a VOX-keyed link. VOX takes time to notice audio and
        the transmitter is off until it does, so without this the preamble is
        what gets eaten -- and because the preamble is sized from the symbol
        rate, a faster configuration loses proportionally more of it, which
        presents as "some configurations never sync" rather than a clean
        across-the-board failure. One configuration went 100% -> 0% -> 100%
        purely on the presence of this tone.

        At the first tone, because that is guaranteed inside the passband and
        is a frequency the receiver is already looking near.

        RETURNED IN codec2's INTEGER SCALE, not in floats -- see this
        module's docstring. modulate_burst concatenates this with the
        modulator's own output and normalises once at the end.
        """
        n = int(round(seconds * self.sample_rate_hz))
        amp = 0.5 * FREEDV_PEAK
        if hop:
            p = self.profile
            tones = [p.first_tone_hz + k * p.tone_spacing_hz
                     for k in range(p.tones)]
            seg = max(1, int(round(HOP_SEGMENT_S * self.sample_rate_hz)))
            out = []
            phase = 0.0     # continuous across hops: no click at a boundary
            for i in range(n):
                phase += 2.0 * math.pi * tones[(i // seg) % len(tones)] \
                    / self.sample_rate_hz
                out.append(amp * math.sin(phase))
            return out
        f = self.profile.first_tone_hz
        return [amp * math.sin(2.0 * math.pi * f * i / self.sample_rate_hz)
                for i in range(n)]

    def modulate_burst(self, frames, lead_in_s=0.0, hop_lead_in=False):
        """One physical transmission: lead-in, preamble, every frame,
        postamble. Returns float samples scaled to +/-1.0.

        Frames share ONE preamble and postamble. That is not an optimisation,
        though it is also much cheaper (preamble and postamble are the largest
        per-transmission cost after key-up): concatenating N complete bursts
        loses frames from the MIDDLE of the sequence regardless of how much
        silence separates them. Frames sharing one preamble decode
        independently -- a copy destroyed by a noise burst is simply absent
        and its neighbours are unaffected.

        `frames` are already-framed, already-full-size physical payloads from
        fm_framing.pack(). This method appends codec2's CRC and nothing else;
        it does NOT pad, because padding with zeros is a measured bug and
        choosing the filler is fm_framing's job.
        """
        lib = _lib()
        frames = list(frames)
        if not frames:
            raise ModemError("modulate_burst needs at least one frame")
        for f in frames:
            if len(f) != self.frame_payload_bytes:
                raise ModemError(
                    "frame is %d B, this code's frame payload is %d B -- "
                    "frames must be packed to full size by fm_framing.pack() "
                    "before modulation, so the filler is random rather than "
                    "zeros (see fm_framing's docstring)"
                    % (len(f), self.frame_payload_bytes))

        # TX-side bookkeeping only: the receiver decodes every frame it can
        # regardless of what count, or none at all, it was told. Confirmed
        # against RX-side settings of 1, 3 and 8 against a real 3-frame burst.
        lib.freedv_set_frames_per_burst(self._f, len(frames))

        out = list(self.lead_in(lead_in_s, hop=hop_lead_in)) if lead_in_s else []

        n = lib.freedv_get_n_tx_preamble_modem_samples(self._f)
        pre = (ctypes.c_short * n)()
        got = lib.freedv_rawdatapreambletx(self._f, pre)
        out.extend(pre[:got])

        for frame in frames:
            buf = bytearray(frame)
            crc = lib.freedv_gen_crc16(
                (ctypes.c_ubyte * len(buf)).from_buffer_copy(bytes(buf)),
                len(buf))
            buf += bytes(((crc >> 8) & 0xFF, crc & 0xFF))
            n = lib.freedv_get_n_tx_modem_samples(self._f)
            block = (ctypes.c_short * n)()
            lib.freedv_rawdatatx(
                self._f, block,
                (ctypes.c_ubyte * len(buf)).from_buffer_copy(bytes(buf)))
            out.extend(block[:n])

        n = lib.freedv_get_n_tx_postamble_modem_samples(self._f)
        post = (ctypes.c_short * n)()
        got = lib.freedv_rawdatapostambletx(self._f, post)
        out.extend(post[:got])

        return [s / FREEDV_PEAK for s in out]

    def burst_duration_s(self, frames, lead_in_s=0.0):
        """Computed, not modulated -- the sweeps ask this far more often than
        they transmit.

        ASKS THE LIBRARY, where fm_config.LinkConfig.burst_duration_s() does the
        same arithmetic from the code table alone. That duplication is
        deliberate: the config has to be able to refuse an over-long burst
        without libcodec2 present, and this is what says whether its model is
        still true of the modem that will actually transmit. test_fm_modem
        asserts the two agree across every code, both tone counts and several
        rates, so a codec2 build that changes a frame size fails there rather
        than on a hillside.
        """
        lib = _lib()
        samples = (lib.freedv_get_n_tx_preamble_modem_samples(self._f)
                   + lib.freedv_get_n_tx_postamble_modem_samples(self._f)
                   + frames * lib.freedv_get_n_tx_modem_samples(self._f))
        return lead_in_s + samples / float(self.sample_rate_hz)

    # -- receive ------------------------------------------------------------

    def _to_int16(self, block):
        return (ctypes.c_short * len(block))(
            *[int(max(-32768, min(32767, round(s * FREEDV_PEAK))))
              for s in block])

    def feed(self, samples):
        """Incremental demod for a live, unbounded stream.

        Two differences from demodulate(): no flush padding, because a live
        stream has no end to flush against (frames still surface with the
        same roughly-one-burst-length latency, they just are not forced out
        early), and leftover samples persist on self between calls. codec2's
        own sync state is exactly what demodulate() already relies on being
        stateful across chunks within one call; this extends that across
        calls.

        Safe to call for the life of one instance. THE INSTANCE MUST BE
        PERSISTENT: rebuilding a modem per chunk of audio throws the sync
        state away and never lets it acquire at all.
        """
        lib = _lib()
        self._feed_buf.extend(samples)
        buf = (ctypes.c_ubyte * (self._bytes_per_frame + 8))()
        got = []
        while True:
            nin = lib.freedv_nin(self._f)
            if nin <= 0 or len(self._feed_buf) < nin:
                break
            block = self._feed_buf[:nin]
            self._feed_buf = self._feed_buf[nin:]
            nbytes = lib.freedv_rawdatarx(self._f, buf, self._to_int16(block))
            if nbytes > 0:
                got.append(bytes(bytearray(buf[:nbytes])))
        return got

    def demodulate(self, samples):
        """Decode one finite burst. Returns a list of frames, possibly empty.

        FEEDS SILENCE AFTER THE BURST, and that is not padding for the
        radio's benefit -- it is how the last frame is flushed out. Measured:
        the frame does not surface until the demodulator has been fed roughly
        as many samples again as the burst itself. Anything less and short
        bursts decode while long ones silently do not, which looks exactly
        like a bad configuration and is not. Costs no airtime, since this is
        silence fed to a decoder rather than transmitted.
        """
        lib = _lib()
        buf = (ctypes.c_ubyte * (self._bytes_per_frame + 8))()
        got = []
        flush = [0.0] * (len(samples)
                         + lib.freedv_get_n_max_modem_samples(self._f) * 4)
        signal = list(samples) + flush
        n = len(signal)
        pos = 0
        while pos < n:
            nin = lib.freedv_nin(self._f)
            if nin <= 0:
                break
            block = signal[pos:pos + nin]
            if len(block) < nin:
                # Zero-pad a short final chunk rather than dropping it:
                # dropping it loses whatever frame it would have completed.
                block = block + [0.0] * (nin - len(block))
            pos += nin
            nbytes = lib.freedv_rawdatarx(self._f, buf, self._to_int16(block))
            if nbytes > 0:
                got.append(bytes(bytearray(buf[:nbytes])))
        return got

    def has_sync(self):
        """Whether the demodulator currently has real sync.

        USES freedv_get_rx_status(), NOT freedv_get_sync(). That was tried
        first and measured directly against a real burst at the instant it
        decoded: freedv_get_sync() returns 0 throughout, never going
        non-zero, for this API path (freedv_open_advanced's FSK_LDPC raw-data
        mode). It is very likely a voice-mode-only signal in this build.

        freedv_get_rx_status() is a bitmask (trial-sync / sync / bits-out /
        bit-errors) that does move: 0 through idle and through pure noise,
        jumping to a synced value the instant a frame decodes, and decaying
        back to 0 over roughly 0.5-0.6 s afterward.

        ANY non-zero value counts as busy rather than masking for the sync
        bit specifically: trial-sync alone is real evidence of FSK-shaped
        energy worth deferring for, and the cost of one false "busy" is a
        short wait, not a wrong send.
        """
        return _lib().freedv_get_rx_status(self._f) != 0

    def describe(self):
        return ("%dFSK Rs=%d tones %s Hz  %s  frame payload %d B  "
                "%.0f bps raw / %.1f bps payload"
                % (self.profile.tones, self.profile.symbol_rate_bd,
                   "/".join(str(t) for t in self.profile.tone_list()),
                   self.codename, self.frame_payload_bytes,
                   self.raw_bps(), self.payload_bps()))
