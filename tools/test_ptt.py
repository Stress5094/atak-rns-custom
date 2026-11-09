"""Tests for keying PTT from a serial cable.

    python3 -m unittest discover -s tools -p 'test_*.py'

Most of these are about releasing the line rather than asserting it. A PTT that
fails to assert wastes a bench run; a PTT that fails to RELEASE leaves a
transmitter keyed on a live channel until someone notices, and the code path
that does it is the one that runs when something has already gone wrong.
"""

import io
import os
import struct
import sys
import termios
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ptt_test as pt

BOTH = termios.TIOCM_RTS | termios.TIOCM_DTR


class FakeIoctl(object):
    def __init__(self):
        self.calls = []

    def __call__(self, fd, request, arg=None):
        bits = struct.unpack("I", arg)[0] if arg else None
        self.calls.append((request, bits))
        return 0

    def requests(self):
        return [c[0] for c in self.calls]


def fake_port(line="rts"):
    """A PttPort whose ioctls are recorded instead of reaching a device."""
    f = FakeIoctl()
    patches = [
        mock.patch.object(pt.fcntl, "ioctl", f),
        mock.patch.object(pt.os, "open", return_value=7),
        mock.patch.object(pt.os, "close"),
    ]
    return f, patches


class _PatchCase(unittest.TestCase):
    def _port(self, line="rts"):
        f, patches = fake_port(line)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return f, pt.PttPort("/dev/fake", line)


class TestLineRelease(_PatchCase):

    def test_open_clears_both_lines_before_anything_else(self):
        # The kernel raises DTR and RTS when a tty is opened. On a cable wired
        # for PTT that is a transmission nobody asked for, so clearing them has
        # to be the very first thing that happens.
        f, port = self._port()
        with port:
            pass
        self.assertEqual(f.calls[0], (termios.TIOCMBIC, BOTH))

    def test_exit_releases_even_when_the_body_raises(self):
        f, port = self._port()
        with self.assertRaises(RuntimeError):
            with port as p:
                p.key()
                raise RuntimeError("boom")
        self.assertEqual(f.calls[-1], (termios.TIOCMBIC, BOTH),
                         "line was not released on the error path")

    def test_keyed_for_releases_even_if_the_sleep_raises(self):
        f, port = self._port()
        with port as p:
            with mock.patch.object(pt.time, "sleep",
                                   side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    p.keyed_for(1.0)
            self.assertEqual(f.calls[-1], (termios.TIOCMBIC, BOTH))

    def test_unkey_clears_both_lines_not_just_the_selected_one(self):
        # Only one line is driven deliberately, but open() raised both.
        f, port = self._port("dtr")
        with port:
            pass
        for request, bits in f.calls:
            if request == termios.TIOCMBIC:
                self.assertEqual(bits, BOTH)


class TestKeying(_PatchCase):

    def test_rts_keys_with_the_rts_bit(self):
        f, port = self._port("rts")
        with port as p:
            p.key()
        self.assertIn((termios.TIOCMBIS, termios.TIOCM_RTS), f.calls)

    def test_dtr_keys_with_the_dtr_bit(self):
        f, port = self._port("dtr")
        with port as p:
            p.key()
        self.assertIn((termios.TIOCMBIS, termios.TIOCM_DTR), f.calls)

    def test_break_uses_the_break_ioctls(self):
        f, port = self._port("break")
        with port as p:
            p.key()
            p.unkey()
        self.assertIn(pt.TIOCSBRK, f.requests())
        self.assertIn(pt.TIOCCBRK, f.requests())

    def test_keyed_for_reports_the_measured_hold_not_the_requested_one(self):
        # A scheduler hiccup between key and unkey would otherwise be charged
        # to the radio's turnaround.
        f, port = self._port()
        with port as p:
            held = p.keyed_for(0.05)
        self.assertGreaterEqual(held, 0.05)
        self.assertLess(held, 0.5)


class TestValidation(unittest.TestCase):

    def test_an_unknown_line_is_refused_at_construction(self):
        with self.assertRaises(ValueError):
            pt.PttPort("/dev/fake", "cts")

    def test_the_error_names_the_lines_that_do_work(self):
        try:
            pt.PttPort("/dev/fake", "cts")
        except ValueError as e:
            for line in pt.LINES:
                self.assertIn(line, str(e))


class TestProbe(_PatchCase):

    def test_probe_tries_every_line(self):
        f, _ = self._port()
        out = io.StringIO()
        with mock.patch.object(pt.time, "sleep"):
            pt.probe("/dev/fake", key_s=0.0, gap_s=0.0, out=out)
        text = out.getvalue()
        for line in pt.LINES:
            self.assertIn(line, text)

    def test_probe_says_what_it_means_if_nothing_keys(self):
        f, _ = self._port()
        out = io.StringIO()
        with mock.patch.object(pt.time, "sleep"):
            pt.probe("/dev/fake", key_s=0.0, gap_s=0.0, out=out)
        self.assertIn("does not bring PTT out", out.getvalue())


def carrier_recording(carrier_s, asserted_s=1.0, lead_s=None, tail_s=None,
                      hiss=0.30, quiet=0.01, seed=3, fs=8000):
    """What an open-squelch receiver hears while the other radio is keyed.

    Loud hiss, then a quiet stretch while the carrier holds, then hiss again.
    No modulation anywhere -- PTT keying needs no audio, which is the whole
    reason a cable that carries no audio can still measure it.
    """
    import random
    rng = random.Random(seed)
    lead_s = pt.MEASURE_LEAD_S if lead_s is None else lead_s
    tail_s = pt.MEASURE_TAIL_S if tail_s is None else tail_s
    out = []
    for _ in range(int(lead_s * fs)):
        out.append(rng.gauss(0.0, hiss))
    for _ in range(int(carrier_s * fs)):
        out.append(rng.gauss(0.0, quiet))
    for _ in range(int(tail_s * fs)):
        out.append(rng.gauss(0.0, hiss))
    return out, fs


def squelched_recording(carrier_s, asserted_s=1.0, lead_s=None, tail_s=None,
                        floor=0.002, carrier=1.2, click_s=0.05, seed=5,
                        fs=8000):
    """What a CLOSED-squelch receiver hears, which is the inverse shape.

    Idle is muted near-silence rather than hiss, and a carrier that breaks
    squelch is LOUD rather than quiet. `carrier_s=0` gives the no-carrier
    case: the two PTT switching transients at each end of the assertion with
    the floor in between, which is what a bench recording of a transmitter
    that never put out a carrier actually looks like.

    Calibrated against two real recordings taken 2026-10-03 with level-3
    squelch -- a working direction at floor 2.1 / carrier ~1400, and a dead
    one at floor 28 with 95 ms transients.
    """
    import random
    rng = random.Random(seed)
    lead_s = pt.MEASURE_LEAD_S if lead_s is None else lead_s
    tail_s = pt.MEASURE_TAIL_S if tail_s is None else tail_s

    def block(seconds, level):
        return [rng.gauss(0.0, level) for _ in range(int(seconds * fs))]

    out = block(lead_s, floor)
    if carrier_s > 0:
        out += block(carrier_s, carrier)
        out += block(max(0.0, tail_s), floor)
    else:
        # No carrier: a click, the asserted window at the floor, a click.
        out += block(click_s, carrier)
        out += block(max(0.0, asserted_s - click_s), floor)
        out += block(click_s, carrier)
        out += block(max(0.0, tail_s), floor)
    return out, fs


class TestAClosedSquelchIsDiagnosedNotJustRejected(unittest.TestCase):
    """With squelch closed the carrier is LOUD, not quiet, so the
    quiet-stretch search cannot see it -- but throwing the recording away
    discards the one thing that separates "PTT keys nothing" from "the
    squelch hid it", which is the distinction this whole check exists to
    make. Both shapes below were taken off real hardware before being
    reduced to these synthetic ones.
    """

    def test_a_closed_squelch_is_named_as_such(self):
        samples, fs = squelched_recording(1.2)
        r = pt.analyze_carrier(samples, fs, 1.0)
        self.assertIs(r["squelch_open"], False)
        self.assertIn("squelch is closed", r["problem"])

    def test_an_open_squelch_is_recorded_as_open(self):
        samples, fs = carrier_recording(1.05)
        r = pt.analyze_carrier(samples, fs, 1.0)
        self.assertIs(r["squelch_open"], True)

    def test_a_sustained_carrier_under_closed_squelch_says_ptt_is_keying(self):
        """The useful answer: the check cannot report a TIME, but it can
        report that the radio is keying and a carrier is arriving."""
        samples, fs = squelched_recording(1.2)
        r = pt.analyze_carrier(samples, fs, 1.0)
        self.assertEqual(r["loud_runs"], 1)
        self.assertAlmostEqual(r["loud_s"], 1.2, delta=0.1)
        self.assertIn("PTT IS keying", r["problem"])

    def test_switching_transients_with_no_carrier_say_so(self):
        """The failing direction on this bench: two brief stretches at each
        end of the assertion with the floor in between."""
        samples, fs = squelched_recording(0.0)
        r = pt.analyze_carrier(samples, fs, 1.0)
        self.assertGreaterEqual(r["loud_runs"], 2)
        self.assertLess(r["loud_s"], 0.5)
        self.assertIn("NO carrier passes the squelch", r["problem"])
        self.assertNotIn("PTT IS keying", r["problem"])

    def test_a_uniform_recording_stays_on_the_ambiguous_path(self):
        """A floor with NOTHING loud anywhere never reaches the closed-squelch
        branch, and should not: with no contrast at all the recording cannot
        say whether nothing keyed, nothing was received, or there was never
        any hiss to stand out against. The pre-existing message lists all
        three in the order they are cheap to rule out, which beats this new
        branch guessing.
        """
        samples, fs = squelched_recording(0.0, click_s=0.0)
        r = pt.analyze_carrier(samples, fs, 1.0)
        self.assertIsNone(r["carrier_s"])
        self.assertIn("the squelch is open on the receiving radio",
                      r["problem"])
        self.assertIn("both radios are on the same frequency", r["problem"])

    def test_the_closed_squelch_case_never_reports_a_timing_figure(self):
        """The edges there belong to the squelch's attack and hang, not to the
        radio's keying -- the same recording measured 1.255 s and 1.595 s on
        two runs 4 minutes apart. Reporting either as a key-up would be
        exactly the kind of number this project keeps retracting."""
        for carrier_s in (0.0, 1.2):
            r = pt.analyze_carrier(*squelched_recording(carrier_s),
                                   asserted_s=1.0)
            self.assertIsNone(r["carrier_s"])
            self.assertIsNone(r["overhead_s"])

    def test_the_report_never_claims_a_squelch_state_it_did_not_determine(self):
        """This line read "(squelch open)" whatever the analysis concluded, so
        a closed-squelch recording printed that claim directly above a failure
        saying the squelch was not open."""
        closed = pt.report_carrier(
            pt.analyze_carrier(*squelched_recording(1.2), asserted_s=1.0))
        self.assertIn("squelch closed", closed)
        self.assertNotIn("squelch open", closed)
        opened = pt.report_carrier(
            pt.analyze_carrier(*carrier_recording(1.05), asserted_s=1.0))
        self.assertIn("squelch open", opened)
        self.assertNotIn("squelch closed", opened)


class TestCarrierMeasurement(unittest.TestCase):

    def test_overhead_is_carrier_minus_asserted(self):
        samples, fs = carrier_recording(1.05)
        r = pt.analyze_carrier(samples, fs, 1.0)
        self.assertIsNone(r["problem"], r["problem"])
        self.assertAlmostEqual(r["carrier_s"], 1.05, delta=0.03)
        self.assertAlmostEqual(r["overhead_s"], 0.05, delta=0.03)

    def test_a_fast_radio_reports_a_small_overhead(self):
        samples, fs = carrier_recording(1.04)
        r = pt.analyze_carrier(samples, fs, 1.0)
        self.assertLess(r["overhead_s"], 0.1)

    def test_a_slow_radio_reports_a_large_one(self):
        samples, fs = carrier_recording(2.5)
        r = pt.analyze_carrier(samples, fs, 1.0)
        self.assertAlmostEqual(r["overhead_s"], 1.5, delta=0.05)

    def test_no_carrier_is_reported_not_guessed_at(self):
        # Uniform hiss throughout: nothing ever keyed.
        samples, fs = carrier_recording(0.0)
        r = pt.analyze_carrier(samples, fs, 1.0)
        self.assertIsNone(r["carrier_s"])
        self.assertIn("no carrier", r["problem"])

    def test_no_carrier_names_all_three_causes(self):
        # A wrong frequency, a closed squelch and a dead PTT line all look
        # identical from here, and saying so is cheaper than an evening with a
        # soldering iron.
        samples, fs = carrier_recording(0.0)
        r = pt.analyze_carrier(samples, fs, 1.0)
        for cause in ("squelch", "frequency", "PTT"):
            self.assertIn(cause, r["problem"])

    def test_a_uniformly_quiet_recording_is_not_read_as_a_carrier(self):
        # Squelch closed and nothing modulated: the whole file is one level.
        # There is no information here, and inventing a carrier from it would
        # report a working link that does not exist.
        samples, fs = carrier_recording(1.0, hiss=0.01, quiet=0.01)
        r = pt.analyze_carrier(samples, fs, 1.0)
        self.assertIsNone(r["carrier_s"])

    def test_a_quiet_stretch_running_off_the_end_is_refused(self):
        samples, fs = carrier_recording(1.0, tail_s=0.0)
        r = pt.analyze_carrier(samples, fs, 1.0)
        self.assertIsNone(r["overhead_s"])
        self.assertIn("floor rather than a measurement", r["problem"])

    def test_report_prints_no_numbers_without_a_carrier(self):
        samples, fs = carrier_recording(0.0)
        text = pt.report_carrier(pt.analyze_carrier(samples, fs, 1.0))
        self.assertNotIn("per-transmission overhead", text)
        self.assertIn("FAILED", text)

    def test_report_compares_against_the_measured_vox_figures(self):
        # 210 ms, measured through this same interface on a clean capture. The 3300 ms this used
        # to assert was taken through the laptop's analog output, whose residue
        # after playback appears to have held VOX keyed -- it timed the sound
        # card rather than the radio, and quoting it made PTT look 20x better
        # than it is.
        samples, fs = carrier_recording(1.05)
        text = pt.report_carrier(pt.analyze_carrier(samples, fs, 1.0))
        self.assertIn("per-transmission overhead", text)
        self.assertIn("210 ms", text)
        self.assertNotIn("3300", text)


class TestCm108Report(unittest.TestCase):
    """The five bytes that key a C-Media chip's GPIO.

    Four bytes is what the datasheet implies and what fails with EPIPE; five is
    what hamlib and direwolf write and what works. The mask byte is set whether
    keying or releasing -- it declares the pin an output, and only the data
    byte carries the state.
    """

    def test_gpio3_key_and_release_bytes(self):
        p = pt.Cm108Ptt(path="/dev/null", gpio=3)
        self.assertEqual(p._report(True), bytes([0, 0, 0x04, 0x04, 0]))
        self.assertEqual(p._report(False), bytes([0, 0, 0x00, 0x04, 0]))

    def test_reports_are_five_bytes(self):
        p = pt.Cm108Ptt(path="/dev/null", gpio=3)
        self.assertEqual(len(p._report(True)), pt.CM108_REPORT_LEN)
        self.assertEqual(len(p._report(False)), pt.CM108_REPORT_LEN)

    def test_gpio_number_maps_to_its_bit(self):
        for gpio, bit in ((1, 0x01), (2, 0x02), (3, 0x04), (8, 0x80)):
            p = pt.Cm108Ptt(path="/dev/null", gpio=gpio)
            self.assertEqual(p._report(True)[2], bit)
            self.assertEqual(p._report(True)[3], bit)

    def test_digirig_defaults_to_gpio3(self):
        self.assertEqual(pt.CM108_DEFAULT_GPIO, 3)
        self.assertEqual(pt.Cm108Ptt(path="/dev/null")._report(True)[3], 0x04)

    def test_an_out_of_range_gpio_is_refused(self):
        for bad in (0, 9, -1):
            with self.assertRaises(ValueError):
                pt.Cm108Ptt(path="/dev/null", gpio=bad)


class TestUsbDeviceId(unittest.TestCase):

    def _fake_usb_tree(self, devname, extra_levels):
        """A physical USB device (DEVTYPE=usb_device, given DEVNAME) with
        `extra_levels` of plain nested subdirectories under it and no
        `uevent` of their own except the leaf, mimicking how a hidraw
        node's `device` symlink lands two levels deeper (interface, then
        HID class device) than an ALSA card's (straight at the interface).
        Returns the leaf directory -- what a `device` symlink would point at.
        """
        import tempfile
        base = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, base)
        device_dir = os.path.join(base, "device")
        os.makedirs(device_dir)
        with open(os.path.join(device_dir, "uevent"), "w") as fh:
            fh.write("DEVTYPE=usb_device\nDEVNAME=%s\n" % devname)
        leaf = device_dir
        for i in range(extra_levels):
            leaf = os.path.join(leaf, "level%d" % i)
            os.makedirs(leaf)
        return leaf

    def test_finds_devname_at_the_node_itself(self):
        leaf = self._fake_usb_tree("bus/usb/003/013", extra_levels=0)
        self.assertEqual(pt.usb_device_id(leaf), "bus/usb/003/013")

    def test_finds_devname_several_levels_up(self):
        # The exact asymmetry this exists for: a hidraw node's `device`
        # symlink lands on the HID class device, two levels below the
        # physical USB device an ALSA card's `device` points at directly.
        leaf = self._fake_usb_tree("bus/usb/003/013", extra_levels=2)
        self.assertEqual(pt.usb_device_id(leaf), "bus/usb/003/013")

    def test_returns_none_with_no_usb_device_ancestor(self):
        import tempfile
        base = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, base)
        self.assertIsNone(pt.usb_device_id(base))


class TestCm108Discovery(unittest.TestCase):

    def _fake_sysfs(self, entries):
        """`entries`: (hidraw_name, hid_id, usb_devname_or_None) tuples.

        Each hidraw node's `device` is a real symlink into its own little
        USB device tree (built the same depth-asymmetric way real hardware
        nests a HID class device two levels under its physical USB device
        -- see TestUsbDeviceId), so usb_device_id() is exercised the same
        way find_cm108() actually calls it, not a shortcut.
        """
        import tempfile
        base = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, base)
        for name, hid_id, usb_devname in entries:
            os.makedirs(os.path.join(base, name))
            device_dir = os.path.join(base, name, "_device")
            os.makedirs(device_dir)
            with open(os.path.join(device_dir, "uevent"), "w") as fh:
                fh.write("DRIVER=hid-generic\nHID_ID=%s\n" % hid_id)
            if usb_devname is not None:
                usb_dir = os.path.join(base, name, "_usbdev")
                os.makedirs(usb_dir)
                with open(os.path.join(usb_dir, "uevent"), "w") as fh:
                    fh.write("DEVTYPE=usb_device\nDEVNAME=%s\n" % usb_devname)
                # device_dir's parent must resolve to usb_dir for
                # usb_device_id's walk-up to find it -- symlink the HID
                # node's device dir INTO the fake usb device tree instead
                # of leaving it a sibling.
                nested = os.path.join(usb_dir, "iface", "hidclass")
                os.makedirs(os.path.dirname(nested))
                os.rename(device_dir, nested)
                device_dir = nested
            os.symlink(device_dir, os.path.join(base, name, "device"))
        return base

    def test_finds_the_c_media_node_among_others(self):
        base = self._fake_sysfs([
            ("hidraw0", "0003:0000046D:0000C52B", "bus/usb/001/001"),
            ("hidraw1", "0003:00000D8C:00000012", "bus/usb/001/002"),
            ("hidraw2", "0003:0000248A:00008367", "bus/usb/001/003"),
        ])
        self.assertEqual(pt.find_cm108(base=base), "/dev/hidraw1")

    def test_returns_none_when_absent(self):
        base = self._fake_sysfs([
            ("hidraw0", "0003:0000046D:0000C52B", "bus/usb/001/001"),
        ])
        self.assertIsNone(pt.find_cm108(base=base))

    def test_does_not_just_take_hidraw0(self):
        # Pointing PTT at whatever landed on hidraw0 is how people key the
        # wrong device. The number depends on plug order, the vendor does not.
        base = self._fake_sysfs([
            ("hidraw0", "0003:0000046D:0000C52B", "bus/usb/001/001"),
            ("hidraw9", "0003:00000D8C:00000012", "bus/usb/001/009"),
        ])
        self.assertEqual(pt.find_cm108(base=base), "/dev/hidraw9")

    def test_raises_when_two_cm108_nodes_are_ambiguous(self):
        # Two Digirig Lites report the identical vendor:product -- the exact
        # case that broke this bench: keying "whichever sorts first" silently
        # picked the wrong cable's PTT once a second, identical unit was
        # plugged in.
        base = self._fake_sysfs([
            ("hidraw7", "0003:00000D8C:00000012", "bus/usb/003/013"),
            ("hidraw8", "0003:00000D8C:00000012", "bus/usb/003/014"),
        ])
        with self.assertRaises(RuntimeError):
            pt.find_cm108(base=base)

    def test_usb_device_disambiguates_between_two_cm108_nodes(self):
        base = self._fake_sysfs([
            ("hidraw7", "0003:00000D8C:00000012", "bus/usb/003/013"),
            ("hidraw8", "0003:00000D8C:00000012", "bus/usb/003/014"),
        ])
        self.assertEqual(
            pt.find_cm108(base=base, usb_device="bus/usb/003/013"),
            "/dev/hidraw7")
        self.assertEqual(
            pt.find_cm108(base=base, usb_device="bus/usb/003/014"),
            "/dev/hidraw8")

    def test_usb_device_with_no_match_returns_none(self):
        base = self._fake_sysfs([
            ("hidraw7", "0003:00000D8C:00000012", "bus/usb/003/013"),
        ])
        self.assertIsNone(
            pt.find_cm108(base=base, usb_device="bus/usb/999/999"))


class TestPttFactory(unittest.TestCase):

    def test_cm108_line_gives_the_hid_backend(self):
        self.assertIsInstance(pt.make_ptt("cm108", path="/dev/null"),
                              pt.Cm108Ptt)

    def test_serial_lines_give_the_serial_backend(self):
        for line in pt.LINES:
            self.assertIsInstance(pt.make_ptt(line), pt.PttPort)

    def test_cm108_is_offered_alongside_the_serial_lines(self):
        self.assertIn("cm108", pt.ALL_LINES)
        for line in pt.LINES:
            self.assertIn(line, pt.ALL_LINES)


class TestCm108Permissions(unittest.TestCase):

    def test_permission_error_points_at_the_udev_rule(self):
        # Root-only hidraw is THE reason CM108 PTT silently does nothing, and
        # an unexplained EACCES sends people looking at their wiring instead.
        p = pt.Cm108Ptt(path="/dev/null")
        with mock.patch.object(pt.os, "open", side_effect=PermissionError()):
            with self.assertRaises(PermissionError) as cm:
                p._open()
        self.assertIn("99-digirig.rules", str(cm.exception))

    def test_release_is_written_on_exit(self):
        written = []
        p = pt.Cm108Ptt(path="/dev/null", gpio=3)
        with mock.patch.object(pt.os, "open", return_value=9), \
             mock.patch.object(pt.os, "close"), \
             mock.patch.object(pt.os, "write",
                               side_effect=lambda fd, b: written.append(b)):
            with p:
                p.key()
        self.assertEqual(written[-1], bytes([0, 0, 0x00, 0x04, 0]))


class TestHissWindowCannotReachTheCarrier(unittest.TestCase):
    """The noise floor must be measured before anything keys, with margin.

    The recorder does not start capturing the moment it is launched, so the
    carrier lands earlier in the file than the nominal lead implies. Sizing the
    hiss window off the full lead leaves no room for that, and a floor computed
    partly from the carrier is how a real measurement turns into a refusal.
    """

    def test_window_ends_well_before_a_nominal_lead(self):
        step = 0.005
        usable = pt.MEASURE_LEAD_S - pt.tt.STARTUP_SKIP_S
        window_s = int(usable * 0.7 / step) * step
        ends_at = pt.tt.STARTUP_SKIP_S + window_s
        self.assertLess(ends_at, pt.MEASURE_LEAD_S - 0.2,
                        "hiss window ends at %.3f s, too close to keying at "
                        "%.3f s" % (ends_at, pt.MEASURE_LEAD_S))

    def test_floor_survives_a_recorder_that_started_late(self):
        # 300 ms of capture lost at the front: the carrier now begins 300 ms
        # earlier in the file than the lead length says it should.
        late = 0.30
        samples, fs = carrier_recording(1.05, lead_s=pt.MEASURE_LEAD_S - late)
        r = pt.analyze_carrier(samples, fs, 1.0)
        self.assertIsNone(r["problem"], r["problem"])
        self.assertAlmostEqual(r["overhead_s"], 0.05, delta=0.04)


class TestDeadCaptureIsRefused(unittest.TestCase):
    """A file of one repeated sample is not a quiet radio.

    pw-record given a --target that does not exist writes exactly that, and the
    carrier logic then reports "no carrier" -- blaming the radio, the frequency
    and the wiring for a device name that had silently lost a ".2" suffix when
    another sound card was plugged in. The real recording had 261120 samples
    and one distinct value.
    """

    def test_a_constant_recording_is_reported_as_a_dead_device(self):
        samples = [15124] * (5 * 8000)
        r = pt.analyze_carrier(samples, 8000, 1.0)
        self.assertIsNone(r["carrier_s"])
        self.assertIn("dead", r["problem"])

    def test_digital_silence_is_also_refused(self):
        r = pt.analyze_carrier([0] * (5 * 8000), 8000, 1.0)
        self.assertIsNone(r["carrier_s"])
        self.assertIn("dead", r["problem"])

    def test_it_does_not_blame_the_radio(self):
        # The old message sent you to check the squelch, the frequency and the
        # wiring. None of those was the problem.
        r = pt.analyze_carrier([15124] * (5 * 8000), 8000, 1.0)
        self.assertNotIn("frequency", r["problem"])
        self.assertIn("pactl", r["problem"])

    def test_a_real_recording_still_passes_the_guard(self):
        samples, fs = carrier_recording(1.05)
        r = pt.analyze_carrier(samples, fs, 1.0)
        self.assertIsNone(r["problem"], r["problem"])


class TestSourcePreflight(unittest.TestCase):

    AVAIL = ["alsa_input.pci-0000_03_00.6.HiFi__Mic1__source",
             "alsa_input.usb-C-Media_Electronics_Inc._USB_Audio_Device-00.mono-fallback"]

    def test_a_known_device_passes(self):
        pt.check_source(self.AVAIL[0], available=self.AVAIL)

    def test_an_unknown_device_is_refused_before_recording(self):
        with self.assertRaises(RuntimeError) as cm:
            pt.check_source("alsa_input.pci-0000_03_00.6.HiFi__Mic1__source.2",
                            available=self.AVAIL)
        self.assertIn("does not exist", str(cm.exception))

    def test_the_error_lists_what_is_actually_there(self):
        with self.assertRaises(RuntimeError) as cm:
            pt.check_source("nope", available=self.AVAIL)
        for name in self.AVAIL:
            self.assertIn(name, str(cm.exception))

    def test_an_empty_listing_does_not_block_the_run(self):
        # If pactl is missing we cannot know, and guessing wrong would refuse a
        # working setup. Absence of evidence is not a reason to stop.
        pt.check_source("anything", available=[])


class TestSlowToKeyIsNotFreeAirtime(unittest.TestCase):
    """A carrier shorter than the assertion is a cost, not a discount.

    VOX is slow at the back -- it holds the channel after the audio stops -- so
    the carrier runs LONGER than the signal and subtracting one from the other
    gives a positive overhead. PTT turned out to be slow at the FRONT: the
    390's carrier came up 165 ms after the line went active, making the carrier
    SHORTER than the assertion. The same subtraction then reported -165 ms and
    a packet rate better than a radio with no overhead at all.
    """

    def test_a_slow_key_is_reported_as_overhead_not_a_negative(self):
        samples, fs = carrier_recording(0.835)
        r = pt.analyze_carrier(samples, fs, 1.0)
        self.assertIsNone(r["problem"], r["problem"])
        self.assertGreater(r["overhead_s"], 0.0)
        self.assertAlmostEqual(r["overhead_s"], 0.165, delta=0.03)

    def test_the_slow_end_is_named(self):
        samples, fs = carrier_recording(0.835)
        self.assertEqual(pt.analyze_carrier(samples, fs, 1.0)["dominant"],
                         "attack")
        samples, fs = carrier_recording(1.4)
        self.assertEqual(pt.analyze_carrier(samples, fs, 1.0)["dominant"],
                         "hold")

    def test_packet_rate_accounts_for_a_slow_key(self):
        # 12 s of airtime a minute / (0.165 s dead + 0.4 s of data) = 21, not
        # the 30 you get by clamping the overhead to zero.
        samples, fs = carrier_recording(0.835)
        text = pt.report_carrier(pt.analyze_carrier(samples, fs, 1.0))
        self.assertIn("about 21", text)

    def test_report_says_which_end_and_why_it_costs(self):
        samples, fs = carrier_recording(0.835)
        text = pt.report_carrier(pt.analyze_carrier(samples, fs, 1.0))
        self.assertIn("slow to KEY", text)
        self.assertIn("would be lost", text)

    def test_report_admits_the_figure_is_a_floor(self):
        # Keying delay and hold-over cannot be separated without a clock shared
        # by the keying and the recorder, so a radio doing both shows their
        # difference rather than their sum.
        samples, fs = carrier_recording(0.835)
        self.assertIn("floor", pt.report_carrier(
            pt.analyze_carrier(samples, fs, 1.0)))


class TestClippingIsNoted(unittest.TestCase):

    def test_a_railed_recording_is_flagged_but_still_measured(self):
        samples, fs = carrier_recording(1.05, hiss=0.30)
        rail = 0.30
        samples = [max(-rail, min(rail, x)) for x in samples]
        r = pt.analyze_carrier(samples, fs, 1.0)
        self.assertIsNone(r["problem"], r["problem"])
        self.assertGreater(r["clipped_fraction"], 0.01)
        self.assertIn("clipping", pt.report_carrier(r))

    def test_a_clean_recording_is_not_flagged(self):
        samples, fs = carrier_recording(1.05)
        r = pt.analyze_carrier(samples, fs, 1.0)
        self.assertLess(r["clipped_fraction"], 0.01)
        self.assertNotIn("clipping", pt.report_carrier(r))


class TestClippingIsTreatedAsSuspect(unittest.TestCase):
    """Clipping moves the carrier edge; it is not cosmetic.

    A saturated hiss stays pinned at the rail while the true level is already
    falling, so the carrier reads as arriving later than it did. The same radio
    measured 165 ms with a heavily clipped capture and 95 ms with a less
    clipped one, which is most of the difference between "PTT is 20x better
    than VOX" and "PTT is about twice as good".

    It also clips in the capture hardware, upstream of any PipeWire volume, so
    the obvious fix does not work: the Brio was pinned at 0.064 full scale
    because pactl 40% is cubic (0.4^3) and had merely scaled a railed signal
    down. The gain that mattered was +30 dB in the ALSA mixer.
    """

    def _clipped(self):
        samples, fs = carrier_recording(1.05, hiss=0.30)
        rail = 0.28
        return [max(-rail, min(rail, x)) for x in samples], fs

    def test_clipping_is_a_warning_not_a_note(self):
        samples, fs = self._clipped()
        text = pt.report_carrier(pt.analyze_carrier(samples, fs, 1.0))
        self.assertIn("WARNING", text)
        self.assertIn("SUSPECT", text)

    def test_it_does_not_call_clipping_harmless(self):
        samples, fs = self._clipped()
        self.assertNotIn("Harmless",
                         pt.report_carrier(pt.analyze_carrier(samples, fs, 1.0)))

    def test_it_points_at_the_gain_that_actually_matters(self):
        # Not the PipeWire slider, which is downstream and cubic.
        samples, fs = self._clipped()
        text = pt.report_carrier(pt.analyze_carrier(samples, fs, 1.0))
        self.assertIn("amixer", text)
        self.assertIn("UPSTREAM", text)

    def test_a_clean_capture_gets_no_warning(self):
        samples, fs = carrier_recording(1.05)
        self.assertNotIn("SUSPECT", pt.report_carrier(
            pt.analyze_carrier(samples, fs, 1.0)))
