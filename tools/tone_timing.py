"""Measure what the hop-rate sweep could not: are the tones arriving at the
right FREQUENCY, and are the symbol boundaries arriving at the right TIME.

docs/open-questions.md ("a hop-RATE sweep ... retires the AGC collapse")
narrowed this bench's failure to something neither the level collapse nor
tone discrimination explains: at Rs=100 -- a symbol rate the channel
follows at 100% with good margin -- codec2 still never acquires sync. The
hop metric that established that is handed segment boundaries and takes
the best alignment, so it measures discrimination ALONE. It is blind to
exactly three things, and a real demodulator depends on all of them:

  1. FREQUENCY OFFSET. If tones arrive shifted as a group, a per-segment
     energy comparison still picks the right one (so the hop metric reads
     "fine") while a demodulator that estimates and tracks tone frequency
     is thrown off.
  2. TIMING. If the interval between symbol boundaries is not what the
     receiver expects -- a steady rate error, or jitter around it -- the
     demodulator's clock recovery cannot lock even though every individual
     symbol is perfectly readable.
  3. TRANSITION SHARPNESS. If a tone change takes milliseconds to settle
     rather than being effectively instant, consecutive symbols smear into
     each other. That is invisible to a metric that samples the MIDDLE of
     each segment, and it gets worse in exact proportion to symbol rate --
     the signature this bench's failure has.

    python3 tools/tone_timing.py --freq       # tone frequency vs time
    python3 tools/tone_timing.py --timing     # boundary interval + rise time
"""

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import afsk_modem
import agc_confirm
import otair_test

FS = agc_confirm.FS

# Frequency search. Coarse pass finds the peak's neighbourhood, fine pass
# locates it -- a single 1 Hz scan across the whole range would be tens of
# millions of operations in pure Python for no extra accuracy.
COARSE_SPAN_HZ = 400.0
COARSE_STEP_HZ = 20.0
FINE_SPAN_HZ = 25.0
FINE_STEP_HZ = 0.5

# How far past zero the discriminator must swing before a state change is
# believed. See the hysteresis comment in measure_timing().
HYSTERESIS_FRAC = 0.25

FREQ_WINDOW_S = 0.1     # per-estimate window; 1/T = 10 Hz peak width, and a
                        # peak's LOCATION is resolvable far finer than its width


def estimate_freq(samples, fs, expect_hz):
    """Received frequency of a single strong tone, by two-stage search."""
    def energy(f):
        return afsk_modem.goertzel(samples, f, fs)

    best_f, best_e = expect_hz, -1.0
    f = expect_hz - COARSE_SPAN_HZ / 2.0
    while f <= expect_hz + COARSE_SPAN_HZ / 2.0:
        e = energy(f)
        if e > best_e:
            best_e, best_f = e, f
        f += COARSE_STEP_HZ

    coarse = best_f
    best_e = -1.0
    f = coarse - FINE_SPAN_HZ
    while f <= coarse + FINE_SPAN_HZ:
        e = energy(f)
        if e > best_e:
            best_e, best_f = e, f
        f += FINE_STEP_HZ
    return best_f


def _burst_span(samples, fs):
    """(start, end) sample indices of the transmitted burst."""
    levels = agc_confirm.envelope_rms(samples, fs)
    w = agc_confirm.WINDOW_S
    floor_n = max(1, int(agc_confirm.LEAD_SILENCE_S / w * 0.6))
    head = levels[:floor_n]
    floor = sorted(head)[len(head) // 2] if head else 0.0
    thresh = max(floor * 4.0, 1e-5)
    on = [i for i, v in enumerate(levels) if v >= thresh]
    if not on:
        return None
    # Require a sustained run so a key-up click is not mistaken for the start.
    start = None
    run = 0
    for i, v in enumerate(levels):
        if v >= thresh:
            run += 1
            if run >= 3 and start is None:
                start = i - 2
        else:
            run = 0
    if start is None:
        return None
    # End is derived from the KNOWN transmitted duration, not from where the
    # envelope last exceeds threshold. Inferring it lets the key-down
    # transient and VOX hang extend "the burst" past the tone, and the
    # frequency estimator then happily reports the peak of the noise floor
    # as if it were signal -- which it did, turning a dead-flat 1500.0 Hz
    # result into a fake "-17.6 Hz offset, 56.5 Hz drift". Same bug class as
    # agc_confirm.measure()'s on_end, fixed the same way.
    begin = int(start * w * fs)
    return begin, min(len(samples), begin + int(agc_confirm.TONE_S * fs))


def measure_frequency(rec_wav, expect_hz):
    raw, fs = otair_test.read_wav(rec_wav)
    samples = [s / 32768.0 for s in raw]
    span = _burst_span(samples, fs)
    if span is None:
        print("no burst found")
        return
    start, end = span
    # Skip the key-up transient; stop before the tail.
    start += int(0.25 * fs)
    end -= int(0.15 * fs)
    step = int(FREQ_WINDOW_S * fs)

    print("  t(s)    freq(Hz)   offset(Hz)")
    freqs = []
    t = start
    while t + step <= end:
        f = estimate_freq(samples[t:t + step], fs, expect_hz)
        freqs.append(f)
        print("  %5.2f   %8.1f   %+9.1f"
              % ((t - start) / float(fs), f, f - expect_hz))
        t += step

    if not freqs:
        return
    mean = sum(freqs) / len(freqs)
    var = sum((f - mean) ** 2 for f in freqs) / len(freqs)
    print("\n  transmitted %.1f Hz" % expect_hz)
    print("  received    %.1f Hz mean  (offset %+.1f Hz, %+.0f ppm)"
          % (mean, mean - expect_hz, 1e6 * (mean - expect_hz) / expect_hz))
    print("  drift       %.1f Hz sd, %.1f Hz min-to-max"
          % (math.sqrt(var), max(freqs) - min(freqs)))
    print("\n  For reference: codec2's tones are %d Hz apart, so an offset "
          "of\n  more than about %d Hz starts moving a tone toward its "
          "neighbour." % (500, 250))


def measure_timing(rec_wav, hop_period_s, lo_hz, hi_hz, win_ms=None):
    """Interval between tone changes, and how long each change takes."""
    raw, fs = otair_test.read_wav(rec_wav)
    samples = [s / 32768.0 for s in raw]
    span = _burst_span(samples, fs)
    if span is None:
        print("no burst found")
        return
    start, end = span
    start += int(0.25 * fs)
    end -= int(0.15 * fs)

    # Window well under one hop so a transition is resolved rather than
    # averaged away; step finely so its position is located precisely.
    # Window must be SHORT enough to resolve a transition but LONG enough
    # to tell the two tones apart (roughly 1/spacing). With `win_ms` given,
    # rise time can be measured on the same footing across hop rates
    # instead of the window scaling with the symbol and dragging the
    # measurement floor along with it.
    if win_ms:
        win = max(8, int(win_ms / 1000.0 * fs))
    else:
        win = max(8, int(hop_period_s * fs / 5))
    step = max(1, win // 8)
    centers, d = [], []
    i = start
    while i + win <= end:
        w = samples[i:i + win]
        centers.append(i + win / 2.0)
        d.append(afsk_modem.goertzel(w, hi_hz, fs)
                 - afsk_modem.goertzel(w, lo_hz, fs))
        i += step

    # Sub-sample transition positions, with HYSTERESIS -- a plain zero
    # crossing is not enough here and reporting one cost a wrong headline
    # number once already.
    #
    # A real received discriminator wanders across zero several times around
    # each transition. A plain crossing detector counts every one of those as
    # a separate transition, so the measured "jitter" depends entirely on how
    # finely the discriminator happens to be sampled: at a coarse step the
    # extra crossings are invisible and it reports a plausible 0.196 ms, and
    # at a fine step it reports 3.6 ms. Neither is the channel. The tell is
    # that the number does not CONVERGE as the step shrinks -- a real
    # measurement gets more precise, not wilder.
    #
    # Requiring the signal to swing decisively past +/-h before accepting a
    # state change (while still timestamping the zero crossing itself, which
    # is where the transition really is) makes noise near zero unable to
    # register. With this, the same recording measures 0.165/0.153/0.182/
    # 0.203 ms across a 12x range of step sizes -- stable, therefore real.
    # Remove the discriminator's baseline first. The two tones do not
    # produce equal energy even on a perfect signal -- a 2 ms window holds
    # 2.6 cycles of 1300 Hz but 3.6 of 1800 Hz, so leakage differs -- and
    # this bench has a real tone tilt on top of that. Left in, the bias
    # makes `d` swing hard one way and barely the other, the negative
    # threshold never trips, and only every other transition is counted
    # (which showed up as an empty result on the CLEAN signal, where it is
    # unmistakably a measurement bug rather than a channel effect).
    if not d:
        print("no discriminator samples")
        return
    base = sorted(d)[len(d) // 2]
    d = [x - base for x in d]
    mags = sorted(abs(x) for x in d)
    h = HYSTERESIS_FRAC * mags[len(mags) // 2] if mags else 0.0
    trans = []
    state = 0
    armed = None
    for j in range(len(d)):
        if state <= 0 and d[j] > h:
            if armed is not None:
                trans.append(armed)
            state, armed = 1, None
        elif state >= 0 and d[j] < -h:
            if armed is not None:
                trans.append(armed)
            state, armed = -1, None
        if j > 0 and (d[j - 1] >= 0) != (d[j] >= 0):
            den = d[j - 1] - d[j]
            frac = (d[j - 1] / den) if abs(den) > 1e-15 else 0.5
            frac = min(1.0, max(0.0, frac))
            armed = centers[j - 1] + frac * (centers[j] - centers[j - 1])
    if len(trans) < 4:
        print("only %d transitions found -- not enough to measure" % len(trans))
        return

    ideal = hop_period_s * fs
    if len(trans) < 20:
        print("only %d transitions -- not enough to fit a clock" % len(trans))
        return

    # Jitter as the SPREAD OF RESIDUALS about a least-squares clock fit,
    # NOT as the spread of gaps between consecutive transitions.
    #
    # Gap-differencing cannot survive a missed transition: one miss
    # corrupts two gaps, and discarding them biases the result toward the
    # transitions that happened to be detected cleanly. That is not a
    # theoretical worry -- the gap method here missed ~20% of transitions
    # on real recordings and reported 0.082 ms where the fit reports 0.487,
    # having quietly measured only the easy ones. Assigning each transition
    # an integer index k and fitting t = anchor + k*period absorbs misses by
    # construction (a miss just makes k jump by 2) and uses every detection.
    t0 = trans[0]
    ks = [round((t - t0) / ideal) for t in trans]
    n = len(trans)
    mk = sum(ks) / float(n)
    mt = sum(trans) / float(n)
    num = sum((k - mk) * (t - mt) for k, t in zip(ks, trans))
    den = sum((k - mk) ** 2 for k in ks)
    if den == 0:
        print("degenerate clock fit")
        return
    period = num / den
    anchor = mt - period * mk
    res = [t - (anchor + k * period) for t, k in zip(trans, ks)]
    sd = math.sqrt(sum(r * r for r in res) / len(res))
    mean = period

    rate_err = (mean - ideal) / ideal
    if abs(rate_err) > 0.10:
        print("  MEASUREMENT INVALID: implied rate error %+.0f%% -- the hop "
              "rate cannot\n  really differ from what was sent, so "
              "transition detection is picking up\n  spurious crossings. "
              "Widen --win-ms, or use a tone pair this window\n  length can "
              "actually resolve." % (100.0 * rate_err))
        return

    print("  transmitted hop period : %.3f ms (%.1f samples)"
          % (hop_period_s * 1000.0, ideal))
    print("  received interval      : %.4f ms fitted (%.1f samples)"
          % (mean / fs * 1000.0, mean))
    print("  rate error             : %+.0f ppm" % (1e6 * rate_err))
    print("  jitter                 : %.3f ms sd (%.1f%% of a symbol)"
          % (sd / fs * 1000.0, 100.0 * sd / ideal))
    print("  transitions used       : %d (all detections, none discarded)"
          % len(trans))

    # Rise time: how long the discriminator takes to cross the middle 80% of
    # its swing at each transition. This is the number that decides whether
    # consecutive symbols smear together at speed.
    swing = (sum(abs(x) for x in d) / len(d)) * 2.0
    rises = []
    for t in trans:
        j = min(range(len(centers)), key=lambda k: abs(centers[k] - t))
        lo_i = hi_i = None
        for k in range(j, max(0, j - 40), -1):
            if abs(d[k]) >= 0.4 * swing:
                lo_i = k
                break
        for k in range(j, min(len(d), j + 40)):
            if abs(d[k]) >= 0.4 * swing:
                hi_i = k
                break
        if lo_i is not None and hi_i is not None and hi_i > lo_i:
            rises.append(centers[hi_i] - centers[lo_i])
    if rises:
        rises.sort()
        med = rises[len(rises) // 2]
        print("  transition rise time   : %.3f ms median (%.0f%% of a symbol)"
              % (med / fs * 1000.0, 100.0 * med / ideal))
        print("\n  A rise time that is a large fraction of a symbol means "
              "consecutive\n  symbols smear into each other -- and it costs "
              "proportionally more\n  the faster the modem runs.")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--freq", action="store_true",
                    help="transmit a steady tone and measure its received "
                         "frequency over time")
    ap.add_argument("--timing", action="store_true",
                    help="transmit a hop pattern and measure boundary "
                         "intervals, jitter and transition rise time")
    ap.add_argument("--analyze", metavar="WAV",
                    help="use an existing recording instead of transmitting")
    ap.add_argument("--tone", type=float, default=agc_confirm.TONE_HZ)
    ap.add_argument("--hop-ms", type=float, default=10.0,
                    help="hop period for --timing (default %(default)s ms, "
                         "a rate this channel follows at 100%%)")
    ap.add_argument("--win-ms", type=float, default=None,
                    help="fixed analysis window for --timing, in ms. Needed "
                         "to compare rise time across hop rates on equal "
                         "footing; must exceed ~1/tone-spacing to resolve "
                         "the pair at all (so use the WIDE 800/2300 pair "
                         "when going short).")
    ap.add_argument("--hop-lo", type=float, default=1300.0)
    ap.add_argument("--hop-hi", type=float, default=1800.0)
    ap.add_argument("--play-dev", default="5")
    ap.add_argument("--rec-dev", default="6")
    ap.add_argument("--gpio", type=int, default=3)
    ap.add_argument("--vox", action="store_true")
    ap.add_argument("--out", default="tone_timing_tx.wav")
    ap.add_argument("--rec-out", default="tone_timing_rx.wav")
    args = ap.parse_args(argv)

    if args.freq:
        if not args.analyze:
            agc_confirm.run(args.play_dev, args.rec_dev, args.out,
                            args.rec_out, args.gpio, vox=args.vox,
                            lead_mode="steady", quiet=True)
        measure_frequency(args.analyze or args.rec_out, args.tone)
        return 0
    if args.timing:
        hop_s = args.hop_ms / 1000.0
        if not args.analyze:
            agc_confirm.run(args.play_dev, args.rec_dev, args.out,
                            args.rec_out, args.gpio, vox=args.vox,
                            lead_mode="hop", hop_period_s=hop_s,
                            hop_lo=args.hop_lo, hop_hi=args.hop_hi, quiet=True)
        measure_timing(args.analyze or args.rec_out, hop_s,
                       args.hop_lo, args.hop_hi, win_ms=args.win_ms)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
