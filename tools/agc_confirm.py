"""Re-run the steady-tone AGC/leveling-collapse check from
docs/open-questions.md ("FM handheld -- a radio-side AGC/compander collapses
every transmission", 2026-09-13).

That finding used a plain, unmodulated tone -- nothing for a demodulator to
get wrong -- and found the received level collapsing ~10-13x about 0.9 s into
every transmission, then holding a dead-flat plateau. This script reproduces
that exact method (not a modem, not otair_test.py's burst) so a fresh trace
can be compared against the numbers already recorded in that doc.

Bench wiring assumed, matching the existing hw:5 -> hw:6 pairing already used
for this finding:

    TX   laptop -> hw:5,0 (Digirig Lite, CM108 PTT) -> radio A -> air
    RX   air -> radio B -> hw:6,0 (K1 PRO) -> laptop

    python3 tools/agc_confirm.py --run
    python3 tools/agc_confirm.py --analyze agc_confirm_rec.wav
"""

import argparse
import math
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import afsk_modem
import otair_test
import ptt_test

FS = 48000
TONE_HZ = 1500.0          # mid-passband, same as tone_test.DEFAULT_TONE_HZ
LEAD_SILENCE_S = 0.5
TONE_S = 5.0               # long enough to see the ~0.9s collapse AND a
                            # multi-second plateau afterward
TAIL_SILENCE_S = 1.0
AMP = 0.8

WINDOW_S = 0.05             # envelope resolution

# A drop at least this large counts as "the collapse" -- but only once it is
# SUSTAINED, not a single window. The very first thing after key-up is a
# brief loud click (PTT/VOX transient, independent of program audio level --
# see docs/open-questions.md's note on peak-pinned-at-1.000 not meaning
# clipping) that decays within 1-2 windows on its own; a naive "first big
# drop" scan catches that click decaying, not the real compressor collapse
# roughly a second later. Requiring the drop to hold for SUSTAIN_S tells
# them apart.
DROP_RATIO = 3.0
MIN_RUN_S = 0.15    # consecutive time above threshold before "on" is trusted
SUSTAIN_S = 0.5     # a drop must hold at least this long to count
SKIP_AFTER_ONSET_S = 0.15   # let the click decay before sampling "early"
EARLY_WINDOW_S = 0.3
LATE_WINDOW_S = 1.0


# Tone-hop frequencies for --lead-mode hop: the same two extremes of this
# project's own FSK tone plan (fsk_modem's default first_tone=800,
# tone_spacing=500, M=4 -> 800/1300/1800/2300), so the experiment is asking
# the actual question this bench cares about: does content shaped like our
# own modem's tones avoid the trigger, not an arbitrary pair.
HOP_LO_HZ = 800.0
HOP_HI_HZ = 2300.0
HOP_PERIOD_S = 0.1     # how long each tone is held before switching

AM_RATE_HZ = 5.0       # tremolo rate for --lead-mode am
AM_DEPTH = 0.6         # fraction of amplitude the tremolo swings

CHIRP_LO_HZ = 800.0    # sweep range for --lead-mode chirp
CHIRP_HI_HZ = 2400.0
CHIRP_PERIOD_S = 0.5   # one full sweep up-and-down, repeated


def _payload_steady(n, fs, tone_hz, amp):
    return [amp * math.sin(2.0 * math.pi * tone_hz * i / fs) for i in range(n)]


def _payload_am(n, fs, tone_hz, amp):
    """Constant frequency, but level tremolos -- targets an AGC/compander
    that reacts to a sustained UNCHANGING LEVEL, even if the frequency
    never varies."""
    out = []
    for i in range(n):
        t = i / float(fs)
        env = 1.0 - AM_DEPTH * 0.5 * (1.0 - math.cos(2.0 * math.pi * AM_RATE_HZ * t))
        out.append(amp * env * math.sin(2.0 * math.pi * tone_hz * i / fs))
    return out


def _payload_hop(n, fs, amp, hop_period_s=HOP_PERIOD_S, lo_hz=None, hi_hz=None):
    """Alternates between two tones from this project's own FSK plan --
    targets an AGC/compander that reacts to a sustained UNCHANGING
    SPECTRUM, even if level is constant.

    CONTINUOUS PHASE, deliberately. Building each segment from absolute
    time (`sin(2*pi*f*i/fs)`, what this did first) makes the phase JUMP
    every time the frequency changes -- one discontinuity per hop, each
    splattering energy across the whole band. At 100 ms hops that is 10
    clicks/second and barely matters; swept down to 1 ms it is 1000/second
    and would dominate the measurement, making fast hops look terrible for
    a reason that has nothing to do with the channel. Real FSK is
    continuous-phase for exactly this reason, so this has to be too or the
    hop-rate sweep measures its own generator."""
    lo_hz = HOP_LO_HZ if lo_hz is None else lo_hz
    hi_hz = HOP_HI_HZ if hi_hz is None else hi_hz
    out = []
    hop_n = max(1, int(hop_period_s * fs))
    phase = 0.0
    for i in range(n):
        seg = i // hop_n
        freq = lo_hz if seg % 2 == 0 else hi_hz
        out.append(amp * math.sin(phase))
        phase += 2.0 * math.pi * freq / fs
        if phase > 2.0 * math.pi:
            phase -= 2.0 * math.pi
    return out


def _payload_chirp(n, fs, amp):
    """Continuously sweeps frequency -- another way to avoid a sustained
    unchanging spectrum, without the hop's discontinuities at each switch."""
    out = []
    for i in range(n):
        t = i / float(fs)
        phase_frac = (t % CHIRP_PERIOD_S) / CHIRP_PERIOD_S
        # Triangle sweep 0..1..0 so frequency ramps up then back down,
        # rather than jumping from CHIRP_HI_HZ back to CHIRP_LO_HZ each cycle.
        tri = 2.0 * phase_frac if phase_frac < 0.5 else 2.0 * (1.0 - phase_frac)
        freq = CHIRP_LO_HZ + tri * (CHIRP_HI_HZ - CHIRP_LO_HZ)
        out.append(amp * math.sin(2.0 * math.pi * freq * t))
    return out


LEAD_MODES = ("steady", "am", "hop", "chirp")


def make_tone(fs=FS, tone_hz=TONE_HZ, amp=AMP, tx_gain=1.0, lead_mode="steady",
              hop_period_s=HOP_PERIOD_S, hop_lo=None, hop_hi=None):
    lead = [0.0] * int(LEAD_SILENCE_S * fs)
    n = int(TONE_S * fs)
    if lead_mode == "steady":
        tone = _payload_steady(n, fs, tone_hz, amp)
    elif lead_mode == "am":
        tone = _payload_am(n, fs, tone_hz, amp)
    elif lead_mode == "hop":
        tone = _payload_hop(n, fs, amp, hop_period_s=hop_period_s,
                            lo_hz=hop_lo, hi_hz=hop_hi)
    elif lead_mode == "chirp":
        tone = _payload_chirp(n, fs, amp)
    else:
        raise ValueError("unknown lead_mode %r, must be one of %s"
                         % (lead_mode, LEAD_MODES))
    if tx_gain != 1.0:
        tone = [s * tx_gain for s in tone]
    tail = [0.0] * int(TAIL_SILENCE_S * fs)
    return lead + tone + tail


def envelope_rms(samples, fs, window_s=WINDOW_S):
    step = max(1, int(window_s * fs))
    out = []
    for i in range(0, len(samples) - step + 1, step):
        block = samples[i:i + step]
        out.append(math.sqrt(sum(s * s for s in block) / len(block)))
    return out


# Same probe otair_test.py needed for the same reason (the Digirig Lite's
# mono-only capture) -- shared rather than duplicated a second time.
capture_channels = otair_test.capture_channels


def run(play_dev, rec_dev, out_wav, rec_wav, gpio, vox=False, tx_gain=1.0,
        quiet=False, lead_mode="steady", hop_period_s=HOP_PERIOD_S,
        hop_lo=None, hop_hi=None):
    samples = make_tone(tx_gain=tx_gain, lead_mode=lead_mode,
                        hop_period_s=hop_period_s, hop_lo=hop_lo,
                        hop_hi=hop_hi)
    otair_test.write_wav(out_wav, samples, FS, channels=2)
    air_s = len(samples) / float(FS)

    rec_alsa_dev = otair_test.resolve_alsa_device(rec_dev, "capture")
    ch = capture_channels(rec_alsa_dev)

    play_alsa_dev = otair_test.resolve_alsa_device(play_dev, "playback")
    # Which physical Digirig Lite keys PTT is derived from play_dev itself
    # (same reasoning as otair_test.transmit_and_record's own cm108_path):
    # with two identical CM108 devices plugged in, vendor ID alone can no
    # longer tell them apart, and keying whichever one sorts first is how a
    # bench with two Digirigs quietly keys the WRONG one.
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
        if vox:
            # No PTT to assert -- the tone itself has to key the radio, the
            # same as the K1 PRO's own auto-PTT threshold circuit does for
            # every real transmission through it.
            if not quiet:
                print("  playing %.2f s [%s] at gain %.4f, VOX to key it ..."
                      % (air_s, lead_mode, tx_gain))
            subprocess.run(play_cmd, check=False)
        else:
            with ptt_test.make_ptt("cm108", gpio=gpio,
                                   usb_device=usb_device) as ptt:
                ptt.key()
                try:
                    if not quiet:
                        print("  keyed, playing %.2f s [%s] at gain %.4f ..."
                              % (air_s, lead_mode, tx_gain))
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
    if not quiet:
        print("  recorded to %s" % rec_wav)
    return measure(rec_wav)


def measure_hop_fidelity(rec_wav, hop_period_s, lo_hz=None, hi_hz=None):
    """How well the channel still tells the two hop tones apart, at this
    hop rate. THE metric for the hop-rate sweep.

    Per received hop segment: energy at the tone that SHOULD be there vs.
    energy at the other one. That ratio is a pure within-window comparison,
    so it is immune to the level collapse this file's other sections spend
    so long on -- a channel that halves in gain has the same margin, a
    channel that can no longer follow the hops does not. `decodable` (the
    fraction of segments where the right tone wins) is effectively a
    per-symbol decode rate, and is directly comparable across hop rates:
    ~100% means a modem at this baud could work, ~50% is a coin flip.

    Uses afsk_modem's EXACT-frequency Goertzel, not the integer-bin one
    the rest of these tools share -- at these short windows the bin
    spacing is far too coarse (see that function's own comment).
    """
    lo_hz = HOP_LO_HZ if lo_hz is None else lo_hz
    hi_hz = HOP_HI_HZ if hi_hz is None else hi_hz
    raw, fs = otair_test.read_wav(rec_wav)
    if not raw:
        return None
    samples = [s / 32768.0 for s in raw]
    hop_n = max(1, int(hop_period_s * fs))

    levels = envelope_rms(samples, fs)
    floor_n = max(1, int(LEAD_SILENCE_S / WINDOW_S * 0.6))
    floor = sorted(levels[:floor_n])[len(levels[:floor_n]) // 2] if floor_n else 0.0
    thresh = max(floor * 4.0, 1e-5)
    on_start = None
    run_len = 0
    for i, lvl in enumerate(levels):
        if lvl >= thresh:
            run_len += 1
            if run_len >= 3 and on_start is None:
                on_start = i - 2
        else:
            run_len = 0
    if on_start is None:
        return None

    # Well inside the tone: skip the key-up transient, stop before it ends.
    begin = int(on_start * WINDOW_S * fs) + int(0.25 * fs)
    end = min(len(samples), begin + int(3.5 * fs))

    # The hop pattern's phase AND parity are unknown in the recording, so
    # scan offsets across two hop periods and keep the best -- this is a
    # channel characterisation, not a detection, so taking the best
    # alignment is the right thing (it measures the channel's BEST case).
    best = None
    step = max(1, (2 * hop_n) // 24)
    for off in range(0, 2 * hop_n, step):
        wins, margins = 0, []
        total = 0
        pos = begin + off
        seg = 0
        while pos + hop_n <= end:
            window = samples[pos:pos + hop_n]
            lo = afsk_modem.goertzel(window, lo_hz, fs)
            hi = afsk_modem.goertzel(window, hi_hz, fs)
            want_lo = (seg % 2 == 0)
            expected, other = (lo, hi) if want_lo else (hi, lo)
            if expected > other:
                wins += 1
            margins.append(expected / other if other > 1e-12 else 999.0)
            total += 1
            pos += hop_n
            seg += 1
        if total == 0:
            continue
        frac = wins / float(total)
        margins.sort()
        median = margins[len(margins) // 2]
        # Rank on (decodable, margin), not decodable alone: many offsets tie
        # at 100% on a clean signal and whichever was found first wins
        # otherwise -- often a boundary-straddling alignment whose margin
        # badly understates the channel. Margin is the sensitive number
        # (it degrades long before `decodable` starts to fall), so it has to
        # be what breaks the tie.
        if best is None or (frac, median) > (best[0], best[1]):
            best = (frac, median, total)
    if best is None:
        return None
    return dict(decodable=best[0], median_margin=best[1], segments=best[2])


def measure(rec_wav):
    """The envelope trace plus what matters about it, without printing.

    Split out from analyze() so a gain sweep can ask "did this level
    collapse, and by how much" for many recordings without a full
    per-window trace for each one.

    Distinguishes the real, SUSTAINED compressor collapse from the brief
    key-up click: onset is only trusted after MIN_RUN_S held above
    threshold, "early" is sampled after skipping the click's own decay, and
    a drop only counts once it holds for SUSTAIN_S -- not on the first
    window that happens to dip.
    """
    raw, fs = otair_test.read_wav(rec_wav)
    empty = dict(levels=[], collapse_t=None, peak=0.0,
                pre=0.0, post=0.0, ratio=1.0)
    if not raw:
        return empty
    samples = [s / 32768.0 for s in raw]
    levels = envelope_rms(samples, fs)
    n = len(levels)

    floor_n = max(1, int(LEAD_SILENCE_S / WINDOW_S * 0.6))
    floor = sorted(levels[:floor_n])[len(levels[:floor_n]) // 2] if floor_n else 0.0
    thresh = max(floor * 4.0, 1e-5)

    min_run = max(1, int(MIN_RUN_S / WINDOW_S))
    on_start = None
    run = 0
    for i, lvl in enumerate(levels):
        if lvl >= thresh:
            run += 1
            if run >= min_run and on_start is None:
                on_start = i - min_run + 1
        else:
            run = 0
    if on_start is None:
        # The tone never sustained above the noise floor -- too quiet to
        # have keyed the radio at all, at this gain.
        return dict(levels=levels, collapse_t=None, peak=max(levels),
                   pre=0.0, post=0.0, ratio=1.0)

    skip = int(SKIP_AFTER_ONSET_S / WINDOW_S)
    early_n = max(1, int(EARLY_WINDOW_S / WINDOW_S))
    early_slice = levels[on_start + skip:on_start + skip + early_n]
    early = sum(early_slice) / len(early_slice) if early_slice else levels[on_start]

    # on_end is derived from on_start + TONE_S, not scanned for, because a
    # backward scan for "the last sustained-above-threshold run" is fooled
    # by a key-release transient (a real, separate blip after the tone
    # legitimately ends -- see the postamble spike around t=7.0s in every
    # trace so far) into extending "on" well past where the tone actually
    # stopped. Found via --lead-mode hop reporting a "collapse" at t=5.95s,
    # just after the true 5.5s end (0.5s lead + 5.0s tone) -- not real. We
    # generate the tone ourselves and know its duration exactly, so there is
    # no need to infer the end at all.
    on_end = min(n - 1, on_start + int(TONE_S / WINDOW_S))

    sustain_n = max(1, int(SUSTAIN_S / WINDOW_S))
    collapse_t = None
    search_start = on_start + skip + early_n
    for i in range(search_start, on_end):
        window = levels[i:i + sustain_n]
        if len(window) < sustain_n or i + sustain_n > on_end:
            break
        if all(v < early / DROP_RATIO for v in window):
            collapse_t = i * WINDOW_S
            break

    late_n = max(1, int(LATE_WINDOW_S / WINDOW_S))
    late_slice = levels[max(search_start, on_end - late_n):on_end]
    late = sum(late_slice) / len(late_slice) if late_slice else early
    ratio = (early / late) if late > 1e-9 else 1.0
    return dict(levels=levels, collapse_t=collapse_t, peak=max(levels),
               pre=early, post=late, ratio=ratio)


def analyze(rec_wav):
    m = measure(rec_wav)
    levels = m["levels"]
    if not levels:
        print("empty recording")
        return m
    print("\n  t(s)    level      ratio-to-prev")
    prev = None
    for i, lvl in enumerate(levels):
        t = i * WINDOW_S
        ratio = (lvl / prev) if prev and prev > 1e-9 else 1.0
        flag = "  <-- COLLAPSE" if m["collapse_t"] == t else ""
        print("  %5.2f   %.5f   %.3f%s" % (t, lvl, ratio, flag))
        prev = lvl

    print()
    if m["collapse_t"] is not None:
        print("Collapse detected at t=%.2fs into the recording -- a "
              "sustained %.1fx drop (held >%gs)."
              % (m["collapse_t"], m["ratio"], SUSTAIN_S))
        print("Consistent with the 2026-09-13 finding "
              "(docs/open-questions.md) if this lands ~%.1fs after the tone "
              "starts (t=%.2fs)." % (0.9, LEAD_SILENCE_S + 0.9))
    else:
        print("No collapse of this size detected -- either the tone never "
              "keyed the radio (check peak level below), or the leveling "
              "behavior is not reproducing this time.")
    print("Peak level: %.4f" % m["peak"])
    return m


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--analyze", metavar="WAV")
    ap.add_argument("--play-dev", default="5")
    ap.add_argument("--rec-dev", default="6")
    ap.add_argument("--gpio", type=int, default=ptt_test.CM108_DEFAULT_GPIO)
    ap.add_argument("--vox", action="store_true",
                    help="let the tone itself key the radio (needed on the "
                         "K1 PRO, which has no CM108 PTT) instead of CM108")
    ap.add_argument("--tx-gain", type=float, default=1.0,
                    help="scale the played tone before it reaches the "
                         "radio's mic input, same meaning as "
                         "otair_test.py's --tx-gain")
    ap.add_argument("--sweep", metavar="G1,G2,...",
                    help="run once per comma-separated gain and print a "
                         "one-line summary for each, instead of a single "
                         "run's full trace")
    ap.add_argument("--lead-mode", choices=LEAD_MODES, default="steady",
                    help="steady = the original constant tone (collapses). "
                         "am = constant frequency, tremolo level. "
                         "hop = alternates between this project's own "
                         "800/2300 Hz FSK tone-plan extremes. "
                         "chirp = continuous frequency sweep 800-2400 Hz.")
    ap.add_argument("--mode-sweep", metavar="MODE1,MODE2,...",
                    help="run once per comma-separated lead-mode and print "
                         "a one-line summary for each, to compare which "
                         "signal shapes avoid the collapse")
    ap.add_argument("--hop-sweep", metavar="MS1,MS2,...",
                    help="transmit the two-tone hop pattern once per "
                         "comma-separated hop period (in MILLISECONDS) and "
                         "report how well the channel still tells the two "
                         "tones apart at each rate -- finds the symbol rate "
                         "where this channel stops being able to carry data "
                         "at all, which no steady-tone probe can see")
    ap.add_argument("--hop-lo", type=float, default=HOP_LO_HZ,
                    help="low tone for hop modes (default %(default)s)")
    ap.add_argument("--hop-hi", type=float, default=HOP_HI_HZ,
                    help="high tone for hop modes (default %(default)s). "
                         "Set this to an ADJACENT tone of the real plan "
                         "(e.g. 1300/1800) to measure what the 4FSK "
                         "demodulator actually has to resolve -- the "
                         "800/2300 extremes are the easiest pair and "
                         "overstate the channel.")
    ap.add_argument("--out", default="agc_confirm_tx.wav")
    ap.add_argument("--rec-out", default="agc_confirm_rec.wav")
    args = ap.parse_args(argv)

    if args.analyze:
        analyze(args.analyze)
    elif args.sweep:
        gains = [float(g) for g in args.sweep.split(",")]
        print("  gain      peak     collapse_t   ratio   verdict")
        for g in gains:
            m = run(args.play_dev, args.rec_dev, args.out, args.rec_out,
                    args.gpio, vox=args.vox, tx_gain=g,
                    lead_mode=args.lead_mode, quiet=True)
            if m["collapse_t"] is None:
                verdict = "NO collapse"
            else:
                verdict = "collapses"
            print("  %.4f   %.4f   %s   %5.2fx   %s"
                  % (g, m["peak"],
                     ("%5.2f" % m["collapse_t"]) if m["collapse_t"] is not None
                     else " --  ",
                     m["ratio"], verdict))
    elif args.mode_sweep:
        modes = args.mode_sweep.split(",")
        print("  mode      peak     collapse_t   ratio   verdict")
        for mode in modes:
            m = run(args.play_dev, args.rec_dev, args.out, args.rec_out,
                    args.gpio, vox=args.vox, tx_gain=args.tx_gain,
                    lead_mode=mode, quiet=True)
            verdict = "NO collapse" if m["collapse_t"] is None else "collapses"
            print("  %-8s  %.4f   %s   %5.2fx   %s"
                  % (mode, m["peak"],
                     ("%5.2f" % m["collapse_t"]) if m["collapse_t"] is not None
                     else " --  ",
                     m["ratio"], verdict))
    elif args.hop_sweep:
        periods_ms = [float(p) for p in args.hop_sweep.split(",")]
        print("  hop_ms   baud-equiv   decodable   median_margin   segments")
        for ms in periods_ms:
            run(args.play_dev, args.rec_dev, args.out, args.rec_out,
                args.gpio, vox=args.vox, tx_gain=args.tx_gain,
                lead_mode="hop", hop_period_s=ms / 1000.0, quiet=True,
                hop_lo=args.hop_lo, hop_hi=args.hop_hi)
            m = measure_hop_fidelity(args.rec_out, ms / 1000.0,
                                     lo_hz=args.hop_lo, hi_hz=args.hop_hi)
            if m is None:
                print("  %6.1f   %10s   %9s   %13s   %8s"
                      % (ms, "-", "no burst", "-", "-"))
                continue
            print("  %6.1f   %10.0f   %8.1f%%   %13.2f   %8d"
                  % (ms, 1000.0 / ms, 100.0 * m["decodable"],
                     m["median_margin"], m["segments"]))
    elif args.run:
        run(args.play_dev, args.rec_dev, args.out, args.rec_out, args.gpio,
            vox=args.vox, tx_gain=args.tx_gain, lead_mode=args.lead_mode)
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
