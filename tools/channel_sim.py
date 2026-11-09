"""Inject the impairments this bench actually measured into a clean signal,
and find how much of each the modem really tolerates.

tools/tone_timing.py measured the radio path and eliminated most suspects:
frequency is clean (+0.6 Hz on 1500), timing rate is clean (~-500 ppm), and
tone discrimination is fine at the rates used. What it did find was
smearing in the TIME domain -- symbol-boundary jitter of ~0.2-0.3 ms, and a
transition that takes a few ms to settle -- both roughly FIXED in absolute
time, so both eat a larger share of every symbol the faster the modem runs.

That is a hypothesis about why codec2 fails over the air, not yet a
demonstration. This closes the loop the cheap way, with no radio at all:
take codec2's own modulated burst, add a measured impairment in a
controlled amount, and decode it. Sweeping the amount finds the breaking
point, and comparing that against what the radio path actually does says
whether the impairment is sufficient to explain the failure -- or whether
something still unmeasured has to be involved.

    python3 tools/channel_sim.py --jitter-sweep 0,0.05,0.1,0.2,0.3,0.5
    python3 tools/channel_sim.py --lowpass-sweep 0,50,100,200,400
"""

import argparse
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import otair_test

TRIALS = 8


def apply_jitter(samples, sd_samples, corr_n, rng):
    """Time-warp the signal so symbol boundaries wander by `sd_samples`.

    The offset is redrawn every `corr_n` samples (one symbol) and linearly
    interpolated between draws, which is what "the boundary wobbles from
    symbol to symbol" actually means -- as opposed to a constant offset
    (that is a timing OFFSET, which a receiver simply locks to) or
    white per-sample noise (that is not a timing effect at all).
    """
    if sd_samples <= 0:
        return list(samples)
    n = len(samples)
    npts = n // corr_n + 3
    offs = [rng.gauss(0.0, sd_samples) for _ in range(npts)]
    out = []
    for i in range(n):
        k = i / float(corr_n)
        k0 = int(k)
        frac = k - k0
        off = offs[k0] * (1.0 - frac) + offs[k0 + 1] * frac
        src = i + off
        i0 = int(math.floor(src))
        f = src - i0
        if i0 < 0 or i0 + 1 >= n:
            out.append(0.0)
        else:
            out.append(samples[i0] * (1.0 - f) + samples[i0 + 1] * f)
    return out


def apply_drift(samples, ppm):
    """Resample as if the receiver's clock ran `ppm` fast/slow.

    This, NOT random jitter, is what this bench actually does. The measured
    residual is a dead-straight ramp (lag-1 autocorrelation +0.974) of
    1778 ppm for ~1.35 s, a sharp re-lock, then 234 ppm -- a clock RATE
    offset that drifts and occasionally resets, not uncorrelated wander. A
    timing-recovery loop treats the two completely differently: random
    jitter it averages away, a steady rate offset it must actively track or
    else accumulate, and accumulated error grows with burst length.
    """
    if ppm == 0:
        return list(samples)
    rate = 1.0 + ppm / 1e6
    n = len(samples)
    out = []
    i = 0.0
    while i < n - 1:
        i0 = int(i)
        f = i - i0
        out.append(samples[i0] * (1.0 - f) + samples[i0 + 1] * f)
        i += rate
    return out


def apply_lowpass(samples, fs, tau_us):
    """One-pole low-pass -- bandwidth limiting, which is what makes a tone
    change take time to settle rather than being instant. Same filter
    otair_test.deemphasis() uses, just swept over a much wider range here
    because the question is 'how much smearing breaks it', not 'what
    corrects this radio pair'."""
    return otair_test.deemphasis(list(samples), tau_us, fs)


def trial(modem, payload, impair, rng):
    burst = modem.modulate_burst([payload], lead_in_s=0.1)
    sig = impair(list(burst), rng)
    frames = modem.demodulate(sig)
    _, exact, _ = otair_test.compare(payload, frames)
    return exact > 0


def run_sweep(label, values, make_impair, codename, rs, trials, unit):
    modem = otair_test.build_modem(codename=codename, Rs=rs)
    payload = otair_test.payload_for(modem.payload_bytes)
    sym_s = 1.0 / rs
    print("  %s  (codec2 %s, Rs=%d, one symbol = %.2f ms)"
          % (label, codename, rs, sym_s * 1000.0))
    print("  %-12s  %-16s  decoded" % (unit, "as %% of a symbol"))
    rng = random.Random(1234)
    for v in values:
        impair = make_impair(v, modem)
        ok = sum(trial(modem, payload, impair, rng) for _ in range(trials))
        pct = 100.0 * (v / 1000.0) / sym_s if unit.startswith("jitter") else float("nan")
        pct_s = ("%.0f%%" % pct) if pct == pct else "-"
        print("  %-12.3f  %-16s  %d/%d" % (v, pct_s, ok, trials))
    modem.close()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jitter-sweep", metavar="MS1,MS2,...",
                    help="symbol-boundary jitter, in MILLISECONDS sd. The "
                         "radio path measured ~0.2-0.3 ms.")
    ap.add_argument("--drift-sweep", metavar="PPM1,PPM2,...",
                    help="clock RATE offset in ppm -- the impairment this "
                         "bench actually has (measured 234-1778 ppm)")
    ap.add_argument("--lowpass-sweep", metavar="US1,US2,...",
                    help="one-pole low-pass time constant in MICROSECONDS -- "
                         "bandwidth limiting, i.e. how slowly a tone change "
                         "is allowed to settle")
    ap.add_argument("--codename", default="H_256_512_4")
    ap.add_argument("--rs", type=int, default=500)
    ap.add_argument("--trials", type=int, default=TRIALS)
    args = ap.parse_args(argv)

    if args.jitter_sweep:
        vals = [float(v) for v in args.jitter_sweep.split(",")]

        def mk(ms, modem):
            sd = ms / 1000.0 * modem.Fs
            corr = max(1, int(modem.Fs / args.rs))
            return lambda s, rng: apply_jitter(s, sd, corr, rng)

        run_sweep("JITTER SWEEP", vals, mk, args.codename, args.rs,
                  args.trials, "jitter ms")
        return 0

    if args.drift_sweep:
        vals = [float(v) for v in args.drift_sweep.split(",")]

        def mk(ppm, modem):
            return lambda s, rng: apply_drift(s, ppm)

        run_sweep("DRIFT SWEEP", vals, mk, args.codename, args.rs,
                  args.trials, "drift ppm")
        return 0

    if args.lowpass_sweep:
        vals = [float(v) for v in args.lowpass_sweep.split(",")]

        def mk(us, modem):
            return lambda s, rng: apply_lowpass(s, modem.Fs, us)

        run_sweep("LOW-PASS SWEEP", vals, mk, args.codename, args.rs,
                  args.trials, "tau us")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
