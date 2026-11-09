#!/usr/bin/env python3
"""Listen on the laptop's Digirig and log every mesh frame a phone sends over FM.

The receive half that mesh_tx_burst.py lacks: with both, a phone <-> laptop
link can be scored in both directions on the SAME settings, including a
phone's Custom ones (--net-code).

THE PHONE'S OWN RECEIVE PATH, not a bench copy of it: audio goes through
fm_link.feed_audio -- levels, de-emphasis, the codec2 modem, unscramble and
unframe -- exactly as FmAudioBridgeService feeds it on the phone, at the
phone's 8000 Hz (ALSA's plughw resamples). So a frame this logs is a frame the
phone's code would have recovered from this audio.

Each line is one frame: unix time, what it was, and for a beacon its sequence
number, so delivery is a count against the sender's log.

    .venv/bin/python tools/fm_listen.py --rec plughw:3,0 \\
        --passphrase-file F --net-code 4T-1000/500-500Bd-H1024.2048 \\
        --seconds 300 --csv results/....csv
"""
import argparse
import array
import csv
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app", "src", "main", "python"))

import rns_stub  # noqa: E402

rns_stub.install()

import cot_codec  # noqa: E402
import fm_config  # noqa: E402
import fm_custom  # noqa: E402
import fm_link  # noqa: E402
import mesh  # noqa: E402

CHUNK_BYTES = 1600      # 0.1 s at 8000 Hz mono 16-bit


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rec", required=True,
                    help="capture device; use plughw: so ALSA resamples to 8000")
    ap.add_argument("--passphrase-file", required=True)
    ap.add_argument("--net-code", default="",
                    help="the phone's Custom net code; default = the operating point")
    ap.add_argument("--seconds", type=float, required=True)
    ap.add_argument("--csv", required=True)
    args = ap.parse_args(argv)

    if args.net_code:
        spec = fm_custom.parse_net_code(args.net_code)
        _name, err = fm_link.set_custom(spec.to_json())
        if err:
            ap.error("net code refused: %s" % err)
    else:
        fm_link.set_profile(fm_config.DEFAULT_PROFILE)
    print("listening on: %s" % fm_link.summary_line())
    print("net code %s" % fm_link.net_code())

    key = mesh.derive_key(open(args.passphrase_file).read().strip())
    print("key fingerprint %s" % mesh.key_fingerprint(key))
    m = mesh.Mesh(os.urandom(3), key, epoch=0)
    parser = fm_link.FrameParser()

    rec = subprocess.Popen(
        ["arecord", "-q", "-D", args.rec, "-f", "S16_LE", "-r",
         str(fm_config.SAMPLE_RATE_HZ), "-c", "1", "-t", "raw"],
        stdout=subprocess.PIPE)
    new = not os.path.exists(args.csv)
    end = time.time() + args.seconds
    frames = 0
    peak, next_level = 0, time.time() + 5.0
    try:
        with open(args.csv, "a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["unix", "kind", "origin", "seq", "bytes",
                            "net_code", "detail"])
            while time.time() < end:
                pcm = rec.stdout.read(CHUNK_BYTES)
                if not pcm:
                    print("capture ended early")
                    return 1
                # The level, every 5 s: "no audio at all" and "audio that
                # does not decode" are different faults, and only this tells
                # them apart.
                peak = max(peak, max(abs(v) for v in array.array("h", pcm)))
                if time.time() >= next_level:
                    print("%.0f level peak %.3f" % (time.time(), peak / 32768.0),
                          flush=True)
                    peak, next_level = 0, time.time() + 5.0
                for raw in parser.feed(fm_link.feed_audio("", pcm)):
                    now = time.time()
                    frames += 1
                    inb = m.unwrap(raw)
                    if inb is None:
                        kind, origin, seq, detail = ("not-ours", "", "",
                                                     str(m.stats()))
                    else:
                        origin = mesh.node_id_hex(inb.origin)
                        kind, seq, detail = "frame", "", ""
                        if cot_codec.is_beacon(inb.frame):
                            b = cot_codec.decode_beacon(inb.frame)
                            kind, seq = "beacon", b.seq
                            detail = "name=%s" % (b.name,)
                    w.writerow(["%.2f" % now, kind, origin, seq, len(raw),
                                fm_link.net_code(), detail])
                    fh.flush()
                    print("%.2f %s %s seq=%s %dB %s"
                          % (now, kind, origin, seq, len(raw), detail),
                          flush=True)
    finally:
        rec.terminate()
    print("done: %d frame(s) in %.0f s" % (frames, args.seconds))
    return 0


if __name__ == "__main__":
    sys.exit(main())
