"""Interleaved comparison: phone TX via VOX ("Beacon burst TX") vs. phone TX
via CM108 PTT ("Beacon burst TX (CM108 PTT)"), same beacon-shaped payload,
same production CODENAME/RS/REPEATS, against the laptop's own Digirig Lite
RX -- built 2026-09-20 once CM108 PTT was confirmed to actually key this
phone's radio (see docs/open-questions.md, "investigating the laptop-TX
per-copy softness, CM108 PTT on the phone turns out to WORK").

Interleaved deliberately (one VOX trial, then one CM108 trial, repeated),
same reasoning as phone_laptop_beacon_batch.py: this whole investigation's
own standing lesson is that reliability swings with time, and a batch-per-
keying-method design would measure each one in a different, incomparable
channel state.

    .venv/bin/python3 tools/phone_cm108_tx_batch.py \\
        --serial 192.168.77.113:38435 --card hw:6,0 --trials 10
"""
import argparse
import contextlib
import io
import os
import sys
import threading
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "app", "src", "main", "python"))

import adb_ui  # noqa: E402
import otair_test as ot  # noqa: E402
import beacon_bench as bb  # noqa: E402
import phone_laptop_beacon_test as plb  # noqa: E402

VOX_BUTTON = "Beacon burst TX"
CM108_BUTTON = "Beacon burst TX (CM108 PTT)"
RX_LEAD_S = 2.0


def _dump_bounds_exact(serial, text):
    """Like adb_ui.dump_bounds, but requires an EXACT (case-insensitive)
    match rather than a substring -- "Beacon burst TX" is itself a PREFIX
    of "Beacon burst TX (CM108 PTT)"'s label, so adb_ui's substring match
    cannot tell the two buttons apart reliably.
    """
    import subprocess
    subprocess.run(["adb", "-s", serial, "shell", "uiautomator", "dump",
                    "/sdcard/window_dump.xml"], capture_output=True, check=True)
    xml = subprocess.run(["adb", "-s", serial, "shell", "cat",
                          "/sdcard/window_dump.xml"], capture_output=True,
                         text=True, check=True).stdout
    root = ET.fromstring(xml)
    needle = text.lower()
    for node in root.iter("node"):
        label = node.get("text") or node.get("content-desc") or ""
        if label.lower() == needle:
            m = adb_ui._BOUNDS_RE.match(node.get("bounds") or "")
            x1, y1, x2, y2 = (int(v) for v in m.groups())
            return (x1 + x2) // 2, (y1 + y2) // 2
    raise RuntimeError("no EXACT view matching %r on %s" % (text, serial))


def _run_captured(fn, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args)
    return buf.getvalue()


def phone_tx_trial(serial, card, button_text):
    adb_ui.dismiss_dialog_if_present(serial)
    xy = _dump_bounds_exact(serial, button_text)

    out = {}
    t = threading.Thread(
        target=lambda: out.__setitem__("text", _run_captured(plb.do_rx, card)))
    t.start()
    time.sleep(RX_LEAD_S)
    adb_ui.tap(serial, xy)
    t.join()
    return out["text"].strip().splitlines()[-1] if out.get("text") else "FAIL: no output"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--serial", required=True)
    ap.add_argument("--card", required=True, help="e.g. hw:6,0")
    ap.add_argument("--trials", type=int, default=10,
                    help="trials PER KEYING METHOD (default 10)")
    args = ap.parse_args(argv)

    results = {"vox": [], "cm108": []}
    for i in range(args.trials):
        r1 = phone_tx_trial(args.serial, args.card, VOX_BUTTON)
        results["vox"].append(r1)
        print("[%2d] VOX   TX -> laptop RX: %s" % (i + 1, r1), flush=True)

        r2 = phone_tx_trial(args.serial, args.card, CM108_BUTTON)
        results["cm108"].append(r2)
        print("[%2d] CM108 TX -> laptop RX: %s" % (i + 1, r2), flush=True)

    print("\n== summary ==")
    for method, rs in results.items():
        passed = sum(1 for r in rs if r.startswith("PASS"))
        print("%s: %d/%d delivery" % (method, passed, len(rs)))
        for r in rs:
            print("    %s" % r)


if __name__ == "__main__":
    sys.exit(main())
