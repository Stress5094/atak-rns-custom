"""Minimal adb driving helpers for milestone 8's phone-to-phone bench.

uiautomator dump for real button bounds, matched by visible text -- never
guessed from a screenshot. See docs/open-questions.md, "Driven over wireless
adb": the first attempt at this used screenshot-guessed coordinates and
silently missed the button, opening a settings dialog instead, which looked
exactly like "nothing happened" until logcat's AudioTrack timestamps were
checked against the recording. A missing/renamed button should raise here,
loudly, rather than tap the wrong thing.
"""
import re
import subprocess
import time
import xml.etree.ElementTree as ET

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def _adb(serial, *args):
    return subprocess.run(
        ["adb", "-s", serial] + list(args),
        capture_output=True, text=True, check=True)


def dump_bounds(serial, text):
    """(x, y) centre of the first view whose text or content-desc contains
    `text`, from a fresh uiautomator dump on `serial`.

    Matched case-insensitively: Android's default Button style renders text
    in all caps (a Kotlin-side `text = "FM RX test"` shows up in the dump as
    "FM RX TEST (10S)"), and a caller matching on the source string's exact
    case would silently find nothing -- which looks identical to "wrong
    screen" or "button renamed" from the exception alone.
    """
    _adb(serial, "shell", "uiautomator", "dump", "/sdcard/window_dump.xml")
    xml = _adb(serial, "shell", "cat", "/sdcard/window_dump.xml").stdout
    root = ET.fromstring(xml)
    needle = text.lower()
    for node in root.iter("node"):
        label = node.get("text") or node.get("content-desc") or ""
        if needle in label.lower():
            m = _BOUNDS_RE.match(node.get("bounds") or "")
            if not m:
                continue
            x1, y1, x2, y2 = (int(v) for v in m.groups())
            return (x1 + x2) // 2, (y1 + y2) // 2
    raise RuntimeError(
        "no view matching %r in uiautomator dump on %s -- button renamed, "
        "wrong screen, or app not in foreground?" % (text, serial))


def tap(serial, xy):
    x, y = xy
    _adb(serial, "shell", "input", "tap", str(x), str(y))


def tap_text(serial, text):
    """Find and tap a button by its visible text, in one call."""
    tap(serial, dump_bounds(serial, text))


# (serial, text.lower()) -> (x, y). A plain module-level dict is fine here --
# this is a short-lived driver script, not a long-running service, and a
# button's position on a static screen like this one doesn't move between
# trials, only its dialog cover comes and goes (handled separately, below).
_bounds_cache = {}


def cached_tap_text(serial, text):
    """Like tap_text, but skips the uiautomator dump on every call after the
    first for a given (serial, text).

    Worth it because uiautomator dump is the slow part of a trial -- two
    lookups (RX button, TX button) plus two dialog-dismiss checks each did
    their own dump, which is most of why trials were landing ~22-24s apart
    against a ~12s deliberate wait (see fm_phone_batch.py's RX_LEAD_S/
    RX_WINDOW_S/TAIL_S). The button itself doesn't move between trials --
    only whether a dialog is covering it does, and that is re-checked fresh
    every time by dismiss_dialog_if_present, never cached.
    """
    key = (serial, text.lower())
    xy = _bounds_cache.get(key)
    if xy is None:
        xy = dump_bounds(serial, text)
        _bounds_cache[key] = xy
    tap(serial, xy)


def invalidate(serial, text=None):
    """Drop a cached position (or all of them for `serial`) so the next
    cached_tap_text call re-dumps. Call this after a trial that looks like it
    silently missed -- a layout change, rotation, or this being run against a
    screen this cache has never seen would otherwise tap the same wrong
    coordinates forever.
    """
    if text is None:
        for key in [k for k in _bounds_cache if k[0] == serial]:
            del _bounds_cache[key]
    else:
        _bounds_cache.pop((serial, text.lower()), None)


def dismiss_dialog_if_present(serial, button_text="OK"):
    """Tap an AlertDialog's dismiss button if one is up, else do nothing.

    Both "FM TX test" and "FM RX test" leave their result AlertDialog open
    until it is tapped away -- confirmed the hard way: it sits directly over
    the buttons a driver needs to tap next, so a second trial's dump finds
    nothing and looks exactly like a renamed/missing button. Call this
    between trials, on both phones, before tapping anything else.
    """
    try:
        tap(serial, dump_bounds(serial, button_text))
    except RuntimeError:
        pass


def newest_name(serial, remote_glob):
    """The name of whichever file matching remote_glob has the newest mtime
    on `serial`, or None if nothing matches yet.
    """
    listing = _adb(serial, "shell", "ls", "-t", remote_glob).stdout.strip()
    if not listing:
        return None
    return listing.splitlines()[0].strip()


def pull_newest(serial, remote_glob, local_path, since=None,
                 poll_s=1.0, timeout_s=20.0):
    """Pull whichever file matching remote_glob has the newest mtime on
    `serial`, to `local_path`. Returns the pulled file's remote name (pass it
    back in as `since` on the next call for the same glob).

    `since`, if given, is the name pull_newest last returned for this glob.
    Found found on 2026-09-26 (docs/open-questions.md): a caller that always
    grabbed "whatever is newest right now" silently re-pulled the SAME file
    twice when the RX phone's record/decode/save cycle took longer than the
    caller's own fixed sleep -- on that bench, one phone's cycle sometimes
    exceeded fm_phone_batch.py's ~33s per-trial pacing, and consecutive
    trials for that direction ended up scoring the previous trial's result
    a second time, quietly doubling the reported count while halving the
    real sample size. Passing `since` makes that loud instead of silent:
    this polls until the newest name actually CHANGES, and raises rather
    than returning a repeat.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        newest = newest_name(serial, remote_glob)
        if newest is None:
            raise RuntimeError("no file matching %r on %s" % (remote_glob, serial))
        if since is None or newest != since:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "no NEW file matching %r appeared on %s within %.0fs -- still "
                "%r from last time. The RX phone may still be processing the "
                "previous trial (record/decode/save taking longer than this "
                "trial's wait), or its RX button tap didn't register." %
                (remote_glob, serial, timeout_s, since))
        time.sleep(poll_s)
    _adb(serial, "pull", newest, local_path)
    return newest
