#!/usr/bin/env python3
"""Hand-off-to-delivery latency between two Meshtastic boards on ONE host clock.

Phase 2 of the Meshtastic parity plan: the app's retry and ack timers come from
an airtime MODEL (mt_link.time_on_air_s, Semtech's formula over the packet the
board builds). Nothing had measured what the model leaves out -- the board's own
queueing, its listen-before-talk back-off, decryption, the serial hop -- or
checked the formula's slope against a real radio.

Two boards on one laptop remove the clock problem entirely: the send and the
delivery are stamped by the same time.monotonic(). That is the whole reason this
is a desktop tool rather than a phone measurement.

What it reports, per direction and per payload size:
    latency = (time the receiving board handed us the packet)
              - (time the sending board accepted it from us)
and a least-squares line through latency against modelled time on air. A slope
near 1.0 says the formula's per-byte cost is right; the intercept is the fixed
cost the model does not have.

Sizes are shuffled within every round and the two directions alternate packet
by packet, so a slow patch of channel lands on every cell instead of on one.

Usage:
    .venv/bin/python tools/mt_latency.py --a /dev/serial/by-id/...RAK... \\
        --b /dev/serial/by-id/...TRACKER... --rounds 30 --csv out.csv
"""
import argparse
import csv
import os
import random
import statistics
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "app", "src", "main", "python"))

import mt_link  # noqa: E402
import mt_port  # noqa: E402

MAGIC = b"LAT1"
DEFAULT_SIZES = (16, 64, 128, 231)


class Endpoint(object):
    def __init__(self, label, path):
        self.label = label
        self.port = mt_port.MeshtasticPort(mt_port.serial_stream_opener(path))
        self.arrivals = {}          # (run, seq) -> (monotonic, rssi, snr)
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.duplicates = 0          # a second copy of a packet already heard
        self.repeated_paths = 0      # the board says another board repeated it
        self.hopped = 0              # hop_start - hop_limit > 0
        self.port.set_receiver(self._rx)
        self.port.log = self._diag

    def _diag(self, line):
        # mt_port's per-packet line. Our packets go out with hop_limit 0, so a
        # board honouring it never repeats them; either marker below means one
        # did -- the open question the parity plan's warning wording waits on.
        if line.startswith("rx from="):
            if "REPEATED" in line:
                self.repeated_paths += 1
            if " hops=0 " not in line:
                self.hopped += 1

    def _rx(self, payload, rssi, snr, sender):
        now = time.monotonic()
        if len(payload) < 10 or payload[:4] != MAGIC:
            return
        run, seq = struct.unpack(">HI", payload[4:10])
        with self.lock:
            if (run, seq) in self.arrivals:
                self.duplicates += 1
            self.arrivals.setdefault((run, seq), (now, rssi, snr))
        self.event.set()


def payload_for(run, seq, size):
    head = MAGIC + struct.pack(">HI", run, seq)
    return head + os.urandom(size - len(head))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--a", required=True, help="serial device of board A")
    p.add_argument("--b", required=True, help="serial device of board B")
    p.add_argument("--rounds", type=int, default=20)
    p.add_argument("--sizes", default=",".join(str(s) for s in DEFAULT_SIZES))
    p.add_argument("--timeout", type=float, default=10.0,
                   help="seconds to wait for one delivery before calling it lost")
    p.add_argument("--gap", type=float, default=1.0,
                   help="extra random 0..GAP seconds between packets")
    p.add_argument("--preset", default="Long Fast")
    p.add_argument("--csv", default="")
    args = p.parse_args()
    sizes = [int(s) for s in args.sizes.split(",")]
    for s in sizes:
        if not 10 <= s <= mt_link.PAYLOAD_MAX_B:
            p.error("size %d outside 10..%d" % (s, mt_link.PAYLOAD_MAX_B))

    a, b = Endpoint("A", args.a), Endpoint("B", args.b)
    for e in (a, b):
        e.port.start()
    for e in (a, b):
        if not e.port.wait_online(30):
            print("[fatal] board %s did not come online: %s"
                  % (e.label, e.port.last_error))
            return 2
        print("[ok] %s online, node %s, preset %s"
              % (e.label, "%x" % (e.port.node_num or 0), e.port.board_preset()))

    run = random.randrange(1 << 16)
    rows = []
    seq = 0
    start = time.time()
    for rnd in range(args.rounds):
        plan = [(src, dst, size) for size in sizes for (src, dst) in ((a, b), (b, a))]
        random.shuffle(plan)
        for src, dst, size in plan:
            seq += 1
            data = payload_for(run, seq, size)
            dst.event.clear()
            t_handoff = time.monotonic()
            ok = src.port.send(data)
            t_accepted = time.monotonic()
            arrived = None
            if ok:
                deadline = t_accepted + args.timeout
                while time.monotonic() < deadline:
                    with dst.lock:
                        arrived = dst.arrivals.get((run, seq))
                    if arrived is not None:
                        break
                    dst.event.wait(0.05)
                    dst.event.clear()
            toa = mt_link.time_on_air_s(size, args.preset)
            row = {
                "round": rnd, "seq": seq, "dir": "%s->%s" % (src.label, dst.label),
                "size": size, "model_toa_s": round(toa, 4),
                "handoff_s": round(t_accepted - t_handoff, 4),
                "accepted": int(bool(ok)),
                "delivered": int(arrived is not None),
                "latency_s": "" if arrived is None else round(arrived[0] - t_accepted, 4),
                "rssi": "" if arrived is None else arrived[1],
                "snr": "" if arrived is None else arrived[2],
                "wall": round(time.time(), 2),
            }
            rows.append(row)
            print("[%3d] %s %3d B  %s  model %.2f s%s"
                  % (rnd, row["dir"], size,
                     "latency %.2f s" % row["latency_s"] if arrived else
                     ("LOST" if ok else "NOT ACCEPTED (%s)" % src.port.last_error),
                     toa, ""))
            sys.stdout.flush()
            time.sleep(0.3 + random.random() * args.gap)

    for e in (a, b):
        print("[board %s] sent=%d received=%d refused=%s rejected=%d queue_full=%d "
              "stale=%d duplicates=%d repeated_path=%d hops>0=%d"
              % (e.label, e.port.sent, e.port.received, e.port.tx_refused,
                 e.port.board_rejected, e.port.send_refused_full,
                 e.port.dropped_stale, e.duplicates, e.repeated_paths, e.hopped))
        e.port.stop()

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print("[csv] %s" % args.csv)

    summarize(rows, time.time() - start)
    return 0


def summarize(rows, elapsed):
    print("\n== %d packets in %.0f s ==" % (len(rows), elapsed))
    print("%-6s %4s %6s %6s %7s %7s %7s %7s %7s"
          % ("dir", "size", "deliv", "model", "min", "median", "p90", "max", "excess"))
    for d in sorted({r["dir"] for r in rows}):
        for s in sorted({r["size"] for r in rows}):
            cell = [r for r in rows if r["dir"] == d and r["size"] == s]
            if not cell:
                continue
            lat = sorted(r["latency_s"] for r in cell if r["delivered"])
            n_ok = len(lat)
            model = cell[0]["model_toa_s"]
            if lat:
                p90 = lat[min(len(lat) - 1, int(round(0.9 * (len(lat) - 1))))]
                print("%-6s %4d %3d/%-3d %6.2f %7.2f %7.2f %7.2f %7.2f %+7.2f"
                      % (d, s, n_ok, len(cell), model, lat[0],
                         statistics.median(lat), p90, lat[-1],
                         statistics.median(lat) - model))
            else:
                print("%-6s %4d %3d/%-3d %6.2f   (none delivered)"
                      % (d, s, n_ok, len(cell), model))
    pts = [(r["model_toa_s"], r["latency_s"]) for r in rows if r["delivered"]]
    if len(pts) >= 3 and len({x for x, _ in pts}) >= 2:
        xs, ys = zip(*pts)
        mx, my = statistics.mean(xs), statistics.mean(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in pts) / sxx
        icpt = my - slope * mx
        resid = [y - (icpt + slope * x) for x, y in pts]
        print("\nfit: latency = %.3f + %.3f x model_toa   (resid sd %.3f s, n=%d)"
              % (icpt, slope, statistics.pstdev(resid), len(pts)))
        print("     slope 1.0 = the formula's per-byte cost is right; the intercept "
              "is the board's fixed cost")


if __name__ == "__main__":
    sys.exit(main())
