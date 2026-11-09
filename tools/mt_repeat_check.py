#!/usr/bin/env python3
"""Do Meshtastic boards repeat the app's hop-limit-0 packets? Read the boards'
OWN counters to find out, because watching for duplicates cannot: a receiving
board drops a duplicate itself before the client ever sees it.

Two boards on this host, and the board on the phone (or any other) on the same
channel as a third witness. Two windows of equal length, idle then sending:

    idle     nothing of ours on the air; the boards' own housekeeping only
    sending  board S sends N of our packets (PRIVATE_APP, hop limit 0)

and for each window the change in each board's LocalStats -- relays it sent
(num_tx_relay), duplicates it heard (num_rx_dupe), packets it sent and heard.
If boards honour hop limit 0, `sending` adds N to S's tx, N to R's rx, and
NOTHING to any relay or duplicate counter beyond what `idle` shows.

The counters are the board's own LocalStats, which it pushes to its client
every 60 s unasked; a window therefore runs from one push to a later one, and
its length is whole minutes rather than exactly --count x --every.
"""
import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "app", "src", "main", "python"))

from google.protobuf import json_format  # noqa: E402
from meshtastic.protobuf import telemetry_pb2  # noqa: E402
from pubsub import pub  # noqa: E402

import mt_port  # noqa: E402

FIELDS = ("numPacketsTx", "numPacketsRx", "numTxRelay", "numTxRelayCanceled",
          "numRxDupe", "numPacketsRxBad")


class Board(object):
    def __init__(self, label, path):
        self.label = label
        self.port = mt_port.MeshtasticPort(mt_port.serial_stream_opener(path))
        self._stats = None
        self._ev = threading.Event()
        pub.subscribe(self._on, "meshtastic.receive")

    def _on(self, packet, interface):
        if interface is not self.port._iface:
            return
        d = packet.get("decoded") or {}
        if d.get("portnum") != "TELEMETRY_APP":
            return
        t = telemetry_pb2.Telemetry()
        t.ParseFromString(d["payload"])
        if t.HasField("local_stats") and packet.get("from") == self.port.node_num:
            self._stats = json_format.MessageToDict(t.local_stats)
            self._ev.set()

    def stats(self):
        """The NEXT local-stats report the board pushes (every 60 s, unasked).

        Asking for one does not work twice: the first request was answered in
        0.2 s, the next within two minutes was not answered at all, so the
        board limits how often it replies. Waiting for the push costs up to a
        minute per reading and is reliable."""
        self._ev.clear()
        if not self._ev.wait(75):
            raise RuntimeError("board %s pushed no stats in 75 s" % self.label)
        return {f: int(self._stats.get(f, 0)) for f in FIELDS}


def delta(a, b):
    return {f: b[f] - a[f] for f in FIELDS}


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sender", required=True)
    p.add_argument("--receiver", required=True)
    p.add_argument("--count", type=int, default=40)
    p.add_argument("--every", type=float, default=3.0)
    p.add_argument("--size", type=int, default=64)
    args = p.parse_args()

    s, r = Board("S", args.sender), Board("R", args.receiver)
    for b in (s, r):
        b.port.start()
    for b in (s, r):
        if not b.port.wait_online(30):
            print("[fatal] %s offline: %s" % (b.label, b.port.last_error))
            return 2
    heard = [0]
    r.port.set_receiver(lambda payload, *a: heard.__setitem__(0, heard[0] + 1)
                        if payload[:4] == b"RPT1" else None)
    window = args.count * args.every

    results = {}
    for phase in ("idle", "sending"):
        before = {b.label: b.stats() for b in (s, r)}
        t0 = time.monotonic()
        sent = 0
        while time.monotonic() - t0 < window:
            if phase == "sending" and sent < args.count \
                    and time.monotonic() - t0 >= sent * args.every:
                if s.port.send(b"RPT1" + os.urandom(args.size - 4)):
                    sent += 1
            time.sleep(0.05)
        time.sleep(6.0)        # let the last packet and any repeat of it land
        after = {b.label: b.stats() for b in (s, r)}
        results[phase] = {k: delta(before[k], after[k]) for k in before}
        print("[%s] %.0f s, sent %d of ours, receiver heard %d of ours"
              % (phase, window, sent, heard[0]))
        for k in ("S", "R"):
            print("    %s %s" % (k, results[phase][k]))
        heard[0] = 0

    print("\nexcess of 'sending' over 'idle' (what OUR packets caused):")
    for k in ("S", "R"):
        print("    %s %s" % (k, delta(results["idle"][k], results["sending"][k])))
    for b in (s, r):
        b.port.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
