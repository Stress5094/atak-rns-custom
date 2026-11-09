"""Interleaved bidirectional reliability batch for a two-Digirig desktop bench.

Every prior session on this bench (see docs/open-questions.md, "a two-Digirig
bench") found reliability drifts minutes-to-tens-of-minutes for reasons
unrelated to which direction is under test -- a block design (all of A->B,
then all of B->A) confounds "this direction is weaker" with "conditions
happened to be worse during this block". This alternates A->B/B->A every
round instead, same discipline as repeat_ab_test.py and timed_batch.py.

Every modem parameter that matters is a CLI flag with no bench-specific
default baked in beyond what's documented -- an unpinned run compares to
nothing (otair_test.py's own DEFAULT_CONFIG codename, H_1024_2048_4f, is NOT
this bench's historical baseline, H_128_256_5).

    python3 tools/bidir_batch.py --rounds 20 --codename H_128_256_5 --rs 500 \\
        --card-a hw:5,0 --card-b hw:6,0 --log baseline.log
"""

import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import otair_test as ot


def run_one(modem, payload, play_dev, rec_dev, tag, round_i, repeats,
           lead_tone_s, tx_gain, vox, line, log):
    wav = "bidir_tx_%s.wav" % tag
    rec = "bidir_rx_%s_round%d.wav" % (tag, round_i)
    air = ot.transmit_and_record(modem, payload, wav, rec, play_dev, rec_dev,
                                 line=line, vox=vox, lead_tone_s=lead_tone_s,
                                 repeats=repeats, tx_gain=tx_gain, quiet=True)
    got, peak = ot.decode_recording(modem, rec)
    frames, exact, errs = ot.compare(payload, got)
    ok = exact > 0
    now = datetime.datetime.now().strftime("%H:%M:%S")
    line_txt = ("%s dir=%s round=%2d ok=%-5s frames=%d exact=%d air=%5.2fs "
                "peak=%.3f errs=%s"
                % (now, tag, round_i, ok, frames, exact, air, peak, errs))
    print(line_txt)
    log.write(line_txt + "\n")
    log.flush()
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rounds", type=int, default=20,
                    help="trials PER DIRECTION (default 20 -- prior 5/8-"
                         "trial batches on this bench swung 20-40 points and "
                         "one all-clean 8-batch didn't hold up)")
    ap.add_argument("--card-a", required=True, help="e.g. hw:5,0")
    ap.add_argument("--card-b", required=True, help="e.g. hw:6,0")
    ap.add_argument("--codename", required=True,
                    choices=["H_1024_2048_4f", "H_128_256_5", "H_256_512_4",
                             "H_256_768_22", "H_4096_8192_3d"])
    ap.add_argument("--rs", type=int, required=True)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--lead-tone", type=float, default=None,
                    help="seconds before the preamble (default: "
                         "otair_test's own 0.10 PTT / 0.30 VOX)")
    ap.add_argument("--tx-gain", type=float, default=1.0)
    ap.add_argument("--vox", action="store_true")
    ap.add_argument("--line", default="cm108",
                    choices=["rts", "dtr", "break", "cm108"])
    ap.add_argument("--log", default="bidir_batch.log")
    ap.add_argument("--first-tone", type=int, default=None)
    ap.add_argument("--tone-spacing", type=int, default=None)
    args = ap.parse_args(argv)

    over = dict(codename=args.codename, Rs=args.rs)
    if args.first_tone is not None:
        over["first_tone"] = args.first_tone
    if args.tone_spacing is not None:
        over["tone_spacing"] = args.tone_spacing
    modem = ot.build_modem(**over)
    payload = ot.payload_for(modem.payload_bytes)

    print("config: %s" % over)
    print("repeats=%d lead_tone=%s tx_gain=%.3f vox=%s line=%s payload=%dB"
          % (args.repeats, args.lead_tone, args.tx_gain, args.vox, args.line,
             len(payload)))

    results = {"AtoB": [], "BtoA": []}
    with open(args.log, "a") as log:
        log.write("=== %s config=%s repeats=%d lead_tone=%s tx_gain=%.3f "
                  "vox=%s line=%s ===\n"
                  % (datetime.datetime.now().isoformat(), over, args.repeats,
                     args.lead_tone, args.tx_gain, args.vox, args.line))
        for round_i in range(1, args.rounds + 1):
            ok = run_one(modem, payload, args.card_a, args.card_b, "AtoB",
                        round_i, args.repeats, args.lead_tone, args.tx_gain,
                        args.vox, args.line, log)
            results["AtoB"].append(ok)
            ok = run_one(modem, payload, args.card_b, args.card_a, "BtoA",
                        round_i, args.repeats, args.lead_tone, args.tx_gain,
                        args.vox, args.line, log)
            results["BtoA"].append(ok)

    print()
    for tag in ("AtoB", "BtoA"):
        r = results[tag]
        print("%s: %d/%d = %.0f%%" % (tag, sum(r), len(r), 100.0 * sum(r) / len(r)))
    print("BIDIR_BATCH_DONE")


if __name__ == "__main__":
    sys.exit(main())
