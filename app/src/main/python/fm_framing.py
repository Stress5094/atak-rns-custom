"""Turning a payload into one fixed-size physical frame, and back.

TWO BUGS LIVED HERE, both of which broke the link completely while looking
like something else entirely. They are the whole reason this is its own
module with its own tests, instead of six lines inside the modem.

BUG ONE: THE PHYSICAL LAYER HAS NO LENGTH FIELD. Every frame is padded to the
code's full payload size before modulation, and an FSK-LDPC frame carries no
length of its own. A receiver gets back exactly the frame size every time:
real content, then padding, then the decoder's own trailing CRC, with no way
to tell where the real content ended. The mesh layer authenticates by exact
ciphertext length, so both the CRC trailer and any leftover padding always
failed it -- which means for a long time EVERY real frame this link ever
received was discarded, independent of RF quality, and the symptom was a link
that reported frames arriving and delivered nothing.

BUG TWO: ZERO PADDING IS NOT NEUTRAL. A genuine ~22 B beacon inside a 125 B
code (about 83% padding) failed to decode AT ALL -- zero frames recovered at
the physical layer, not merely a content mismatch -- while a near-full 125 B
payload on the same link, same radios, same distance, decoded cleanly. A long
run of zero bytes produces weak, low-diversity modulation: the received signal
was observed stuck parked on one tone for ~1 s stretches instead of hopping
across all four, and the demodulator could not recover timing or sync from it.

THE FIX IS SCRAMBLING, NOT RANDOM PADDING. Both solve the zero run, and the
first draft of this module used random padding, but scrambling is strictly
better on three counts:

  * It whitens the WHOLE frame, including the length prefix and the content.
    Random padding leaves real content unwhitened, and real content has runs
    of its own -- a CoT position report is largely ASCII digits, and a zeroed
    field inside a payload is just as parked-on-one-tone as a zeroed pad.
  * The frame becomes DETERMINISTIC. The same payload produces the same
    samples, so a bench run is reproducible and a tx/rx comparison is an
    equality rather than an unpack-and-compare. That is worth real money on a
    link where every measurement is a counting experiment.
  * It costs nothing: no entropy source to inject, nothing to go wrong on a
    platform where os.urandom is slow or unavailable.

THE ORDER IS LOAD-BEARING: SCRAMBLE INSIDE THE FEC. Scramble before the
encoder sees the bytes, unscramble after the decoder returns them. IL2P's
specification states the rule and the reason: a free-running scrambler
multiplies each received bit error by its tap count, so scrambling OUTSIDE the
error-correcting block destroys the error correction you are paying for.
Handing codec2 an already-scrambled payload is automatically the correct order,
which is most of why this lives here rather than in the modem.

AND THIS SCRAMBLER IS ADDITIVE, NOT SELF-SYNCHRONISING, which is the other
half of the same point. The keystream is a function of position in the frame,
generated from a fixed seed, so a bit error stays exactly one bit error. A
self-synchronising (multiplicative) scrambler would feed received bits back
into its own state and smear each error across its taps -- the failure mode
IL2P is warning about. Position synchronisation is free here because codec2
hands back whole frames, aligned, or nothing at all.

THE SECOND BUG IS INVISIBLE TO A THROUGHPUT TEST. A sweep that measures with
near-full payloads never produces the long zero run, so it passes. The traffic
this link actually carries -- beacons, short position reports -- is almost all
padding. Any future bench that tests only large payloads is not testing this.

PURE BYTES IN, PURE BYTES OUT. No modem, no audio, no RNS, so every case
above is a desktop unit test rather than a hillside. See test_fm_framing.py.
"""

import struct


# A 1-byte big-endian length prefix, PHYSICALLY TRANSMITTED, inside the
# frame's own payload space.
#
# One byte and not two because every code small enough to be worth using on
# this channel has a payload under 256 B, and on a link whose entire beacon
# budget was once 18 B a spare byte is real capacity. `capacity_for` below
# refuses a frame too large for the prefix rather than silently truncating,
# so choosing a bigger code is a caught error and not a corruption.
_LEN_PREFIX = struct.Struct(">B")
LEN_PREFIX_BYTES = _LEN_PREFIX.size

# The largest real payload a 1-byte prefix can describe.
MAX_ADDRESSABLE = 0xFF


class FramingError(ValueError):
    """A payload that cannot be framed for the configured code."""


# ---------------------------------------------------------------------------
# The scrambler.
#
# x^9 + x^4 + 1, seeded to all ones, which is the polynomial and seed IL2P
# specifies for exactly this job (whitening a short fixed-size frame ahead of
# its Reed-Solomon encoder). Taken from a specification rather than invented
# because there is nothing to gain by inventing one and a subtle way to lose:
# a poorly chosen tap set has short cycles, and a short cycle is a repeating
# byte pattern, which is the problem this is here to solve.
#
# THE PERIOD IS 511 BITS, WHICH IS 511 BYTES. The register is maximal-length,
# so the bit sequence repeats every 2^9-1 = 511 bits -- and because 511 is not
# a multiple of 8, each byte of output starts at a different phase and the BYTE
# stream does not repeat until 511 bytes. That is longer than every code in the
# table bar the 512 B one, where only the last byte wraps. Asserted in the
# tests, because a tap set with a short cycle would produce a repeating byte
# pattern, which is the exact problem this is here to solve.
#
# IT IS NOT A CRYPTOGRAPHIC REQUIREMENT -- nothing about the keystream is
# secret, the frame is already authenticated by the mesh layer underneath this,
# and treating a whitener as a cipher is how a link ends up with neither. The
# requirement is only that the modulator keeps hopping between tones.
# ---------------------------------------------------------------------------

_LFSR_SEED = 0x1FF        # 9 bits, all ones
_LFSR_TAPS = (9, 4)       # x^9 + x^4 + 1


def keystream(nbytes):
    """`nbytes` of scrambler output, from the fixed seed.

    Rebuilt per call rather than cached, which is cheap at these sizes (30 B
    for the shipped code) and keeps the function pure -- a cached buffer
    shared between a transmit thread and a receive thread is a race for no
    benefit.
    """
    reg = _LFSR_SEED
    out = bytearray()
    for _ in range(nbytes):
        byte = 0
        for _bit in range(8):
            # Fibonacci LFSR, MSB-first into the byte so the keystream reads
            # the same way the bytes do.
            feedback = ((reg >> (_LFSR_TAPS[0] - 1))
                        ^ (reg >> (_LFSR_TAPS[1] - 1))) & 1
            byte = (byte << 1) | feedback
            reg = ((reg << 1) | feedback) & 0x1FF
        out.append(byte)
    return bytes(out)


def scramble(data):
    """XOR `data` with the keystream. Its own inverse, which is why there is
    no separate unscramble(): one function means the two directions cannot
    drift apart, and a scrambler that does not invert itself exactly turns
    every frame into a rejection with no clue why."""
    ks = keystream(len(data))
    return bytes(bytearray(a ^ b for a, b in zip(bytearray(data), bytearray(ks))))


def capacity_for(frame_payload_bytes):
    """How much real content fits in a frame whose payload space is
    `frame_payload_bytes`.

    `frame_payload_bytes` is the code's payload EXCLUDING whatever trailing
    CRC the physical layer appends for itself -- see fm_modem.py, which is
    what knows that number for a given waveform. Passing the wrong one of
    those two figures is the kind of off-by-two that produces a link which
    works for small payloads and corrupts large ones, so the modem owns the
    distinction and this function takes the already-correct value.
    """
    if frame_payload_bytes <= LEN_PREFIX_BYTES:
        raise FramingError(
            "a frame payload of %d B cannot carry even the %d B length "
            "prefix, let alone any content"
            % (frame_payload_bytes, LEN_PREFIX_BYTES))
    capacity = frame_payload_bytes - LEN_PREFIX_BYTES
    if capacity > MAX_ADDRESSABLE:
        raise FramingError(
            "a frame payload of %d B gives %d B of capacity, which a %d-byte "
            "length prefix cannot describe (max %d). Widen _LEN_PREFIX to "
            '">H" before using a code this large -- and note that costs a '
            "byte on every frame, including the short ones that make up most "
            "of this link's traffic"
            % (frame_payload_bytes, capacity, LEN_PREFIX_BYTES,
               MAX_ADDRESSABLE))
    return capacity


def pack(payload, frame_payload_bytes):
    """One payload -> exactly `frame_payload_bytes` of physical frame payload.

    Layout before scrambling: a 1-byte length, the real content, then zeros out
    to the full frame. Then the WHOLE frame is scrambled, which is what makes
    the zero padding safe -- see this module's docstring. That ordering is the
    measured requirement: an unwhitened frame that is mostly zeros parks the
    modulator on one tone and decodes nothing at all.

    DETERMINISTIC. The same payload always produces the same frame, so a bench
    trial is reproducible and a transmit/receive comparison is a plain equality.
    The earlier draft of this function took an `entropy` callable for random
    padding; the scrambler supersedes it, and a random frame is a measurement
    that cannot be repeated.
    """
    payload = bytes(payload)
    capacity = capacity_for(frame_payload_bytes)
    if len(payload) > capacity:
        raise FramingError(
            "payload %d B exceeds this link's %d B capacity (a %d B frame "
            "payload less the %d B length prefix). Fragment it upstream, or "
            "pick a larger code -- do not raise the frame size to fit, since "
            "burst duration is itself a hard constraint (see fm_config."
            "MAX_BURST_S)"
            % (len(payload), capacity, frame_payload_bytes, LEN_PREFIX_BYTES))

    framed = _LEN_PREFIX.pack(len(payload)) + payload
    framed += bytes(frame_payload_bytes - len(framed))
    return scramble(framed)


def unpack(frame, frame_payload_bytes):
    """A decoded physical frame -> the real content, or None if it is not a
    frame of ours.

    `frame` is what the demodulator handed back, which may be longer than
    `frame_payload_bytes`: the physical layer appends its own CRC, which has
    already been checked by the time a frame is returned at all, and which
    must be stripped before the length prefix is trusted. Not stripping it is
    bug one in this module's docstring.

    THE CRC IS STRIPPED BEFORE UNSCRAMBLING, because codec2 computed it over
    the scrambled bytes it was handed and it is not part of the scrambled
    region. Unscrambling it along with the payload would shift nothing and
    corrupt nothing -- the keystream is positional -- but it would mean the
    length prefix is read from an offset that depends on the trailer, which is
    precisely bug one wearing a different hat.

    Returns None rather than raising for a frame that fails the sanity check.
    A frame reaching here has ALREADY passed the physical layer's own CRC, so
    it is one of two things: genuinely foreign traffic that happened to
    satisfy that CRC, or -- far more likely on this link -- a payload
    corrupted in a way that still did. The caller counts these and surfaces
    them, because on this link they overwhelmingly mean the receive level is
    wrong: clipping makes the decoder settle on wrong-but-CRC-valid codewords.
    A silent `continue` here is what once let a link report "Hearing" while
    delivering nothing for an entire evening.
    """
    body = scramble(bytes(frame)[:frame_payload_bytes])
    if len(body) < LEN_PREFIX_BYTES:
        return None
    (real_len,) = _LEN_PREFIX.unpack_from(body)
    content = body[LEN_PREFIX_BYTES:LEN_PREFIX_BYTES + real_len]
    if len(content) != real_len:
        return None
    return content


def describe_rejection(frame, frame_payload_bytes):
    """Why `unpack` returned None, as a line for a log.

    Separate from unpack() so the hot receive path stays a cheap
    None-or-bytes check and only the rare rejection pays for a message. These
    are inherently rare -- a frame has to pass the physical CRC to get here --
    so a few per five minutes even on a badly mis-levelled link, which is why
    they can be logged per occurrence rather than rate-limited.
    """
    body = scramble(bytes(frame)[:frame_payload_bytes])
    if len(body) < LEN_PREFIX_BYTES:
        return "frame too short (%d B, need at least %d)" % (
            len(body), LEN_PREFIX_BYTES)
    (real_len,) = _LEN_PREFIX.unpack_from(body)
    present = len(body) - LEN_PREFIX_BYTES
    return ("length prefix says %d B, only %d B present (capacity %d) -- "
            "payload corrupt past the framing; check the receive level"
            % (real_len, present, capacity_for(frame_payload_bytes)))
