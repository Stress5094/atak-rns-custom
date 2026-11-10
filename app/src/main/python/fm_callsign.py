"""Station identification on FM: the operator's callsign as audible Morse.

SENT IN THE CLEAR, ON PURPOSE. Everything else this app puts on FM is sealed
with the network key (mesh.py) and modulated by the data modem, so a listener
without both hears noise. A station ID exists for the opposite reason -- anyone
with a receiver, including a regulator with a scanner, must be able to tell who
is transmitting -- so it uses neither: no key, no modem, just an on/off tone
any ear or CW decoder reads.

The audio service (FmAudioBridgeService) asks for this every N minutes while
the FM link is open and plays it through the same keying, key-up wait,
AudioTrack and cable as a data burst, after listening for a clear channel.

PURE: no RNS, no Android, no libcodec2, so it is testable on the desktop.
"""

import math
import struct

# Standard International Morse for what a callsign can contain. "/" covers
# portable/mobile suffixes (KX1ABC/P, KX1ABC/M).
MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    "/": "-..-.",
}

MIN_LEN = 3
MAX_LEN = 12

# 20 WPM (PARIS timing: one dit = 1.2 / WPM seconds = 60 ms). Slow enough for
# a human or any software decoder, fast enough that a typical 6-character call
# is ~4.7 s of airtime.
WPM = 20
# Mid-band for an FM voice channel: well inside the 300-3000 Hz audio passband
# on every radio, clear of CTCSS (< 260 Hz) and away from the pre-emphasis
# boost at the top.
TONE_HZ = 800
# Same peak as the tune sweep's lead tone (fm_tune.AMP): a level this cable
# path has already been driven at without clipping the radio's mic input.
AMP = 0.5
# Raised-cosine edges. A hard-keyed tone splatters key clicks across the
# channel; 5 ms is the usual shaping for 20 WPM.
RAMP_S = 0.005
# Silence after the last element, so the AudioTrack drain is not cutting off a
# dah.
TAIL_S = 0.10


def normalize(callsign):
    """The callsign as it will be sent (upper case, trimmed), or None when it
    is empty or not something to put on the air: wrong length, a character
    Morse here does not cover, or no digit (every amateur/GMRS callsign has
    one, and a missing digit is almost always a typo or a name)."""
    if callsign is None:
        return None
    c = str(callsign).strip().upper()
    if not (MIN_LEN <= len(c) <= MAX_LEN):
        return None
    if any(ch not in MORSE for ch in c):
        return None
    if not any(ch.isdigit() for ch in c) or not any(ch.isalpha() for ch in c):
        return None
    return c


def dit_s(wpm=WPM):
    return 1.2 / wpm


def keying(text, wpm=WPM):
    """The on/off schedule for `text`: a list of (on, seconds). Dit 1, dah 3,
    1 between elements, 3 between letters, 7 between words, in dits."""
    d = dit_s(wpm)
    out = []
    for wi, word in enumerate(text.split()):
        if wi:
            out.append((False, 7 * d))
        for li, ch in enumerate(word):
            if li:
                out.append((False, 3 * d))
            for ei, el in enumerate(MORSE[ch]):
                if ei:
                    out.append((False, d))
                out.append((True, d if el == "." else 3 * d))
    return out


def duration_s(text, wpm=WPM, lead_s=0.0):
    return lead_s + sum(s for _, s in keying(text, wpm)) + TAIL_S


def samples(text, fs, wpm=WPM, tone_hz=TONE_HZ, amp=AMP, lead_s=0.0):
    """Float samples of `text` in Morse, preceded by `lead_s` of steady tone.

    The lead tone is for a cable with no PTT pin: the radio's VOX switches on
    0.3-0.7 s after audio starts, and without something in front it would eat
    the first letter. Under CM108 keying it is 0 -- the key-up wait already
    holds the audio until the carrier is up. Phase-continuous throughout, so
    the only edges are the shaped ones."""
    sched = ([(True, lead_s), (False, 3 * dit_s(wpm))] if lead_s > 0 else [])
    sched += keying(text, wpm)
    sched.append((False, TAIL_S))
    step = 2.0 * math.pi * tone_hz / fs
    ramp = max(1, int(round(RAMP_S * fs)))
    out = []
    n = 0
    for on, secs in sched:
        count = int(round(secs * fs))
        if not on:
            out.extend([0.0] * count)
            n += count
            continue
        r = min(ramp, count // 2)
        for i in range(count):
            if i < r:
                env = 0.5 - 0.5 * math.cos(math.pi * i / r)
            elif i >= count - r:
                env = 0.5 - 0.5 * math.cos(math.pi * (count - 1 - i) / r)
            else:
                env = 1.0
            out.append(amp * env * math.sin(step * (n + i)))
        n += count
    return out


def pcm16(text, fs, **kw):
    """`samples` as mono PCM16LE bytes, what AudioTrack plays."""
    s = samples(text, fs, **kw)
    return struct.pack("<%dh" % len(s), *(int(round(v * 32767)) for v in s))
