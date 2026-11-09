"""Compare the K1 PRO and Digirig Lite as RECEIVERS, holding the
transmitter, both radios, and volumes fixed -- the next test flagged by the
2026-09-13 investigation (docs/open-questions.md, "K1 PRO receiving loses
~10-25%, Digirig Lite receiving has been 100%") but never actually run:
nobody has looked at WHY beyond pass/fail. This inspects the raw envelope
and (in --mode burst) the decode outcome, the same way the TX-side
AGC/leveling collapse was originally found in agc_confirm.py, but with the
RECEIVING cable as the thing that changes between two captures instead of
the transmitting one.

Swapping which cable receives is a physical action (unplug/replug), so this
is a two-step, two-invocation workflow, not a single automated run:

    python3 tools/rx_compare.py --capture --label k1pro    --tx-dev 5 --rx-dev 6 --vox
    # now swap the RX cable for the other one, same radio, same volume
    python3 tools/rx_compare.py --capture --label digirig  --tx-dev 5 --rx-dev 6 --vox
    python3 tools/rx_compare.py --compare rx_compare_k1pro.wav rx_compare_digirig.wav

Use --vox on whichever end has no CM108 (the K1 PRO has none). VOX vs CM108
keying was already shown irrelevant to the TX-side collapse mechanism
(docs/open-questions.md, "the collapse reconfirms... direction is what
matters, not keying method") -- reused here rather than re-litigated, since
holding the TRANSMIT side's cable and keying method fixed across both
captures is what makes this an A/B on the receiver alone.

Two probe modes:
  --mode steady (default): agc_confirm's plain tone. Answers "does a
      leveling/AGC-shaped decay show up on receive the way it does on
      transmit" -- same detector (agc_confirm.measure), same thresholds, so
      a result is directly comparable to every number already on record.
  --mode burst: a real FSK burst (otair_test's own DEFAULT_CONFIG modem).
      Reports decode pass/fail, peak, and the envelope trace across the
      found burst window -- catches a receive-side effect that a steady
      tone can't (real FSK's own earlier finding: the TX-side collapse
      never touched a real burst's envelope, so a steady-tone-only result
      here would not by itself explain the decode-rate gap).

NOT built here (bigger job, flagged as follow-up in the project memory
file): the ground-truth symbol confusion-matrix method that actually found
the 800 Hz tone-plan bug (compare received symbols against the KNOWN
transmitted sequence, since the payload is known). If steady/burst envelope
comparison doesn't explain the gap, that is the next tool to extract.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agc_confirm
import otair_test
import ptt_test

BURST_REPEATS = 3  # matches production's fm_interface default


def capture_steady(tx_dev, rx_dev, out_wav, gpio, vox, tx_gain, quiet):
    return agc_confirm.run(tx_dev, rx_dev, "rx_compare_tx.wav", out_wav, gpio,
                            vox=vox, tx_gain=tx_gain, quiet=quiet)


def capture_burst(tx_dev, rx_dev, out_wav, gpio, vox, tx_gain, quiet,
                   deemphasis_us=0.0):
    modem = otair_test.build_modem()
    payload = otair_test.payload_for(modem.payload_bytes)
    otair_test.transmit_and_record(
        modem, payload, "rx_compare_tx.wav", out_wav,
        play_dev=tx_dev, rec_dev=rx_dev, gpio=gpio, vox=vox,
        repeats=BURST_REPEATS, tx_gain=tx_gain, quiet=quiet)
    decoded, peak = otair_test.decode_recording(modem, out_wav,
                                                deemphasis_us=deemphasis_us)
    frames, exact, byte_errors = otair_test.compare(payload, decoded)
    return dict(ok=exact > 0, frames=frames, peak=peak,
               payload_len=len(payload))


def burst_envelope(wav_path):
    """Envelope trace across the found burst window, same detector
    agc_confirm.measure() uses for the steady-tone case, so a decay shape
    (if any) is comparable on the same axis."""
    raw, fs = otair_test.read_wav(wav_path)
    if not raw:
        return []
    start, end = otair_test.find_burst(raw, fs)
    samples = [s / 32768.0 for s in raw[start:end]]
    return agc_confirm.envelope_rms(samples, fs)


def _print_trace(label, levels):
    print("\n  --- %s ---" % label)
    if not levels:
        print("  (empty)")
        return
    prev = None
    for i, lvl in enumerate(levels):
        t = i * agc_confirm.WINDOW_S
        ratio = (lvl / prev) if prev and prev > 1e-9 else 1.0
        print("  %5.2f   %.5f   %.3f" % (t, lvl, ratio))
        prev = lvl


def compare_steady(wav_a, label_a, wav_b, label_b, verbose):
    ma = agc_confirm.measure(wav_a)
    mb = agc_confirm.measure(wav_b)
    if verbose:
        _print_trace(label_a, ma["levels"])
        _print_trace(label_b, mb["levels"])
    print("\n  %-10s  peak     collapse_t   pre->post ratio" % "receiver")
    for label, m in ((label_a, ma), (label_b, mb)):
        ct = ("%.2fs" % m["collapse_t"]) if m["collapse_t"] is not None else "none"
        print("  %-10s  %.4f   %-11s  %.2fx (%.4f -> %.4f)"
              % (label, m["peak"], ct, m["ratio"], m["pre"], m["post"]))
    if (ma["collapse_t"] is None) != (mb["collapse_t"] is None):
        collapsing = label_a if ma["collapse_t"] is not None else label_b
        print("\n  DIFFERENCE: %s shows the AGC/leveling-style collapse on "
              "receive, the other does not. Same shape as the TX-side "
              "finding, now on the receive path." % collapsing)
    else:
        print("\n  No difference in collapse behavior between the two "
              "receivers at the steady-tone level -- if the decode-rate gap "
              "is real, --mode burst or the ground-truth symbol method "
              "(not yet built, see this file's docstring) is the next "
              "place to look.")


def compare_burst(wav_a, label_a, wav_b, label_b, verbose):
    ea = burst_envelope(wav_a)
    eb = burst_envelope(wav_b)
    if verbose:
        _print_trace(label_a, ea)
        _print_trace(label_b, eb)
    print("\n  %-10s  peak       burst env: first-half avg -> second-half avg"
          % "receiver")
    for label, env in ((label_a, ea), (label_b, eb)):
        raw, fs = otair_test.read_wav(wav_a if label == label_a else wav_b)
        start, end = otair_test.find_burst(raw, fs) if raw else (0, 0)
        peak = max((abs(s) for s in raw[start:end]), default=0) / 32768.0 if raw else 0.0
        if env:
            half = max(1, len(env) // 2)
            first = sum(env[:half]) / half
            second = sum(env[half:]) / max(1, len(env) - half)
            print("  %-10s  %.4f     %.5f -> %.5f (%.2fx)"
                  % (label, peak, first, second,
                     (first / second) if second > 1e-9 else 1.0))
        else:
            print("  %-10s  %.4f     (no burst found)" % (label, peak))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--capture", action="store_true")
    ap.add_argument("--compare", nargs=2, metavar=("WAV_A", "WAV_B"))
    ap.add_argument("--label", default=None,
                    help="capture only: name for this receiver, used to "
                         "build the default --out path and shown in "
                         "--compare output")
    ap.add_argument("--label-a", default=None, help="compare only: override "
                    "the label shown for WAV_A (default: derived from the "
                    "filename)")
    ap.add_argument("--label-b", default=None, help="compare only: override "
                    "the label shown for WAV_B")
    ap.add_argument("--mode", choices=("steady", "burst"), default="steady")
    ap.add_argument("--tx-dev", default="5")
    ap.add_argument("--rx-dev", default="6")
    ap.add_argument("--gpio", type=int, default=ptt_test.CM108_DEFAULT_GPIO)
    ap.add_argument("--vox", action="store_true",
                    help="let the audio key the transmitting radio, for "
                         "whichever end has no CM108 (the K1 PRO)")
    ap.add_argument("--tx-gain", type=float, default=1.0)
    ap.add_argument("--rx-deemphasis-us", type=float, default=0.0,
                    help="burst mode only, passed to decode_recording")
    ap.add_argument("--out", default=None,
                    help="capture only: output wav path (default "
                         "rx_compare_<label>.wav)")
    ap.add_argument("--verbose", action="store_true",
                    help="compare only: print the full per-window envelope "
                         "trace for both recordings, not just the summary")
    args = ap.parse_args(argv)

    if args.capture:
        if not args.label:
            ap.error("--capture needs --label")
        out = args.out or ("rx_compare_%s.wav" % args.label)
        if args.mode == "steady":
            m = capture_steady(args.tx_dev, args.rx_dev, out, args.gpio,
                               args.vox, args.tx_gain, quiet=False)
            print("recorded %s -> %s (peak %.4f, collapse_t=%s)"
                  % (args.label, out, m["peak"],
                     ("%.2fs" % m["collapse_t"]) if m["collapse_t"] is not None
                     else "none"))
        else:
            r = capture_burst(args.tx_dev, args.rx_dev, out, args.gpio,
                              args.vox, args.tx_gain, quiet=False,
                              deemphasis_us=args.rx_deemphasis_us)
            print("recorded %s -> %s (peak %.4f, decode %s)"
                  % (args.label, out, r["peak"], "PASS" if r["ok"] else "FAIL"))
        return 0

    if args.compare:
        wav_a, wav_b = args.compare
        label_a = args.label_a or os.path.splitext(os.path.basename(wav_a))[0]
        label_b = args.label_b or os.path.splitext(os.path.basename(wav_b))[0]
        if not os.path.exists(wav_a):
            ap.error("no such file: %s" % wav_a)
        if not os.path.exists(wav_b):
            ap.error("no such file: %s" % wav_b)
        if args.mode == "steady":
            compare_steady(wav_a, label_a, wav_b, label_b, args.verbose)
        else:
            compare_burst(wav_a, label_a, wav_b, label_b, args.verbose)
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
