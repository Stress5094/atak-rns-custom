"""A minimal Bell 202 AFSK modem, to test whether APRS's own approach
survives a channel that broke codec2's FSK-LDPC.

docs/open-questions.md ("a real burst with a near-zero lead-in still fails")
found the Digirig-Lite-transmit leg corrupts codec2 FSK-LDPC even with a
short lead-in: the received level decays CONTINUOUSLY across the whole
burst (~13x over ~1.8s), and codec2's demodulator -- which needs a long
unique-word correlation across many symbols to acquire sync -- never locks.

Bell 202 AFSK (1200 baud, tones at 1200/2200 Hz) is what APRS has carried
over exactly this kind of link -- soundcard, audio cable, handheld FM
radio, whatever AGC/compander that radio has -- for over 40 years. The
reason it might tolerate what codec2 could not is structural, not just
"more tested": classic AFSK demod decides each bit INDEPENDENTLY, from a
~0.8 ms tone-energy comparison, rather than correlating a long pattern
across hundreds of milliseconds the way codec2's sync acquisition does. A
per-symbol decision does not care what the channel gain was one symbol
ago; a block correlation does.

This is a minimal, from-scratch prototype -- raw framing (preamble + sync
word + length + payload + CRC-16), not full AX.25 -- built to answer one
question on real hardware before investing in a real AX.25 implementation:
does APRS's OWN modulation approach survive where codec2's did not.

    python3 tools/afsk_modem.py --loopback
    python3 tools/afsk_modem.py --run --play-dev 5 --rec-dev 6 --trials 10
    python3 tools/afsk_modem.py --analyze some_recording.wav
"""

import argparse
import math
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import otair_test
import ptt_test

FS = 48000            # matches both bench cards' fixed capture/playback rate
                      # exactly -- no resampling either direction.
BAUD = 1200.0          # Bell 202, same as APRS
SYM_N = int(FS / BAUD)   # 40 samples/symbol exactly (48000/1200)
assert SYM_N * BAUD == FS, "FS must be an exact multiple of BAUD"

MARK_HZ = 1200.0       # binary 1
SPACE_HZ = 2200.0      # binary 0

PREAMBLE_BITS = [i % 2 for i in range(128)]   # alternating 0/1 for clock training
SYNC_WORD = [0, 1, 1, 1, 1, 1, 1, 0] * 2      # two AX.25-style 0x7E flags

LEAD_SILENCE_S = 0.3
TAIL_SILENCE_S = 0.3


def crc16_ccitt(data, poly=0x1021, init=0xFFFF):
    crc = init
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def bytes_to_bits(data):
    bits = []
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    return bits


def bits_to_bytes(bits):
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        b = 0
        for j in range(8):
            b = (b << 1) | bits[i + j]
        out.append(b)
    return bytes(out)


def frame_bits(payload, repeats=1):
    """preamble + sync + REPEATS copies of [1-byte length + payload +
    CRC16], as bits -- blind redundancy, same idea as fsk_modem.py's
    modulate_burst: one preamble/sync pays for all copies instead of each
    copy needing its own, and the receiver counts it delivered if ANY copy
    decodes."""
    crc = crc16_ccitt(payload)
    body = bytes([len(payload)]) + payload + bytes([crc >> 8, crc & 0xFF])
    return PREAMBLE_BITS + SYNC_WORD + bytes_to_bits(body) * repeats


def modulate(payload, amp=0.8, repeats=1):
    """Continuous-phase 2FSK: phase accumulates across symbol boundaries so
    switching tones never produces a phase-discontinuity click, the same
    principle fsk_modem.py's codec2 backend handles internally -- here it
    has to be done by hand."""
    bits = frame_bits(payload, repeats=repeats)
    samples = [0.0] * int(LEAD_SILENCE_S * FS)
    phase = 0.0
    for bit in bits:
        freq = MARK_HZ if bit else SPACE_HZ
        step = 2.0 * math.pi * freq / FS
        for _ in range(SYM_N):
            samples.append(amp * math.sin(phase))
            phase += step
            if phase > 2.0 * math.pi:
                phase -= 2.0 * math.pi
    samples += [0.0] * int(TAIL_SILENCE_S * FS)
    return samples


def goertzel(samples, freq, fs=FS):
    """Deliberately NOT the project's usual integer-bin Goertzel (see
    tone_test.py etc): this modem's window is one symbol (SYM_N=40
    samples), so the bin spacing (fs/N = 1200 Hz) is comparable to the
    1000 Hz gap between MARK and SPACE themselves. Rounding to the nearest
    bin (the usual shortcut) puts 2200 Hz on the 2400 Hz bin -- a 200 Hz
    miss that measures the wrong frequency entirely, found by a loopback
    regression this modem's own PLL/tilt-correction rewrite introduced.
    Evaluating the exact requested frequency instead (still the Goertzel
    recursion, just not snapped to fs/N multiples) fixes it."""
    n = len(samples)
    if n == 0:
        return 0.0
    w = (2.0 * math.pi * freq) / fs
    coeff = 2.0 * math.cos(w)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s2, s1 = s1, s0
    power = s1 * s1 + s2 * s2 - coeff * s1 * s2
    return math.sqrt(max(0.0, power)) / n


def _rms(samples):
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def find_burst_start(samples, window=SYM_N * 4):
    """Coarse envelope-threshold anchor -- only used to know roughly where
    to start LOOKING for preamble transitions, not trusted as the actual
    symbol boundary."""
    step = window
    levels = []
    for i in range(0, len(samples) - step + 1, step):
        levels.append((i, _rms(samples[i:i + step])))
    if not levels:
        return 0
    floor_n = max(1, len(levels) // 10)
    floor = sorted(v for _, v in levels[:floor_n])[floor_n // 2]
    thresh = max(floor * 4.0, 1e-4)
    for i, v in levels:
        if v >= thresh:
            return i
    return 0


# How finely to step the sliding mark/space correlator while hunting for
# preamble transitions. 5 samples/symbol (SYM_N=40) is enough resolution to
# interpolate a transition's true sub-sample timing without the cost of
# evaluating every single sample.
DISCRIM_STEP = 5

# How far past the coarse envelope anchor to hunt for preamble transitions --
# generous, since that anchor is only approximate.
DISCRIM_SEARCH_SYMS = len(PREAMBLE_BITS) + 8


def _discriminator(samples, start, stop, step=DISCRIM_STEP):
    """mark energy minus space energy, on a sliding SYM_N window, at every
    `step`'th sample position from `start` to `stop`. Returns parallel lists
    (window_center_sample, mark_e, space_e)."""
    centers, marks, spaces = [], [], []
    half = SYM_N // 2
    for i in range(start, stop, step):
        window = samples[i:i + SYM_N]
        if len(window) < SYM_N:
            break
        marks.append(goertzel(window, MARK_HZ))
        spaces.append(goertzel(window, SPACE_HZ))
        centers.append(i + half)
    return centers, marks, spaces


def _find_transitions(centers, marks, spaces):
    """Sub-sample-interpolated positions where the mark/space discriminator
    changes sign -- i.e. candidate symbol boundaries, since the preamble
    (strictly alternating) has a real tone change at every one of them."""
    d = [m - s for m, s in zip(marks, spaces)]
    out = []
    for j in range(1, len(d)):
        if (d[j - 1] >= 0) != (d[j] >= 0):
            denom = (d[j - 1] - d[j])
            frac = (d[j - 1] / denom) if abs(denom) > 1e-12 else 0.5
            frac = min(1.0, max(0.0, frac))
            out.append(centers[j - 1] + frac * (centers[j] - centers[j - 1]))
    return out


def _fit_clock(transitions):
    """Least-squares fit of transition sample-position vs. its integer
    symbol-boundary index, over ALL preamble transitions (not just one
    offset guess) -- gives a sub-sample-accurate anchor (symbol boundary 0's
    sample position) and the ACTUAL measured samples/symbol (absorbing any
    real play/record clock-rate mismatch), rather than assuming a fixed
    SYM_N held exactly. Returns (anchor, period) or None if too few
    transitions to fit.
    """
    if len(transitions) < 4:
        return None
    t0 = transitions[0]
    ks = [round((t - t0) / SYM_N) for t in transitions]
    n = len(transitions)
    mean_k = sum(ks) / n
    mean_t = sum(transitions) / n
    num = sum((k - mean_k) * (t - mean_t) for k, t in zip(ks, transitions))
    den = sum((k - mean_k) ** 2 for k in ks)
    if den == 0:
        return None
    period = num / den
    anchor = mean_t - period * mean_k
    return anchor, period


def find_sync(bits):
    """First index right after a SYNC_WORD match in a decoded bitstream."""
    n = len(SYNC_WORD)
    for i in range(len(bits) - n):
        if bits[i:i + n] == SYNC_WORD:
            return i + n
    return None


def demodulate(samples, payload_len_hint=None, max_repeats=8):
    """Returns (payload_bytes_or_None, detail_dict).

    Two real weaknesses in the first version of this were replaced here,
    both found by testing against real hardware (docs/open-questions.md,
    "tried a from-scratch Bell 202 AFSK prototype"): a single best-scoring
    fixed offset for the whole burst (no continuous clock recovery, so any
    timing drift across the burst broke a frame even when the front decoded
    almost perfectly) is now a proper transition-locked clock fit over the
    preamble; and mark/space are now compared after removing this bench's
    own measured tilt between them, rather than assuming a flat channel.

    `payload_len_hint`, if known (the bench harness always knows what it
    sent), pins the chunk size used to split repeated copies rather than
    trusting a possibly-corrupted length byte for that; `max_repeats`
    bounds how many copies are attempted.
    """
    anchor_guess = find_burst_start(samples)
    lo = max(0, anchor_guess - 2 * SYM_N)
    hi = anchor_guess + DISCRIM_SEARCH_SYMS * SYM_N
    centers, marks, spaces = _discriminator(samples, lo, hi)
    transitions = _find_transitions(centers, marks, spaces)
    fit = _fit_clock(transitions)

    detail = dict(anchor_guess=anchor_guess, n_transitions=len(transitions),
                 preamble_len=len(PREAMBLE_BITS))
    if fit is None:
        detail["fit"] = None
        return None, detail
    anchor, period = fit
    detail["fit"] = dict(anchor=anchor, period=period)

    # Tilt TREND, not one static average: split the preamble in half and
    # measure true-mark/true-space tilt separately in each half, at each
    # half's own center time, then log-linearly extrapolate that trend
    # forward through the sync/payload region. A single whole-preamble
    # average (the previous version) assumes the channel is constant for
    # as long as it's being measured AND for however long after that it's
    # relied on -- docs/open-questions.md's per-trial tilt numbers
    # (0.32x-2.11x, no stable value) say that assumption doesn't hold on
    # this bench; a channel that is still changing needs its rate of
    # change estimated, not just its instantaneous value.
    def half_tilt(bit_range):
        tm, ts = [], []
        for i in bit_range:
            expected = PREAMBLE_BITS[i]
            start = int(round(anchor + i * period))
            window = samples[start:start + SYM_N]
            if len(window) < SYM_N:
                return None, None
            m, s = goertzel(window, MARK_HZ), goertzel(window, SPACE_HZ)
            (tm if expected else ts).append(m if expected else s)
        if not tm or not ts or sum(ts) <= 0:
            return None, None
        return (sum(tm) / len(tm)) / (sum(ts) / len(ts)), sum(i for i in bit_range) / len(bit_range)

    half = len(PREAMBLE_BITS) // 2
    tilt1, t1 = half_tilt(range(0, half))
    tilt2, t2 = half_tilt(range(half, len(PREAMBLE_BITS)))
    if tilt1 is None or tilt2 is None or tilt1 <= 0 or tilt2 <= 0:
        log_tilt1, log_tilt2, t1, t2 = 0.0, 0.0, 0.0, 1.0
    else:
        log_tilt1, log_tilt2 = math.log(tilt1), math.log(tilt2)
    slope = (log_tilt2 - log_tilt1) / (t2 - t1) if t2 != t1 else 0.0

    def tilt_at(k):
        return math.exp(log_tilt1 + slope * (k - t1))

    detail["tilt_at_0"] = tilt_at(0)
    detail["tilt_at_preamble_end"] = tilt_at(len(PREAMBLE_BITS))

    def decode_bit(k):
        start = int(round(anchor + k * period))
        window = samples[start:start + SYM_N]
        if len(window) < SYM_N:
            return None
        m = goertzel(window, MARK_HZ)
        s = goertzel(window, SPACE_HZ) * tilt_at(k)
        return 1 if m >= s else 0

    preamble_bits = [decode_bit(k) for k in range(len(PREAMBLE_BITS))]
    detail["preamble_score"] = sum(1 for b, e in zip(preamble_bits, PREAMBLE_BITS)
                                   if b == e)

    sync_len = len(SYNC_WORD)
    sync_bits = [decode_bit(len(PREAMBLE_BITS) + i) for i in range(sync_len)]
    if None in sync_bits or sync_bits != SYNC_WORD:
        # Fall back to searching a slightly wider window in case the
        # preamble score above is imperfect enough to have shifted where
        # the sync word actually starts.
        probe_n = len(PREAMBLE_BITS) + sync_len + 8
        probe = [decode_bit(k) for k in range(probe_n)]
        if None in probe:
            detail["sync_at"] = None
            return None, detail
        sync_at = find_sync(probe)
        detail["sync_at"] = sync_at
        if sync_at is None:
            return None, detail
    else:
        sync_at = len(PREAMBLE_BITS) + sync_len
        detail["sync_at"] = sync_at

    return _decode_copies(decode_bit, sync_at, detail,
                          payload_len_hint=payload_len_hint,
                          max_repeats=max_repeats)


def _decode_copies(decode_bit, sync_at, detail, payload_len_hint=None,
                   max_repeats=8):
    """Decode one or more back-to-back [length][payload][crc] copies
    starting at symbol index `sync_at`, returning the first that passes
    CRC -- blind-repeat redundancy, matches frame_bits()'s REPEATS. All
    copies carry the identical payload, so once one copy's length byte is
    trusted (or given directly as `payload_len_hint`), every copy is
    chunked at the same fixed size rather than re-trusting a possibly
    corrupted length byte each time."""
    length = payload_len_hint
    detail["copies"] = []
    k = sync_at
    for copy_i in range(max_repeats):
        len_bits = [decode_bit(k + i) for i in range(8)]
        if None in len_bits:
            break
        this_length = int("".join(str(b) for b in len_bits), 2)
        if length is None:
            length = this_length
        needed = 8 + length * 8 + 16
        body_bits = [decode_bit(k + i) for i in range(needed)]
        if None in body_bits:
            break
        body_bytes = bits_to_bytes(body_bits)
        payload = body_bytes[1:1 + length]
        crc_rx = (body_bytes[1 + length] << 8) | body_bytes[2 + length]
        crc_calc = crc16_ccitt(payload)
        ok = (crc_rx == crc_calc)
        detail["copies"].append(dict(length=this_length, crc_ok=ok))
        if ok:
            detail["length"] = length
            return bytes(payload), detail
        k += needed
    detail["length"] = length
    return None, detail


def loopback_selftest():
    payload = otair_test.payload_for(20)
    samples = modulate(payload)
    got, detail = demodulate(samples)
    ok = got == payload
    print("loopback: %s  (sent %d B, got %r)"
         % ("PASS" if ok else "FAIL", len(payload), got))
    print("  detail:", detail)
    return ok


def capture_channels(dev):
    proc = subprocess.run(["arecord", "-D", dev, "--dump-hw-params", "-d", "1"],
                          capture_output=True, text=True, check=False)
    out = proc.stdout + proc.stderr
    for line in out.splitlines():
        if line.strip().startswith("CHANNELS:"):
            return int(line.split(":", 1)[1].strip())
    return 2


def transmit_and_record(payload, play_dev, rec_dev, out_wav, rec_wav, gpio,
                        repeats=1):
    samples = modulate(payload, repeats=repeats)
    otair_test.write_wav(out_wav, samples, FS, channels=2)
    air_s = len(samples) / float(FS)

    rec_alsa_dev = otair_test.resolve_alsa_device(rec_dev, "capture")
    ch = capture_channels(rec_alsa_dev)
    play_alsa_dev = otair_test.resolve_alsa_device(play_dev, "playback")
    # See otair_test.transmit_and_record's cm108_path docstring: with two
    # identical CM108 devices plugged in, PTT has to be tied to whichever
    # cable play_dev actually names, not just "the first one found".
    usb_device = otair_test.alsa_usb_device(play_alsa_dev)
    play_cmd = ["aplay", "-q", "-D", play_alsa_dev, out_wav]
    rec_cmd = ["arecord", "-q", "-f", "S16_LE", "-r", str(FS), "-c", str(ch),
               "-D", rec_alsa_dev, rec_wav]

    rec = subprocess.Popen(rec_cmd, stderr=subprocess.PIPE, text=True)
    try:
        subprocess.run(["sleep", "0.4"], check=False)
        if rec.poll() is not None:
            raise RuntimeError("recorder exited immediately: %s"
                               % (rec.stderr.read().strip() or "no message"))
        with ptt_test.make_ptt("cm108", gpio=gpio, usb_device=usb_device) as ptt:
            ptt.key()
            try:
                print("  keyed, playing %.2f s AFSK burst ..." % air_s)
                subprocess.run(play_cmd, check=False)
            finally:
                ptt.unkey()
        subprocess.run(["sleep", "0.4"], check=False)
        rec.terminate()
        rec.wait(timeout=30)
    finally:
        if rec.poll() is None:
            rec.terminate()

    if not os.path.exists(rec_wav):
        raise RuntimeError("no recording was produced")
    return rec_wav


def analyze_file(rec_wav, expected=None, payload_len_hint=None):
    raw, fs = otair_test.read_wav(rec_wav)
    if fs != FS:
        raise RuntimeError("recording is %d Hz, modem is %d Hz" % (fs, FS))
    samples = [s / 32768.0 for s in raw]
    got, detail = demodulate(samples, payload_len_hint=payload_len_hint)
    ok = got is not None and (expected is None or got == expected)
    print("  decode: %s  detail=%s" % ("PASS" if ok else "FAIL", detail))
    return ok, detail


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loopback", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--analyze", metavar="WAV")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--play-dev", default="5")
    ap.add_argument("--rec-dev", default="6")
    ap.add_argument("--gpio", type=int, default=ptt_test.CM108_DEFAULT_GPIO)
    ap.add_argument("--payload-bytes", type=int, default=20)
    ap.add_argument("--repeats", type=int, default=1,
                    help="send the same payload this many times inside one "
                         "continuous burst, sharing one preamble/sync -- "
                         "blind redundancy, counts delivered if ANY copy "
                         "decodes (default 1, no repetition)")
    ap.add_argument("--out", default="afsk_tx.wav")
    ap.add_argument("--rec-out", default="afsk_rx.wav")
    args = ap.parse_args(argv)

    if args.loopback:
        return 0 if loopback_selftest() else 1
    if args.analyze:
        ok, _ = analyze_file(args.analyze)
        return 0 if ok else 1
    if args.run:
        payload = otair_test.payload_for(args.payload_bytes)
        passed = 0
        for t in range(1, args.trials + 1):
            print("trial %d/%d" % (t, args.trials))
            transmit_and_record(payload, args.play_dev, args.rec_dev,
                               args.out, args.rec_out, args.gpio,
                               repeats=args.repeats)
            ok, detail = analyze_file(args.rec_out, expected=payload,
                                      payload_len_hint=len(payload))
            passed += ok
        print("\nOVER THE AIR: %d/%d delivered intact" % (passed, args.trials))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
