"""Measure what the radio path actually does to each frequency, instead of
assuming it is flat between 300 and 3000 Hz.

WHY THIS EXISTS (2026-09-13). fsk_modem's tones are 800/1300/1800/2300 Hz
and FskModem.fits_passband() confirms they sit inside an FM handheld's
300-3000 Hz voice passband -- but "inside the passband" is not "on a flat
part of the passband", and a bench run finally caught the difference. A
burst that arrived STRONG (peak 0.805) decoded 0/3, and profiling the
recording per 100 ms showed the bottom tone arriving at 1-5% of the burst's
energy where the transmitted file had it at 20-40%: one symbol of a
four-symbol alphabet effectively missing. No normalisation or de-emphasis
value recovers that (swept 0-1500 us, nothing decoded), because the
information is gone before the demodulator sees it.

docs/open-questions.md already asked for this, under the de-emphasis
section: "The generalisable version measures the tilt instead of assuming
it ... Not built. Worth doing before this is asked to work with a radio it
hasn't been bench-tested against." This is that, in its simplest useful
form -- a tone at a time, level in, level out.

    python3 tools/passband_sweep.py --play-dev hw:5,0 --rec-dev hw:6,0

WHAT IT DOES NOT TELL YOU. The radio's mic input has its own automatic
level compensation (measured on this bench 2026-09-06: it holds flat down
to ~10% drive and then collapses), so an AGC that tracks slowly will
flatten real differences between tones. Everything here is therefore
RELATIVE, measured inside ONE continuous transmission at ONE drive level,
and the shape across frequency is the result -- not an absolute gain
figure. Two runs at different volumes are not comparable.

The sweep is one continuous keyed transmission with no gaps, deliberately:
a gap long enough to matter would let a VOX cable unkey mid-sweep and the
missing segment would read as a dead spot in the radio's response.
"""

import argparse
import math
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# fm_config lives in the app tree, the same way fm_bench.py reaches it. The
# sweep needs it for ONE thing -- the current tone plan, so the report can
# mark which swept tones are the modem's own -- and deriving that from the
# operating point is what stops it going stale the way it once did across the
# 800 -> 1500 Hz plan fix (see _current_modem_tones()).
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "src", "main", "python"))

# otair_test.py was deleted in the 2026-10-01 FM rebuild ("Remove the FM
# transport"); fm_bench_audio is the proven device and keying layer that
# replaced it, and this tool was left importing the deleted module -- which is
# why it could not run on the rebuilt tree at all. Every ot.* call below now
# resolves to fm_bench_audio, the same layer fm_bench.py drives.
import fm_bench_audio as audio
import ptt_test
import tone_test as tt

import fm_config

FS = audio.BENCH_FS

# Wide enough to find both edges of a voice passband, dense enough around
# the modem's own current tones -- the operating point's 1000/1400/1800/2200
# (see fm_config.OPERATING_POINT) -- to show what happens to each of them,
# and around the OLD 800-2300 plan too, since a regression back toward that
# range should still be visible.
DEFAULT_TONES = (300, 400, 500, 600, 700, 800, 900, 1000, 1200, 1300, 1400,
                 1500, 1600, 1800, 2000, 2200, 2300, 2500, 2800, 3000)

SEG_S = 0.40          # per tone
LEAD_S = 1.6          # at the modem's own first_tone, to key VOX -- the
                      # VOX lead-in the production chain used when this was
                      # written. fm_config now keys by method (0.10 s under
                      # CM108 PTT), and under PTT this is only AGC settle
                      # time; keeping it long also keeps the sweep's own
                      # low-tone segments away from any keying transient.
LEAD_TONE_HZ = 800
AMP = 0.5             # every tone at the SAME amplitude; the point is the
                      # difference in what comes BACK
EDGE_SKIP = 0.25      # ignore this fraction at each end of a segment, so a
                      # tone transition's smear is not measured as the tone

# Above this, a received segment can be trusted to carry its own
# fundamental. Below it, this bench measured the fundamental arriving
# WEAKER than its own 3rd or 5th harmonic -- 800 Hz came back as 2400 Hz --
# so such a segment cannot be used to align the sweep, and its "level"
# needs reporting alongside where the energy actually went.
HARMONIC_SAFE_HZ = 1200
HARMONICS = (2, 3, 5)


def build_sweep(tones, fs=FS):
    """Lead tone then each tone in turn, continuous, as float samples."""
    out = []
    for i in range(int(LEAD_S * fs)):
        out.append(AMP * math.sin(2 * math.pi * LEAD_TONE_HZ * i / fs))
    for hz in tones:
        n = int(SEG_S * fs)
        for i in range(n):
            out.append(AMP * math.sin(2 * math.pi * hz * i / fs))
    return out


def play_and_record(wav_path, rec_path, play_dev, rec_dev, quiet=False,
                    vox=False, line="cm108", gpio=3, cm108_path=None):
    """One continuous keyed transmission, recorded on the far radio.

    Keyed by PTT (deterministic, matching production -- fm_bench_audio's
    default -- and the old otair_test.py's own default) unless `vox` asks for the original audio-threshold keying
    -- VOX cannot be trusted to reproduce a real passband measurement on
    this bench: a low lead tone that itself sits in a weak part of the
    passband can fail to trip VOX at all, so the sweep's own early
    (low-frequency) segments come back looking dead for a keying reason,
    not a channel one -- exactly what an early version of this measurement
    silently confused as HARMONIC-DOMINATED. PTT sidesteps it entirely by
    asserting before any audio plays.
    """
    # Channel count is ASKED of the card, not assumed -- a hardcoded -c 2
    # only ever worked as long as the receiving card was fixed-stereo; a
    # Digirig Lite's capture is mono-only and fm_bench_audio hit this same
    # bug first (see its own transmit_and_record comment).
    rec_alsa_dev = audio.resolve_alsa_device(rec_dev, "capture") if rec_dev else None
    ch = audio.capture_channels(rec_alsa_dev) if rec_alsa_dev else 2
    rec_cmd = ["arecord", "-q", "-f", "S16_LE", "-r", str(FS), "-c", str(ch)]
    if rec_dev:
        rec_cmd += ["-D", rec_alsa_dev]
    rec_cmd += [rec_path]
    play_alsa_dev = audio.resolve_alsa_device(play_dev, "playback") if play_dev else None
    play_cmd = ["aplay", "-q"]
    if play_alsa_dev:
        play_cmd += ["-D", play_alsa_dev]
    play_cmd += [wav_path]

    # Which physical Digirig Lite keys PTT is derived from play_dev itself --
    # see fm_bench_audio.transmit_and_record's cm108_path docstring. Two
    # identical CM108 devices can be plugged in at once, and keying
    # whichever sorts first is how a two-Digirig bench keys the WRONG one.
    usb_device = None
    if not vox and line == ptt_test.CM108_LINE and cm108_path is None and play_alsa_dev:
        usb_device = audio.alsa_usb_device(play_alsa_dev)

    before = os.path.getmtime(rec_path) if os.path.exists(rec_path) else None
    rec = subprocess.Popen(rec_cmd, stderr=subprocess.PIPE, text=True)
    try:
        subprocess.run(["sleep", "0.4"], check=False)
        if rec.poll() is not None:
            raise RuntimeError("recorder exited immediately: %s"
                               % (rec.stderr.read().strip() or "no message"))
        if vox:
            if not quiet:
                print("transmitting the sweep, VOX to key it ...")
            subprocess.run(play_cmd, check=False)
        else:
            with ptt_test.make_ptt(line, gpio=gpio, path=cm108_path,
                                   usb_device=usb_device) as ptt:
                ptt.key()
                try:
                    if not quiet:
                        print("keyed, transmitting the sweep ...")
                    subprocess.run(play_cmd, check=False)
                finally:
                    ptt.unkey()
        subprocess.run(["sleep", "0.4"], check=False)
        rec.terminate()
        rec.wait(timeout=30)
    finally:
        if rec.poll() is None:
            rec.terminate()

    if not os.path.exists(rec_path):
        raise RuntimeError("no recording was produced")
    if before is not None and os.path.getmtime(rec_path) <= before:
        raise RuntimeError(
            "the recording file was not updated -- the recorder failed and an "
            "OLDER file is still on disk. Refusing to analyse it.")


def find_start(samples, fs=FS, thresh=2000):
    """Where audio first arrives in the recording.

    NOT the same thing as where the played file's origin lands -- see
    align(). A VOX cable keys late, so the front of the lead tone is
    missing and this returns a point already some way into it.
    """
    step = int(0.02 * fs)
    for i in range(0, len(samples) - step + 1, step):
        block = samples[i:i + step]
        rms = math.sqrt(sum(x * x for x in block) / len(block))
        if rms > thresh:
            return i
    return None


def align(samples, tones, fs=FS, search_s=1.5):
    """Find where the played file's origin sits in the recording, by search.

    ANCHORING TO FIRST-AUDIO IS WRONG AND THIS COST A WHOLE MEASUREMENT
    (2026-09-13). VOX keys late -- 0.3 s late on the run this was written
    for -- so the first part of the lead tone never reaches the air, and
    treating first-audio as the origin shifts every segment by that much.
    The result was not a plausible-but-wrong number either: it put three of
    the four modem tones in fake local minima and read as if the radio were
    notching our exact tone plan.

    So the offset is searched rather than assumed: score each candidate by
    the energy found at the frequency each segment is SUPPOSED to carry,
    and keep the best. Only tones above HARMONIC_SAFE_HZ are scored,
    because below it the fundamental can be weaker than its own harmonic
    (the finding this tool exists to document) and a segment whose
    fundamental is missing cannot vote on alignment.
    """
    best, best_score = None, -1.0
    step = int(0.01 * fs)
    scored = [(i, hz) for i, hz in enumerate(tones) if hz >= HARMONIC_SAFE_HZ]
    for off in range(0, int(search_s * fs), step):
        score = 0.0
        for i, hz in scored:
            a = off + int((LEAD_S + i * SEG_S) * fs) + int(SEG_S * 0.25 * fs)
            b = off + int((LEAD_S + (i + 1) * SEG_S) * fs) - int(SEG_S * 0.25 * fs)
            if b > len(samples):
                score = -1.0
                break
            score += tt.goertzel(samples[a:b], fs, hz)
        if score > best_score:
            best, best_score = off, score
    return best


def measure(samples, tones, offset, fs=FS):
    """Received level per swept tone, as [(hz, fundamental, rms, harmonics)].

    `harmonics` is {multiple: level} for each of HARMONICS, because a
    fundamental that arrives weaker than its own harmonic is the whole
    finding this tool was built to catch -- reporting only the fundamental
    would show a dead spot and hide WHY it is dead.

    Segments are located from `offset` (see align()) by their known
    duration: a segment the path kills entirely has no energy to search
    for, and must still be reported rather than skipped.
    """
    out = []
    base = offset + int(LEAD_S * fs)
    skip = int(SEG_S * EDGE_SKIP * fs)
    for i, hz in enumerate(tones):
        a = base + int(i * SEG_S * fs) + skip
        b = base + int((i + 1) * SEG_S * fs) - skip
        seg = samples[a:b]
        if len(seg) < fs // 100:
            out.append((hz, 0.0, 0.0, {}))
            continue
        rms = math.sqrt(sum(x * x for x in seg) / len(seg))
        harm = {}
        for n in HARMONICS:
            if hz * n < fs / 2:
                harm[n] = tt.goertzel(seg, fs, hz * n)
        out.append((hz, tt.goertzel(seg, fs, hz), rms, harm))
    return out


def _current_modem_tones():
    """The active tone plan's frequencies, derived rather than hardcoded --
    report()'s own default silently went stale across the 800->1500 Hz
    tone-plan fix once before; deriving it from fm_config's operating point
    (the same object fm_bench.py's defaults come from) means it can't happen
    again the same way.
    """
    return tuple(fm_config.OPERATING_POINT.profile.tone_list())


def report(rows, modem_tones=None, out=None):
    """A table plus a bar chart, in dB relative to the strongest tone."""
    if out is None:
        out = sys.stdout
    if modem_tones is None:
        modem_tones = _current_modem_tones()
    peak = max((g for _, g, _, _ in rows), default=0.0)
    if peak <= 0:
        out.write("nothing was received at any frequency\n")
        return
    out.write("\n  freq   fundamental     dB    rms  strongest   %s\n"
              % "(bar = fundamental, dB below the best)")
    for hz, g, rms, harm in rows:
        db = 20 * math.log10(g / peak) if g > 0 else float("-inf")
        bar = "#" * max(0, int(round(40 + db))) if db > -40 else ""
        # Which of the fundamental and its harmonics actually dominates.
        cand = [(1, g)] + sorted(harm.items())
        n, _ = max(cand, key=lambda c: c[1])
        which = "fundamental" if n == 1 else "%dx = %d Hz" % (n, hz * n)
        mark = " <- modem tone" if hz in modem_tones else ""
        out.write("  %4d  %11.1f  %6.1f  %5.0f  %-11s %-40s%s\n"
                  % (hz, g, db, rms, which, bar, mark))

    harmed = [(hz, harm, g) for hz, g, _, harm in rows
              if harm and max(harm.values()) > g]
    if harmed:
        out.write("\n  HARMONIC-DOMINATED (the fundamental arrived WEAKER "
                  "than its own harmonic):\n")
        for hz, harm, g in harmed:
            n, v = max(harm.items(), key=lambda c: c[1])
            out.write("    %4d Hz -> %4d Hz (%dx), %.1fx stronger than the "
                      "fundamental\n" % (hz, hz * n, n, v / g if g else 0))
        out.write("    A 4FSK symbol at such a tone does not arrive weak, it "
                  "arrives\n    AT THE WRONG FREQUENCY -- which for this tone "
                  "plan can land on\n    top of another symbol's tone.\n")

    modem = [(hz, g) for hz, g, _, _ in rows if hz in modem_tones]
    if len(modem) == len(modem_tones):
        lo = min(g for _, g in modem)
        hi = max(g for _, g in modem)
        out.write("\n  across the modem's own tones %s: %.1fx spread (%.1f dB)"
                  "\n" % (list(modem_tones), hi / lo if lo else float("inf"),
                          20 * math.log10(hi / lo) if lo else float("inf")))
        out.write("  weakest: %d Hz\n" % min(modem, key=lambda m: m[1])[0])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--play-dev", default=None, help="ALSA card, TX radio")
    ap.add_argument("--rec-dev", default=None, help="ALSA card, RX radio")
    ap.add_argument("--out", default="sweep_tx.wav")
    ap.add_argument("--rec-out", default="sweep_rx.wav")
    ap.add_argument("--analyze", metavar="WAV",
                    help="measure an existing recording instead of "
                         "transmitting a new one")
    ap.add_argument("--vox", action="store_true",
                    help="key with audio (the original behaviour) instead "
                         "of PTT. Not recommended: a lead tone that itself "
                         "sits in a weak part of the passband can fail to "
                         "trip VOX at all, which then looks exactly like a "
                         "dead low-frequency response rather than a keying "
                         "problem.")
    ap.add_argument("--line", choices=ptt_test.ALL_LINES, default="cm108")
    ap.add_argument("--gpio", type=int, default=3)
    ap.add_argument("--cm108-path", default=None,
                    help="hidraw node to key for --line cm108, overriding "
                         "auto-detection from --play-dev (see "
                         "fm_bench.py's own --cm108-path)")
    args = ap.parse_args(argv)

    tones = list(DEFAULT_TONES)
    if not args.analyze:
        samples = build_sweep(tones)
        audio.write_wav(args.out, samples, FS, channels=2)
        print("sweep: %d tones, %.1f s lead at %d Hz, %.2f s each, %.1f s total"
              % (len(tones), LEAD_S, LEAD_TONE_HZ, SEG_S,
                 len(samples) / float(FS)))
        play_and_record(args.out, args.rec_out, args.play_dev, args.rec_dev,
                        vox=args.vox, line=args.line, gpio=args.gpio,
                        cm108_path=args.cm108_path)
        rec_path = args.rec_out
    else:
        rec_path = args.analyze

    raw, fs = audio.read_wav(rec_path)
    if fs != FS:
        raise RuntimeError("recording is %d Hz, expected %d" % (fs, FS))
    first = find_start(raw, fs)
    if first is None:
        print("no audio found in the recording -- did the radio key at all?")
        return 1
    offset = align(raw, tones, fs)
    print("audio first arrives at %.2f s; played-file origin aligned to "
          "%.2f s" % (first / float(fs), offset / float(fs)))
    late = (first - offset) / float(fs)
    if late > 0.05:
        print("  -> the transmitter keyed %.2f s LATE: that much of the lead "
              "tone never reached the air" % late)
    report(measure(raw, tones, offset, fs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
