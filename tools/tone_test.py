"""Prove the audio path to the radio, and measure what VOX actually costs.

    python3 tools/tone_test.py --devices          # what can play and record
    python3 tools/tone_test.py --make             # write the test tone
    python3 tools/tone_test.py --run --play-dev X --rec-dev Y
    python3 tools/tone_test.py --analyze rec.wav  # a recording made any way

WHY THIS IS THE FIRST THING ON HARDWARE. Every airtime figure in this project
-- the channel budget, the digest interval, the bundling window, how many
messages a minute the link carries -- is computed from two numbers that are
currently GUESSES: how long VOX takes to key the transmitter, and how long it
holds it open afterwards. Between a short and a long hang setting the channel's
whole capacity roughly halves, which is a larger effect than anything
achievable in software. Measuring them is worth more than any amount of
further modelling.

HOW IT MEASURES WITHOUT SYNCHRONISED CLOCKS. Playback and recording are
separate devices with separate clocks, and lining them up is fiddly and
error-prone. It is also unnecessary, because both answers are visible in the
RECEIVED audio alone:

    played:    [silence][=========== tone ===========][silence....]
    received:  [silence][xxx][====== tone =======][~~~~~~][silence]
                         ^                        ^      ^
                         VOX keys the TX          |      VOX drops
                                                  tone ends

  attack = how much of the tone went missing  = played length - heard length
  hang   = carrier still up after the tone     = carrier end - tone end

Both are differences within one recording, so no clock alignment is involved.

THE SETUP IT EXPECTS. Two radios: the laptop drives radio A through the
audio cable, radio A transmits, radio B receives, and the laptop records radio
B -- either through a second audio input or, quite adequately, through its own
built-in microphone pointed at radio B's speaker. Sound travels a foot in a
millisecond, which is nothing next to a VOX hang measured in hundreds.

RUN THE LOOPBACK FIRST. With the adapter's output wired straight back to its
input and no radio involved, attack and hang must both come out near zero. If
they do not, the fault is in the audio path and no radio measurement taken
afterwards means anything.

VOX hang depends on the CABLE at the radio end. One MD-UV390, one channel, one
VOX setting, one receiver, minutes apart:

    Digirig Lite + Digirig Kenwood cable    125, 125, 145 ms hang
    APRS-K1 + laptop analog jack                     3215 ms hang
    APRS-K1 + UGREEN CM722 USB-C DAC                 3205 ms hang

The first reading of this was that an analog output leaves residue above the
VOX threshold while a USB DAC goes truly silent. The UGREEN run kills that: it
is a USB DAC and it behaves like the analog jack. What the slow cases share is
the APRS-K1, not the sound card, and swapping the Digirig cable back on gave
145 ms again -- five runs, both cables reproducible.

The mechanism is not established. Worth noting that 3.2 s reproduces to within
10 ms across two entirely different DACs, which looks like a fixed timer in the
radio rather than anything analogue decaying -- so the open question may be why
the Digirig cable gets ~130 ms, not why the APRS-K1 gets 3.2 s.

Hang is the trustworthy half of this measurement. It comes from the hiss
returning, which is unambiguous. ATTACK is not: it needs a tone edge, and those
ramp. The same cable measured 85 ms and then 170 ms after the receiving radio's
volume went up. Use the PTT figure for keying delay -- that one needs no tone
at all.

Either way the practical shape is clear, and it is the wrong way round for the
obvious setup: a phone driving a handheld through an APRS-K1 on VOX costs 3.3 s
of dead air per transmission, about 3 packets a minute. The APRS-K1 has no PTT
line, so there is no way to fix it in software.
"""

import argparse
import json
import math
import os
import struct
import subprocess
import sys
import wave

DEFAULT_TONE_HZ = 1500.0     # mid-passband: clear of both filter edges
DEFAULT_FS = 48000           # what the phone will use, so use it here too
LEAD_SILENCE_S = 1.0
TONE_S = 1.0
# Must exceed the longest plausible VOX hang, or the hiss never returns inside
# the recording and hang comes back as a floor rather than a measurement. Was
# 3.0 until a UV-5R measured 4.5 s and very nearly ran off the end of the file.
TAIL_SILENCE_S = 8.0
ENVELOPE_WINDOW_S = 0.005    # 5 ms: fine enough to time VOX, coarse enough
                             # not to chase individual cycles


# -- signal generation ------------------------------------------------------

def make_test_signal(fs=DEFAULT_FS, tone_hz=DEFAULT_TONE_HZ, amp=0.5):
    """Silence, one tone burst, then silence long enough to see VOX drop."""
    out = [0.0] * int(LEAD_SILENCE_S * fs)
    out += [amp * math.sin(2.0 * math.pi * tone_hz * i / fs)
            for i in range(int(TONE_S * fs))]
    out += [0.0] * int(TAIL_SILENCE_S * fs)
    return out


def write_wav(path, samples, fs=DEFAULT_FS):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes(b"".join(
            struct.pack("<h", int(max(-32768, min(32767, round(s * 32767)))))
            for s in samples))


def read_wav(path):
    """Returns (samples as floats in +/-1, sample rate). Mixes to mono, since
    a recording interface may hand back stereo whether asked to or not."""
    with wave.open(path, "rb") as w:
        ch, width, fs, n = (w.getnchannels(), w.getsampwidth(),
                            w.getframerate(), w.getnframes())
        raw = w.readframes(n)
    if width != 2:
        raise ValueError("expected 16-bit WAV, got %d-bit" % (width * 8))
    vals = struct.unpack("<%dh" % (len(raw) // 2), raw)
    if ch > 1:
        vals = [sum(vals[i:i + ch]) / float(ch)
                for i in range(0, len(vals) - ch + 1, ch)]
    return [v / 32768.0 for v in vals], fs


# -- analysis ---------------------------------------------------------------

def goertzel(samples, fs, freq):
    """Energy at one frequency, normalised by length.

    Goertzel rather than a full transform because exactly one frequency is of
    interest and this is a handful of operations per sample -- no numpy, no
    dependency, and fast enough to run over a whole recording per window.
    """
    n = len(samples)
    if n == 0:
        return 0.0
    k = int(0.5 + (n * freq) / fs)
    w = (2.0 * math.pi / n) * k
    coeff = 2.0 * math.cos(w)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s2, s1 = s1, s0
    power = s1 * s1 + s2 * s2 - coeff * s1 * s2
    return math.sqrt(max(0.0, power)) / n


def tone_ratio(block, fs, tone_hz):
    """How much this block looks like a TONE rather than like noise.

    Energy at the tone frequency divided by energy either side of it. A real
    tone is a peak and scores high; broadband noise has energy everywhere and
    scores about 1 whatever its level.

    Measuring absolute energy at the tone frequency instead does not work, and
    failing to notice that is what the first version did: when the squelch
    opens, the received noise floor jumps by more than an order of magnitude,
    and raw energy at the tone frequency rises with it. The detector then
    reports the tone lasting the entire transmission -- which makes VOX hang
    look like zero, the one number this tool exists to measure.
    """
    on = goertzel(block, fs, tone_hz)
    off = 0.5 * (goertzel(block, fs, tone_hz * 0.72)
                 + goertzel(block, fs, tone_hz * 1.28))
    return on / off if off > 1e-12 else 0.0


# A tone must stand this far above its neighbours to count as present.
# Comfortably above what noise produces (~1) and below a clean tone (>>10).
TONE_RATIO_THRESHOLD = 3.0


# Before a window's peakiness is believed, it must also be genuinely loud.
# Expressed against the loudest tone energy in the recording rather than
# against the median, because the median of a mostly-quiet file is minute and
# any multiple of it still admits noise. A real tone is a large fraction of
# the peak; a quotient of two tiny numbers is a fraction of a percent of it.
# The median term is a backstop for a recording with no real tone at all,
# where the "peak" is itself only noise.
TONE_PEAK_FRACTION = 0.03
TONE_MEDIAN_FACTOR = 10.0


def envelope(samples, fs, window_s=ENVELOPE_WINDOW_S, tone_hz=None,
             absolute=False):
    """Per-window measure. With `tone_hz`, how tone-like the window is (or,
    with `absolute`, raw energy at that frequency); without, the broadband
    level -- which is what shows the carrier opening."""
    step = max(1, int(window_s * fs))
    out = []
    for i in range(0, len(samples) - step + 1, step):
        block = samples[i:i + step]
        if tone_hz is None:
            out.append(math.sqrt(sum(x * x for x in block) / len(block)))
        elif absolute:
            out.append(goertzel(block, fs, tone_hz))
        else:
            out.append(tone_ratio(block, fs, tone_hz))
    return out, step / float(fs)


def _noise_floor(levels, step=None):
    """The level before anything was transmitted.

    Measured over a FIXED duration -- the silence the test signal begins with
    -- and not over a fraction of the recording. It was a fraction, and
    lengthening the tail from 3 s to 8 s stretched that window past the start
    of the tone, so the "silence" reference quietly came to include the tone
    itself and every threshold derived from it was wrong. A constant that
    depends on the length of an unrelated constant is a trap.
    """
    if step:
        n = max(10, int((LEAD_SILENCE_S - STARTUP_SKIP_S) * 0.8 / step))
    else:
        n = max(1, len(levels) // 10)
    head = levels[:min(n, len(levels))]
    return sorted(head)[len(head) // 2] if head else 0.0


def find_span(levels, floor, factor=4.0, min_run=3):
    """First and last window that is clearly above the noise floor.

    `min_run` consecutive windows are required so a single click -- which an
    FM link below threshold produces plenty of -- cannot be mistaken for the
    start of a transmission.
    """
    thresh = max(floor * factor, 1e-6)
    start = end = None
    run = 0
    for i, v in enumerate(levels):
        if v >= thresh:
            run += 1
            if run >= min_run:
                if start is None:
                    start = i - min_run + 1
                end = i
        else:
            run = 0
    return start, end, thresh


# Recording devices produce a loud click as they start. It clips, it lands
# inside the window the noise floor is measured over, and it is the loudest
# thing in a quiet recording -- so it drags the threshold up above everything
# real. Skipped outright; the tone does not begin until LEAD_SILENCE_S anyway.
STARTUP_SKIP_S = 0.25


def analyze(samples, fs, tone_hz=DEFAULT_TONE_HZ, played_tone_s=TONE_S):
    """What the recording says about the audio path and about VOX."""
    samples = samples[int(STARTUP_SKIP_S * fs):]
    broad, step = envelope(samples, fs)
    tonal, _ = envelope(samples, fs, tone_hz=tone_hz)

    # A ratio alone is not enough. In near-silence the tone frequency and its
    # neighbours are BOTH minute, and their quotient is just noise -- so
    # isolated windows score well above the threshold on energy a hundred
    # times weaker than any real tone. Because a span runs from the first hit
    # to the last, one such blip seconds after the transmission stretched the
    # measured tone across the whole recording and made a 1.0 s burst look
    # like 2.1 s.
    #
    # So a window must be BOTH peaked and genuinely loud: a real tone stands
    # far above the recording's own median at that frequency, and a quotient
    # of two tiny numbers does not.
    absolute, _ = envelope(samples, fs, tone_hz=tone_hz, absolute=True)
    med = sorted(absolute)[len(absolute) // 2] if absolute else 0.0
    peak_abs = max(absolute) if absolute else 0.0
    floor_abs = max(peak_abs * TONE_PEAK_FRACTION,
                    med * TONE_MEDIAN_FACTOR, 1e-9)
    tonal = [r if a >= floor_abs else 0.0 for r, a in zip(tonal, absolute)]

    c0, c1, cth = find_span(broad, _noise_floor(broad, step))
    # The tone series is already a ratio, so it needs an absolute threshold
    # rather than one derived from a noise floor.
    t0, t1, tth = find_span(tonal, 1.0, factor=TONE_RATIO_THRESHOLD)

    r = {
        "seconds": len(samples) / float(fs),
        "window_s": step,
        "carrier": None,
        "tone": None,
        "attack_s": None,
        "hang_s": None,
        "heard_tone_s": None,
        "problem": None,
    }

    # Without the tone there is nothing at all: no path, no attack, no hang.
    if t0 is None:
        r["problem"] = ("no tone found -- nothing came back at %d Hz. The "
                        "audio path is broken, or the radio never keyed, or "
                        "the recording device is wrong." % tone_hz)
        return r

    # Attack needs only the tone. How much of a known-length burst went
    # missing off the front IS the keying delay, and it stands whether or not
    # the carrier can be tracked -- which is why it is computed before any of
    # the carrier reasoning below, rather than being abandoned along with it.
    r["tone"] = (t0 * step, t1 * step)
    heard = (t1 - t0 + 1) * step
    r["heard_tone_s"] = heard
    r["attack_s"] = max(0.0, played_tone_s - heard)
    if c0 is not None:
        r["carrier"] = (c0 * step, c1 * step)

    # Hang needs the carrier to outlast the tone, and to have bracketed it in
    # the first place. Where it does not, the subtraction goes negative and
    # clamping it produces a confident "0 ms" for the single number this whole
    # tool exists to produce. Refused instead: a missing answer can be chased,
    # a plausible wrong one cannot.
    #
    # Tolerance of two windows because a carrier that drops exactly with the
    # tone -- a loopback, no VOX -- lands either side of the boundary
    # depending on quantisation, and that is a real zero, not a failure.
    if heard > played_tone_s * 1.5:
        r["problem"] = ("tone detected for %.2f s but only %.2f s was played "
                        "-- the detector is latching onto something that is "
                        "not the tone. Treat every figure below as suspect."
                        % (heard, played_tone_s))

    if c1 is not None and c1 >= t1 - 2:
        r["hang_s"] = max(0.0, (c1 - t1) * step)
    else:
        r["hang_s"] = None
        r["problem"] = (
            "carrier could not be tracked past the tone, so HANG WAS NOT "
            "MEASURED. The tone was found, so the audio path works and the "
            "attack figure above stands. Expected on an acoustic loopback, "
            "where there is no squelch to open. On a real radio it means the "
            "recording cannot separate an open carrier from room noise -- "
            "turn the receiving radio up, or move the microphone closer.")
        return r

    if heard > played_tone_s * 1.5:
        r["problem"] = ("tone detected for far longer than it was played -- "
                        "the detector is probably latching onto noise; check "
                        "the recording level and that the tone frequency "
                        "matches")
    return r


def report(r):
    lines = []
    lines.append("recording: %.2f s, %.0f ms resolution"
                 % (r["seconds"], r["window_s"] * 1000))
    if r["problem"] and r["tone"] is None:
        lines.append("")
        lines.append("FAILED: " + r["problem"])
        return "\n".join(lines)

    if r["carrier"]:
        lines.append("carrier up:  %.3f s -> %.3f s" % r["carrier"])
    lines.append("tone heard:  %.3f s -> %.3f s  (%.3f s of %.3f s played)"
                 % (r["tone"][0], r["tone"][1], r["heard_tone_s"], TONE_S))
    lines.append("")
    lines.append("VOX attack:  %.0f ms   (lost off the front of every burst)"
                 % (r["attack_s"] * 1000))
    if r["hang_s"] is None:
        lines.append("VOX hang:    NOT MEASURED")
        lines.append("")
        lines.append("WARNING: " + r["problem"])
        return "\n".join(lines)
    lines.append("VOX hang:    %.0f ms   (channel held after every burst)"
                 % (r["hang_s"] * 1000))
    overhead = r["attack_s"] + r["hang_s"]
    lines.append("")
    lines.append("per-transmission overhead: %.0f ms" % (overhead * 1000))
    if overhead > 0:
        lines.append("  = %.0f bytes of airtime at 1200 bps, spent on nothing"
                     % (overhead * 1200 / 8))
        lines.append("  -> at 20%% duty the channel carries about %.0f "
                     "60-byte packets/min"
                     % (12.0 / (overhead + (60 * 8) / 1200.0)))
    if r["problem"]:
        lines.append("")
        lines.append("WARNING: " + r["problem"])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Measuring hang without being able to see the carrier
#
# The single-burst method above cannot measure hang, and the first run on real
# hardware is what proved it. During VOX hang the transmitter is keyed but
# nothing is modulating it, so a receiving FM radio outputs SILENCE -- which at
# the far end is indistinguishable from room noise. The recording went straight
# from tone to noise with no squelch tail and no quiet-carrier period, so the
# "carrier" being tracked was only ever the tone's own loudness, and hang came
# out as 10 ms: the width of the tone's decay, not a property of the radio.
#
# It also leaves attack ambiguous. 135 ms of tone went missing, and nothing in
# a single burst says whether it was lost off the FRONT (slow keying) or off
# the BACK (VOX dropping mid-tone on marginal audio). Both look identical.
#
# Two bursts with a gap between them resolve both, and never need the carrier:
#
#   hang > gap :  the transmitter never drops, so the second burst is heard
#                 whole -- and the difference between the two burst lengths is
#                 exactly the attack, measured rather than assumed.
#   hang < gap :  the transmitter drops and re-keys, so the second burst loses
#                 its front just as the first did.
#
# Sweep the gap; the shortest one that makes the second burst come back short
# is the hang time.
# ---------------------------------------------------------------------------

BURST_S = 0.6


def make_two_burst_signal(gap_s, fs=DEFAULT_FS, tone_hz=DEFAULT_TONE_HZ,
                          amp=0.5):
    def tone(n):
        return [amp * math.sin(2.0 * math.pi * tone_hz * i / fs)
                for i in range(int(n * fs))]
    return ([0.0] * int(LEAD_SILENCE_S * fs) + tone(BURST_S)
            + [0.0] * int(gap_s * fs) + tone(BURST_S)
            + [0.0] * int(TAIL_SILENCE_S * fs))


def find_bursts(samples, fs, tone_hz=DEFAULT_TONE_HZ, min_gap_s=0.03):
    """Every run of tone in the recording, as (start, end) seconds."""
    samples = samples[int(STARTUP_SKIP_S * fs):]
    ratio, step = envelope(samples, fs, tone_hz=tone_hz)
    absolute, _ = envelope(samples, fs, tone_hz=tone_hz, absolute=True)
    med = sorted(absolute)[len(absolute) // 2] if absolute else 0.0
    peak = max(absolute) if absolute else 0.0
    floor_abs = max(peak * TONE_PEAK_FRACTION, med * TONE_MEDIAN_FACTOR, 1e-9)

    on = [r >= TONE_RATIO_THRESHOLD and a >= floor_abs
          for r, a in zip(ratio, absolute)]
    bursts = []
    i = 0
    gap_windows = max(1, int(min_gap_s / step))
    while i < len(on):
        if on[i]:
            j = i
            last = i
            # Bridge windows shorter than min_gap so a momentary dropout
            # inside one burst is not counted as two.
            while j < len(on) and (j - last) <= gap_windows:
                if on[j]:
                    last = j
                j += 1
            if (last - i + 1) * step >= 0.05:
                bursts.append((i * step, last * step))
            i = last + 1
        else:
            i += 1
    return bursts


def analyze_two_burst(samples, fs, gap_s, tone_hz=DEFAULT_TONE_HZ):
    """Did the transmitter stay keyed across the gap?"""
    bursts = find_bursts(samples, fs, tone_hz)
    r = {"bursts": bursts, "gap_s": gap_s, "bridged": None,
         "attack_s": None, "problem": None}
    if len(bursts) < 2:
        if len(bursts) == 1 and bursts[0][1] - bursts[0][0] > BURST_S * 1.5:
            # One long run covering both -- the gap was bridged AND the
            # receiver stayed quiet enough that the two never separated.
            r["bridged"] = True
            return r
        r["problem"] = ("expected two tone bursts, found %d -- check the "
                        "audio level and that the radio keyed at all"
                        % len(bursts))
        return r

    first = bursts[0][1] - bursts[0][0]
    second = bursts[1][1] - bursts[1][0]

    # The discriminator is that the two bursts DIFFER, not that the second is
    # short. The first burst always pays the attack, so:
    #
    #   bridged      -- transmitter stayed keyed, the second burst is heard
    #                   whole, and comes back LONGER than the first by
    #                   exactly the attack.
    #   not bridged  -- transmitter dropped and keyed again, so the second
    #                   burst pays the attack too and matches the first.
    #
    # Getting this backwards reads "bridged" in both cases, which is what the
    # first version did.
    r["bridged"] = second > first + 0.02

    # Attack comes from the first burst, which is keyed from cold whatever
    # happens afterwards.
    r["attack_s"] = max(0.0, BURST_S - first)
    r["first_s"], r["second_s"] = first, second

    # With no measurable attack the two bursts are identical either way and
    # nothing can be concluded -- say so instead of guessing.
    if r["attack_s"] < 0.03:
        r["problem"] = ("attack is too small to tell a re-key from a bridged "
                        "gap (both bursts arrive whole), so hang cannot be "
                        "inferred this way on this radio")
    return r


# -- running it -------------------------------------------------------------

def list_devices():
    for label, cmd in (("PLAYBACK", ["aplay", "-l"]),
                       ("CAPTURE", ["arecord", "-l"])):
        print("=== %s ===" % label)
        try:
            print(subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=10).stdout.strip() or "(none)")
        except Exception as exc:
            print("could not list: %s" % exc)
        print()
    print("Use the card/device numbers as e.g. --play-dev plughw:1,0")


def list_sources(kind="sources"):
    """PipeWire device names of one kind, or [] if they cannot be listed."""
    try:
        out = subprocess.run(["pactl", "list", "short", kind],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    names = []
    for row in out.stdout.splitlines():
        cols = row.split("\t")
        if len(cols) >= 2 and not cols[1].endswith(".monitor"):
            names.append(cols[1])
    return names


def check_device(name, kind="sources", available=None):
    """Refuse a PipeWire device that is not there.

    pw-record accepts an unknown --target without complaint and writes a file
    of one repeated sample; pw-play accepts one and plays into nothing. Either
    looks downstream like a radio that never transmitted, which is a far more
    interesting conclusion than the truth. Names move, too -- the built-in mic
    lost a ".2" suffix the moment another sound card was plugged in.
    """
    available = list_sources(kind) if available is None else available
    if not available or name in available:
        return
    raise RuntimeError(
        "%s device %r does not exist; it would be accepted silently and "
        "record or play nothing. Available:\n  %s"
        % (kind[:-1], name, "\n  ".join(available)))


def resolve_pw_target(name):
    """A PipeWire node name, resolved to its live object.serial.

    pw-play/pw-record's --target matches only a numeric object.serial, not a
    node name -- passing a name silently falls back to the default sink or
    source, no error, exit 0. USB audio devices accumulate stale
    duplicate-named nodes across reconnects (swapping one cable out for
    another and back), so this resolves to the most recently created match at
    call time, which is the live one.
    """
    if name is None or name.isdigit():
        return name
    try:
        dump = json.loads(subprocess.check_output(["pw-dump"], text=True))
    except (subprocess.CalledProcessError, OSError, ValueError):
        return name
    matches = [
        obj["info"]["props"]["object.serial"]
        for obj in dump
        if obj.get("type") == "PipeWire:Interface:Node"
        and obj.get("info", {}).get("props", {}).get("node.name") == name
    ]
    if not matches:
        return name
    return str(max(matches, key=int))


def run_test(wav_path, rec_path, play_dev, rec_dev, fs=DEFAULT_FS,
             use_pipewire=False, rec_pipewire=False, quiet=False):
    """Record while playing. Recording starts first and stops last, so the
    whole transmission including the VOX tail is inside it."""
    total = LEAD_SILENCE_S + TONE_S + TAIL_SILENCE_S + 1.0
    secs = int(math.ceil(total))

    # Checked before anything is recorded, so a moved device name costs a
    # message rather than a bench run and a confident wrong answer.
    if rec_pipewire and rec_dev:
        check_device(rec_dev, "sources")
    if use_pipewire and play_dev:
        check_device(play_dev, "sinks")

    # PipeWire will happily hold the capture device, at which point arecord
    # gets EBUSY. On this bench it claimed the microphone array between one
    # run and the next, so both paths have to exist.
    if rec_pipewire:
        rec_cmd = ["pw-record", "--rate", str(fs), "--channels", "1",
                   "--format", "s16"]
        if rec_dev:
            rec_cmd += ["--target", resolve_pw_target(rec_dev)]
        rec_cmd += [rec_path]
    else:
        rec_cmd = ["arecord", "-q", "-f", "S16_LE", "-r", str(fs), "-c", "1",
                   "-d", str(secs), rec_path]
        if rec_dev:
            rec_cmd[1:1] = ["-D", rec_dev]

    if use_pipewire:
        play_cmd = ["pw-play", wav_path]
        if play_dev:
            play_cmd[1:1] = ["--target", resolve_pw_target(play_dev)]
    else:
        play_cmd = ["aplay", "-q", wav_path]
        if play_dev:
            play_cmd[1:1] = ["-D", play_dev]

    # Remember what was here before. A failed recorder leaves the PREVIOUS
    # run's file sitting on disk, and analysing that reports an earlier
    # measurement as if it were this one -- which on this bench would have
    # passed off a loopback as a radio result. Staleness is checked, not
    # assumed.
    before = os.path.getmtime(rec_path) if os.path.exists(rec_path) else None

    if not quiet:
        print("recording %s for %d s ..." % (rec_path, secs))
    rec = subprocess.Popen(rec_cmd, stderr=subprocess.PIPE, text=True)
    try:
        # Let the recorder settle before any audio exists, so the head of the
        # file is genuine noise floor for the threshold to key off.
        subprocess.run(["sleep", "0.4"], check=False)
        if rec.poll() is not None:
            raise RuntimeError("recorder exited immediately: %s"
                               % (rec.stderr.read().strip() or "no message"))
        if not quiet:
            print("playing tone ...")
        subprocess.run(play_cmd, check=False)
        if rec_pipewire:
            subprocess.run(["sleep", str(TAIL_SILENCE_S)], check=False)
            rec.terminate()
        rec.wait(timeout=secs + 10)
    finally:
        if rec.poll() is None:
            rec.terminate()

    if not os.path.exists(rec_path):
        raise RuntimeError("no recording was produced: %s"
                           % (rec.stderr.read().strip() if rec.stderr else ""))
    after = os.path.getmtime(rec_path)
    if before is not None and after <= before:
        raise RuntimeError(
            "the recording file was not updated -- the recorder failed and "
            "an OLDER file is still on disk. Refusing to analyse it: it would "
            "report a previous run's numbers as this one's.")
    return rec_path


# Extended past 2 s after a UV-5R bridged every gap up to it. TAIL_SILENCE_S
# has to outlast the longest gap or the recording ends mid-measurement.
HANG_GAPS = (0.2, 0.5, 1.0, 2.0, 3.0, 4.5, 6.0)


def hang_sweep(args):
    global TAIL_SILENCE_S
    """Two bursts with a growing gap. The shortest gap the transmitter does
    NOT bridge is the hang time."""
    TAIL_SILENCE_S = 2.0
    print("VOX hang sweep: two %.1f s bursts, gap growing.\n"
          "A bridged gap means the transmitter never dropped.\n" % BURST_S)
    print("%6s  %-9s  %-8s  %-8s  %s"
          % ("gap", "bursts", "1st", "2nd", "verdict"))
    print("-" * 56)
    attacks = []
    crossover = None
    for gap in HANG_GAPS:
        write_wav(args.out, make_two_burst_signal(gap, args.fs, args.tone),
                  args.fs)
        try:
            run_test(args.out, args.rec_out, args.play_dev, args.rec_dev,
                     args.fs, use_pipewire=args.pw, rec_pipewire=args.rec_pw,
                     quiet=True)
        except RuntimeError as exc:
            print("%6.1f  FAILED: %s" % (gap, exc))
            return 1
        samples, fs = read_wav(args.rec_out)
        r = analyze_two_burst(samples, fs, gap, args.tone)

        # A row that did not produce exactly two bursts of plausible length
        # measured nothing, and must not reach the summary. The control run
        # -- no radio, no VOX at all -- had one row find FOUR bursts of 55 and
        # 130 ms, and that single piece of garbage was enough to print
        # "VOX attack: 545 ms" and "hang longer than 6.0 s" for a path with
        # neither. Every other row had correctly declined to measure.
        usable = (len(r["bursts"]) == 2 and not r["problem"]
                  and r.get("first_s", 0) > BURST_S * 0.3
                  and r.get("second_s", 0) > BURST_S * 0.3)
        if not usable:
            why = r["problem"] or ("found %d bursts, expected 2"
                                   % len(r["bursts"]))
            print("%6.1f  %-9d  %s" % (gap, len(r["bursts"]), why[:44]))
            continue

        attacks.append(r["attack_s"])
        verdict = "bridged" if r["bridged"] else "RE-KEYED"
        if not r["bridged"] and crossover is None:
            crossover = gap
        print("%6.1f  %-9d  %-8.3f  %-8.3f  %s"
              % (gap, len(r["bursts"]), r["first_s"], r["second_s"], verdict))

    print()
    # No usable row means no measurement -- which is the honest outcome for a
    # path with no VOX in it, and must not be dressed up as one.
    if not attacks:
        print("NOTHING MEASURED: no gap produced a usable pair of bursts.")
        print("  Expected when there is no VOX in the path at all (the")
        print("  control run), because with no attack the two bursts are")
        print("  identical whether the transmitter re-keyed or not.")
        print("  With a radio in the path it means the audio level is wrong")
        print("  or the radio is not keying.")
        return 1

    print("VOX attack: %.0f ms (mean of %d usable gaps)"
          % (1000 * sum(attacks) / len(attacks), len(attacks)))
    if crossover is None:
        print("VOX hang:   longer than %.1f s -- every usable gap was bridged"
              % max(g for g in HANG_GAPS))
    else:
        prev = [g for g in HANG_GAPS if g < crossover]
        lo = prev[-1] if prev else 0.0
        print("VOX hang:   between %.1f s and %.1f s" % (lo, crossover))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--devices", action="store_true")
    ap.add_argument("--squelch-open", action="store_true",
                    help="the receiving radio's squelch is open, so hang is "
                         "visible as a quiet gap before the hiss returns")
    ap.add_argument("--hang-sweep", action="store_true",
                    help="measure VOX hang with two bursts and a growing gap")
    ap.add_argument("--make", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--analyze", metavar="WAV")
    ap.add_argument("--play-dev")
    ap.add_argument("--pw", action="store_true",
                    help="play through PipeWire (pw-play) rather than ALSA; "
                         "--play-dev then names a sink, not a hw device")
    ap.add_argument("--rec-dev")
    ap.add_argument("--rec-pw", action="store_true",
                    help="record through PipeWire (pw-record); needed when "
                         "PipeWire holds the capture device. --rec-dev then "
                         "names a source, not a hw device")
    ap.add_argument("--tone", type=float, default=DEFAULT_TONE_HZ)
    ap.add_argument("--fs", type=int, default=DEFAULT_FS)
    ap.add_argument("--out", default="tone_test.wav")
    ap.add_argument("--rec-out", default="tone_test_rec.wav")
    args = ap.parse_args(argv)

    if args.devices:
        list_devices()
        return 0

    if args.hang_sweep:
        return hang_sweep(args)

    if args.make or args.run:
        write_wav(args.out, make_test_signal(args.fs, args.tone), args.fs)
        print("wrote %s (%.1f s: %.1f silence, %.1f tone at %.0f Hz, "
              "%.1f silence)" % (args.out,
                                 LEAD_SILENCE_S + TONE_S + TAIL_SILENCE_S,
                                 LEAD_SILENCE_S, TONE_S, args.tone,
                                 TAIL_SILENCE_S))
        if not args.run:
            return 0

    path = args.analyze
    if args.run:
      try:
        path = run_test(args.out, args.rec_out, args.play_dev, args.rec_dev,
                        args.fs, use_pipewire=args.pw,
                        rec_pipewire=args.rec_pw)
      except RuntimeError as exc:
        print("\nFAILED: %s" % exc)
        return 1

    if not path:
        ap.print_help()
        return 2
    if not os.path.exists(path):
        print("no such recording: %s" % path)
        return 1

    samples, fs = read_wav(path)
    print()
    if args.squelch_open:
        print(report_open_squelch(
            analyze_open_squelch(samples, fs, args.tone)))
    else:
        print(report(analyze(samples, fs, args.tone)))
    return 0




# ---------------------------------------------------------------------------
# Measuring hang with the receiving radio's squelch OPEN
#
# Three methods failed before this one, each producing a believable number
# that was an artefact: 10 ms, then "longer than 2 s", then "longer than 6 s".
# All three failed for the same underlying reason -- a transmitter that is
# keyed but unmodulated produces SILENCE at the receiver, and silence is
# exactly what a closed squelch produces too. The thing being measured had no
# signature.
#
# Opening the squelch inverts that, and turns the quietest possible event into
# the loudest possible change:
#
#     no carrier     ->  loud hiss
#     carrier, tone  ->  tone
#     carrier, quiet ->  SILENCE   <- this is the hang, and now it is obvious
#     no carrier     ->  loud hiss returns
#
# So hang is the length of the quiet gap between the tone stopping and the
# hiss coming back. That is a large level change with a sharp edge, rather
# than a 50 ms difference between two burst lengths competing with 85 ms of
# scatter -- which is why the previous method could not resolve it.
# ---------------------------------------------------------------------------

# How far below the un-keyed hiss level a window must fall to count as the
# receiver being quieted by a carrier.
# A quieted carrier has to sit this far below the hiss. Not tighter: on a real
# recording the quiet state measured 51% of the hiss floor -- a genuine 4.4 s
# hang with clean edges -- and a 50% threshold refused it by 0.0006. Room noise
# and a running air conditioner put a floor under the "quiet" state that no
# radio can push below, so the test has to be what a RECEIVER can achieve
# rather than what silence would look like.
QUIET_FRACTION = 0.6

# How long the hiss must stay back before the transmitter is believed to have
# dropped. A quieted carrier sits near the threshold and individual windows
# cross it constantly.
HISS_RETURN_S = 0.10
# Where to call the edge of the received tone, as a fraction of the tone's own
# plateau. Half power is the conventional place to measure a pulse width, and
# more to the point it is the only reference here that does not move with the
# recording level.
TONE_HALF_POWER = 0.5
# How much longer than the played tone the received tone may measure before the
# span is treated as untrustworthy rather than clamped. Window quantisation is
# a few ms; 15% is far outside it.
TONE_OVERRUN_TOLERANCE = 1.15


def _refine_tone_span(absolute, t0, t1, fraction=TONE_HALF_POWER):
    """Re-measure a tone span against a fraction of its own plateau.

    The coarse span comes from a ratio test plus a fixed fraction of the whole
    recording's peak, and that peak moves with the recording level, so the same
    1.000 s tone through the same radio measured 0.555 s in one run and 1.010 s
    in the next -- reporting 440 ms and 0 ms of VOX attack for what is one
    number. 1.010 s is the tell: a tone cannot come back longer than it went
    out, so the span, not the radio, was being measured.

    The tone's own plateau is the one reference in the recording that does not
    depend on the gain someone happened to set. `absolute` is already Goertzel
    magnitude at the tone frequency, so broadband hiss contributes little and a
    half-plateau threshold sits far above it.
    """
    span = absolute[t0:t1 + 1]
    if not span:
        return t0, t1
    # Middle three fifths only: the ends are the ramps, and including them
    # drags the plateau estimate down towards the threshold it is setting.
    trim = len(span) // 5
    core = span[trim:len(span) - trim] or span
    ordered = sorted(core)
    plateau = ordered[len(ordered) // 2]
    if plateau <= 0.0:
        return t0, t1
    thresh = plateau * fraction

    # Scanned over the whole recording rather than only inside the coarse span,
    # because the coarse span is as likely to be too narrow as too wide -- the
    # ratio test breaks a real tone in half when the recording is quiet.
    best_len = 0
    best_start = t0
    i = 0
    while i < len(absolute):
        if absolute[i] >= thresh:
            j = i
            while j < len(absolute) and absolute[j] >= thresh:
                j += 1
            if j - i > best_len:
                best_len, best_start = j - i, i
            i = j
        else:
            i += 1
    if best_len == 0:
        return t0, t1
    return best_start, best_start + best_len - 1


def analyze_open_squelch(samples, fs, tone_hz=DEFAULT_TONE_HZ,
                         played_tone_s=TONE_S):
    """VOX attack and hang, from a recording made with the squelch open."""
    samples = samples[int(STARTUP_SKIP_S * fs):]
    broad, step = envelope(samples, fs)
    ratio, _ = envelope(samples, fs, tone_hz=tone_hz)
    absolute, _ = envelope(samples, fs, tone_hz=tone_hz, absolute=True)

    r = {"seconds": len(samples) / float(fs), "window_s": step,
         "tone": None, "attack_s": None, "hang_s": None,
         "hiss_level": None, "problem": None, "attack_problem": None}

    # A capture that never varies is a dead device, not a quiet radio.
    # pw-record given a target that no longer exists writes a file of one
    # repeated sample, and every threshold below would read that as a
    # transmitter that never keyed.
    if broad and max(broad) - min(broad) <= max(max(broad) * 1e-6, 1e-12):
        r["problem"] = (
            "the capture is dead -- every sample is the same value, so no "
            "audio device was actually recording. Check the device name "
            "against `pactl list short sources`. Nothing here reflects the "
            "radio.")
        return r

    # The head of the recording is un-keyed for certain -- the signal does not
    # start until LEAD_SILENCE_S -- so it is the reference for what hiss
    # sounds like. Median, so a start-up click cannot set it.
    hiss = _noise_floor(broad, step)
    r["hiss_level"] = hiss
    # Relative, not absolute. What matters is whether the un-keyed state is
    # loud compared with the signal -- an absolute threshold is just a guess
    # about someone's recording level.
    loudest = max(broad) if broad else 0.0
    if hiss <= max(loudest * 0.02, 1e-6):
        r["problem"] = ("the recording is silent where it should be hissing "
                        "-- the squelch is not open on the receiving radio, "
                        "or the recording level is far too low. Without hiss "
                        "there is no way to see the transmitter drop.")
        return r

    # Peak-relative only, with no median backstop. That backstop exists for
    # mostly-quiet recordings, where the median is minute and a multiple of it
    # is a sane floor. Here the background is deliberately LOUD -- that is the
    # whole method -- so ten times the median sits above the tone itself and
    # finds nothing. The ratio test still rejects broadband noise, which is
    # what the backstop was guarding against.
    peak = max(absolute) if absolute else 0.0
    floor_abs = max(peak * TONE_PEAK_FRACTION, 1e-9)
    on = [rr >= TONE_RATIO_THRESHOLD and a >= floor_abs
          for rr, a in zip(ratio, absolute)]
    # The LONGEST unbroken run, not the first and last hit. Loud hiss throws
    # isolated windows over the ratio threshold, and taking first-to-last lets
    # any one of them swallow the whole recording -- which is exactly how the
    # single-burst method turned a 1.0 s tone into 2.1 s. A real tone is one
    # long run; noise is scattered singletons.
    best_len = best_start = 0
    i = 0
    while i < len(on):
        if on[i]:
            j = i
            while j < len(on) and on[j]:
                j += 1
            if j - i > best_len:
                best_len, best_start = j - i, i
            i = j
        else:
            i += 1
    if best_len < 3:
        r["problem"] = ("no tone found at %d Hz -- the audio path is broken "
                        "or the radio never keyed" % tone_hz)
        return r

    # Refined against the tone's own plateau. The coarse run above only has to
    # be good enough to say WHERE the tone is; how long it is needs a threshold
    # that does not drift with the recording level.
    t0, t1 = _refine_tone_span(absolute, best_start, best_start + best_len - 1)
    r["tone"] = (t0 * step, t1 * step)
    heard = (t1 - t0 + 1) * step

    # attack is what the radio swallowed: the tone went out for played_tone_s
    # and came back shorter, because VOX had not keyed yet for the front of it.
    if heard > played_tone_s * TONE_OVERRUN_TOLERANCE:
        # Clamping this to zero is how a broken span reports itself as a
        # perfect radio. A tone that comes back LONGER than it was played is
        # not a fast VOX, it is a measurement that has failed, and saying so is
        # the only honest option -- hang is measured independently and is still
        # good, so it is worth carrying on rather than bailing out.
        r["attack_problem"] = (
            "the tone measured %.3f s but only %.3f s was played, so the span "
            "is wrong and attack cannot be derived from it. Hang below is "
            "measured separately and is unaffected."
            % (heard, played_tone_s))
    else:
        r["attack_s"] = max(0.0, played_tone_s - heard)

    # There has to BE a quiet state before hang can be measured. The right
    # test is not whether the tone is loud -- it is whether anything in the
    # recording sits clearly below the hiss, because that is the state being
    # timed. The first real open-squelch run had hiss at 0.053 and its
    # quietest stretch at 0.033: a 37% dip where a quieted carrier should be a
    # collapse. It duly reported "hang 0 ms", which is a number rather than a
    # measurement. The fix for that recording is turning the receiving radio
    # up, not a cleverer threshold.
    # Probed just after the tone, not across the whole tail. A percentile of
    # the tail depends on how long the tail happens to be: with an 8 s tail a
    # 200 ms hang is 2.5% of it and vanishes into the hiss. What matters is
    # whether the receiver goes quiet WHEN THE TONE STOPS.
    #
    # A couple of windows are skipped first so the tone's own decay is not
    # mistaken for the carrier state.
    lo = min(t1 + 3, len(broad))
    hi = min(lo + max(3, int(0.05 / step)), len(broad))
    probe = sorted(broad[lo:hi])
    quietest = probe[len(probe) // 2] if probe else hiss
    if quietest > hiss * QUIET_FRACTION:
        r["problem"] = (
            "no quiet state after the tone (quietest %.4f against hiss %.4f), "
            "which means one of two things and this cannot tell them apart: "
            "the transmitter dropped essentially at once, so there is no hang "
            "to see; or the levels are too close for a quieted carrier to "
            "show, in which case turn the receiving radio up and run it "
            "again. Given VOX normally holds for hundreds of ms, suspect the "
            "levels first." % (quietest, hiss))
        return r

    # Walk forward from the end of the tone for as long as the receiver stays
    # quieted. The hiss returning is the transmitter dropping.
    quiet_thresh = hiss * QUIET_FRACTION
    # The hiss has to come back and STAY back. A quieted carrier sits close
    # enough to the threshold that individual windows flicker across it, and
    # stopping at the first one that pops above reports a 4.4 s hang as 0 ms
    # -- which is what the first good recording did. Require a sustained
    # return before believing the transmitter dropped.
    need = max(3, int(HISS_RETURN_S / step))
    i = t1 + 1
    end = None
    run = 0
    while i < len(broad):
        if broad[i] >= quiet_thresh:
            run += 1
            if run >= need:
                end = i - need + 1
                break
        else:
            run = 0
        i += 1

    if end is None:
        r["hang_s"] = (len(broad) - t1 - 1) * step
        r["problem"] = ("the hiss never came back before the recording ended "
                        "-- hang is at least %.1f s, which is a floor rather "
                        "than a measurement. Lengthen TAIL_SILENCE_S and run "
                        "it again." % r["hang_s"])
        return r

    r["hang_s"] = (end - t1 - 1) * step

    return r


def report_open_squelch(r):
    lines = ["recording: %.2f s, %.0f ms resolution"
             % (r["seconds"], r["window_s"] * 1000)]
    if r["hiss_level"] is not None:
        lines.append("hiss floor: %.4f (squelch open)" % r["hiss_level"])
    if r["tone"] is None:
        lines.append("")
        lines.append("FAILED: " + (r["problem"] or "no tone"))
        return "\n".join(lines)
    lines.append("tone heard: %.3f s -> %.3f s" % r["tone"])
    lines.append("")
    if r["attack_s"] is None:
        lines.append("VOX attack: NOT MEASURED")
        lines.append("  " + (r["attack_problem"] or "unavailable"))
    else:
        lines.append("VOX attack: %4.0f ms" % (r["attack_s"] * 1000))
    if r["hang_s"] is None:
        lines.append("VOX hang:   NOT MEASURED")
        lines.append("")
        lines.append("WARNING: " + (r["problem"] or "no hang available"))
        return "\n".join(lines)
    lines.append("VOX hang:   %4.0f ms   (quiet carrier after the tone)"
                 % (r["hang_s"] * 1000))
    lines.append("")
    if r["attack_s"] is None:
        overhead = r["hang_s"]
        lines.append("per-transmission overhead: at least %.0f ms "
                     "(hang alone; attack unmeasured)" % (overhead * 1000))
    else:
        overhead = r["attack_s"] + r["hang_s"]
        lines.append("per-transmission overhead: %.0f ms" % (overhead * 1000))
    lines.append("  = %.0f bytes of airtime at 1200 bps, spent on nothing"
                 % (overhead * 1200 / 8))
    lines.append("  -> at 20%% duty the channel carries about %.0f "
                 "60-byte packets/min"
                 % (12.0 / (overhead + (60 * 8) / 1200.0)))
    if r["problem"]:
        lines.append("")
        lines.append("WARNING: " + r["problem"])
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
