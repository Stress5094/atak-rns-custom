"""Radio settings the operator can change, and the rules about them.

Testable in isolation, like tx_policy and spectrum: everything here is naming
and validation, with rns_bridge owning the part that talks to the hardware.

WHY MESHTASTIC'S PRESET NAMES. Spreading factor, bandwidth and coding rate are
three numbers that only make sense together, and picking them independently is
how you end up on a combination nobody has ever tested. Meshtastic already
solved the naming problem and their names are the ones people in this space
already reason in -- "Long Fast" means something to anyone who has set up a
mesh radio, where "SF11/250k/4:5" means something only after arithmetic.

Adopting their table exactly, rather than inventing one, also makes the
comparison in the README checkable instead of rhetorical.

The presets are NOT interoperability. Matching the modem settings does not let
this talk to a Meshtastic node -- the framing, addressing and encryption above
the modem are unrelated. It is a shared vocabulary for the radio layer only.
"""

import collections

# name -> (spreading factor, bandwidth Hz, coding rate denominator)
#
# Ordered fastest to slowest, which is also shortest to longest range, so the
# UI list reads as one axis rather than an unordered set.
PRESETS = collections.OrderedDict([
    ("Short Turbo",   (7,  500000, 5)),
    ("Short Fast",    (7,  250000, 5)),
    ("Short Slow",    (8,  250000, 5)),
    ("Medium Fast",   (9,  250000, 5)),
    ("Medium Slow",   (10, 250000, 5)),
    ("Long Fast",     (11, 250000, 5)),
    ("Long Moderate", (11, 125000, 8)),
    ("Long Slow",     (12, 125000, 8)),
])

# Short Slow since 2026-10-15 (was Long Fast, Meshtastic's own default), for
# RNodes and Meshtastic boards alike -- a board is configured from these same
# settings. It is the only preset with real-distance measurements (599 m on the
# 2026-08-21 drive, ~1400 m on 2026-09-27, both at 28 dBm), it carried the most
# traffic in the 2026-08-16 stress bench (~130 msgs/min against Long Fast's
# ~10), and at Long Fast the relay bucket refuses ordinary chat bursts: the
# same six-chat line delivered 6/6 round trips on Short Slow against 2-3/6 on
# Long Fast, and 15 min of real ATAK traffic peaked at 1.0 s of relay in a
# minute against 6.3 s (docs/open-questions.md, the relay bucket section).
# The cost is range: Short Slow (SF8/250 kHz) is several dB less sensitive
# than Long Fast (SF11), which has never been measured at distance here.
#
# Only a node with NO saved settings gets this; a phone that has saved its
# radio settings keeps them.
DEFAULT_PRESET = "Short Slow"

DEFAULT_FREQUENCY_HZ = 915000000
DEFAULT_TXPOWER_DBM = 22

# The SX1262's tuning range. Deliberately not narrowed to one region's ISM
# band: this has to work wherever it is carried, and a hard-coded 902-928 is
# just a US assumption with better manners. Legality is the operator's, and the
# UI says so -- the same position Meshtastic takes.
FREQ_MIN_HZ = 137000000
FREQ_MAX_HZ = 1020000000

# Known ISM allocations, only so the UI can say "this is not a band I
# recognise" rather than silently accepting a typo'd digit.
KNOWN_BANDS = (
    (433050000, 434790000, "433 MHz ISM"),
    (863000000, 870000000, "868 MHz EU"),
    (902000000, 928000000, "915 MHz US"),
)

TXPOWER_MIN_DBM = 0

# 37 because that is RNS's own hard limit: RNodeInterface refuses anything
# higher in __init__, and not with a warning -- the config fails validation and
# the interface never comes up. Matching it exactly means every value this
# accepts is a value that will still work after the next app restart, which a
# higher ceiling would not guarantee.
TXPOWER_MAX_DBM = 37

# What the numbers actually mean on this hardware, which is not the same
# question. The SX1262 in the bench RNodes tops out around 22 dBm and its
# firmware clamps anything higher, reporting the clamped value back. So a
# setting above 22 is not refused -- it is applied as 22 and then flagged by
# the readback check, which is the honest outcome: the radio reports what it
# is really doing rather than what it was asked to do.
#
# Still worth being able to enter, because what matters at the antenna is what
# an external amplifier makes of it, and that is not something this app can
# see.
TYPICAL_BOARD_MAX_DBM = 22


def preset_names():
    return list(PRESETS)


def preset_label(name):
    """One line for the picker: the name, the settings, and the speed.

    The list is ordered fastest to slowest, which is also shortest to longest
    range -- but an ordering nobody can see is not an ordering. These names come
    from Meshtastic for interoperability, and they read as two independent words
    rather than one scale: nothing in "Medium Slow" tells an operator whether it
    beats "Long Fast" (it does, by nearly 2x). So each entry carries the number
    it is actually ordered on, and the order stops being something you have to
    already know.
    """
    params = PRESETS.get(name)
    if params is None:
        return name
    sf, bandwidth, _cr = params
    rate = bitrate_bps(name) or 0.0
    speed = ("%.1f kbps" % (rate / 1000.0)) if rate >= 1000 else ("%d bps" % rate)
    return "%s  ·  SF%d/%dk  ·  %s" % (name, sf, bandwidth // 1000, speed)


def preset_labels():
    """Labels in the same order as preset_names(), so the two index together."""
    return [preset_label(name) for name in PRESETS]


def preset_params(name):
    """(sf, bandwidth_hz, coding_rate) for a preset name, or None."""
    return PRESETS.get(name)


def bitrate_bps(name):
    """On-air bitrate, by RNS's own formula, so it matches what the interface
    will report once the preset is applied."""
    params = PRESETS.get(name)
    if params is None:
        return None
    sf, bw, cr = params
    return sf * ((4.0 / cr) / (2 ** sf / (bw / 1000.0))) * 1000


def band_name(freq_hz):
    """The ISM band this frequency sits in, or None if it is not in one."""
    for low, high, name in KNOWN_BANDS:
        if low <= freq_hz <= high:
            return name
    return None


def validate(preset, freq_hz, txpower_dbm):
    """None if the settings are usable, else a sentence saying what is wrong.

    Returns the operator's problem in the operator's terms. Every one of these
    is a mistake that otherwise surfaces as "the radio does not work", which is
    indistinguishable from a dead board, a wrong passphrase, or being out of
    range -- the failure this whole app is worst at telling apart.
    """
    if preset not in PRESETS:
        return "Unknown preset %r" % (preset,)

    try:
        freq_hz = int(freq_hz)
        txpower_dbm = int(txpower_dbm)
    except (TypeError, ValueError):
        return "Frequency and power must be numbers"

    if not FREQ_MIN_HZ <= freq_hz <= FREQ_MAX_HZ:
        return ("Frequency must be between %.0f and %.0f MHz"
                % (FREQ_MIN_HZ / 1e6, FREQ_MAX_HZ / 1e6))

    if not TXPOWER_MIN_DBM <= txpower_dbm <= TXPOWER_MAX_DBM:
        return ("TX power must be between %d and %d dBm"
                % (TXPOWER_MIN_DBM, TXPOWER_MAX_DBM))

    # The channel has to fit inside the band, not merely start in it -- the
    # same rule the spectrum survey applies when planning channels.
    sf, bandwidth, cr = PRESETS[preset]
    band = band_name(freq_hz)
    if band is not None:
        for low, high, name in KNOWN_BANDS:
            if name != band:
                continue
            if freq_hz - bandwidth // 2 < low or freq_hz + bandwidth // 2 > high:
                return ("At %d kHz wide, %.3f MHz does not fit inside %s"
                        % (bandwidth // 1000, freq_hz / 1e6, band))
    return None


def describe(preset, freq_hz, txpower_dbm):
    """One line for the status screen. Says the preset AND the numbers behind
    it, because two radios that disagree have to be compared at a glance and
    the preset name alone hides a bandwidth difference."""
    params = PRESETS.get(preset)
    if params is None:
        return "%s (unknown preset)" % preset
    sf, bandwidth, cr = params
    return "%s  ·  %.3f MHz  ·  SF%d/%dk/4:%d  ·  %d dBm" % (
        preset, freq_hz / 1e6, sf, bandwidth // 1000, cr, txpower_dbm)
