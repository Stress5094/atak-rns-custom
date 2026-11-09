"""The FM link's operating point, as one object with its evidence attached.

WHY THIS MODULE EXISTS AT ALL. The transport this replaces kept its operating
point as eight module-level globals in a 1045-line file, each carrying a
revision log of every value it had ever held and why that value had been
reverted. Those logs were genuinely valuable -- they are most of what
docs/fm-lessons.md is made of -- but as a way to answer "what is this link
configured as, and is that configuration even legal", they failed in three
specific ways:

  1. Constants that MUST move together could move separately. The symbol rate
     and the in-burst repeat count were coupled (a repeat count validated at
     one symbol rate silently re-broke delivery at another), and the only
     thing enforcing that was a paragraph asking the next reader not to.
  2. An illegal combination was discoverable only by running it. Tones
     spaced closer than the symbol rate are not orthogonal and the
     demodulator cannot separate them; that was checked, but only once a
     modem was constructed, on a phone, from an audio callback.
  3. Nothing tied a tone placement to the RADIOS it was measured on. The
     single largest finding in the whole bench record is that placement is a
     property of a specific radio+cable+radio chain: the placement that
     measured 91% delivered on one bench measured 0 of 9 on another. A bare
     pair of numbers cannot express that, so it got expressed as prose, and
     prose does not stop anyone selecting a placement nothing has measured.

So: one Profile object, validated on construction, carrying the hardware it
was measured on and what it measured. An unmeasured profile is not a
convenience, it is a way for an operator to select a dead link from a
hillside -- see docs/fm-lessons.md.

AND ONE LinkConfig ON TOP OF IT, which is the same argument applied to the
constraint that actually decides whether this link delivers: BURST DURATION.
Duration is a joint function of the code, the symbol rate, the tone count, the
repeat count and the lead-in, and those five used to live in three modules --
which is how a 10.32 s burst came to be carrying 29-byte beacons, 4.4x longer
than it needed to be. A LinkConfig holds all five, checks the ceiling against
them together, and computes the duration in pure arithmetic so the check can
happen when a configuration is CHOSEN rather than after a native modem has been
built from it. fm_modem's native answer is then a cross-check rather than the
only source; test_fm_modem asserts the two agree.

NOTHING HERE TOUCHES AUDIO, A MODEM OR A RADIO. It is arithmetic and
validation over plain numbers, so it runs under bare `python3` with nothing
installed, like tx_policy, airtime, relay and fm_channel. That is what lets
the hard constraints be tested on a desktop instead of discovered on a
hillside.

WHAT IS DELIBERATELY NOT DECIDED HERE. The waveform. Profile describes a
multi-tone FSK operating point because that is what has been measured, and the
PROFILES table below holds the only placements this project has evidence for. A
different waveform needs its own profile type, not a new entry in that table --
see fm_modem.py for the boundary that makes that possible.

THE ONE IMPORT IS fm_framing, which has no imports of its own. It is there so
that the link's MTU has exactly one definition rather than two that can drift
apart at the full-payload boundary -- which is where beacons sit, with 29 B of
content in 29 B of capacity and nothing spare.
"""

import fm_framing


# ---------------------------------------------------------------------------
# Hard constraints.
#
# Each of these is a measured property of real hardware or a hard requirement
# of the decoder, NOT a tuning preference. docs/fm-lessons.md section 1 has
# the evidence for each. They are module constants rather than magic numbers
# inside the validator so a test can assert against them by name, and so the
# error messages below can quote the figure they are enforcing.
# ---------------------------------------------------------------------------

# The audio sample rate on the PHONE. 8000 Hz is what an FM handheld's audio
# stage can carry, and it is what the shipped transport ran at.
#
# IT IS NOT WHAT THE BENCH MEASURED AT, and an earlier version of this
# comment said it was. The desktop bench ran 48000 Hz -- the rate both USB
# sound cards lock their capture to (`arecord --dump-hw-params`), chosen so
# that nothing resamples between the card and the demodulator. So a figure
# from docs/open-questions.md was taken at 48000 unless it says otherwise,
# and `docs/fm-lessons.md` section 2's table lists 8000 with a dash for its
# source because nothing fixed it.
#
# THIS MATTERS FOR MORE THAN BOOKKEEPING, because codec2 requires
# `Fs % Rs == 0`: at 8000 Hz the only legal symbol rates at or under
# 1000 Bd are 1000/800/500/400/320/250, so **375 and 480 Bd cannot be
# constructed here at all** -- and those are two of the three rates section
# 1.2 records as having failed. They divide 48000 cleanly. Comparing a new
# measurement against a recorded one means running at the rate the recorded
# one was taken at; tools/fm_bench.py defaults --fs to 48000 for that reason.
SAMPLE_RATE_HZ = 8000

# Symbol rate ceiling for a chain NOTHING HAS MEASURED, and it is an ANALOG
# limit, not a software one. Above 250 Bd delivery failed even on a clean,
# non-saturated signal: 375, 480 and 500 Bd all measured 0-1/4 delivered with
# healthy received peaks of 0.51-0.55, so no gain adjustment rescued it. The
# cause was identified from PCB photographs -- an LM358-based analog envelope
# follower that cannot track faster tone transitions.
#
# IT BELONGS TO ONE CABLE, ON THE RECEIVE END, AND NOT TO FM. That was
# established on 2026-10-02: with a Digirig Lite doing the RECEIVING, 400 Bd
# ran 178 of 180 copies on the same radios. The 250 Bd figure is the K1 PRO's
# as a receiver, which is consistent with the 2026-09-13 conclusion that the
# receiving cable is what decides whether the link works at all. See
# K1PRO_RX_CEILING_BD and DIGIRIG_RX_CEILING_BD below for the per-cable
# figures, and Chain.symbol_rate_ceiling_bd for how a profile picks one up.
#
# So this stays at the SLOWEST measured cable's figure deliberately: it is
# what an unqualified chain inherits, and inheriting the optimistic number
# would mean a new cable is assumed fast until proven slow. What it must never
# be is raised silently.
MAX_SYMBOL_RATE_BD = 250

# The two receive-end cables with a measured ceiling.
#
# K1 PRO: 250 Bd, the LM358 envelope follower above.
#
# Digirig Lite: 500 Bd. 400 and 500 both measured 98.9% per copy over 90
# copies each on 2026-10-02, fully interleaved across 63 minutes. 600 Bd was
# measured in the same rounds and is NOT included: 95.6% per copy, and it was
# the only rate to lose whole MESSAGES (86 of 90, against 90 of 90 at 400) at
# full signal on a desk. Not statistically significant on its own (p=0.125) --
# which is the reason it is excluded rather than refused. It is constructible;
# it is simply not evidenced, and 400 Bd lost nothing in the same rounds 30 s
# apart.
K1PRO_RX_CEILING_BD = 250
DIGIRIG_RX_CEILING_BD = 500

# The usable audio band, measured end to end on 2026-10-02 by sweeping the
# first tone and counting decodes rather than by looking at levels.
#
# THIS IS THE CONSTRAINT THAT CAPS THE SYMBOL RATE, now that the 250 Bd cable
# limit is known to be one cable's. Orthogonal M-FSK needs (M-1) spacings and
# spacing must be >= the symbol rate, so a 2000 Hz band gives 4FSK at most
# 2000/3 = 666 Bd -- see band_symbol_rate_ceiling_bd(). 1200 Bd would need
# 3600 Hz and cannot be had here at any power.
#
# THE LOW EDGE IS REAL AND SHARP: a 400 Hz first tone delivered 0% even at
# 500 Bd, where the whole alphabet would have fitted comfortably inside the
# band by width alone. A band is two edges, not a width.
MEASURED_USABLE_BAND_HZ = (1000, 3000)

# Minimum samples per symbol the demodulator needs to work with. codec2
# asserts this internally (P >= 8) and aborts the whole process when it
# fails -- a core dump where an error message belongs -- so it is checked
# here instead.
MIN_SAMPLES_PER_SYMBOL = 8

# No physical burst may run longer than this. Burst duration is punished
# roughly monotonically on this channel: ~1.5 s measured 40-100% delivered,
# ~7.4 s measured 0/10 in both directions, and a ~34 s burst measured 100%
# delivery while only 56-69% of individual copies survived -- redundancy
# masking a channel chewing through half of everything.
#
# 11.5 s is the figure the previous transport shipped, where an 11.0 s burst
# measured 30/30 clean with every in-burst copy decoding, on the two-Digirig
# desktop bench. It is a CEILING, not a target: raise it only alongside new
# bench evidence that a longer burst still arrives, never to make a payload
# fit. See docs/fm-lessons.md section 1.3.
MAX_BURST_S = 11.5

class ProfileError(ValueError):
    """An operating point that is not merely suboptimal but illegal.

    Its own class rather than a bare ValueError because the call sites that
    care -- the bridge's startup path, and the bench sweeps -- want to tell
    "this configuration cannot work" apart from any other bad argument, and
    report it as a configuration fault rather than a crash.
    """


# ---------------------------------------------------------------------------
# The LDPC codes, and the arithmetic that turns one into a burst duration.
#
# WHY THE CODE TABLE LIVES HERE AND NOT IN fm_modem. Burst duration is the
# binding constraint on this link, and it depends on the code, the symbol rate,
# the tone count, the repeat count and the lead-in JOINTLY. The previous
# transport had those five numbers in three different modules, which is how a
# repeat count validated at one symbol rate came to be shipped at another. A
# ceiling that can only be checked by constructing a native modem is a ceiling
# that is not checked when a configuration is CHOSEN -- so the arithmetic has
# to be here, in the module that runs under bare python3, and the modem's
# native answer becomes a cross-check rather than the only source.
#
# (test_fm_modem asserts these formulas against the real libcodec2 for every
# code at both tone counts and several rates. If a codec2 build ever changes
# one of them, that test is what says so.)
# ---------------------------------------------------------------------------

# code name -> (data bits, codeword bits). Both are in the name, which is why
# this shape rather than (bytes, rate): a float rate makes the codeword length
# inexact, and the codeword length is what the duration is computed from.
#
# THERE IS NOTHING BETWEEN 32 AND 128 BYTES, which makes "pick the smallest
# code that fits" a far coarser choice than it sounds. H_4096_8192_3d is
# present and measured BADLY: 100% delivery but only 56-69% per copy, at a
# 34 s burst. See docs/fm-lessons.md section 3.2.
CODES = {
    "H_128_256_5":    (128, 256),
    "H_256_512_4":    (256, 512),
    "H_256_768_22":   (256, 768),
    "H_1024_2048_4f": (1024, 2048),
    "H_4096_8192_3d": (4096, 8192),
}

# CHANGED 2026-10-02, back to the 128 B code, and the reason is a measurement
# the rebuild itself produced rather than a reversal of Phase 0.
#
# Phase 0 picked the 32 B H_256_768_22: the smallest code that still holds a
# whole beacon (17 B plaintext + 12 B envelope = 29 B, carried with zero bytes
# to spare), on the sound reasoning that the previous transport's 128 B code was
# the single largest cause of its 10.32 s burst.
#
# WHAT PHASE 4 FOUND is what 29 B costs once the CoT codec is actually wired to
# it. The codec sees 17 B, not 29, because the mesh envelope takes 12. And:
#
#     a position DIFFERENCE          16 B   fits
#     a position                     26 B   floor, with identity removed
#     a position stating identity    55 B   a real 24-char ATAK uid + callsign
#
# So a 17 B budget can carry the difference between two positions but not a
# position, and you cannot send differences until you have sent one position to
# subtract from. The codec rightly refuses to fragment a frame carrying session
# state, so it fell back to zlib'd XML: 12 bursts, 28.2 s, for ONE position --
# and because the session aborted, the cheap difference path never engaged at
# all. Self-position reports simply did not work on this link.
#
# WHAT SEPARATING THE PHASE 0 LEVERS SHOWS is that the code was the cheapest of
# them and the only one that cost capacity. From the 10.32 s baseline:
#
#     400 Bd, CM108 keying, 1 copy, 128 B code     2.95 s   (3.5x, payload intact)
#     ...then the 32 B code                        2.35 s   (0.6 s more, -96 B)
#
# 0.6 s of burst against 96 B of capacity, and those 96 B are the whole position
# path. The delivery figures that condemned this code -- 0/10 at 7.4 s, ~50% at
# 10.32 s -- were all measured at the OLD burst lengths; at 2.95 s it has never
# been on the air. NEITHER has 1 copy. Both are untested and chosen knowingly;
# `evidence_line()` says so.
DEFAULT_CODE = "H_1024_2048_4f"

# codec2's raw-data convention: the last two payload bytes of every frame are
# a CRC over the rest, which the receiver checks and the sender must fill. So
# usable payload is two less than the code's frame size.
CRC_BYTES = 2

# Bits codec2 prepends to each frame as a unique word, so a receiver can find
# the frame boundary inside a synced burst. Measured as exactly 32 bits for
# every code at both M=2 and M=4 (at M=4 that is 16 symbols, at M=2 it is 32).
UNIQUE_WORD_BITS = 32

# Preamble length, in SYMBOLS, is 50 * (M // 2): measured 50 symbols at M=2
# and 100 at M=4, invariant across all five codes and across 8000/48000 Hz and
# 250/400 Bd. Since codec2 supports M = 2 and 4 only, those two measurements
# enumerate the whole domain of this formula rather than sampling it.
#
# There is no postamble: freedv_get_n_tx_postamble_modem_samples() returns 0
# for FSK_LDPC at every combination measured. The modulator still asks for one
# and appends what it gets, which costs nothing and means a build that grows
# one is carried rather than truncated.
PREAMBLE_SYMBOLS_PER_PAIR = 50


def code_bytes(codename):
    """Frame size in bytes, INCLUDING the CRC codec2 appends for itself."""
    return _code(codename)[0] // 8


def code_rate(codename):
    """FEC rate as a float, for reporting. Never for arithmetic -- the
    codeword length is an integer and is what duration is computed from."""
    data, codeword = _code(codename)
    return data / float(codeword)


def _code(codename):
    if codename not in CODES:
        raise ProfileError("unknown code %r; have %s"
                           % (codename, ", ".join(sorted(CODES))))
    return CODES[codename]


def preamble_symbols(tones):
    """Symbols codec2 spends on the preamble before the first frame."""
    return PREAMBLE_SYMBOLS_PER_PAIR * (tones // 2)


def frame_symbols(codename, tones):
    """Symbols one frame of `codename` occupies: codeword plus unique word."""
    _data, codeword = _code(codename)
    # log2(tones), as an exact integer, for a tones that _validate has already
    # established is a power of two: 2 -> 1, 4 -> 2. Spelled this way so the
    # free function needs no Profile, since the sweeps call it for
    # configurations no Profile would accept.
    bits_per_symbol = (tones - 1).bit_length()
    return (codeword + UNIQUE_WORD_BITS) // bits_per_symbol


def burst_symbols(codename, tones, repeats):
    return preamble_symbols(tones) + repeats * frame_symbols(codename, tones)


def burst_duration_s(codename, tones, symbol_rate_bd, repeats, lead_in_s):
    """How long one physical transmission runs, lead-in included.

    THE NUMBER THE WHOLE OPERATING POINT EXISTS TO GET RIGHT. Measured delivery
    against duration: ~1.5 s -> 40-100%, ~2.5 s -> ~50%, ~4.3 s -> per-attempt
    failures appear, ~7.4 s -> 0 of 10 in BOTH directions.

    A FREE FUNCTION as well as a LinkConfig method, because a bench sweep needs
    to price configurations that are ILLEGAL -- a rate above the ceiling, a code
    too large to frame -- and that is most of what a sweep is for. LinkConfig is
    the validated carrier of one chosen point; this is the arithmetic.
    """
    return (lead_in_s
            + burst_symbols(codename, tones, repeats) / float(symbol_rate_bd))


def band_symbol_rate_ceiling_bd(tones, band_hz=MEASURED_USABLE_BAND_HZ):
    """The fastest symbol rate an M-FSK alphabet can run inside `band_hz`.

    Orthogonality needs (M-1) spacings of at least the symbol rate, so this is
    simply the band width over (M-1). It is the ceiling that survives now that
    the 250 Bd one is known to belong to a single cable: 666 Bd for 4FSK in
    the measured 1000-3000 Hz band, and 1200 Bd -- which would need 3600 Hz --
    is not reachable here at any power or gain setting.

    MORE TONES IS SLOWER, which this function makes arithmetic rather than
    argument: the raw rate is log2(M) * B / (M-1), so M=4 gets 0.67 B against
    M=8's 0.43 B. 8FSK in this band would be about a third slower than 4FSK,
    and codec2 refuses it anyway.
    """
    low, high = band_hz
    return (high - low) // (tones - 1)


# -- keying -----------------------------------------------------------------

# How the radio is told to transmit, and the lead-in each one needs. These are
# one constant, not two, because the lead-in is a PROPERTY of the keying
# method and the previous transport's single DEFAULT_LEAD_IN_S was a VOX
# figure applied to a CM108-keyed link.
KEYING_CM108 = "cm108"
KEYING_VOX = "vox"

# CM108 GPIO PTT. Measured 0.055 s key-up, so there is no threshold to trip
# and the lead-in only has to give the receiver's AGC something to settle on
# before the preamble it must sync against. 0.10 s, measured on the desktop
# bench; it is part of the 2.35 s operating point.
#
# VOX. The transmitter is OFF until VOX fires, so whatever is playing at that
# moment is simply not transmitted -- and because the preamble is sized from
# the symbol rate, a faster configuration loses proportionally more of it,
# which presents as "some configurations never sync" rather than a clean
# across-the-board failure. Attack measured 85-90 ms on the desktop radios
# through the desktop cable; 0.30 s leaves ~200 ms of margin.
#
# NEITHER IS THE 1.6 s FIGURE from the handheld chain, and merging them is a
# mistake this project has already made. That one is not VOX attack at all: a
# plain steady tone showed the RECEIVED level collapsing 10-13x about 0.9 s
# into every transmission and then holding flat, in both directions, through
# a VOX cable AND a deterministic CM108 one. It is most likely a leveling
# circuit in the cable, and the lead-in was buying a transmission long enough
# that data starts AFTER the collapse stabilises. It is a per-chain figure --
# see Chain.lead_in_s, which is how a chain overrides these.
KEYING_LEAD_IN_S = {
    KEYING_CM108: 0.10,
    KEYING_VOX: 0.30,
}

# THE LEAD-IN FOR A K1 PRO-CLASS CABLE ON A HANDHELD (Baofeng UV-5R Mini), VOX
# keyed. NOT the 0.30 s above, which was sized from a DESKTOP radio's 85-90 ms
# attack. Measured 2026-10-06, laptop <- OnePlus 8T, both ends K1 PRO + Mini:
# the radio began transmitting 0.34-0.72 s after the audio started (6 bursts;
# +/-0.1 s from the phone/laptop clock offset) and stayed keyed ~0.4 s after it
# ended. With the CM108 figure (0.10 s, via the keyless fallback) the late start
# ate the lead-in AND the lock-on pattern: 0 of 3 bursts decoded.
#
# 2026-10-08, laptop -> Pixel 7 (phone's raw capture, fm_rx_dump):
#   steady 2.2 s tone    0 of 3 decoded   level collapsed ~0.75 s in, back ~0.25 s
#                                         late on the first data
#   hopping 1.0 s        1 of 6 decoded   (live app: 1 of 4)
#   hopping 2.2 s        6 of 6 decoded with 250 us de-emphasis, 3 of 6 without;
#                                         LIVE APP, no filter: 5 of 8 beacons
# So 2.2 s, hopping (see fm_modem.HOP_SEGMENT_S) and filtered (below). Small
# samples, ONE radio pair, one cable -- an operating point, not a
# characterisation. The cost is ~2.1 s of airtime per burst (3.15 s -> ~5.3 s).
VOX_CABLE_LEAD_IN_S = 2.2

# The K1 PRO receive path needs de-emphasis (see fm-lessons / the 2026-10-02
# desk bench: 3 of 48 copies without, 34 of 40 with ~250 us). The Pixel's raw
# capture agreed: 6 of 6 bursts decoded at 250 us against 3 of 6 at none.
VOX_CABLE_DEEMPHASIS_US = 250.0

# How many copies of the same frame share one preamble.
#
# 2, not 3 and not 1. The history matters because it is easy to read the bench
# record as pointing either way:
#
#   * At 250 Bd, 1 copy measured 17/18 against 2 copies' 8/8 -- the extra copy
#     bought nothing but airtime once the symbol rate was slow enough for the
#     receiving cable's analog leveling circuit to track.
#   * At 500 Bd the numbers point the other way: 1 and 2 copies both measured
#     WORSE than 3 (0/4 and 1/4 against 3/7). REPEATS AND THE SYMBOL RATE ARE
#     COUPLED, which is most of why they now live in one validated object.
#   * On the 128 B code at 250 Bd, 2 copies measured 30/30 clean with BOTH
#     copies decoding every trial, where 1 copy's shorter burst measured 80%
#     on the weak leg -- below that session's bar, with no redundancy to mask
#     it.
#
# IN-BURST REPETITION IS NOT RELIABILITY, and that is why this number belongs
# at the bottom of its useful range rather than tuned upward to chase a
# delivery figure. Copies inside one burst are perfectly CORRELATED in channel
# state: they share one preamble, land within seconds, and this link's
# dominant failure is a bad patch persisting for MINUTES. Redundancy against
# THAT is time diversity -- the same frame re-sent minutes later -- and it
# belongs in a scheduler. 1 has no fallback at all if noise lands on its
# single copy; 3 costs real duration for redundancy that cannot outlast a
# multi-minute bad patch anyway. 2 is the floor that still has a fallback.
#
# AND IT WAS THE BIGGEST UNTESTED LEVER LEFT, which 2026-10-02 SPENT. The
# operating point now runs 1 copy, because the 128 B code the position path
# needs costs 5.55 s at 2 copies and 2.95 s at 1 -- and 5.55 s sits in the range
# where the old bench first saw per-attempt failures, while 2.95 s is within
# 0.6 s of the 2.35 s configuration that measured 178/180 copies.
#
# SO THE TWO UNTESTED LEVERS ARE NOW COUPLED, and that is worth stating plainly
# rather than leaving to be discovered: a bench run that finds the operating
# point weak cannot tell whether it was the longer burst or the single copy
# without separating them. The A/B that matters is three cells, not two --
# 32 B/2 copies (2.35 s), 128 B/1 copy (2.95 s), 128 B/2 copies (5.55 s).
#
# This constant stays the DEFAULT for a LinkConfig that does not say, and
# OPERATING_POINT states its own explicitly, so the two cannot drift silently.
DEFAULT_REPEATS = 2


# A receive level outside this band is the single most common fixable fault
# on this link, and the one an operator can actually act on in the field --
# on an FM audio link the volume knob is the gain stage. Below SILENT the
# path is provably dead; above SATURATED the error-correcting decoder starts
# settling on wrong-but-CRC-valid codewords, so an over-levelled link reports
# frames arriving and delivers garbage.
# THE BAND IS NOT A PREDICTOR OF DELIVERY, and 2026-10-02 is why that is
# stated here. On the desktop bench one direction ran 100% per copy at a
# received peak of 0.18 AND at 0.55 -- both outside this band -- while the
# other direction swung 0-100% at fixed settings without the level moving. So
# these are the bounds of a SAFE operating region, which is a real and useful
# thing to aim a volume knob at, and they are NOT a diagnosis: a link inside
# the band can be failing and a link outside it can be perfect. Phase 3's
# observability must say "aim here", never "this is why it is not working".
SILENT_PEAK = 0.02
SATURATED_PEAK = 0.95
TARGET_PEAK_LOW = 0.25
TARGET_PEAK_HIGH = 0.45


class Chain(object):
    """The radio + cable + radio path a placement was measured on.

    A placement without one of these is a guess. This exists because the
    bench record's clearest finding is also its most counterintuitive: tones
    at 1500/2000/2500/3000 Hz measured 91% delivered on the desktop bench and
    0 of 9 phone-to-phone, on the same software, days apart. Attaching the
    hardware to the measurement is the only way that fact survives contact
    with a future reader who has only the numbers.
    """

    def __init__(self, name, radios, cables, measured_on, delivered,
                 tone_spread_db=None, notes="",
                 symbol_rate_ceiling_bd=MAX_SYMBOL_RATE_BD,
                 usable_band_hz=None, deemphasis_us=0.0, lead_in_s=None,
                 sample_rate_hz=None):
        self.name = name
        self.radios = radios              # e.g. "OnePlus 8T <-> Pixel 7, UV-5R pair"
        self.cables = cables              # e.g. "K1 PRO both ends (VOX)"
        self.measured_on = measured_on    # ISO date the evidence was taken
        self.delivered = delivered        # what it measured, in words
        # How evenly the tones arrived, which is the leading indicator for a
        # placement -- within ~5 dB is the bar. 4.7 dB delivered 7 of 9
        # beacons; 13-21 dB delivered nothing at all.
        self.tone_spread_db = tone_spread_db
        self.notes = notes

        # The symbol rate THIS chain's receiving cable has been measured to
        # track. Defaults to the slowest cable known, because an unqualified
        # chain must not inherit the optimistic figure -- see
        # MAX_SYMBOL_RATE_BD.
        self.symbol_rate_ceiling_bd = symbol_rate_ceiling_bd

        # (low, high) Hz this chain actually passes, if it has been swept.
        # None means unswept, and the band check is then skipped rather than
        # guessed: refusing against MEASURED_USABLE_BAND_HZ would be applying
        # one chain's band to another, which is the exact mistake the Chain
        # object exists to prevent.
        self.usable_band_hz = usable_band_hz

        # Receive-side de-emphasis time constant, microseconds. 0 = none.
        #
        # THIS IS A PER-CHAIN NECESSITY, NOT A TUNING KNOB, and it is here
        # because 2026-10-02 measured what happens without it: the K1 PRO
        # receiving decoded 3 of 48 copies unfiltered and 34 of 40 with ~250
        # us, and codec2's FSK estimator would not reach even trial sync
        # against the untreated tilt at any normalisation peak from 0.1 to
        # 0.99. The value is insensitive over 50-900 us, so this is a
        # present/absent decision far more than a numeric one.
        #
        # IT IS THE CABLE, NOT THE RADIO PAIRING. The record originally
        # attributed the tilt to which radio transmitted; swapping in a second
        # Digirig with both radios fixed took the same direction to 20/20
        # unfiltered. A Digirig receiving needs none.
        self.deemphasis_us = deemphasis_us

        # Lead-in this chain needs, overriding the keying method's figure.
        # None means "use the keying default". The handheld chain's 1.6 s is
        # the case this exists for: it is not VOX attack time, it is buying a
        # transmission long enough that data starts after a received-level
        # collapse stabilises, and it applies under deterministic PTT too.
        self.lead_in_s = lead_in_s

        # The sample rate the MEASUREMENT ran at, which is not necessarily the
        # rate a profile using this chain's evidence will run at: the desktop
        # bench runs 48000 Hz and the phone runs 8000. Recorded because
        # confusing the two is one of the three errors Phase 0 found in the
        # record -- and it is not only bookkeeping, since codec2 requires
        # Fs % Rs == 0, so two of the three symbol rates section 1.2 reports as
        # FAILING cannot even be constructed at 8000 Hz. evidence_line() says
        # so when the two rates differ.
        self.sample_rate_hz = sample_rate_hz

    def __repr__(self):
        return "<Chain %s: %s, %s (%s)>" % (
            self.name, self.radios, self.cables, self.measured_on)


class Profile(object):
    """One complete, legal, evidenced FM operating point.

    Validated on construction, so an illegal combination cannot reach a
    modem, an audio thread or a radio. Every check names the measurement it
    is enforcing, because an error that says only "invalid tone spacing"
    sends the next person back to the bench record to find out why.
    """

    def __init__(self, name, first_tone_hz, tone_spacing_hz, tones,
                 symbol_rate_bd, chain,
                 sample_rate_hz=SAMPLE_RATE_HZ,
                 symbol_rate_ceiling_bd=None):
        self.name = name
        self.first_tone_hz = first_tone_hz
        self.tone_spacing_hz = tone_spacing_hz
        self.tones = tones
        self.symbol_rate_bd = symbol_rate_bd
        self.sample_rate_hz = sample_rate_hz
        # Defaults from the CHAIN, because the ceiling is a property of the
        # receiving cable and the chain is what names the cable. The explicit
        # argument stays for one caller: tools/fm_bench.py, whose whole job is
        # to run above a ceiling and find out whether it holds.
        if symbol_rate_ceiling_bd is None:
            symbol_rate_ceiling_bd = (MAX_SYMBOL_RATE_BD if chain is None
                                      else chain.symbol_rate_ceiling_bd)
        self.symbol_rate_ceiling_bd = symbol_rate_ceiling_bd
        self.chain = chain
        self._validate()

    # -- derived ------------------------------------------------------------

    def tone_list(self):
        """Every tone frequency in Hz, for a log line, a status screen or a
        test. Spelled out rather than left as (first, spacing) because the
        name is what two operators compare out loud and the frequencies are
        what tell them whether a radio they have not qualified will pass
        it."""
        return [self.first_tone_hz + i * self.tone_spacing_hz
                for i in range(self.tones)]

    def top_tone_hz(self):
        return self.first_tone_hz + (self.tones - 1) * self.tone_spacing_hz

    def bits_per_symbol(self):
        """log2(tones), as an exact integer -- _validate has already
        established that `tones` is a power of two, so this cannot be
        fractional."""
        bits = 0
        n = self.tones
        while n > 1:
            n >>= 1
            bits += 1
        return bits

    def raw_bps(self):
        """On-air bit rate BEFORE error correction takes its share. Not a
        throughput figure: quoting this as the link's speed is how a 90 bps
        link gets described as 500 bps."""
        return self.symbol_rate_bd * self.bits_per_symbol()

    def samples_per_symbol(self):
        return self.sample_rate_hz // self.symbol_rate_bd

    def occupied_bandwidth_hz(self):
        """How much usable audio band this placement actually needs.

        Worth having as a number because it is the figure that decides
        whether a newly-qualified radio can run this profile at all: four
        tones at 250 Hz spacing need 750 Hz between the first and last tone,
        and a radio whose usable band is narrower than that cannot be
        accommodated by MOVING the tones -- it needs fewer tones or a slower
        symbol rate.
        """
        return self.top_tone_hz() - self.first_tone_hz

    # -- validation ---------------------------------------------------------

    def _validate(self):
        if self.tones < 2 or (self.tones & (self.tones - 1)) != 0:
            raise ProfileError(
                "%s: %d tones -- an M-FSK alphabet must be a power of two "
                "(2, 4, 8, ...) so a whole number of bits maps onto a symbol"
                % (self.name, self.tones))

        # And a power of two is NOT sufficient: codec2 supports 2 and 4 only.
        # `fsk_create_core` asserts `M == 2 || M == 4` and ABORTS THE PROCESS
        # on anything else -- measured 2026-10-02 by constructing an 8-tone
        # profile, which this validator happily accepted and which then core
        # dumped the interpreter. That is the exact failure this module exists
        # to turn into a message, and the power-of-two check above looked like
        # it already covered it.
        #
        # AND LOSING 8FSK COSTS NOTHING, which is worth stating because the
        # opposite is the intuitive reading. More tones carry more bits per
        # symbol, but orthogonal M-FSK needs (M-1) spacings and spacing must
        # be >= the symbol rate, so bandwidth grows FASTER than the bits do.
        # In a fixed span B the raw rate is log2(M) * B / (M-1):
        #
        #     M=2  -> 1 * B/1  = 1.00 B      M=8  -> 3 * B/7 = 0.43 B
        #     M=4  -> 2 * B/3  = 0.67 B      M=16 -> 4 * B/15 = 0.27 B
        #
        # So in a bandwidth-limited channel -- and this one is: the usable
        # span measured 1000-3000 Hz on 2026-10-02 -- FEWER tones is faster,
        # and 8FSK would be a throughput LOSS of about a third against 4FSK.
        # 4 is kept over 2 because non-coherent 4FSK is the more
        # energy-efficient of the two per bit, which is what matters on a
        # weak link once bandwidth is spent.
        if self.tones not in (2, 4):
            raise ProfileError(
                "%s: %d tones -- codec2's FSK modem supports 2 or 4 ONLY "
                "(fsk.c asserts `M == 2 || M == 4` and aborts the process, so "
                "this cannot be left to fail at runtime). Note this is not a "
                "limitation worth routing around: orthogonal M-FSK needs "
                "(M-1) spacings, so in a fixed bandwidth log2(M)/(M-1) falls "
                "as M rises and 8FSK is about a third SLOWER than 4FSK, not "
                "faster."
                % (self.name, self.tones))

        # The constraint that actually binds, and the reason this link has so
        # little room to be clever: tones closer together than the symbol
        # rate are not orthogonal, so the demodulator cannot separate them at
        # all. Passband width divided by tone count caps the spacing, spacing
        # caps the symbol rate, and the symbol rate sets the bitrate.
        if self.tone_spacing_hz < self.symbol_rate_bd:
            raise ProfileError(
                "%s: tone spacing %d Hz is below the symbol rate %d Bd -- the "
                "tones are not orthogonal and the demodulator cannot separate "
                "them. Either widen the spacing (needs more usable passband, "
                "which is what tools/passband_sweep.py measures) or lower the "
                "symbol rate."
                % (self.name, self.tone_spacing_hz, self.symbol_rate_bd))

        if self.symbol_rate_bd > self.symbol_rate_ceiling_bd:
            raise ProfileError(
                "%s: symbol rate %d Bd is above this chain's measured ceiling "
                "of %d Bd. This is an ANALOG limit in the receiving cable (an "
                "LM358-based envelope follower that cannot track faster tone "
                "transitions), not a software setting: 375, 480 and 500 Bd all "
                "measured 0-1/4 delivered with a healthy signal level, so no "
                "gain adjustment rescues it. Raise symbol_rate_ceiling_bd only "
                "with a measurement on THIS chain -- see docs/fm-lessons.md "
                "section 1.2."
                % (self.name, self.symbol_rate_bd, self.symbol_rate_ceiling_bd))

        # Both of these are asserted internally by codec2, which aborts the
        # process rather than returning an error. Checked here so a bad sweep
        # parameter is an exception instead of a dead interpreter.
        if self.sample_rate_hz % self.symbol_rate_bd != 0:
            usable = [self.sample_rate_hz // k for k in (4, 5, 8, 10, 16, 20, 32)
                      if self.sample_rate_hz % (self.sample_rate_hz // k) == 0
                      and self.sample_rate_hz // k <= self.symbol_rate_ceiling_bd]
            raise ProfileError(
                "%s: symbol rate %d Bd must divide the sample rate %d Hz "
                "exactly (codec2 asserts this and aborts the process). Legal "
                "rates at or below this chain's ceiling: %s"
                % (self.name, self.symbol_rate_bd, self.sample_rate_hz,
                   ", ".join(str(u) for u in sorted(set(usable), reverse=True))
                   or "none"))

        if self.samples_per_symbol() < MIN_SAMPLES_PER_SYMBOL:
            raise ProfileError(
                "%s: %d Hz / %d Bd gives only %d samples per symbol; the "
                "demodulator needs at least %d, so the symbol rate must be "
                "%d Bd or less at this sample rate"
                % (self.name, self.sample_rate_hz, self.symbol_rate_bd,
                   self.samples_per_symbol(), MIN_SAMPLES_PER_SYMBOL,
                   self.sample_rate_hz // MIN_SAMPLES_PER_SYMBOL))

        if self.first_tone_hz <= 0:
            raise ProfileError("%s: first tone must be above 0 Hz, got %d"
                               % (self.name, self.first_tone_hz))

        # Nyquist, stated separately from the passband check below because it
        # is a property of the SAMPLING and would corrupt the signal even on a
        # radio with a perfectly flat response out to any frequency.
        if self.top_tone_hz() >= self.sample_rate_hz / 2.0:
            raise ProfileError(
                "%s: top tone %d Hz is at or above Nyquist (%d Hz) for a "
                "%d Hz sample rate -- it would alias rather than transmit"
                % (self.name, self.top_tone_hz(), self.sample_rate_hz // 2,
                   self.sample_rate_hz))

        # The passband, as opposed to Nyquist: tones outside what the radios
        # and cables actually PASS arrive too weak to detect, which is not a
        # degradation but a total failure. Measured 2026-10-02 by sweeping the
        # first tone and counting decodes: a 400 Hz first tone delivered 0%
        # even at 500 Bd, where the alphabet fitted inside the band by width.
        #
        # ONLY CHECKED WHEN THE CHAIN HAS BEEN SWEPT. Applying one chain's
        # measured band to another is the mistake this whole module is built
        # against -- a placement that measured 91% on one bench measured 0 of 9
        # on a second. An unswept chain gets no check rather than a borrowed
        # one.
        if self.chain is not None and self.chain.usable_band_hz:
            low, high = self.chain.usable_band_hz
            if self.first_tone_hz < low or self.top_tone_hz() > high:
                raise ProfileError(
                    "%s: tones %s Hz fall outside the %d-%d Hz band measured "
                    "on %s. A tone outside the passband is not attenuated, it "
                    "is absent -- a 400 Hz first tone measured 0%% delivered "
                    "at a rate that worked at 1000 Hz. This alphabet needs "
                    "%d Hz and the band is %d Hz wide, so at %d tones the "
                    "fastest rate that fits is %d Bd."
                    % (self.name,
                       "/".join(str(t) for t in self.tone_list()),
                       low, high, self.chain.name,
                       self.occupied_bandwidth_hz(), high - low, self.tones,
                       band_symbol_rate_ceiling_bd(self.tones,
                                                   self.chain.usable_band_hz)))

        if self.chain is None:
            raise ProfileError(
                "%s: no Chain attached. A tone placement is a property of a "
                "specific radio+cable+radio path, not of FM in general -- the "
                "same placement measured 91%% delivered on one bench and 0 of "
                "9 on another. Measure it with tools/passband_sweep.py and "
                "attach what you measured, or use an existing profile."
                % self.name)

    # -- reporting ----------------------------------------------------------

    def summary_line(self):
        """One line for a log or a status screen.

        Spells out the frequencies AND names the profile, because the two
        answer different questions: the name is what two operators compare,
        and a mismatch between them is total silence with nothing in either
        log to say so -- see docs/fm-lessons.md section 1.1. This line being
        compared by eye was the only mismatch check that ever existed.
        """
        return "FM tones: %s - %s Hz @ %d Bd (both ends must match exactly)" % (
            self.name,
            "/".join(str(t) for t in self.tone_list()),
            self.symbol_rate_bd)

    def evidence_line(self):
        """What this placement actually measured, and on what. For the bench
        log and for anyone about to trust it on different hardware."""
        spread = ("" if self.chain.tone_spread_db is None
                  else ", tones within %.1f dB" % self.chain.tone_spread_db)
        # Say so when the evidence was taken at a different sample rate than
        # this profile runs at. Not pedantry: a symbol rate legal at one rate
        # can be unconstructible at the other, and a figure quoted from the
        # 48000 Hz bench against an 8000 Hz link has been misread before.
        rate = ""
        if (self.chain.sample_rate_hz
                and self.chain.sample_rate_hz != self.sample_rate_hz):
            rate = (" -- NOTE measured at %d Hz, this profile runs at %d"
                    % (self.chain.sample_rate_hz, self.sample_rate_hz))
        return "%s: %s measured %s on %s (%s, %s)%s%s" % (
            self.name, self.chain.name, self.chain.delivered,
            self.chain.measured_on, self.chain.radios, self.chain.cables,
            spread, rate)

    def __repr__(self):
        return "<Profile %s %dFSK %s Hz @ %d Bd>" % (
            self.name, self.tones,
            "/".join(str(t) for t in self.tone_list()), self.symbol_rate_bd)


class LinkConfigError(ProfileError):
    """A complete operating point that is illegal.

    A subclass of ProfileError so a caller that only wants to know "can this
    link come up" catches both with one `except`, while a bench sweep that
    distinguishes "this placement is impossible" from "this placement is legal
    but the burst is too long" can still tell them apart.
    """


class LinkConfig(object):
    """Everything that decides what goes on the air, validated together.

    THE WHOLE POINT IS THE WORD *TOGETHER*. A Profile answers "is this tone
    placement legal" and a code answers "how many bytes fit", but the question
    that actually decides whether this link delivers is burst DURATION, and
    duration is a joint function of five things that used to live in three
    modules: the code, the symbol rate, the tone count, the repeat count and
    the lead-in. The previous transport could move any one of them on its own.
    It did, repeatedly, and the result was a 10.32 s burst delivering 29-byte
    beacons -- 4.4x longer than necessary, almost all of it from the code size,
    the keying and the lead-in rather than from the waveform.

    So an illegal combination cannot be constructed here, and the ceiling is
    checked when a configuration is CHOSEN rather than when a burst is built.
    That matters because the arithmetic is pure: it needs no libcodec2, no
    audio device and no radio, which is what lets the bench sweeps and the
    bridge's startup path both ask "would this fit" for free.

    THE ARITHMETIC IS DUPLICATED ON PURPOSE. fm_modem.Codec2Fsk.burst_duration_s
    asks the native library the same question, and test_fm_modem asserts the
    two agree for every code at both tone counts and several rates. One is the
    model the configuration is validated against; the other is what the modem
    will actually emit. Letting only the second exist is how the ceiling came
    to be unenforceable at configuration time; letting them silently disagree
    would be worse, hence the test.
    """

    def __init__(self, name, profile, codename=DEFAULT_CODE,
                 repeats=DEFAULT_REPEATS, keying=KEYING_CM108,
                 lead_in_s=None, max_burst_s=MAX_BURST_S,
                 deemphasis_us=None):
        self.name = name
        self.profile = profile
        # None = whatever the chain measured; a number overrides it. Used by the
        # VOX-cable option, whose receive path is the K1 PRO's and so needs the
        # filter regardless of which tone placement was picked.
        self._deemphasis_override = deemphasis_us
        self.codename = codename
        self.repeats = repeats
        self.keying = keying
        self.max_burst_s = max_burst_s

        # Lead-in resolution order, most specific first: what the caller
        # explicitly asked for, then what this CHAIN measured, then what the
        # keying method needs. The middle step is the one the previous
        # transport lacked -- it had a single global lead-in, which is how a
        # handheld chain's 1.6 s figure came to be applied to a CM108-keyed
        # desktop link that needed 0.10.
        if lead_in_s is None and profile is not None:
            lead_in_s = profile.chain.lead_in_s if profile.chain else None
        if lead_in_s is None:
            lead_in_s = KEYING_LEAD_IN_S.get(keying)
        self.lead_in_s = lead_in_s

        self._validate()

    # -- derived: bytes -----------------------------------------------------

    def frame_payload_bytes(self):
        """What the modem will carry per frame, EXCLUDING codec2's own CRC.
        This is the number fm_framing sizes a frame to; getting the CRC in or
        out of it is an off-by-two that works for small payloads and corrupts
        large ones."""
        return code_bytes(self.codename) - CRC_BYTES

    def capacity_bytes(self):
        """Real content one frame carries -- the link's MTU.

        Delegates the prefix size to fm_framing rather than restating it, so
        there is exactly one definition of the MTU. The two disagreeing is an
        off-by-one that only shows up at exactly the full-payload boundary,
        which is where beacons sit: 29 B of beacon in 29 B of capacity, zero
        spare.
        """
        return fm_framing.capacity_for(self.frame_payload_bytes())

    def code_rate(self):
        return code_rate(self.codename)

    # -- derived: time ------------------------------------------------------

    def preamble_symbols(self):
        return preamble_symbols(self.profile.tones)

    def frame_symbols(self):
        """Symbols one frame occupies: the codeword plus the unique word."""
        return frame_symbols(self.codename, self.profile.tones)

    def burst_symbols(self):
        return burst_symbols(self.codename, self.profile.tones, self.repeats)

    def burst_duration_s(self):
        """How long one physical transmission runs, lead-in included. See the
        module-level burst_duration_s(), which this is the validated-config
        spelling of."""
        return burst_duration_s(self.codename, self.profile.tones,
                                self.profile.symbol_rate_bd, self.repeats,
                                self.lead_in_s)

    def lead_in_fraction(self):
        """What share of the transmission is a plain tone carrying nothing.

        Worth surfacing rather than deriving on demand: at the previous
        transport's 1.6 s lead-in this would have been most of a short burst,
        and that is the argument for deterministic keying over VOX stated as a
        number instead of an opinion.
        """
        d = self.burst_duration_s()
        return (self.lead_in_s / d) if d > 0 else 0.0

    def goodput_bps(self, payload_bytes):
        """Real content delivered over the WHOLE transmission, lead-in and
        every redundant copy included. This is the figure to quote; the raw
        symbol rate describes a link about five times faster than this one."""
        d = self.burst_duration_s()
        return (payload_bytes * 8.0 / d) if d > 0 else 0.0

    # -- derived: hardware --------------------------------------------------

    def deemphasis_us(self):
        """Receive-side de-emphasis this chain needs, microseconds (0 = none).

        Carried on the config because it is the difference between 3 of 48
        copies and 34 of 40 on the chain that needs it, and because it is a
        property of the CABLE: a config is the only object that knows both
        which chain is in use and that the receive path has to be told.
        """
        if self._deemphasis_override is not None:
            return self._deemphasis_override
        return self.profile.chain.deemphasis_us if self.profile.chain else 0.0

    # -- validation ---------------------------------------------------------

    def _validate(self):
        if self.profile is None:
            raise LinkConfigError("%s: no Profile" % self.name)

        _code(self.codename)   # raises on an unknown code

        if self.keying not in KEYING_LEAD_IN_S:
            raise LinkConfigError(
                "%s: unknown keying %r; have %s. Keying is not cosmetic -- it "
                "sets the lead-in, and the difference between the two is "
                "~0.2 s on every burst."
                % (self.name, self.keying,
                   ", ".join(sorted(KEYING_LEAD_IN_S))))

        if self.repeats < 1:
            raise LinkConfigError(
                "%s: %d copies -- a burst with no frame in it is not a burst"
                % (self.name, self.repeats))

        if self.lead_in_s is None or self.lead_in_s < 0:
            raise LinkConfigError(
                "%s: lead-in %r must be zero or more seconds"
                % (self.name, self.lead_in_s))

        # Capacity, checked here so "this code cannot carry anything" is a
        # configuration error rather than a framing error at the first send.
        #
        # RE-RAISED AS A LinkConfigError, because a caller asking "can this
        # link come up" must not need to know that the length prefix lives in
        # another module to catch the answer. The message is kept verbatim --
        # it is the one that names the fix.
        try:
            self.capacity_bytes()
        except fm_framing.FramingError as e:
            raise LinkConfigError("%s: %s" % (self.name, e))

        duration = self.burst_duration_s()
        if duration > self.max_burst_s:
            raise LinkConfigError(
                "%s: %d copies of %s at %d Bd with a %.2f s lead-in runs "
                "%.2f s, over the %.1f s ceiling. Reduce repeats, pick a "
                "smaller code, or shorten the lead-in -- do NOT raise the "
                "ceiling without new bench evidence that a longer burst still "
                "arrives. A ~7.4 s burst measured 0/10 in both directions, "
                "and a ~34 s one delivered 100%% while only 56-69%% of its "
                "copies survived."
                % (self.name, self.repeats, self.codename,
                   self.profile.symbol_rate_bd, self.lead_in_s, duration,
                   self.max_burst_s))

    # -- reporting ----------------------------------------------------------

    def summary_line(self):
        """One line naming every number that has to match at the far end.

        Two operators comparing this by eye is the only mismatch check that
        can exist on a link with no return path: a receiver that disagrees
        recovers no frames, and so cannot be told that it disagrees.
        """
        return ("FM %s: %s @ %d Bd, %s rate 1/%.0f, %d copies, %s keying "
                "(%.2f s lead-in) = %.2f s burst, %d B capacity "
                "(both ends must match exactly)"
                % (self.name,
                   "/".join(str(t) for t in self.profile.tone_list()),
                   self.profile.symbol_rate_bd, self.codename,
                   1.0 / self.code_rate(), self.repeats, self.keying,
                   self.lead_in_s, self.burst_duration_s(),
                   self.capacity_bytes()))

    def budget_lines(self):
        """Where the airtime actually goes, as lines for a bench log.

        Itemised because the 4.4x the rebuild recovered did NOT come from the
        waveform -- it came from the code size, the repeat count and the
        lead-in, and nobody could see that while the burst was one number.
        """
        rs = float(self.profile.symbol_rate_bd)
        total = self.burst_duration_s()
        rows = [("lead-in (%s)" % self.keying, self.lead_in_s),
                ("preamble", self.preamble_symbols() / rs),
                ("%d x frame (%s)" % (self.repeats, self.codename),
                 self.repeats * self.frame_symbols() / rs)]
        return ["%-28s %6.3f s  %4.1f%%" % (what, secs,
                                            100.0 * secs / total if total else 0)
                for what, secs in rows]

    def evidence_line(self):
        return self.profile.evidence_line()

    def __repr__(self):
        return "<LinkConfig %s %s %s %dx %.2fs>" % (
            self.name, self.profile.name, self.codename, self.repeats,
            self.burst_duration_s())


# ---------------------------------------------------------------------------
# The chains anything has actually been measured on.
# ---------------------------------------------------------------------------

PHONE_PAIR_2026_09_30 = Chain(
    name="two-phone handheld pair",
    radios="OnePlus 8T <-> Pixel 7, handheld FM pair",
    cables="K1 PRO class, VOX keyed",
    measured_on="2026-09-30",
    delivered="7 of 9 beacons byte-perfect",
    tone_spread_db=4.7,
    sample_rate_hz=8000,
    symbol_rate_ceiling_bd=K1PRO_RX_CEILING_BD,
    usable_band_hz=(1250, 2500),
    # 1.6 s, and NOT a VOX figure despite this chain being VOX-keyed. See
    # KEYING_LEAD_IN_S: a plain tone showed the received level collapsing
    # 10-13x about 0.9 s in, under deterministic PTT as well, so this is
    # buying a transmission long enough that data starts after the collapse
    # settles. A different radio pairing on this chain needed 2.5 s.
    lead_in_s=1.6,
    # ZERO, and that is a measurement rather than an omission. The K1 PRO
    # needing ~250 us of de-emphasis AS A RECEIVER was measured on the desktop
    # bench on 2026-10-02 -- a different chain. This one carries K1 PRO-class
    # cables and delivered 7 of 9 unfiltered, so claiming its figure here
    # would be importing another chain's evidence, which is the one thing this
    # object exists to stop. If this chain is ever swept for tilt, THAT is
    # when this number changes.
    deemphasis_us=0.0,
    notes=(
        "The capture that produced this also measured the path's shape: "
        "energy falling away on both sides (1000 Hz at -18.2 dB, 3500 Hz at "
        "-24.3 dB), i.e. an effective bandpass of roughly 1250-2500 Hz with "
        "its best region around 1500-2000. The between-tone figures include "
        "spectral leakage from their strong neighbours, so the true shape "
        "between tones may be worse than measured -- a one-tone-at-a-time "
        "sweep (tools/passband_sweep.py) is what would settle it. This is a "
        "measured improvement on the previous placement, not a proven "
        "optimum."),
)

DESKTOP_DIGIRIG_2026_09_13 = Chain(
    name="two-Digirig desktop bench",
    radios="TYT MD-UV390 <-> Baofeng UV-5R, on a desk",
    cables="two Digirig Lite, deterministic CM108 PTT",
    measured_on="2026-09-13",
    delivered="20 of 22 delivered (91%)",
    tone_spread_db=None,
    sample_rate_hz=48000,
    # Only ever run at 250 Bd, so 250 is what it has evidence for -- even
    # though the SAME cables later measured 500 Bd on the 2026-10-02 chain
    # below. The ceiling is the cable's, but the measurement is this session's,
    # and this session did not take it.
    symbol_rate_ceiling_bd=250,
    # Unswept. This chain is known to have DELIVERED at 1500-3000 Hz; that is
    # not the same as knowing where its edges are, and a band it never
    # measured must not be asserted on its behalf.
    usable_band_hz=None,
    deemphasis_us=0.0,
    notes=(
        "DOES NOT TRANSFER. This exact placement measured 0 of 9 on the "
        "two-phone pair above, seventeen days later, on the same software. "
        "It is recorded because it is real evidence about THIS chain -- a "
        "radio whose audio stage genuinely reaches 3000 Hz gets 500 Hz of "
        "tone separation out of it instead of 250, which is the more robust "
        "spacing where the band allows it. It is not a placement to prefer "
        "on unqualified hardware. Also: every signal on a two-radio desk "
        "bench arrives far too strong, which is why fm_channel.py exists."),
)


DESKTOP_DIGIRIG_2026_10_02 = Chain(
    name="two-Digirig desktop bench, Phase 0",
    radios="TYT MD-UV390 <-> Baofeng UV-5R, on a desk",
    cables="two Digirig Lite, deterministic CM108 PTT",
    measured_on="2026-10-02",
    delivered="178 of 180 copies at 400 Bd; 90 of 90 whole messages",
    tone_spread_db=None,
    sample_rate_hz=48000,
    symbol_rate_ceiling_bd=DIGIRIG_RX_CEILING_BD,
    usable_band_hz=MEASURED_USABLE_BAND_HZ,
    # None needed with a Digirig RECEIVING. The same session measured the K1
    # PRO as receiver at 3 of 48 copies unfiltered against 34 of 40 with
    # ~250 us, and established it is the cable rather than the radio pairing
    # by swapping a second Digirig in with both radios fixed: 20 of 20
    # unfiltered in the direction that had needed the filter.
    deemphasis_us=0.0,
    # Keying default (0.10 s for CM108) rather than a chain override: the
    # 1.6 s received-level collapse that forces the handheld chain's lead-in
    # was not what limited this one.
    lead_in_s=None,
    notes=(
        "THE CHAIN THAT RELOCATED THE 250 Bd CEILING. 400 Bd ran 178 of 180 "
        "copies here with a Digirig Lite receiving, which is the same figure "
        "the record attributes to an analog limit in FM -- it is one cable's, "
        "on the RECEIVE end, consistent with the 2026-09-13 finding that the "
        "receiving cable decides. 400 and 500 Bd were indistinguishable "
        "(98.9% per copy each, 90 copies each, interleaved over 63 minutes); "
        "600 Bd was the only rate to lose whole messages (86 of 90) at full "
        "signal on a desk, so it is measured and not adopted. The band was "
        "swept the same day: 1000-3000 Hz usable, and a 400 Hz first tone "
        "delivered 0% even at a rate that worked at 1000. "
        "EVERYTHING HERE IS FULL SIGNAL AT DESK RANGE -- the weak-link "
        "comparison needs hardware (a low-power setting, real distance, or an "
        "inline attenuator) and has not been done. One direction also swings "
        "0-100% per copy at fixed settings within minutes while the other "
        "holds at 100%, unexplained and not level-related, which is why short "
        "single-direction measurements on this bench are worthless."),
)


# ---------------------------------------------------------------------------
# The profiles themselves.
#
# Deliberately few. Each entry is a placement something has actually been
# measured on; an unmeasured entry is a way for an operator to select a dead
# link in the field, which is worse than having no choice at all.
# ---------------------------------------------------------------------------

# A TRIAL chain, not a measurement. Exists so the two-tone 1000 Bd profile below
# has somewhere honest to say what it stands on: nothing. Its ceiling is set to
# the rate under test so the profile can be constructed at all -- the cable's
# real ceiling at 1000 Bd is exactly what the over-the-air test is for, and
# nothing here may be read as evidence that it holds.
PHONE_PAIR_TWOTONE_TRIAL = Chain(
    name="phone pair, two-tone 1000 Bd TRIAL",
    radios="OnePlus 8T <-> Pixel 7, one handheld each",
    cables="Digirig on each phone",
    measured_on="NOT MEASURED (trial 2026-10-12)",
    delivered="nothing yet; model only: 20/20 at SNR 10-14 dB against 12-15 of 20 "
              "for 400 Bd 4-tone (tools/fm_bench.py --channel, pixel-rx-2026-09-30)",
    tone_spread_db=None,
    sample_rate_hz=8000,
    symbol_rate_ceiling_bd=1000,
    usable_band_hz=(1000, 3000),
    deemphasis_us=0.0,
    notes=(
        "The model ranks, it does not predict: the same model put the 400 Bd "
        "4-tone point at 60-75% where the over-the-air desk runs delivered "
        "100%. The measured response spans 1500-3000 Hz only, so both settings "
        "were modelled from 1500 Hz. Whether the receiving cable tracks 1000 Bd "
        "is untested; the earlier 250 Bd 'ceiling' was one cable's."),
)

NARROW = Profile(
    name="narrow",
    # Inside the 1250-2500 Hz region the 2026-09-30 captures found, and the
    # TIGHTEST LEGAL placement here: spacing must be >= the symbol rate, and
    # 250 Bd is itself a hard floor on this chain. The two hard constraints
    # meet with no slack, which is worth noticing -- this was not a
    # comfortable choice among many, it was the only point satisfying both.
    first_tone_hz=1500,
    tone_spacing_hz=250,
    tones=4,
    symbol_rate_bd=250,
    chain=PHONE_PAIR_2026_09_30,
)

WIDE = Profile(
    name="wide",
    # 1500/2000/2500/3000 Hz. Kept for ONE job: talking to hardware that has
    # actually been qualified at this placement, where 500 Hz of tone
    # separation is the more robust choice. Measured 0 of 9 on the two-phone
    # pair (top tone 13-21 dB down, detected 8.2% against the 25% sent), so
    # it is not a placement to prefer on unqualified radios.
    first_tone_hz=1500,
    tone_spacing_hz=500,
    tones=4,
    symbol_rate_bd=250,
    chain=DESKTOP_DIGIRIG_2026_09_13,
)

FAST = Profile(
    name="fast",
    # 1000/1400/1800/2200 Hz at 400 Bd -- PHASE 0'S OPERATING POINT, and the
    # first placement here chosen from a swept band rather than from a
    # spectrum capture of the tones themselves.
    #
    # Every number is pinned by a different constraint, which is why it looks
    # arbitrary and is not:
    #   * 1000 Hz first tone: the measured LOW EDGE of the usable band. Lower
    #     is not a gentle degradation -- 400 Hz delivered 0%.
    #   * 400 Hz spacing: the tightest legal placement at 400 Bd, since
    #     spacing must be >= the symbol rate. Wider would push the top tone up
    #     for nothing.
    #   * 400 Bd: inside the Digirig receive cable's measured 500 Bd ceiling,
    #     and well inside the band's own 666 Bd ceiling for 4 tones. 500 Bd
    #     measured identically and 600 Bd lost whole messages, so 400 is the
    #     fastest rate with nothing measured against it.
    #
    # It occupies 1200 Hz of the 2000 Hz band, which leaves room to move the
    # whole alphabet up if a chain turns out to roll off at the bottom.
    first_tone_hz=1000,
    tone_spacing_hz=400,
    tones=4,
    symbol_rate_bd=400,
    chain=DESKTOP_DIGIRIG_2026_10_02,
)

TWOTONE = Profile(
    name="twotone",
    # TRIAL (2026-10-12). Two tones 1000 Hz apart at 1000 Bd: with 2 tones the
    # band needed is rate x 1 gap = 1000 Hz, where 4 tones need rate x 3, which
    # is what caps the 4-tone profiles near 666 Bd. 1000 divides the phone's
    # 8000 Hz exactly; 1200 does not, which is why 1200 is not offered.
    # 1500/2500 stays inside the 1500-3000 Hz span the phone response was
    # measured over. Raw rate 1000 b/s against FAST's 800; burst 2.23 s against
    # 2.95 s (preamble and key-up are fixed costs, so not 1.25x shorter).
    first_tone_hz=1500,
    tone_spacing_hz=1000,
    tones=2,
    symbol_rate_bd=1000,
    chain=PHONE_PAIR_TWOTONE_TRIAL,
)

PROFILES = {p.name: p for p in (FAST, NARROW, WIDE)}

TWOTONE_500 = Profile(
    name="twotone500",
    # TRIAL (2026-10-12). IDENTICAL TONES to `twotone` (1500/2500 Hz), only the
    # symbol rate differs, so that the next over-the-air run can say whether the
    # 1000 Bd rate was what failed (twotone delivered 20/42 against fast's
    # 39/42) or the tone count / placement was. 500 Bd divides the phone's 8000 Hz;
    # raw rate is 500 b/s, slower than FAST's 800, so this is a diagnostic and
    # not a candidate.
    first_tone_hz=1500,
    tone_spacing_hz=1000,
    tones=2,
    symbol_rate_bd=500,
    chain=PHONE_PAIR_TWOTONE_TRIAL,
)

TWOTONE_500_LOW = Profile(
    name="twotone500low",
    # TRIAL (2026-10-12). Same as `twotone500` (2 tones 1000 Hz apart, 500 Bd)
    # but moved down to 1000/2000 Hz, inside the 1000-2200 Hz range `fast` runs
    # in. Only the placement differs, to separate "two tones" from "1500/2500 Hz".
    first_tone_hz=1000,
    tone_spacing_hz=1000,
    tones=2,
    symbol_rate_bd=500,
    chain=PHONE_PAIR_TWOTONE_TRIAL,
)

# Placements under TRIAL: constructible, deliberately NOT in PROFILES. PROFILES is
# what the picker offers and what resolve() falls back through, and its header says
# every entry has been measured; a trial would be one tap from an operator's dead
# link. Reached only through trial_link_config(), i.e. the adb test hook.
TRIAL_PROFILES = {p.name: p for p in (TWOTONE, TWOTONE_500, TWOTONE_500_LOW)}

# What a node uses unless told otherwise.
#
# `fast`, from 2026-10-02. Note what that does and does not claim: it is the
# placement with the most recent and most carefully taken evidence (fully
# interleaved, spread over 63 minutes, per-copy reported alongside delivery),
# and it is the operating point the rebuild is built around -- but that
# evidence is from the DESKTOP bench at full signal, and the handheld chain
# `narrow` was measured on has never run it. An operator on handhelds is
# choosing an unqualified placement, which is exactly what evidence_line()
# exists to tell them.
DEFAULT_PROFILE = FAST.name


def profile(name=None):
    """Look up a profile by name, falling back to the measured default.

    FALLS BACK RATHER THAN RAISING, deliberately, because the name arrives
    from Kotlin as a bare string out of SharedPreferences: the case to design
    for is a stored value no longer in the table (a downgrade, or a profile
    withdrawn after a bench session). Falling back leaves a working link;
    raising would take the whole bridge's startup down over a stale
    preference. The caller decides whether to log the substitution -- see
    `resolve` for the version that says what it did.
    """
    return PROFILES.get((name or "").strip().lower() or DEFAULT_PROFILE,
                        PROFILES[DEFAULT_PROFILE])


def resolve(name=None):
    """(profile, substituted) -- the profile in effect, and whether the
    requested name was unknown.

    Separate from `profile()` so the startup path can report a substitution
    at ERROR level while bench tools and tests can ignore it. A silently
    substituted profile is a link that works but not with the peers the
    operator expected, which is exactly the failure this whole module's
    evidence-carrying design is meant to make visible.
    """
    wanted = (name or "").strip().lower() or DEFAULT_PROFILE
    if wanted in PROFILES:
        return PROFILES[wanted], False
    return PROFILES[DEFAULT_PROFILE], True


def profile_names():
    """Every selectable profile name, in table order."""
    return list(PROFILES)


# ---------------------------------------------------------------------------
# The operating point.
#
# ONE OBJECT, so that the burst duration this link actually runs at is a thing
# that can be printed, asserted and compared rather than something assembled
# at four call sites. Phase 0 measured most of these numbers; Phase 4 changed
# two of them, for a reason Phase 0 could not have seen.
# ---------------------------------------------------------------------------

OPERATING_POINT = LinkConfig(
    name="phase4",
    profile=FAST,                   # 400 Bd, tones 1000/1400/1800/2200
    codename=DEFAULT_CODE,          # H_1024_2048_4f, rate 1/2, 125 B capacity
    # EXPLICIT, and deliberately not DEFAULT_REPEATS. The two agree on nothing
    # here: the default is 2, this is 1, and the difference is the whole reason
    # this configuration is affordable. See DEFAULT_REPEATS for why they are
    # allowed to differ and what a bench run has to separate.
    repeats=1,
    keying=KEYING_CM108,            # 0.10 s lead-in
)

# 2.95 s, against the teardown baseline's 10.32 s -- a 3.5x cut with the payload
# INTACT, which is the thing worth stating. Where it came from:
#
#   rate      250 Bd     -> 400 Bd          \
#   lead-in   1.6 s VOX  -> 0.10 s CM108     > 10.32 s -> 2.95 s, payload kept
#   copies    2          -> 1               /
#   code      126 B      -> 126 B, unchanged
#
# PHASE 0 ALSO CUT THE CODE, to 30 B, reaching 2.35 s. Phase 4 put that back,
# because wiring the CoT codec to 29 B of capacity showed what it cost: the
# codec sees 17 B after the mesh envelope, a position report needs 26 B even
# with its identity stripped, and the difference-coding that would fit (16 B)
# cannot start until one absolute position has landed. Self-position reports
# took 12 bursts of zlib'd XML -- 28.2 s each -- or did not work at all.
#
# So the code was worth 0.6 s of burst and 96 B of capacity, and the 96 B are
# the whole position path. See DEFAULT_CODE for the full arithmetic.
#
# TWO THINGS HERE ARE UNMEASURED AND CHOSEN KNOWINGLY: a 2.95 s burst on the
# 128 B code (the figures that condemned this code -- 0/10 at 7.4 s, ~50% at
# 10.32 s -- are all from the old burst lengths), and 1 in-burst copy. They are
# coupled, so the bench run that checks them needs three cells and not two.
# evidence_line() reports the placement's own evidence; these two are recorded
# in docs/open-questions.md and CLAUDE.md, not in the Profile, because they are
# properties of this configuration rather than of the tone placement.
#
# The remaining untested lever is the FEC rate: this code is already 1/2, so the
# 1/3 -> 1/2 saving Phase 0 listed is spent here too. There is no stronger code
# at this size.


def link_config(profile_name=None, **over):
    """A complete operating point for a chosen tone placement.

    Built on demand rather than kept in a second table, so there is nothing to
    drift out of step with PROFILES: the UI picker chooses a placement, and
    everything else comes from the operating point unless explicitly
    overridden.

    FALLS BACK on an unknown placement exactly as profile() does -- the name
    arrives from Kotlin as a bare SharedPreferences string, and raising would
    take the bridge's startup down over a stale preference. Use
    resolve_link_config() where the substitution needs reporting.

    Raises LinkConfigError if the RESULT is illegal, because that is a
    different situation: a legal placement with a code or repeat count that
    does not fit under the duration ceiling is a configuration that cannot be
    silently repaired, and a link that comes up with a different burst length
    than asked for is the fragmentation this module exists to end.
    """
    prof, _substituted = resolve(profile_name)
    kw = dict(name=prof.name, profile=prof, codename=OPERATING_POINT.codename,
              repeats=OPERATING_POINT.repeats, keying=OPERATING_POINT.keying,
              max_burst_s=OPERATING_POINT.max_burst_s)
    kw.update(over)
    return LinkConfig(**kw)


def trial_link_config(name, **over):
    """A complete operating point for a TRIAL placement (TRIAL_PROFILES).
    Raises KeyError for a name that is not one, rather than falling back: a
    test that silently ran a different placement would score the wrong thing."""
    prof = TRIAL_PROFILES[(name or "").strip().lower()]
    kw = dict(name=prof.name, profile=prof, codename=OPERATING_POINT.codename,
              repeats=OPERATING_POINT.repeats, keying=OPERATING_POINT.keying,
              max_burst_s=OPERATING_POINT.max_burst_s)
    kw.update(over)
    return LinkConfig(**kw)


def resolve_link_config(profile_name=None, **over):
    """(config, substituted) -- as resolve(), for the startup path that wants
    to log an unknown placement at ERROR level while tests and bench tools
    ignore it."""
    _prof, substituted = resolve(profile_name)
    return link_config(profile_name, **over), substituted
