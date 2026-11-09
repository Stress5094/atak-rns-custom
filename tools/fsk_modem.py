"""codec2's FSK modem with forward error correction, driven from Python.

WHY CTYPES AND NOT A BUILD. libcodec2 is already a shared library with a
public header, and the desktop rig only needs to CHOOSE a configuration --
tone placement, symbol rate, how much error correction to buy. Answering that
does not require compiling anything, and making it require a build would put a
toolchain between a question and its answer. The Android side will use the
same library through JNI; this file exists to decide what to tell it.

WHY THE FRAMED API RATHER THAN RAW FSK. codec2 exposes two levels. The low one
(fsk_mod/fsk_demod) hands over loose bits and leaves framing, synchronisation
and error correction as an exercise. The high one -- freedv_rawdatatx/rx in
FSK_LDPC mode -- is a complete packet modem: a unique word to find the start
of a burst in noise, LDPC forward error correction, a CRC, and preamble and
postamble generation. That is the whole reason to prefer this over AFSK, which
has no error correction at all, so using the low API would throw away the
advantage being bought.

WHAT THE PARAMETERS MEAN, and why they are not free choices here:

  M             2 or 4 tones. 4 carries two bits per symbol but needs four
                tones to fit inside the passband.
  Rs            symbol rate. Bits/second is Rs * log2(M) BEFORE coding; the
                LDPC code then takes roughly half of it back.
  first_tone,   where the tones sit. An FM handheld passes about 300-3000 Hz
  tone_spacing  and nothing outside it, so every tone -- first_tone through
                first_tone + (M-1)*tone_spacing -- has to land inside that,
                with margin, or the radio simply deletes part of the signal.
  codename      the LDPC code, which fixes the payload size. Pick the smallest
                one that holds a frame: unused payload is airtime spent on
                padding.

Tones must also be spaced at least Rs apart to stay orthogonal, which is the
constraint that actually binds: passband width divided by M sets the spacing,
spacing caps Rs, and Rs sets the bitrate. There is not much room to be clever.
"""

import ctypes
import ctypes.util
import math
import os

FREEDV_MODE_FSK_LDPC = 9

# Peak sample value the modulator produces, from freedv_api.h. Used to convert
# between codec2's int16 world and the float samples the channel model works
# in -- getting this wrong silently scales the signal and every SNR figure
# with it.
FREEDV_PEAK = 16384.0

# LDPC codes present in this build, as (payload bytes, rate). The payload is
# what one modem frame carries INCLUDING the two CRC bytes codec2 expects at
# the end, so usable space is two less than the name suggests.
CODES = {
    "H_128_256_5":    (16, 0.5),
    "H_256_512_4":    (32, 0.5),
    "H_256_768_22":   (32, 1.0 / 3.0),
    "H_1024_2048_4f": (128, 0.5),
    "H_4096_8192_3d": (512, 0.5),
}


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


def _load():
    # RTAK_CODEC2_LIB overrides the search entirely -- needed on Android,
    # where the bundled arm64 .so sits in the app's native lib directory
    # rather than anywhere find_library() or a bare "libcodec2.so" is
    # guaranteed to resolve through the app's linker namespace. Unset on the
    # desktop, so this is a no-op there.
    path = (os.environ.get("RTAK_CODEC2_LIB")
            or ctypes.util.find_library("codec2") or "libcodec2.so")
    lib = ctypes.CDLL(path)
    lib.freedv_open_advanced.restype = ctypes.c_void_p
    lib.freedv_open_advanced.argtypes = [ctypes.c_int,
                                         ctypes.POINTER(_Advanced)]
    lib.freedv_close.argtypes = [ctypes.c_void_p]
    for name in ("freedv_nin", "freedv_get_bits_per_modem_frame",
                 "freedv_get_n_max_modem_samples",
                 "freedv_get_n_tx_modem_samples",
                 "freedv_get_n_tx_preamble_modem_samples",
                 "freedv_get_n_tx_postamble_modem_samples",
                 "freedv_get_modem_sample_rate", "freedv_get_sync",
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
    return lib


_LIB = _load()


class FskModem(object):
    """One modem configuration, as a thing that turns bytes into audio and
    back."""

    # DEFAULT first_tone RAISED 800 -> 1500 on 2026-09-13, on measured
    # evidence, and it is the single largest reliability change this FM link
    # has had.
    #
    # 800 Hz sits in a hole. tools/passband_sweep.py measures this bench's
    # path 12.2 dB weaker at 800 Hz than at its 2800 Hz peak -- but the real
    # problem is worse than "weak": at 800 Hz the THIRD HARMONIC (2400 Hz)
    # arrives 1.4x STRONGER than the fundamental, and 2400 Hz sits right
    # beside the old plan's own 2300 Hz tone. So an 800 Hz symbol did not
    # arrive quiet, it arrived looking like a different symbol.
    #
    # Measured directly against ground truth (the payload is known, so the
    # transmitted symbol sequence is known): 800 Hz decoded correctly 90.2%
    # of the time while 1300/1800/2300 Hz were perfect, 182 of 182. Every
    # systematic symbol error in the entire burst was the bottom tone.
    #
    # End to end, interleaved round-by-round so the bench's notorious
    # time-varying conditions cannot explain it (22 trials per plan):
    #
    #     tones 800-2300   5/22 delivered  (23%)
    #     tones 1400-2900  3/6  delivered  (50%)
    #     tones 1500-3000  20/22 delivered (91%)
    #
    # CAVEAT, deliberately accepted: 1500-3000 puts the top tone exactly at
    # 3000 Hz, so fits_passband() (which wants 100 Hz of margin) returns
    # False for the default plan. That check guards against radios whose
    # audio rolls off before 3000 Hz; this bench measures 3000 Hz at only
    # -0.9 dB, essentially its peak, so the margin buys nothing HERE. On a
    # radio pairing that rolls off earlier the top tone is the one at risk.
    # **If a new radio pairing performs badly, try first_tone=1400 (tones
    # 1400-2900) before anything else** -- it keeps the full margin, passes
    # fits_passband, and still measured 2x the old plan.
    #
    # TX and RX must agree on this, and both ends take it from this default,
    # so a mixed-version pair (old APK against new) will not interoperate.
    def __init__(self, M=4, Rs=500, Fs=8000, first_tone=1500,
                 tone_spacing=500, codename="H_1024_2048_4f"):
        if codename not in CODES:
            raise ValueError("unknown code %r; have %s"
                             % (codename, sorted(CODES)))
        top = first_tone + (M - 1) * tone_spacing
        # codec2 asserts this internally and aborts the process -- a core
        # dump where a message belongs. Checked here so a bad sweep parameter
        # is a Python error rather than a dead interpreter.
        if Fs % Rs != 0:
            raise ValueError(
                "symbol rate %d Hz must divide the sample rate %d Hz exactly "
                "(codec2 asserts this); try %s"
                % (Rs, Fs, ", ".join(str(Fs // k) for k in (16, 10, 8, 5, 4)
                                     if Fs % (Fs // k) == 0)))
        # Also asserted internally by codec2 (P >= 8): the demodulator needs
        # at least 8 samples per symbol to work with. Same reasoning as above
        # -- catch it here rather than lose the interpreter.
        if Fs // Rs < 8:
            raise ValueError(
                "sample rate %d Hz gives only %d samples per symbol at Rs=%d; "
                "codec2 requires at least 8, so Rs must be <= %d here"
                % (Fs, Fs // Rs, Rs, Fs // 8))
        if tone_spacing < Rs:
            raise ValueError(
                "tone spacing %d Hz is below the symbol rate %d Hz -- the "
                "tones overlap and the demodulator cannot separate them"
                % (tone_spacing, Rs))
        self.M, self.Rs, self.Fs = M, Rs, Fs
        self.first_tone, self.tone_spacing = first_tone, tone_spacing
        self.codename = codename
        self.top_tone_hz = top

        adv = _Advanced(0, M, Rs, Fs, first_tone, tone_spacing,
                        codename.encode("ascii"))
        self._adv = adv  # keep alive; codec2 holds the codename pointer
        self._f = _LIB.freedv_open_advanced(FREEDV_MODE_FSK_LDPC,
                                            ctypes.byref(adv))
        if not self._f:
            raise RuntimeError("freedv_open_advanced failed for %r" % codename)
        _LIB.freedv_set_verbose(self._f, 0)

        self.bytes_per_frame = _LIB.freedv_get_bits_per_modem_frame(self._f) // 8
        # codec2's raw-data convention: the last two payload bytes are a CRC
        # over the rest, which the receiver checks and the sender must fill.
        self.payload_bytes = self.bytes_per_frame - 2

    def close(self):
        if getattr(self, "_f", None):
            _LIB.freedv_close(self._f)
            self._f = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def is_synced(self):
        """True if codec2's own demodulator currently has sync on a real
        signal -- it has found and is tracking a burst's preamble/unique
        word, not just responding to whatever energy is on the channel.

        Added 2026-09-20 for listen-before-transmit: a raw audio energy
        threshold (RMS or peak) cannot tell real FSK traffic apart from a
        raw/unfiltered discriminator tap's constant noise floor (measured
        directly: continuous Gaussian noise at the same ~0.68 RMS this
        project's own bench showed never once moved this signal, see
        docs/open-questions.md's "an RMS threshold can't tell real traffic
        from this cable's constant noise" finding) -- this asks the actual
        decoder instead, which already does that discrimination correctly
        because that is its whole job.

        USES freedv_get_rx_status(), NOT freedv_get_sync() -- tried that
        first, and measured it directly (a real burst, checked at the exact
        instant it decoded): freedv_get_sync() returns 0 throughout, never
        going non-zero, for this codec2 API path (freedv_open_advanced's
        FSK_LDPC raw-data mode). It is very likely a voice-mode-only
        signal on this build. freedv_get_rx_status() is a bitmask (trial
        sync / sync / bits-out / bit-errors) that DOES move: measured 0
        throughout idle and pure noise, jumping to a real synced value the
        instant a frame decodes and staying non-zero for roughly 0.5-0.6s
        afterward before decaying back to 0 (a real, bounded, measured
        lag -- see TestIsSynced in tools/test_fsk_modem.py for the
        reproduction). Any non-zero value is treated as busy here rather
        than masking for the SYNC bit specifically: TRIAL_SYNC alone is
        still evidence of real FSK-shaped energy worth deferring for, and
        the cost of one false "busy" is a short wait, not a wrong send.
        """
        return _LIB.freedv_get_rx_status(self._f) != 0

    # -- rates ---------------------------------------------------------------

    def raw_bps(self):
        """On-air bit rate before error correction takes its share."""
        return self.Rs * int(math.log2(self.M))

    def payload_bps(self):
        """What actually gets delivered, which is the only rate worth
        quoting."""
        frames_per_s = self.raw_bps() / float(
            _LIB.freedv_get_bits_per_modem_frame(self._f) / CODES[self.codename][1])
        return frames_per_s * self.payload_bytes * 8

    def fits_passband(self, low_hz=300.0, high_hz=3000.0, margin_hz=100.0):
        """Do all M tones land where an FM radio will actually pass them?"""
        return (self.first_tone - margin_hz >= low_hz
                and self.top_tone_hz + margin_hz <= high_hz)

    # -- the modem itself ----------------------------------------------------

    def lead_in(self, seconds):
        """A plain tone to trip VOX before the preamble starts.

        NOT optional on a VOX-keyed link, and the rig proved why: VOX takes
        time to notice audio and the transmitter is off until it does, so
        without a lead-in the preamble is what gets eaten. codec2 sizes its
        preamble from the symbol rate, so a fast configuration has a short
        preamble and loses proportionally more of it -- which is why this
        shows up as "some configurations never sync" rather than as a clean
        across-the-board failure. 2FSK at Rs=1000 went from 100% to 0% to 100%
        again purely on the presence of this tone.

        At first_tone because it is guaranteed to be inside the passband and
        is a frequency the receiver is already looking near.

        In codec2's int16 scale, NOT in float. modulate() concatenates this
        with the modulator's own output and normalises the whole burst once at
        the end, so a lead-in built in float units gets divided by the int16
        peak along with everything else and arrives at 1/16384 amplitude --
        present in the samples, inaudible to VOX, and indistinguishable from
        having no lead-in at all. That cost a full sweep that looked like a
        modem failure.
        """
        n = int(round(seconds * self.Fs))
        amp = 0.5 * FREEDV_PEAK
        return [amp * math.sin(2.0 * math.pi * self.first_tone * i / self.Fs)
                for i in range(n)]

    def modulate(self, payload, lead_in_s=0.0):
        """One burst: lead-in, preamble, one frame, postamble. Returns float
        samples scaled to +/-1.0, which is what the channel model expects."""
        return self.modulate_burst([payload], lead_in_s=lead_in_s)

    def modulate_burst(self, payloads, lead_in_s=0.0):
        """One physical transmission carrying one or more frames, sharing a
        single preamble and postamble. Returns float samples scaled to
        +/-1.0.

        WHY THIS EXISTS, and why it is not just modulate() called N times
        with silence in between: that was tried first, and it does not work.
        Concatenating N complete bursts (each with its own preamble and
        postamble) loses frames from the MIDDLE of the sequence regardless of
        how much silence separates them -- the demodulator's sync state
        machine is not designed to re-acquire cleanly between two bursts it
        was never told were related. Repeated frames sharing one preamble
        decode independently and correctly: a frame corrupted by a noise
        burst is silently absent from the result and its neighbours are
        unaffected, which is the entire point of sending the same payload
        more than once. freedv_set_frames_per_burst is a TX-side bookkeeping
        call only -- the receiver decodes every frame it can regardless of
        what count (or none at all) it was told, confirmed against RX-side
        settings of 1, 3 and 8 against a real 3-frame burst.

        Also much cheaper: the preamble and postamble are codec2's largest
        per-transmission cost after key-up, so repeating within one burst
        pays that once for N copies instead of N times.
        """
        payloads = list(payloads)
        if not payloads:
            raise ValueError("modulate_burst needs at least one payload")
        for payload in payloads:
            if len(payload) > self.payload_bytes:
                raise ValueError("payload %d B exceeds %d B for this code"
                                 % (len(payload), self.payload_bytes))

        _LIB.freedv_set_frames_per_burst(self._f, len(payloads))

        out = list(self.lead_in(lead_in_s)) if lead_in_s else []
        n = _LIB.freedv_get_n_tx_preamble_modem_samples(self._f)
        pre = (ctypes.c_short * n)()
        got = _LIB.freedv_rawdatapreambletx(self._f, pre)
        out.extend(pre[:got])

        for payload in payloads:
            buf = bytearray(payload) + bytes(self.payload_bytes - len(payload))
            crc = _LIB.freedv_gen_crc16(
                (ctypes.c_ubyte * len(buf)).from_buffer_copy(bytes(buf)),
                len(buf))
            buf += bytes(((crc >> 8) & 0xFF, crc & 0xFF))

            n = _LIB.freedv_get_n_tx_modem_samples(self._f)
            frame = (ctypes.c_short * n)()
            _LIB.freedv_rawdatatx(
                self._f, frame,
                (ctypes.c_ubyte * len(buf)).from_buffer_copy(bytes(buf)))
            out.extend(frame[:n])

        n = _LIB.freedv_get_n_tx_postamble_modem_samples(self._f)
        post = (ctypes.c_short * n)()
        got = _LIB.freedv_rawdatapostambletx(self._f, post)
        out.extend(post[:got])

        return [s / FREEDV_PEAK for s in out]

    def feed(self, chunk):
        """Incremental demod for a live, unbounded stream -- e.g. a
        continuously-running receiver, as opposed to demodulate()'s one
        finite pre-recorded buffer.

        Two differences from demodulate(): no flush padding (there is no
        "end" to a live stream to flush against -- frames still come out
        with the same roughly-one-burst-length latency demodulate()
        documents, they just aren't forced out early), and leftover samples
        that don't yet total a full `nin` persist on `self` between calls
        instead of being rebuilt from scratch each time. Safe to call
        repeatedly for the life of one FskModem instance; codec2's own sync
        state (self._f) is exactly what demodulate() already relies on
        being stateful across chunks within one call, this just extends
        that across calls too.

        `chunk`: float samples, same +/-1.0 scale as modulate()/demodulate().
        Returns whatever frames completed during this call -- usually [].
        """
        if not hasattr(self, "_feed_buf"):
            self._feed_buf = []
        self._feed_buf.extend(chunk)

        buf = (ctypes.c_ubyte * (self.bytes_per_frame + 8))()
        got = []
        while True:
            nin = _LIB.freedv_nin(self._f)
            if nin <= 0 or len(self._feed_buf) < nin:
                break
            block, self._feed_buf = self._feed_buf[:nin], self._feed_buf[nin:]
            c_chunk = (ctypes.c_short * nin)(
                *[int(max(-32768, min(32767, round(s * FREEDV_PEAK))))
                  for s in block])
            nbytes = _LIB.freedv_rawdatarx(self._f, buf, c_chunk)
            if nbytes > 0:
                got.append(bytes(bytearray(buf[:nbytes])))
        return got

    def demodulate(self, samples):
        """Feed a whole burst in and collect whatever frames survived.

        Returns a list of payloads. An empty list means nothing decoded --
        which is the answer the survival curve is made of, so it is a normal
        result and not an error.
        """
        buf = (ctypes.c_ubyte * (self.bytes_per_frame + 8))()
        got = []
        pos = 0

        # The demodulator accumulates internally and only emits a frame once
        # it has been fed past the end of it, so a burst handed over exactly
        # is a burst whose last frame never comes out. Feeding silence after
        # it is not padding for the radio's benefit -- it is how the frame is
        # flushed, and leaving it out cost an afternoon of believing the modem
        # did not work at all.
        # Measured, not guessed: the frame does not come out until the
        # demodulator has been fed roughly as many samples again as the burst
        # itself. Anything less and short bursts decode while long ones
        # silently do not, which looks exactly like "this configuration is bad"
        # and is not. Costs no airtime -- this is silence fed to a decoder,
        # not transmitted -- but it does mean a frame is recovered about one
        # burst-length after the burst ends, which is real latency and belongs
        # in the delivery budget.
        flush = [0.0] * (len(samples)
                         + _LIB.freedv_get_n_max_modem_samples(self._f) * 4)
        signal = list(samples) + flush
        n = len(signal)

        while pos < n:
            nin = _LIB.freedv_nin(self._f)
            if nin <= 0:
                break
            # Zero-pad a short final chunk rather than dropping it, for the
            # same reason: dropping it loses whatever frame it would complete.
            block = signal[pos:pos + nin]
            if len(block) < nin:
                block = block + [0.0] * (nin - len(block))
            pos += nin
            chunk = (ctypes.c_short * nin)(
                *[int(max(-32768, min(32767, round(s * FREEDV_PEAK))))
                  for s in block])
            nbytes = _LIB.freedv_rawdatarx(self._f, buf, chunk)
            if nbytes > 0:
                got.append(bytes(bytearray(buf[:nbytes])))
        return got

    def describe(self):
        return ("%dFSK  Rs=%d  tones %d-%d Hz (spacing %d)  %s  "
                "payload %d B  %.0f bps raw"
                % (self.M, self.Rs, self.first_tone, self.top_tone_hz,
                   self.tone_spacing, self.codename, self.payload_bytes,
                   self.raw_bps()))
