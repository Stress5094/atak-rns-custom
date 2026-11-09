"""A/B test: does sending the same payload N times inside one keyed burst
(blind redundancy, no ack, no return channel) recover deliveries that a
single transmission misses?

Interleaves repeat counts round by round -- one trial at each count, in
sequence, repeated -- rather than running each count as its own block of
trials. 2026-09-06's timed batch found reliability itself swings with time
(98% back to back, 50% spread over an hour); a block design would confound
"repeats help" with "conditions happened to be better during this block".
Interleaving means every count sees the same sequence of channel conditions.

    python3 tools/repeat_ab_test.py --rounds 10
"""

import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import otair_test as ot

# ALSA card ids, overridable per run -- see timed_batch.py's copy of this
# comment for why the CARD= spelling rather than an index, and for the
# warning that which cable holds which radio cannot be read from software.
PLAY_DEV = os.environ.get("RTAK_PLAY_DEV", "hw:CARD=Audio,DEV=0")
REC_DEV = os.environ.get("RTAK_REC_DEV", "hw:CARD=Audio_1,DEV=0")

REPEAT_COUNTS = (1, 2, 3)


def run_one(modem, payload, repeats, rec_out, vox, tx_gain=1.0):
    air = ot.transmit_and_record(modem, payload, "repeat_tx.wav", rec_out,
                                 PLAY_DEV, REC_DEV, vox=vox, quiet=True,
                                 repeats=repeats, tx_gain=tx_gain)
    got, peak = ot.decode_recording(modem, rec_out)
    frames, exact, errs = ot.compare(payload, got)
    return bool(exact), frames, air, peak


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--no-vox", action="store_true",
                    help="key with PTT instead of VOX")
    ap.add_argument("--tx-gain", type=float, default=1.0,
                    help="deliberately weaken the transmitted signal -- see "
                         "otair_test.py --tx-gain. 0.085 was the calibrated "
                         "value found 2026-09-06 on this bench: this radio's "
                         "mic input holds up fine down to about 0.09 and "
                         "then collapses sharply, so there is a narrow band "
                         "worth aiming at rather than a smooth slope.")
    args = ap.parse_args(argv)
    vox = not args.no_vox

    modem = ot.build_modem()
    payload = ot.payload_for(modem.payload_bytes)

    results = {r: [] for r in REPEAT_COUNTS}
    with open("repeat_ab.log", "w") as log:
        for round_i in range(1, args.rounds + 1):
            for repeats in REPEAT_COUNTS:
                rec_out = "repeat_r%d_round%d.wav" % (repeats, round_i)
                ok, frames, air, peak = run_one(modem, payload, repeats,
                                                rec_out, vox, args.tx_gain)
                results[repeats].append(ok)
                now = datetime.datetime.now().strftime("%H:%M:%S")
                line = ("%s round=%2d repeats=%d ok=%-5s frames=%d/%d "
                        "air=%5.2fs peak=%.3f"
                        % (now, round_i, repeats, ok, frames, repeats, air,
                           peak))
                print(line)
                log.write(line + "\n")
                log.flush()

    print()
    for repeats in REPEAT_COUNTS:
        r = results[repeats]
        print("repeats=%d: %d/%d = %.0f%%"
              % (repeats, sum(r), len(r), 100.0 * sum(r) / len(r)))
    print("REPEAT_AB_DONE")


if __name__ == "__main__":
    sys.exit(main())
