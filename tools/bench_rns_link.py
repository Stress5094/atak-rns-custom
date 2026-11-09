#!/usr/bin/env python3
"""Reproduce the app's exact RNS setup on the desktop: does the PLAIN
broadcast destination actually deliver between two Reticulum instances?

The phones showed both ends transmitting and neither receiving. That is either
an RNS-usage problem (in which case it reproduces here in seconds) or something
Android-specific (in which case this passes and the USB path is the suspect).
Each phone is one process with one Reticulum, one RNode and one PLAIN
destination -- so this runs as two separate processes, not two interfaces in
one, to match.

Usage -- receiver first, then sender:

    sg uucp -c "PYTHON tools/bench_rns_link.py /dev/ttyUSB1 recv"
    sg uucp -c "PYTHON tools/bench_rns_link.py /dev/ttyUSB0 send"
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "app", "src", "main", "python"))

import RNS  # noqa: E402

# MUST match rns_bridge.py exactly -- the destination hash is derived from
# these, and a mismatch means packets transmit fine and are silently dropped.
APP_NAME = "atakrns"
ASPECTS = ("cot", "broadcast")
FREQ, BW, SF, CR = 915000000, 125000, 8, 5
# rns_bridge uses 17; 2 is used here because the radios are inches apart, where
# high power desensitizes the receiver. Any failure at 2 dBm is a logic problem,
# not an RF one.
TXPOWER = int(os.environ.get("BENCH_TXPOWER", "2"))

CONFIG = """[reticulum]
  enable_transport = Yes
  share_instance = No
[logging]
  loglevel = 4
[interfaces]
"""


def main():
    port, role = sys.argv[1], sys.argv[2]
    cfgdir = os.path.expanduser("~/.reticulum-link-%s" % role)
    os.makedirs(cfgdir, exist_ok=True)
    with open(os.path.join(cfgdir, "config"), "w") as f:
        f.write(CONFIG)
    RNS.Reticulum(configdir=cfgdir)

    from RNS.Interfaces.RNodeInterface import RNodeInterface
    from RNS.Interfaces.Interface import Interface
    iface = RNodeInterface(RNS.Transport, {
        "name": "rnode-%s" % role, "port": port,
        "frequency": str(FREQ), "bandwidth": str(BW), "txpower": str(TXPOWER),
        "spreadingfactor": str(SF), "codingrate": str(CR),
    })
    # Exactly what rns_bridge._build_rnode_interface does after construction.
    iface.OUT = True
    iface.IN = True
    iface.mode = Interface.MODE_FULL
    iface.ifac_size = RNodeInterface.DEFAULT_IFAC_SIZE
    RNS.Transport.add_interface(iface)
    iface.final_init()

    deadline = time.time() + 20
    while time.time() < deadline and not iface.online:
        time.sleep(0.5)
    print("[%s] RNode online=%s bitrate=%s" % (role, iface.online, iface.bitrate))
    if not iface.online:
        return 1

    dest = RNS.Destination(None, RNS.Destination.IN, RNS.Destination.PLAIN,
                           APP_NAME, *ASPECTS)
    print("[%s] destination hash = %s" % (role, dest.hexhash))

    received = []
    dest.set_packet_callback(lambda data, packet: received.append(data))

    if role == "send":
        time.sleep(2.0)
        for i in range(3):
            payload = b"BENCH-PACKET-%d-" % i + os.urandom(48)
            RNS.Packet(dest, payload).send()
            print("[send] sent %d bytes (txb now %d)" % (len(payload), iface.txb))
            time.sleep(4.0)
        print("[send] done. txb=%d rxb=%d" % (iface.txb, iface.rxb))
    else:
        print("[recv] listening for 30s ...")
        deadline = time.time() + 30
        while time.time() < deadline:
            if received:
                print("[recv] GOT %d bytes: %r" % (len(received[-1]), received[-1][:24]))
                received.pop()
            time.sleep(0.1)
        print("[recv] done. txb=%d rxb=%d" % (iface.txb, iface.rxb))

    try:
        iface.serial.close()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
