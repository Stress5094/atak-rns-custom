#!/usr/bin/env python3
"""Send real mesh beacons from the laptop's Digirig, so a phone can score them.

WHY THIS EXISTS. fm_bench's bursts are bare frames; a phone only logs frames
that decode AND authenticate as mesh traffic, so a laptop->phone bench run
scored nothing on the phone (the 2026-10-04 CSV rows read 0 delivered with
"the real result is on the phone"). This builds the same bytes the app puts on
the air -- beacon -> mesh envelope (AES-CCM under the group key) -> fm_framing ->
codec2 burst -- and keys the radio with the
bench's own PTT path.

SCORING. The receiving phone counts a peer's beacons by sequence number and
reports "N/M lost" in its status line, so every burst here carries a
consecutive seq starting at 1 and the CSV records seq and the unix time it was
sent. A trailing loss (the LAST beacon) is invisible to a seq-gap counter until
another arrives, so send one more than you care about, or score from the CSV.

THE KEY. Read from ~/.atakrns_passphrase (never printed). A wrong key is silent
on the air -- the phone just counts `unauthenticated` up -- so the printed key
fingerprint must be compared with the one on the phone's status screen BEFORE
a run is believed. A fresh random origin id per run keeps (origin, epoch, seq)
nonces unique across runs.

Usage:
    .venv/bin/python tools/mesh_tx_burst.py --tx hw:3,0 --rec hw:1,0 \\
        --count 30 --gap 8 --note "..."
"""
import argparse
import csv
import datetime
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "src", "main", "python"))
sys.path.insert(0, HERE)

import cot_codec  # noqa: E402
import fm_burst  # noqa: E402
import fm_config  # noqa: E402
import fm_custom  # noqa: E402
import fm_modem  # noqa: E402
import mesh  # noqa: E402
import fm_bench  # noqa: E402
import fm_bench_audio as audio  # noqa: E402

# NO RNS HEADER. On FM the app puts the mesh frame on the air bare: the phone's
# own log reads "frame=29B, 41B on air" (29 + the 12 B envelope). A first
# version of this tool prepended a 19-byte RNS PLAIN header, and the phone
# counted the result as `foreign`. The 19 B in airtime.RNS_HEADER_BYTES is a
# cost MODEL, not what is transmitted.


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tx", required=True, help="playback card of the Digirig")
    ap.add_argument("--rec", required=True,
                    help="capture card; a dummy, so the Digirig only plays")
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--gap", type=float, default=8.0)
    ap.add_argument("--passphrase-file",
                    default=os.path.expanduser("~/.atakrns_passphrase"))
    ap.add_argument("--name", default="bench")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--note", required=True)
    ap.add_argument("--setting", action="append", default=[])
    ap.add_argument("--fs", type=int, default=48000)
    ap.add_argument("--net-code", default="",
                    help="send on these settings instead of the operating "
                         "point, e.g. 4T-1000/500-500Bd-H1024.2048 -- the "
                         "phone's Custom FM net code. Copies, keying and "
                         "lead-in stay the operating point's (this cable "
                         "keys by CM108).")
    args = ap.parse_args(argv)

    key = mesh.derive_key(open(args.passphrase_file).read().strip())
    print("key fingerprint %s -- compare with the phone's status screen"
          % mesh.key_fingerprint(key))
    origin = os.urandom(3)
    m = mesh.Mesh(origin, key, epoch=int(time.time()) & 0xFF)
    print("origin id %s" % mesh.node_id_hex(m.origin))

    op = fm_config.OPERATING_POINT
    chain = fm_bench.bench_chain(args.note, args.tx, args.rec)
    if args.net_code:
        # The NET settings from the code; the per-node ones stay this
        # cable's. Built at the bench's sample rate through the same
        # fm_custom checks the phone's editor runs (at 8000 Hz), so a code
        # the phone refuses is refused here too.
        spec = fm_custom.parse_net_code(args.net_code)
        fm_custom.link_config(spec)
        rate, first, spacing, tones, code = (
            spec.symbol_rate_bd, spec.first_tone_hz, spec.tone_spacing_hz,
            spec.tones, spec.codename)
        ceiling = spec.symbol_rate_bd
    else:
        rate, first, spacing, tones, code = (
            op.profile.symbol_rate_bd, op.profile.first_tone_hz,
            op.profile.tone_spacing_hz, op.profile.tones, op.codename)
        ceiling = op.profile.symbol_rate_ceiling_bd
    prof = fm_bench.make_profile(rate, first, spacing, tones, args.fs,
                                 ceiling, chain)
    cfg = fm_config.LinkConfig(name="mesh-tx", profile=prof,
                               codename=code, repeats=op.repeats,
                               keying=op.keying, lead_in_s=op.lead_in_s)
    print("on air: %s (net code %s)" % (cfg.summary_line(),
                                        fm_custom.net_code(cfg)))
    scratch = os.path.join(os.environ.get("TMPDIR", "/tmp"), "mesh_tx_burst")
    os.makedirs(scratch, exist_ok=True)
    new = not os.path.exists(args.csv)
    with open(args.csv, "a", newline="") as fh, \
            fm_modem.Codec2Fsk(prof, cfg.codename) as modem:
        w = csv.writer(fh)
        if new:
            w.writerow(["utc", "unix", "seq", "origin", "burst_s", "wire_b",
                        "settings", "error", "note"])
        settings = ";".join(args.setting)
        for seq in range(1, args.count + 1):
            frame = cot_codec.encode_beacon(m.origin, seq, name=args.name)
            packet = m.wrap(frame)
            row_err = ""
            try:
                samples = fm_burst.build(modem, packet, cfg)
                audio.transmit_and_record(
                    samples, args.fs, os.path.join(scratch, "tx.wav"),
                    os.path.join(scratch, "rx.wav"), play_dev=args.tx,
                    rec_dev=args.rec, quiet=True)
            except Exception as exc:  # a dead instrument is not a lost burst
                row_err = "%s: %s" % (type(exc).__name__, exc)
            now = time.time()
            w.writerow([datetime.datetime.fromtimestamp(
                now, datetime.timezone.utc).isoformat(timespec="seconds"),
                "%.2f" % now, seq, mesh.node_id_hex(m.origin),
                "%.3f" % cfg.burst_duration_s(), len(packet), settings,
                row_err, args.note])
            fh.flush()
            print("sent seq %d/%d%s" % (seq, args.count,
                                        "  ERROR " + row_err if row_err else ""))
            if row_err:
                return 1
            if seq < args.count:
                time.sleep(args.gap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
