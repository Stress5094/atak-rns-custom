#!/usr/bin/env python3
"""Log every packet each board hands its client: kind, sender, packet id, hop
counts, the board that last transmitted it, size. Several boards at once, one
host clock, so the same packet id seen twice -- or a hop count above zero --
says exactly who repeated what.

    .venv/bin/python tools/mt_sniff.py --seconds 180 PORT [PORT ...]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "app", "src", "main", "python"))

from pubsub import pub  # noqa: E402

import mt_port  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seconds", type=float, default=180)
    p.add_argument("ports", nargs="+")
    args = p.parse_args()

    boards = []
    for i, path in enumerate(args.ports):
        port = mt_port.MeshtasticPort(mt_port.serial_stream_opener(path))
        port.start()
        boards.append((chr(ord("A") + i), port))
    for label, port in boards:
        if not port.wait_online(30):
            print("[fatal] %s offline: %s" % (label, port.last_error))
            return 2
        print("# board %s = node %08x  %s" % (label, port.node_num, path_of(args, label)))
    t0 = time.monotonic()

    def on_packet(packet, interface):
        for label, port in boards:
            if interface is port._iface:
                break
        else:
            return
        d = packet.get("decoded") or {}
        print("%7.2f %s from=%08x to=%s id=%08x port=%-14s hops=%d/%d relay=%s next=%s len=%s rssi=%s"
              % (time.monotonic() - t0, label, packet.get("from") or 0,
                 "all" if packet.get("to") == 0xFFFFFFFF else "%08x" % (packet.get("to") or 0),
                 packet.get("id") or 0, d.get("portnum", "ENCRYPTED"),
                 packet.get("hopStart", 0), packet.get("hopLimit", 0),
                 "%02x" % packet["relayNode"] if "relayNode" in packet else "-",
                 "%02x" % packet["nextHop"] if "nextHop" in packet else "-",
                 len(d.get("payload", b"")) if d else "?", packet.get("rxRssi")))
        sys.stdout.flush()

    pub.subscribe(on_packet, "meshtastic.receive")
    while time.monotonic() - t0 < args.seconds:
        time.sleep(0.5)
    for _, port in boards:
        port.stop()
    return 0


def path_of(args, label):
    return args.ports[ord(label) - ord("A")].rsplit("/", 1)[-1]


if __name__ == "__main__":
    sys.exit(main())
