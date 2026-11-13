"""Assembling one physical transmission, and refusing to build an illegal one.

This is the layer that composes fm_framing (what a frame looks like) with an
fm_modem.Modem (how a frame becomes audio), and it exists as its own module
for one reason: THE DURATION CEILING HAS TO BE ENFORCED SOMEWHERE THAT CANNOT
BE BYPASSED.

Burst duration is punished roughly monotonically on this channel -- ~1.5 s
measured 40-100% delivered, ~7.4 s measured 0/10 in both directions, and a
~34 s burst measured 100% delivery while only 56-69% of individual copies
survived. The previous transport checked the ceiling too, and that check
caught real regressions, but it lived inside a function that also did the
framing, the modem construction, the padding and the int16 conversion. Here
it is the thing the module is for.

WHAT IS AND IS NOT IN SCOPE. This builds one burst. It does not decide
whether to send it, when, or how many times over what span -- that is
scheduling, it is a separate concern, and conflating the two is how in-burst
repetition came to be treated as a reliability mechanism when it is not one
(see below).

IN-BURST REPETITION IS NOT RELIABILITY. Copies inside one burst are perfectly
CORRELATED in channel state: they share one preamble, land within seconds of
each other, and this link's dominant failure mode is a bad patch persisting
for MINUTES to tens of minutes. Three copies three seconds apart are three
samples of the same bad patch. Worse, they lengthen the burst, which is the one
thing the channel consistently punishes. Redundancy against the real failure
mode is time diversity -- re-sending the same frame minutes later, so each
attempt samples an independent channel state -- and it belongs in a scheduler,
not here. See the FM bench notes, sections 3.3 and 3.4.

WHICH IS WHY THE NUMBERS LEFT THIS FILE. `repeats` and the lead-in used to be
module constants here, carrying exactly that argument in their comments, while
the code lived in fm_modem and the symbol rate lived in fm_config -- and burst
duration depends on all four JOINTLY. They are now fields of one validated
fm_config.LinkConfig, with their evidence, and this module takes that object
rather than loose numbers it cannot check against each other.
"""

import fm_config
import fm_framing


class BurstTooLong(ValueError):
    """A burst that exceeds the measured duration ceiling.

    Its own class so the bench sweeps can treat "this configuration is too
    long" as a result to record rather than an error to abort on -- it is a
    normal outcome of exploring a parameter space, and the sweeps exist
    precisely to find where that boundary is.
    """


def plan(config, payload_bytes):
    """What a burst WOULD cost, without building it or needing a payload.

    For choosing a configuration rather than transmitting one: the sweeps and
    the startup path both want to know what a combination costs before
    committing to it, and modulating a burst to find out is both slow and
    requires real bytes to send.

    PURE ARITHMETIC NOW -- no modem, so no libcodec2. That is the change this
    phase made: a LinkConfig already knows the code, the rate, the tone count,
    the repeats and the lead-in, and burst duration is a function of exactly
    those. `fits` is always True for a LinkConfig that exists, because the
    ceiling is checked at construction; it is kept in the dict because a bench
    sweep logs these rows side by side with rows it built by hand.

    Returns a dict, deliberately, rather than a tuple -- these figures get
    logged and compared across configurations, and a positional tuple of five
    floats is how a bench log comes to mean something other than what it says.
    """
    duration = config.burst_duration_s()
    return {
        "capacity_bytes": config.capacity_bytes(),
        "payload_bytes": payload_bytes,
        "repeats": config.repeats,
        "lead_in_s": config.lead_in_s,
        "burst_s": duration,
        "max_burst_s": config.max_burst_s,
        "fits": duration <= config.max_burst_s,
        # The honest throughput figure: real content delivered, over the whole
        # transmission including the lead-in and every redundant copy. This is
        # the number to quote. The raw symbol rate describes a link roughly
        # five times faster than this one.
        "goodput_bps": config.goodput_bps(payload_bytes),
        # What fraction of the transmission is the lead-in. Worth surfacing:
        # at the old 1.6 s lead-in on a ~2 s burst most of the airtime is a
        # tone, and that is a real argument for deterministic keying over VOX
        # rather than a rounding error.
        "lead_in_fraction": config.lead_in_fraction(),
    }


def build(modem, payload, config):
    """One payload -> float audio samples for one physical transmission.

    TWO CEILING CHECKS, AND THEY ARE NOT THE SAME CHECK. LinkConfig refused an
    over-long burst when it was constructed, from arithmetic. This one asks the
    MODEM how long the burst it is about to emit really is, and refuses if
    those disagree past the ceiling. That catches the case the arithmetic
    cannot: a modem whose real frame size differs from the model -- a codec2
    build with a different unique word, a profile whose sample rate was changed
    after the config was built, a replacement waveform whose duration model was
    never written. The check is BEFORE modulation, not after: modulating a 34 s
    burst to then refuse it wastes the time the check exists to protect.
    """
    if modem.frame_payload_bytes != config.frame_payload_bytes():
        raise BurstTooLong(
            "modem carries %d B per frame, the configuration expects %d B -- "
            "the modem was built with a different code than %r, so every "
            "duration and capacity figure on this config is wrong"
            % (modem.frame_payload_bytes, config.frame_payload_bytes(),
               config.codename))

    frame = fm_framing.pack(payload, modem.frame_payload_bytes)

    duration = modem.burst_duration_s(config.repeats, config.lead_in_s)
    if duration > config.max_burst_s:
        raise BurstTooLong(
            "a %d-copy burst with a %.2f s lead-in runs %.2f s by the modem's "
            "own count (the configuration's arithmetic said %.2f s), over the "
            "%.1f s ceiling. Reduce repeats, pick a smaller code, or shorten "
            "the lead-in -- do NOT raise the ceiling without new bench "
            "evidence that a longer burst still arrives. A ~7.4 s burst "
            "measured 0/10 in both directions, and a ~34 s one delivered "
            "100%% while only 56-69%% of its copies survived."
            % (config.repeats, config.lead_in_s, duration,
               config.burst_duration_s(), config.max_burst_s))

    # A VOX lead-in HOPS through the tones rather than holding one: see
    # fm_modem.HOP_SEGMENT_S. CM108 keeps the plain tone -- its 0.10 s is too
    # short for any of this to matter and that path is hardware-verified as is.
    return modem.modulate_burst(
        [frame] * config.repeats, lead_in_s=config.lead_in_s,
        hop_lead_in=(config.keying == fm_config.KEYING_VOX))


def to_pcm16(samples):
    """Float samples (+/-1.0) -> raw PCM16LE bytes, for an audio sink.

    Here rather than in the modem because it is the boundary to the platform's
    audio API, not to the waveform: a different modem produces the same float
    samples and needs the same conversion. Clamped rather than wrapped --
    wrapping turns an overshoot into a loud discontinuity, which on this link
    reads as a decode failure.
    """
    import struct
    # Scale by 32768.0, matching from_pcm16's division, so the two are exact
    # inverses and a sample round-trips within half an LSB. Scaling by 32767
    # here against 32768 there left a ~1 LSB systematic error on full-scale
    # values -- harmless for audio, but the kind of asymmetry a foundations
    # test exists to surface before it reaches a bench.
    clamped = [max(-32768, min(32767, int(round(s * 32768.0))))
               for s in samples]
    return struct.pack("<%dh" % len(clamped), *clamped)


def from_pcm16(pcm_bytes):
    """Raw PCM16LE bytes -> float samples (+/-1.0), for the receive path."""
    import struct
    n = len(pcm_bytes) // 2
    if n == 0:
        return []
    raw = struct.unpack("<%dh" % n, bytes(pcm_bytes)[:n * 2])
    return [s / 32768.0 for s in raw]
