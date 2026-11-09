"""Key a radio's PTT from a USB serial cable, and find out what it costs.

VOX costs 3300 ms of dead air per transmission on the MD-UV390 and 4475 ms on
the UV-5R, measured. The case for a PTT interface rests on the claim that
hardware keying costs almost nothing by comparison -- but "almost nothing" has
been an assertion in this project, never a measurement, and this file exists to
stop it being one.

A Baofeng CH340 programming cable is enough to find out. It carries no audio,
so it cannot pass data, but PTT does not need audio: key the transmitter for a
known length of time and see how long the carrier is actually up.

    python3 tools/ptt_test.py --probe            # which line, if any, keys it
    python3 tools/ptt_test.py --measure --line rts

The measurement compares a DURATION against a DURATION -- carrier-on against
PTT-asserted -- so it never needs to know when the recorder started relative to
the keying. That distinction is not pedantic: measuring a timestamp against a
timestamp across two processes is exactly what made the VOX attack figure
oscillate between 0 ms and 440 ms for the same radio.
"""

import argparse
import fcntl
import math
import os
import re
import struct
import subprocess
import sys
import termios
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tone_test as tt

DEFAULT_DEV = "/dev/ttyUSB0"

# Linux asm-generic/ioctls.h. Python's termios exposes TIOCMGET and friends but
# not the break pair on every build, so they are named here rather than
# depended upon.
TIOCSBRK = 0x5427
TIOCCBRK = 0x5428

LINES = ("rts", "dtr", "break")

# The Digirig Lite has no serial chip at all -- it is a bare C-Media codec, and
# PTT is a GPIO on the sound chip itself, driven by writing a HID report. Same
# job as RTS, different wire entirely.
CM108_LINE = "cm108"
ALL_LINES = LINES + (CM108_LINE,)

CM108_VENDOR = 0x0D8C          # C-Media
CM108_DEFAULT_GPIO = 3         # what Digirig wires PTT to

# Five bytes: [report id, 0, data, mask, 0]. The datasheet describes four and
# four fails with EPIPE -- hamlib and direwolf both write five, and five is
# what works.
CM108_REPORT_LEN = 5

# How long to hold the line during a probe. Long enough to see an LED and hear
# the other radio go quiet, short enough that nobody is transmitting into a
# stuck key while they look for a torch.
PROBE_KEY_S = 2.0
PROBE_GAP_S = 2.0

# The keying interval for a measurement. One second is far longer than any
# plausible turnaround, so the difference is the overhead and not the noise.
MEASURE_KEY_S = 1.0

# Recorded silence before and after the keying. The lead is the hiss reference
# and has to be long enough to take a median over; the tail has to outlast any
# hold-over the radio has after the line is released.
MEASURE_LEAD_S = 2.0
MEASURE_TAIL_S = 2.5

# A carrier quiets an open squelch. Below this fraction of the hiss counts as
# keyed -- the same threshold tone_test uses, and for the same reason: an
# absolute level is a guess about somebody's recording gain.
CARRIER_QUIET_FRACTION = 0.6

# How far above the idle floor a window has to sit to count as LOUD, for the
# squelch-closed case below. Measured against two real recordings taken on
# this bench 2026-10-03 with level-3 squelch: a working direction sat at an
# idle floor of 2.3 with the carrier at ~1400 (600x), and a direction with no
# carrier sat at 29 with only key-switching transients reaching ~340 in the
# median (12x) for 90 ms. Any factor from 5 to 100 separates those two, so 10
# is the middle of a wide valley rather than a tuned threshold.
LOUD_FACTOR = 10.0


def read_uevent_field(path, field):
    """One `KEY=value` field out of a sysfs uevent file, or None."""
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return None
    prefix = field + "="
    for row in text.splitlines():
        if row.startswith(prefix):
            return row[len(prefix):]
    return None


def usb_device_id(path):
    """DEVNAME of the physical USB device that `path` (or an ancestor of
    it) belongs to, or None.

    Walks upward from `path` (resolving symlinks first) until a
    directory's own `uevent` says `DEVTYPE=usb_device` -- the actual
    physical USB device, common ancestor of every interface/class node
    hanging off it -- rather than assuming a fixed number of `..` hops.
    That assumption does not hold here: an ALSA card's `device` symlink
    points straight at its USB *interface* directory, while a hidraw
    node's points two levels deeper, at the HID class device nested
    inside its own interface directory in turn. Matching by DEVNAME
    (e.g. "bus/usb/003/013") rather than by sysfs path also survives a
    replug that changes which port/bus numbers a device lands on but
    not which physical unit it is -- not relied on here, but free.

    This is how a hidraw node and an ALSA card get tied together as two
    interfaces of the SAME physical Digirig Lite: neither the CM108
    vendor:product ID nor either interface's own class alone says which
    cable it is once a second, identical unit is plugged in.
    """
    path = os.path.realpath(path)
    for _ in range(8):  # USB nesting never remotely reaches this deep
        if read_uevent_field(os.path.join(path, "uevent"),
                             "DEVTYPE") == "usb_device":
            return read_uevent_field(os.path.join(path, "uevent"), "DEVNAME")
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    return None


def find_cm108(vendor=CM108_VENDOR, base="/sys/class/hidraw", usb_device=None):
    """The hidraw node belonging to a C-Media codec, or None.

    Found by vendor rather than by a fixed /dev/hidrawN, because the number
    depends on what else was plugged in first and pointing PTT at whatever
    landed on hidraw0 is how people key the wrong device.

    `usb_device` narrows the search to one physical USB device -- the
    output of `usb_device_id(<node>/device)`, the same value
    `otair_test.alsa_usb_device()` computes for an ALSA card's kernel node.
    Two Digirig Lites report the identical vendor:product, so once a
    second one is plugged in, vendor alone is no longer enough to say
    which cable's PTT this is.

    With two or more CM108 nodes found and no `usb_device` to disambiguate,
    raises rather than silently keying whichever sorts first alphabetically
    -- the exact failure this function was written to avoid in the first
    place, just one step further along now that a vendor match is not
    unique either.
    """
    if not os.path.isdir(base):
        return None
    candidates = []
    for name in sorted(os.listdir(base)):
        device_dir = os.path.join(base, name, "device")
        uevent = os.path.join(device_dir, "uevent")
        try:
            with open(uevent) as fh:
                text = fh.read()
        except OSError:
            continue
        for row in text.splitlines():
            if not row.startswith("HID_ID="):
                continue
            parts = row.split("=", 1)[1].split(":")
            if len(parts) == 3 and int(parts[1], 16) == vendor:
                candidates.append((name, device_dir))
            break
    if usb_device is not None:
        matches = [name for name, device_dir in candidates
                   if usb_device_id(device_dir) == usb_device]
        return "/dev/" + matches[0] if matches else None
    if len(candidates) > 1:
        names = ", ".join("/dev/" + name for name, _ in candidates)
        raise RuntimeError(
            "%d CM-Media HID devices found (%s) -- ambiguous which one is "
            "PTT with more than one Digirig Lite plugged in. Pass a "
            "usb_device to disambiguate (otair_test.py derives this "
            "automatically from --play-dev), or a specific --cm108-path."
            % (len(candidates), names))
    if not candidates:
        return None
    return "/dev/" + candidates[0][0]


class _PttBase(object):
    """Shared open/release discipline.

    The release path is the one that matters. A PTT that fails to assert wastes
    a bench run; a PTT left asserted is a transmitter keyed on a live channel
    until somebody notices, so it is released on entry, on exit, and on the
    error path.
    """

    def __enter__(self):
        self._open()
        self.unkey()
        return self

    def __exit__(self, *exc):
        try:
            self.unkey()
        finally:
            self._close()
        return False

    def keyed_for(self, seconds):
        """Hold PTT for `seconds`, and report how long it was really held.

        The returned figure is the measured hold, not the requested one. A
        scheduler hiccup between key and unkey would otherwise be silently
        charged to the radio.
        """
        self.key()
        t0 = time.monotonic()
        try:
            time.sleep(seconds)
        finally:
            held = time.monotonic() - t0
            self.unkey()
        return held


class Cm108Ptt(_PttBase):
    """PTT as a GPIO pin on a C-Media sound chip, set by a HID report."""

    line = CM108_LINE

    def __init__(self, path=None, gpio=CM108_DEFAULT_GPIO, usb_device=None):
        if not 1 <= gpio <= 8:
            raise ValueError("cm108 gpio must be 1-8, not %r" % (gpio,))
        self.gpio = gpio
        self.path = path
        # Only consulted when `path` itself is None -- an explicit path always
        # wins, the same precedence resolve_alsa_device gives an explicit
        # hw:N,0 over a substring match.
        self.usb_device = usb_device
        self.fd = None

    def _report(self, on):
        bit = 1 << (self.gpio - 1)
        # The mask byte declares the pin an output and is set whether keying or
        # releasing; only the data byte changes.
        return bytes([0, 0, bit if on else 0, bit, 0])

    def _open(self):
        path = self.path or find_cm108(usb_device=self.usb_device)
        if path is None:
            raise RuntimeError(
                "no C-Media HID device found. The Digirig Lite should appear "
                "as a hidraw node with vendor 0d8c -- check it is plugged in.")
        self.path = path
        try:
            self.fd = os.open(path, os.O_WRONLY)
        except PermissionError:
            raise PermissionError(
                "cannot write to %s -- hidraw nodes are root-only by default, "
                "which is the usual reason CM108 PTT silently does nothing. "
                "Install tools/99-digirig.rules to fix it." % path)

    def _close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def key(self):
        os.write(self.fd, self._report(True))

    def unkey(self):
        # Called by __exit__ after _close on some paths; guard it.
        if self.fd is not None:
            os.write(self.fd, self._report(False))


def make_ptt(line, dev=DEFAULT_DEV, gpio=CM108_DEFAULT_GPIO, path=None,
             usb_device=None):
    """The right PTT backend for the named line."""
    if line == CM108_LINE:
        return Cm108Ptt(path=path, gpio=gpio, usb_device=usb_device)
    return PttPort(dev, line)


class PttPort(_PttBase):
    """A serial port with its control lines used as a PTT switch.

    Opening a tty raises DTR and RTS -- that is the kernel's doing, not ours,
    and on a cable wired for PTT it means the radio transmits from the instant
    the port is opened. They are cleared here before anything else happens, so
    the transmit window is the USB round trip rather than the length of the
    run.
    """

    def __init__(self, dev=DEFAULT_DEV, line="rts"):
        if line not in LINES:
            raise ValueError("line must be one of %s, not %r"
                             % (", ".join(LINES), line))
        self.dev = dev
        self.line = line
        self.fd = None

    def _open(self):
        self.fd = os.open(self.dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)

    def _close(self):
        os.close(self.fd)
        self.fd = None

    def _bit(self):
        return {"rts": termios.TIOCM_RTS, "dtr": termios.TIOCM_DTR}[self.line]

    def key(self):
        if self.line == "break":
            fcntl.ioctl(self.fd, TIOCSBRK)
        else:
            fcntl.ioctl(self.fd, termios.TIOCMBIS, struct.pack("I", self._bit()))

    def unkey(self):
        if self.line == "break":
            fcntl.ioctl(self.fd, TIOCCBRK)
        else:
            # Both control lines, not just the selected one, because open()
            # raised both and only one of them is being driven deliberately.
            fcntl.ioctl(self.fd, termios.TIOCMBIC,
                        struct.pack("I", termios.TIOCM_RTS | termios.TIOCM_DTR))


def probe(dev=DEFAULT_DEV, key_s=PROBE_KEY_S, gap_s=PROBE_GAP_S, out=sys.stdout):
    """Key each line in turn so a human can see which one the cable wires up.

    Software can drive DTR and RTS on any CH340. Whether either reaches the
    radio is a question about how the cable was assembled, and most Baofeng
    programming cables run only TXD, RXD and ground to the plug. There is no
    way to read that off the chip -- it has to be watched on the radio.
    """
    out.write("Watch the radio's TX indicator. Each line is held for %.0f s.\n\n"
              % key_s)
    for line in LINES:
        out.write("  %-5s keying now ... " % line)
        out.flush()
        with PttPort(dev, line) as p:
            held = p.keyed_for(key_s)
        out.write("released (held %.2f s)\n" % held)
        time.sleep(gap_s)
    out.write("\nWhichever one lit the TX indicator is the line to use.\n"
              "If none did, the cable does not bring PTT out to the plug.\n")


def _loud_stretch(broad, floor, step):
    """(longest run above the floor in seconds, where it started, how many).

    For the squelch-CLOSED polarity. Not a carrier measurement -- a squelch
    gates its own edges, so the length here is the squelch's attack and hang
    as much as the radio's keying -- but the SHAPE is diagnostic: one stretch
    about as long as the assertion is a carrier that broke squelch, while two
    brief ones are the key and the unkey with nothing in between.
    """
    thresh = floor * LOUD_FACTOR
    runs = []
    i = 0
    while i < len(broad):
        if broad[i] > thresh:
            j = i
            while j < len(broad) and broad[j] > thresh:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    if not runs:
        return None, None, 0
    start, end = max(runs, key=lambda ab: ab[1] - ab[0])
    return (end - start) * step, start * step, len(runs)


def _closed_squelch_reading(r, asserted_s):
    """What the loud stretches say about whether PTT fired at all.

    This is the judgement the check exists for, and with squelch closed it is
    the only one available. Deliberately stops short of a number: the edges
    belong to the squelch, so calling any of this a key-up time would be
    exactly the kind of figure this project keeps having to retract.
    """
    if not r["loud_runs"]:
        # DEFENSIVE, and unreachable as the caller stands: this branch is only
        # entered when something is already ~50x the floor, which guarantees
        # at least one window over LOUD_FACTOR. Kept because the alternative
        # on a future caller is comparing None against a float. A recording
        # with no contrast at all never gets here -- it falls through to the
        # quiet-stretch search, which lists all three causes rather than
        # guessing between them.
        return ("NOTHING in the recording rose above that floor, so there is "
                "no evidence PTT put anything on the air at all.")
    longest = r["loud_s"]
    if longest >= 0.5 * asserted_s:
        return ("A single loud stretch of %.2f s was seen %.2f s in, against "
                "a %.2f s assertion -- PTT IS keying and a carrier IS "
                "arriving, so this is a reporting limit and not a fault."
                % (longest, r["loud_at_s"], asserted_s))
    return ("%d brief loud stretches were seen, the longest %.0f ms, with the "
            "floor in between -- the shape of PTT switching at each end of "
            "the assertion while NO carrier passes the squelch. Either the "
            "transmitter is too weak to open it or the line never reaches the "
            "radio's PTT, and a closed squelch cannot tell those apart."
            % (r["loud_runs"], longest * 1000))


def analyze_carrier(samples, fs, asserted_s, lead_s=MEASURE_LEAD_S):
    """How long the carrier was really up, against how long PTT was asserted.

    With the receiving radio's squelch open, no carrier is LOUD hiss and an
    unmodulated carrier is quiet. So the carrier is the quiet stretch, and its
    length minus the asserted length is what the radio costs per transmission.

    Two durations, never two timestamps. The recorder and the keying are
    separate things whose start times differ by an unknown few hundred ms, and
    building that difference into an answer is precisely how VOX attack came
    out as 0 ms on one run and 440 ms on the next.
    """
    samples = samples[int(tt.STARTUP_SKIP_S * fs):]
    broad, step = tt.envelope(samples, fs)
    r = {"seconds": len(samples) / float(fs), "window_s": step,
         "hiss_level": None, "carrier_s": None, "asserted_s": asserted_s,
         "overhead_s": None, "net_s": None, "dominant": None,
         "clipped_fraction": 0.0, "problem": None,
         # Whether the idle floor IS hiss, which is the question rather than
         # a given. None until determined. report_carrier used to label the
         # floor "(squelch open)" unconditionally, which printed that claim
         # directly above a failure saying the squelch was not open.
         "squelch_open": None,
         # The longest stretch ABOVE the floor, for the squelch-closed case:
         # with squelch closed the carrier is loud rather than quiet, and
         # throwing that away is throwing away the one thing that separates
         # "PTT keys nothing" from "the squelch hid it".
         "loud_s": None, "loud_at_s": None, "loud_runs": 0}
    if not broad:
        r["problem"] = "the recording is empty"
        return r

    # A capture that never varies is not quiet audio, it is a dead device.
    # pw-record given a target that no longer exists does not fail -- it
    # produces a file full of one repeated sample, and every threshold below
    # would then dutifully report "no carrier" about a radio that worked
    # perfectly. Ask whether anything moved before asking what it means.
    if max(broad) - min(broad) <= max(max(broad) * 1e-6, 1e-12):
        r["problem"] = (
            "the capture is dead -- every sample is the same value, so no "
            "audio device was actually recording. This is what pw-record does "
            "when --target names a device that does not exist; check the name "
            "against `pactl list short sources`. Nothing here reflects the "
            "radio.")
        return r

    # Measured over the lead that is actually left after the startup skip,
    # and over only part of THAT. The recorder does not begin capturing the
    # instant it is launched, so the keying lands earlier in the file than the
    # lead length suggests, and a window sized off the nominal lead reaches
    # into the carrier. A noise floor that quietly includes the signal is the
    # bug that has already cost this project two measurements.
    usable = max(0.1, lead_s - tt.STARTUP_SKIP_S)
    n = max(10, int(usable * 0.7 / step))
    n = min(n, len(broad))
    head = sorted(broad[:n])
    hiss = head[len(head) // 2]
    r["hiss_level"] = hiss

    peak = max(broad)
    if hiss <= max(peak * 0.02, 1e-9):
        # The idle floor is not hiss: something in this recording is ~50x
        # louder than the level the lead sat at. That is the squelch-CLOSED
        # shape -- idle is muted silence and a carrier breaking squelch is
        # loud -- which is the exact inverse of what the quiet-stretch search
        # below looks for.
        r["squelch_open"] = False
        r["loud_s"], r["loud_at_s"], r["loud_runs"] = _loud_stretch(
            broad, hiss, step)
        r["problem"] = ("the idle floor is not hiss -- the squelch is closed "
                        "on the receiving radio, or the level is far too low. "
                        + _closed_squelch_reading(r, asserted_s) +
                        " Open the squelch (SQL 0 or monitor) and run it "
                        "again: with it closed a WEAK carrier cannot be told "
                        "from no carrier, which is the distinction this check "
                        "exists to make.")
        return r
    r["squelch_open"] = True

    thresh = hiss * CARRIER_QUIET_FRACTION
    best_len = best_start = 0
    i = 0
    while i < len(broad):
        if broad[i] < thresh:
            j = i
            while j < len(broad) and broad[j] < thresh:
                j += 1
            if j - i > best_len:
                best_len, best_start = j - i, i
            i = j
        else:
            i += 1

    if best_len < 3:
        # Three causes, and the recording cannot separate them: a quiet
        # stretch is missing whether nothing keyed, nothing was received, or
        # there was never any hiss for it to stand out against. Listing all
        # three beats picking the most interesting one -- the cheap checks are
        # the frequency and the squelch, and the expensive one is the wiring.
        r["problem"] = ("no carrier: the hiss never went quiet. Check, in "
                        "this order -- the squelch is open on the receiving "
                        "radio; both radios are on the same frequency; the "
                        "line actually reaches PTT. This cannot tell those "
                        "apart, so rule out the first two before rewiring "
                        "anything.")
        return r

    carrier = best_len * step
    r["carrier_s"] = carrier
    # The whole recording going quiet is not a carrier, it is a squelch that
    # closed or a receiver that was turned down between runs.
    if best_start == 0 or best_start + best_len >= len(broad) - 1:
        r["problem"] = ("the quiet stretch runs to the edge of the recording, "
                        "so its length is a floor rather than a measurement. "
                        "Lengthen the lead or the tail and run it again.")
        return r

    # Signed, because which way it goes says which end of the transmission
    # the radio is slow at -- and unlike VOX, PTT turned out to be slow at the
    # FRONT. A radio that brings the carrier up 165 ms after the line is
    # asserted gives a carrier SHORTER than the assertion, and subtracting the
    # wrong way round reports that as a radio which costs less than nothing.
    net = carrier - asserted_s
    r["net_s"] = net
    r["overhead_s"] = abs(net)
    r["dominant"] = "attack" if net < 0 else "hold"

    # Clipping does not stop the carrier being FOUND -- the quiet state sits
    # far below any threshold either way -- but it moves the EDGE. A saturated
    # hiss stays pinned at the rail while the true level is already falling, so
    # the carrier looks like it arrived later than it did. Called harmless
    # once; the same radio then measured 165 ms clipped and 95 ms less
    # clipped, which is most of the answer.
    peak_abs = max(abs(x) for x in samples) if samples else 0
    if peak_abs > 0:
        railed = sum(1 for x in samples if abs(x) >= peak_abs * 0.995)
        r["clipped_fraction"] = railed / float(len(samples))
    return r


def report_carrier(r):
    lines = ["recording: %.2f s, %.0f ms resolution"
             % (r["seconds"], r["window_s"] * 1000)]
    if r["hiss_level"] is not None:
        # DERIVED, not asserted. This line read "(squelch open)" whatever the
        # analysis had concluded, so a squelch-closed recording printed
        # "squelch open" immediately above a failure saying the squelch was
        # not open -- a contradiction in the one tool whose job is to tell
        # faults apart.
        lines.append("idle floor: %.4f%s" % (
            r["hiss_level"],
            "  (hiss -- squelch open)" if r.get("squelch_open")
            else "  (not hiss -- squelch closed)" if r.get("squelch_open") is False
            else ""))
    if r["carrier_s"] is None:
        lines.append("")
        lines.append("FAILED: " + (r["problem"] or "no carrier"))
        return "\n".join(lines)
    lines.append("")
    lines.append("PTT asserted: %.3f s" % r["asserted_s"])
    lines.append("carrier up:   %.3f s" % r["carrier_s"])
    if r["overhead_s"] is None:
        lines.append("")
        lines.append("FAILED: " + (r["problem"] or "no overhead available"))
        return "\n".join(lines)
    over = r["overhead_s"]
    lines.append("")
    if r["dominant"] == "attack":
        lines.append("the radio was slow to KEY: the carrier came up %.0f ms "
                     "after the line" % (over * 1000))
        lines.append("  data sent in that window would be lost, so every "
                     "transmission has to wait it out")
    else:
        lines.append("the radio HELD the carrier %.0f ms past the line"
                     % (over * 1000))
        lines.append("  that is airtime occupied while sending nothing")
    lines.append("")
    lines.append("per-transmission overhead: %.0f ms" % (over * 1000))
    lines.append("  -> at 20%% duty the channel carries about %.0f "
                 "60-byte packets/min"
                 % (12.0 / (over + (60 * 8) / 1200.0)))
    lines.append("")
    lines.append("This is the NET of keying delay and hold-over, so it is a "
                 "floor:")
    lines.append("  a radio doing both would show their difference, not their "
                 "sum. Separating")
    lines.append("  them needs a clock shared by the keying and the recorder, "
                 "which there is not.")
    lines.append("")
    # The 3300 ms this used to quote was measured through the laptop's analog
    # output, whose residue after playback appears to have held VOX keyed --
    # it timed the sound card, not the radio. Through this interface, with the
    # audio genuinely stopping, VOX measures 205 ms on the same radio.
    lines.append("VOX through this same interface: 210 ms on this radio "
                 "(85 ms to key, 125 ms hang).")
    if r.get("clipped_fraction", 0.0) > 0.01:
        lines.append("")
        lines.append("WARNING: %.0f%% of samples sit at one value -- the "
                     "capture is clipping, and the" % (100 * r["clipped_fraction"]))
        lines.append("  numbers above are SUSPECT. A saturated hiss stays "
                     "pinned while the real level is")
        lines.append("  already falling, so the carrier reads as arriving "
                     "late and the overhead too high.")
        lines.append("  This clips in the capture hardware, UPSTREAM of any "
                     "PipeWire volume -- lowering")
        lines.append("  the pactl slider only scales the clipped signal down. "
                     "Fix it at the source:")
        lines.append("    amixer -c <card> sset Mic 25%   "
                     "# ALSA capture gain, or turn the radio down")
    if r["problem"]:
        lines.append("")
        lines.append("WARNING: " + r["problem"])
    return "\n".join(lines)


def list_sources():
    """PipeWire capture device names, or [] if they cannot be listed."""
    try:
        out = subprocess.run(["pactl", "list", "short", "sources"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    names = []
    for row in out.stdout.splitlines():
        cols = row.split("\t")
        if len(cols) >= 2 and not cols[1].endswith(".monitor"):
            names.append(cols[1])
    return names


def check_source(name, available=None):
    """Refuse a capture device that is not there.

    pw-record accepts an unknown --target without complaint and writes a file
    of one repeated sample. That file then looks like a radio that never
    transmitted, which is a far more interesting conclusion than the truth and
    took a bench run to notice. Device names also move -- this one lost a ".2"
    suffix when another sound card was plugged in.
    """
    available = list_sources() if available is None else available
    if not available or name in available:
        return
    raise RuntimeError(
        "capture device %r does not exist. pw-record would accept it silently "
        "and record nothing. Available:\n  %s"
        % (name, "\n  ".join(available)))


def record_while_keying(rec_path, rec_dev, line, dev=DEFAULT_DEV,
                        key_s=MEASURE_KEY_S, fs=tt.DEFAULT_FS,
                        rec_pipewire=True, quiet=False,
                        gpio=CM108_DEFAULT_GPIO):
    """Record the receiving radio while keying the transmitting one."""
    total = MEASURE_LEAD_S + key_s + MEASURE_TAIL_S + 0.5
    secs = int(math.ceil(total))

    if rec_pipewire and rec_dev:
        check_source(rec_dev)

    if rec_pipewire:
        rec_cmd = ["pw-record", "--rate", str(fs), "--channels", "1",
                   "--format", "s16"]
        if rec_dev:
            rec_cmd += ["--target", tt.resolve_pw_target(rec_dev)]
        rec_cmd += [rec_path]
    else:
        rec_cmd = ["arecord", "-q", "-f", "S16_LE", "-r", str(fs), "-c", "1",
                   "-d", str(secs), rec_path]
        if rec_dev:
            rec_cmd[1:1] = ["-D", rec_dev]

    # A recorder that fails leaves the PREVIOUS run's file on disk, and
    # analysing that reports an old measurement as a new one.
    before = os.path.getmtime(rec_path) if os.path.exists(rec_path) else None

    if not quiet:
        print("recording %s for %d s ..." % (rec_path, secs))
    rec = subprocess.Popen(rec_cmd, stderr=subprocess.PIPE, text=True)
    held = None
    try:
        subprocess.run(["sleep", str(MEASURE_LEAD_S)], check=False)
        if rec.poll() is not None:
            raise RuntimeError("recorder exited immediately: %s"
                               % (rec.stderr.read().strip() or "no message"))
        if not quiet:
            print("keying %s for %.3f s ..." % (line, key_s))
        with make_ptt(line, dev=dev, gpio=gpio) as p:
            held = p.keyed_for(key_s)
        subprocess.run(["sleep", str(MEASURE_TAIL_S)], check=False)
        rec.terminate()
        rec.wait(timeout=secs + 10)
    finally:
        if rec.poll() is None:
            rec.terminate()

    if not os.path.exists(rec_path):
        raise RuntimeError("no recording was produced")
    after = os.path.getmtime(rec_path)
    if before is not None and after <= before:
        raise RuntimeError(
            "the recording file was not updated -- the recorder failed and an "
            "OLDER file is still on disk. Refusing to analyse it.")
    return held


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--probe", action="store_true",
                    help="key each control line in turn to find which works")
    ap.add_argument("--dev", default=DEFAULT_DEV)
    ap.add_argument("--line", choices=ALL_LINES, default="rts")
    ap.add_argument("--gpio", type=int, default=CM108_DEFAULT_GPIO,
                    help="cm108 GPIO pin used for PTT (Digirig wires 3)")
    ap.add_argument("--key", type=float, metavar="SECONDS",
                    help="key once for this long, to watch the TX indicator")
    ap.add_argument("--key-seconds", type=float, default=PROBE_KEY_S)
    ap.add_argument("--measure", action="store_true",
                    help="key for a known interval and measure the carrier")
    ap.add_argument("--rec-dev", default=None)
    ap.add_argument("--rec-out", default="ptt_test_rec.wav")
    ap.add_argument("--arecord", action="store_true",
                    help="record with arecord instead of pw-record")
    args = ap.parse_args(argv)

    if args.probe:
        probe(args.dev, key_s=args.key_seconds)
        return 0
    if args.key is not None:
        with make_ptt(args.line, dev=args.dev, gpio=args.gpio) as p:
            print("keying %s for %.2f s ..." % (args.line, args.key))
            held = p.keyed_for(args.key)
        print("released (held %.3f s)" % held)
        return 0
    if args.measure:
        held = record_while_keying(args.rec_out, args.rec_dev, args.line,
                                   dev=args.dev, key_s=MEASURE_KEY_S,
                                   rec_pipewire=not args.arecord,
                                   gpio=args.gpio)
        samples, fs = tt.read_wav(args.rec_out)
        print()
        print(report_carrier(analyze_carrier(samples, fs, held)))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
