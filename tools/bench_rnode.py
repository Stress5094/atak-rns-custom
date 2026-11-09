#!/usr/bin/env python3
"""Desktop LoRa bench: two RNodes on the dev machine, one real air gap.

Answers questions the desktop unit suite structurally cannot, without needing
the phones:

  * Does a full-size RNS packet (464 payload + 19 header = 483 B) survive the
    radio? The SX1262 buffer maxes at 255 bytes and the firmware's splitting
    behaviour is not documented in this tree.
  * Does high-entropy (compressed) data survive real firmware's KISS parser?
    Milestone 1 only ever sent ASCII, which needs no escaping at all.
  * Does an unpaced fragment burst overrun the firmware TX buffer?

WHAT THIS DOES NOT COVER: on a desktop, Python reaches the RNode through
pyserial and the Linux kernel CDC driver, so the entire Kotlin bridge
(socket 7100, UsbSerialBridgeService, BulkOnlyCdcDriver) is bypassed. Nor does
it involve ATAK. Treat a pass here as clearing the codec, KISS and the radio
only -- if something passes here and fails on the phones, the Android USB path
is the remaining suspect.

Usage (needs read/write on the serial devices; on Arch that is the `uucp`
group, and a fresh login or `sg uucp -c ...` if the membership is newer than
your session):

    sg uucp -c "/path/to/python tools/bench_rnode.py"

Requires RNS importable. If it lives in a pipx venv, use that venv's
interpreter, e.g.
~/.local/share/pipx/venvs/rns/bin/python

Environment: BENCH_TXPOWER (default 2 dBm -- deliberately low, since radios on
the same desk desensitize each other at high power), BENCH_PORT_A,
BENCH_PORT_B, BENCH_BURST.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "app", "src", "main", "python"))

import RNS  # noqa: E402
import cot_codec  # noqa: E402

PORT_A = os.environ.get("BENCH_PORT_A", "/dev/ttyUSB0")
PORT_B = os.environ.get("BENCH_PORT_B", "/dev/ttyUSB1")
TXPOWER = int(os.environ.get("BENCH_TXPOWER", "2"))
BURST = int(os.environ.get("BENCH_BURST", "8"))

# Match the phone bench so results transfer.
FREQ, BW, SF, CR = 915000000, 125000, 8, 5

MINIMAL_CONFIG = """# bench harness -- intentionally no [[interfaces]] entries
[reticulum]
  enable_transport = No
  share_instance = No
[logging]
  loglevel = 3
[interfaces]
"""


class Capture:
    """Stands in for RNS.Transport: process_incoming() calls owner.inbound()."""

    def __init__(self):
        self.frames = []
        self.lock = threading.Lock()

    def inbound(self, data, iface):
        with self.lock:
            self.frames.append(data)

    def drain(self):
        with self.lock:
            out, self.frames = self.frames, []
            return out


def start_reticulum():
    """Interface.__init__ reaches for RNS.Reticulum.get_instance(), so one
    must exist -- but ONE serves both interfaces, and since we never call
    Transport.add_interface() the router stays out of the data path."""
    cfgdir = os.path.join(os.path.expanduser("~"), ".reticulum-bench")
    os.makedirs(cfgdir, exist_ok=True)
    with open(os.path.join(cfgdir, "config"), "w") as f:
        f.write(MINIMAL_CONFIG)
    return RNS.Reticulum(configdir=cfgdir)


def make_iface(port, owner, name):
    from RNS.Interfaces.RNodeInterface import RNodeInterface
    from RNS.Interfaces.Interface import Interface
    iface = RNodeInterface(owner, {
        "name": name, "port": port,
        "frequency": str(FREQ), "bandwidth": str(BW), "txpower": str(TXPOWER),
        "spreadingfactor": str(SF), "codingrate": str(CR),
    })
    iface.OUT = True
    iface.IN = True
    iface.mode = Interface.MODE_FULL
    return iface


def bring_up():
    start_reticulum()
    a_cap, b_cap = Capture(), Capture()
    print("bringing up %s and %s ..." % (PORT_A, PORT_B))
    a = make_iface(PORT_A, a_cap, "rnode-a")
    b = make_iface(PORT_B, b_cap, "rnode-b")
    for name, iface in (("A", a), ("B", b)):
        deadline = time.time() + 20
        while time.time() < deadline and not iface.online:
            time.sleep(0.5)
        print("  RNode %s online=%s bitrate=%s bps flow_control=%s"
              % (name, iface.online, getattr(iface, "bitrate", "?"), iface.flow_control))
    if not (a.online and b.online):
        raise SystemExit("an RNode never came online")
    return a, b, a_cap, b_cap


def airtime(n, bitrate):
    return ((n + 19) * 8.0) / (bitrate or 3125)


def sweep(a, b_cap, bitrate):
    """Every payload shape milestone 2 can produce, plus the structural edges."""
    from test_cot_codec import PLI, GEOCHAT
    cases = [
        ("compact PLI (real codec output)", cot_codec.encode(PLI)),
        ("zlib GeoChat (real codec output)", cot_codec.encode(GEOCHAT)),
        ("ASCII 200 B", b"<event>" + b"A" * 186 + b"</event>"),
        ("high-entropy 200 B", os.urandom(200)),
        ("high-entropy 400 B", os.urandom(400)),
        ("full MDU payload 464 B", os.urandom(cot_codec.RNS_MAX_PAYLOAD)),
        ("max RNS packet 483 B", os.urandom(cot_codec.RNS_MAX_PAYLOAD + 19)),
        # Worst case for KISS: every byte needs escaping, doubling on the wire.
        ("escape-heavy (all 0xC0) 300 B", b"\xc0" * 300),
        ("escape-heavy (all 0xDB) 300 B", b"\xdb" * 300),
    ]
    print("\n%-36s %6s  %-8s %s" % ("case", "bytes", "result", "detail"))
    print("-" * 78)
    failures = 0
    for label, payload in cases:
        b_cap.drain()
        try:
            a.process_outgoing(payload)
        except Exception as e:
            print("%-36s %6d  %-8s TX raised %s" % (label, len(payload), "FAIL", e))
            failures += 1
            continue
        deadline = time.time() + max(3.0, airtime(len(payload), bitrate) * 4 + 2.0)
        got = []
        while time.time() < deadline and not got:
            got = b_cap.drain()
            time.sleep(0.05)
        if not got:
            print("%-36s %6d  %-8s nothing received" % (label, len(payload), "FAIL"))
            failures += 1
        elif got[0] == payload:
            print("%-36s %6d  %-8s byte-exact" % (label, len(payload), "OK"))
        else:
            print("%-36s %6d  %-8s got %d bytes, %s" % (
                label, len(payload), "CORRUPT", len(got[0]),
                "truncated" if payload.startswith(got[0]) else "mismatched"))
            failures += 1
        time.sleep(0.3)
    print("-" * 78)
    print("%d/%d cases passed" % (len(cases) - failures, len(cases)))
    return failures


def burst(a, b_cap, bitrate):
    """Paced vs unpaced. Tests whether _PACING_FACTOR is needed at all."""
    size = cot_codec.RNS_MAX_PAYLOAD
    at = airtime(size, bitrate)
    print("\nburst test: %d frames of %d B, %.2fs airtime each (%.1fs total)"
          % (BURST, size, at, at * BURST))
    failures = 0
    for paced in (False, True):
        label = "PACED" if paced else "UNPACED"
        frames = [bytes([i]) + os.urandom(size - 1) for i in range(BURST)]
        b_cap.drain()
        t0 = time.time()
        for i, f in enumerate(frames):
            a.process_outgoing(f)
            if paced and i + 1 < BURST:
                time.sleep(at * 1.2)
        submitted = time.time() - t0
        deadline = time.time() + at * BURST * 2 + 10
        got = []
        while time.time() < deadline and len(got) < BURST:
            got.extend(b_cap.drain())
            time.sleep(0.05)
        exact = sum(1 for g in got if g in frames)
        order = [g[0] for g in got if len(g) == size]
        print("  %-8s submitted in %6.2fs -> %d/%d arrived, %d byte-exact%s"
              % (label, submitted, len(got), BURST, exact,
                 "" if order == sorted(order) else "  <-- REORDERED"))
        missing = [i for i in range(BURST) if i not in order]
        if missing:
            print("           MISSING: %s" % missing)
            failures += 1
        time.sleep(2.0)
    return failures


def main():
    a, b, _, b_cap = bring_up()
    bitrate = getattr(a, "bitrate", 3125)
    failures = sweep(a, b_cap, bitrate) + burst(a, b_cap, bitrate)
    for iface in (a, b):
        try:
            iface.serial.close()
        except Exception:
            pass
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
