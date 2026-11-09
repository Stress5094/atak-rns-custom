"""What an FM handheld does to audio, so a modem can be judged before any
radio exists.

WHY THIS EXISTS. The modem decision -- tone spacing, baud rate, how much
forward error correction to buy -- has to be made before the first bench, and
a bench with two radios on a desk cannot answer it anyway: every signal there
arrives far too strong. The only way to see a modem fail the way it will fail
in the field is to build the channel that breaks it and turn the signal down.

Deliberately free of numpy, RNS and Android imports, exactly like tx_policy,
relay and spectrum: everything here is arithmetic over a list of floats, so it
runs under plain `python3` with nothing installed. It is slower than numpy and
fast enough -- a full survival sweep is seconds, not minutes.

It lives in tools/ rather than app/src/main/python for a specific reason: that
directory is a Chaquopy source root and everything in it is packaged into the
APK. This is desk equipment and has no business on a phone.

WHAT AN FM VOICE CHANNEL ACTUALLY DOES TO A MODEM, in the order it does it:

1. PASSBAND. Roughly 300-3000 Hz gets through and nothing else does. This is
   the single hardest constraint and it is why modems designed for wider
   channels cannot simply be pointed at a handheld: energy outside the
   passband is not attenuated, it is gone.

2. PRE- AND DE-EMPHASIS. FM transmitters boost treble by 6 dB/octave and
   receivers cut it back by the same amount, which improves voice
   intelligibility and is transparent to voice. It is NOT transparent to a
   modem: two tones an octave apart leave the transmitter at different levels
   than they entered it. A modem whose tones straddle that slope sees them
   arrive unbalanced, and a demodulator comparing tone energies is exactly the
   kind that gets fooled. 750 us is the land-mobile time constant (broadcast
   FM uses 75 us; using the wrong one flatters the modem by 20 dB at 3 kHz).

3. CLIPPING. A phone's headphone output is far hotter than a microphone input
   wants, and the transmitter limits hard. Overdrive does not get louder, it
   gets square -- and a clipped tone grows harmonics that land back inside the
   passband as interference with the signal that made them.

4. NOISE, WITH A CLIFF. This is where FM differs most from the textbook. Above
   its threshold an FM receiver is quiet, and the audio degrades gently. Below
   it the limiter starts capturing noise instead of signal and the output
   collapses into impulsive clicks over a handful of dB. There is no graceful
   region at the bottom: a link that is 1 dB below threshold is not slightly
   worse than one at threshold, it is unusable. A modem tested against plain
   Gaussian noise will look far better than it is, because Gaussian noise
   never produces the clicks that destroy whole frames at once.

THE MODEL IS APPROXIMATE AND SAYS SO. The click process below threshold is a
crude stand-in for real FM click noise, and de-emphasis is modelled as the
exact inverse of pre-emphasis, which a real radio pair is not. What it is for
is RANKING modem configurations against each other under one consistent
insult, the same argument spectrum.py makes about the noise floor: a common-
mode error cancels out of a comparison and rankings survive it. Absolute
survival percentages from this file are not field predictions and must not be
quoted as any.
"""

import math
import random

# The audio a narrowband FM radio will pass. Outside this, nothing.
PASSBAND_LOW_HZ = 300.0
PASSBAND_HIGH_HZ = 3000.0

# Land-mobile emphasis time constant. Broadcast FM is 75 us -- using that here
# would understate the treble tilt by 20 dB at the top of the passband and
# make every modem look better than it is.
EMPHASIS_TAU_S = 750e-6

# Where the FM receiver stops being quiet. Below this the click process starts
# and gets rapidly worse. ~10 dB is the textbook figure for the knee.
DEFAULT_THRESHOLD_DB = 10.0

# What the transmitter clips at, as a multiple of nominal drive. 1.0 means the
# signal is already at the limit; the audio interface should be set below this.
DEFAULT_CLIP = 1.0


def _biquad(samples, b0, b1, b2, a1, a2):
    """Direct-form-I biquad. Enough filter for a channel model, and short
    enough to read."""
    out = []
    x1 = x2 = y1 = y2 = 0.0
    for x0 in samples:
        y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        out.append(y0)
        x2, x1 = x1, x0
        y2, y1 = y1, y0
    return out


def _lowpass(samples, fs, fc, q=0.7071):
    """RBJ cookbook low-pass. q=1/sqrt(2) is Butterworth -- maximally flat,
    which is what a channel model wants: any ripple here would be mistaken for
    the modem's own behaviour."""
    w = 2.0 * math.pi * fc / fs
    cw, sw = math.cos(w), math.sin(w)
    alpha = sw / (2.0 * q)
    a0 = 1.0 + alpha
    return _biquad(samples,
                   ((1.0 - cw) / 2.0) / a0, (1.0 - cw) / a0,
                   ((1.0 - cw) / 2.0) / a0,
                   (-2.0 * cw) / a0, (1.0 - alpha) / a0)


def _highpass(samples, fs, fc, q=0.7071):
    """RBJ cookbook high-pass, same reasoning as _lowpass."""
    w = 2.0 * math.pi * fc / fs
    cw, sw = math.cos(w), math.sin(w)
    alpha = sw / (2.0 * q)
    a0 = 1.0 + alpha
    return _biquad(samples,
                   ((1.0 + cw) / 2.0) / a0, -(1.0 + cw) / a0,
                   ((1.0 + cw) / 2.0) / a0,
                   (-2.0 * cw) / a0, (1.0 - alpha) / a0)


# A 4th-order Butterworth as two cascaded biquads. These are the standard
# stage Q values; a single biquad is 12 dB/octave, which leaves 22% of a tone
# an octave outside the passband still standing. A real radio is far steeper
# than that -- it has to be, or it would splatter into the adjacent channel --
# and a model that lets out-of-band energy through is a model that will
# forgive a modem for putting energy where a radio would have destroyed it.
_BUTTER4_Q = (0.5412, 1.3066)


def bandpass(samples, fs, low_hz=PASSBAND_LOW_HZ, high_hz=PASSBAND_HIGH_HZ):
    """The voice channel's own limits, applied twice over: once in the
    transmitter and once in the receiver, which is what a real pair does."""
    s = samples
    for q in _BUTTER4_Q:
        s = _highpass(s, fs, low_hz, q)
    for q in _BUTTER4_Q:
        s = _lowpass(s, fs, high_hz, q)
    return s


# ---------------------------------------------------------------------------
# MEASURED RESPONSE
#
# Everything above this point is a MODEL of what an FM chain does. This is a
# MEASUREMENT of what one particular chain actually did, and the two are not
# interchangeable.
#
# The model's passband is flat from 300 to 3000 Hz. No chain in this project's
# record is flat anywhere. The 2026-09-30 four-tone capture found the Pixel's
# receive path falling 14 dB across 1500-3000 Hz, and the plan's own Phase 6
# note says the flat model "currently flatters every candidate" because of it:
# a modem that spreads its tones over an octave pays nothing in the model and
# is destroyed on the air.
#
# WHY THIS IS A SEPARATE STAGE AND NOT A CORRECTION TO bandpass(). A measured
# response belongs to ONE chain -- one phone, one cable, one radio, one volume
# setting -- exactly like fm_config.Chain's symbol-rate ceiling and
# de-emphasis, which Phase 1 made per-chain for the same reason. Folding a
# measurement into the generic model would make every future candidate inherit
# one afternoon's hardware.
# ---------------------------------------------------------------------------


class OutOfBand(ValueError):
    """A frequency the measurement does not cover.

    Raised rather than extrapolated. Extrapolating a roll-off is how a model
    comes to flatter or condemn a tone placement on no evidence, which is the
    precise failure Phase 6 exists to remove.
    """


#: Below this many Hz per FIR bin the filter cannot represent the measured
#: curve, so the tap count is derived from it rather than chosen.
_RESPONSE_RESOLUTION_HZ = 100.0

#: Refuse to build a filter costing more than this many multiply-accumulates
#: per burst. 8000 Hz (the phone's rate) needs ~101 taps and costs ~0.4 s for
#: a 2.95 s burst; 48000 Hz needs ~481 for the SAME resolution and costs ~100
#: s, which turns a sweep from seconds into hours. Measured, not guessed.
_RESPONSE_MAX_MACS = 20e6


class Response(object):
    """One chain's measured magnitude response: dB at a tone, as measured.

    Points are (hz, db) pairs, relative -- only the SHAPE is used, because an
    absolute level here would be this chain's volume knob and nothing else.

    INTERPOLATION IS IN dB AGAINST log2(f), which is the space a filter
    roll-off is straight in. Linear-in-Hz interpolation across an octave
    understates the middle of that octave by several dB.

    `source` is mandatory and is written into every report. A response with no
    provenance is a guess wearing a measurement's clothes, and this project has
    twice acted on a number whose origin nobody could reconstruct.
    """

    def __init__(self, name, points, source, usable_hz=None):
        pts = sorted((float(f), float(d)) for f, d in points)
        if len(pts) < 2:
            raise ValueError(
                "a response needs at least two measured points; %s has %d. "
                "One point is a reading, not a curve -- record it in a "
                "comment instead of inventing a shape around it."
                % (name, len(pts)))
        if any(f <= 0.0 for f, _ in pts):
            raise ValueError("%s: frequencies must be positive" % name)
        if len(set(f for f, _ in pts)) != len(pts):
            raise ValueError("%s: duplicate frequency in the points" % name)
        if not source or not source.strip():
            raise ValueError("%s: a response must carry its source" % name)
        self.name = name
        self.points = tuple(pts)
        self.source = source.strip()
        self.low_hz = pts[0][0]
        self.high_hz = pts[-1][0]
        self.usable_hz = tuple(usable_hz) if usable_hz else None

    def __repr__(self):
        return "<Response %s %.0f-%.0f Hz, %d points>" % (
            self.name, self.low_hz, self.high_hz, len(self.points))

    def covers(self, *freqs_hz):
        """True when every frequency given was actually measured over.

        A caller asks this BEFORE running a configuration, so that an
        unmeasured tone placement is refused rather than silently held flat by
        `taps()` below.
        """
        return all(self.low_hz <= float(f) <= self.high_hz for f in freqs_hz)

    def gain_db(self, hz):
        """The measured gain at `hz`, interpolated. Raises outside the span."""
        hz = float(hz)
        if hz < self.low_hz or hz > self.high_hz:
            raise OutOfBand(
                "%s was measured over %.0f-%.0f Hz and %.0f Hz is outside it. "
                "The measurement does not say what this chain does there; "
                "measure it (tools/passband_sweep.py) or choose tones inside "
                "the span." % (self.name, self.low_hz, self.high_hz, hz))
        lo = self.points[0]
        for pt in self.points[1:]:
            if hz <= pt[0]:
                hi = pt
                break
            lo = pt
        else:                                     # pragma: no cover - hz == high
            return self.points[-1][1]
        if hi[0] == lo[0]:                        # pragma: no cover - deduped
            return lo[1]
        span = math.log2(hi[0]) - math.log2(lo[0])
        frac = (math.log2(hz) - math.log2(lo[0])) / span
        return lo[1] + frac * (hi[1] - lo[1])

    def taps(self, fs, resolution_hz=_RESPONSE_RESOLUTION_HZ):
        """A linear-phase FIR realising this response at `fs`.

        Frequency sampling: the response is evaluated on the DFT grid, inverse
        transformed and Hamming windowed. The tap count comes from the
        resolution the measured curve needs, NOT from taste -- too few taps
        and the filter cannot bend steeply enough to show the tilt that is the
        whole point of measuring it.

        OUTSIDE THE MEASURED SPAN THE RESPONSE IS HELD at its nearest measured
        value. That is not a claim about the chain: it is the choice that
        neither flatters nor damns an unmeasured frequency. Callers that care
        must ask covers() first -- holding is a placeholder, not a model.
        """
        n = int(fs / float(resolution_hz))
        n += 1 - (n % 2)                          # odd, for exact linear phase
        n = max(n, 15)
        if n * n > _RESPONSE_MAX_MACS / 10.0 or n > 301:
            raise ValueError(
                "%s at %.0f Hz needs %d taps for %.0f Hz resolution, which "
                "costs minutes per burst. Simulate at the rate the response "
                "was measured at (8000 Hz for the phone chains) -- a response "
                "measured on one chain says nothing about another chain's "
                "sample rate anyway." % (self.name, fs, n, resolution_hz))
        half = n // 2
        mag = []
        for k in range(half + 1):
            f = k * fs / float(n)
            f = min(max(f, self.low_hz), self.high_hz)
            mag.append(10.0 ** (self.gain_db(f) / 20.0))
        h = []
        for i in range(n):
            acc = mag[0]
            for k in range(1, half + 1):
                acc += 2.0 * mag[k] * math.cos(
                    2.0 * math.pi * k * (i - half) / float(n))
            h.append(acc / float(n))
        # Hamming, to stop the truncation ripple from inventing a few dB of
        # its own right where the measured points are.
        return [x * (0.54 - 0.46 * math.cos(2.0 * math.pi * i / (n - 1.0)))
                for i, x in enumerate(h)]

    def apply(self, samples, fs, resolution_hz=_RESPONSE_RESOLUTION_HZ):
        """Put `samples` through this chain's measured response."""
        h = self.taps(fs, resolution_hz)
        if len(samples) * len(h) > _RESPONSE_MAX_MACS:
            raise ValueError(
                "%s at %.0f Hz over %d samples is %.0f million operations. "
                "Shorten the burst or simulate at the measured rate."
                % (self.name, fs, len(samples),
                   len(samples) * len(h) / 1e6))
        half = len(h) // 2
        n = len(samples)
        out = []
        for i in range(n):
            acc = 0.0
            lo = i - half
            for j, c in enumerate(h):
                k = lo + j
                if 0 <= k < n:
                    acc += samples[k] * c
            out.append(acc)
        return out


#: The Pixel's RECEIVE path, 2026-09-30. The only swept-ish measurement of a
#: real chain this project has.
#:
#: Derived as (received dB - sent dB) from the four-tone capture recorded in
#: docs/open-questions.md: sent -0.5/-0.7/-1.2/0.0, received 0.0/-4.4/-7.7/
#: -13.4 at 1500/2000/2500/3000 Hz.
#:
#: TWO THINGS WRONG WITH IT, both from the record's own account, and both
#: reasons this is a floor on knowledge rather than a curve to trust:
#:
#:  * All four tones were transmitted TOGETHER, so each figure includes
#:    leakage from its neighbours. The record names the one-tone-at-a-time
#:    sweep (tools/passband_sweep.py) as the thing that would settle this and
#:    says it has never been run.
#:  * It stops at 1500 Hz. The same captures put 1000 Hz at -13.9 to -18.2 dB,
#:    which says the low side falls away too -- but that reading is not
#:    sent-corrected and is a range rather than a value, so it is recorded
#:    here as prose and NOT as a point. Mixing a corrected and an uncorrected
#:    figure into one curve is how a shape gets invented.
PIXEL_RX_20260930 = Response(
    "pixel-rx-2026-09-30",
    ((1500.0, 0.5), (2000.0, -3.7), (2500.0, -6.5), (3000.0, -13.4)),
    source=("docs/open-questions.md, 2026-09-30 four-tone capture; "
            "received minus sent; neighbour leakage NOT removed; "
            "1000 Hz read -13.9 to -18.2 dB uncorrected and is excluded"),
    usable_hz=(1250.0, 2250.0))

#: The OnePlus receive path from the same session measured 20.7 dB down at
#: 3000 Hz against the Pixel's 13.4 -- 7.3 dB of difference between two chains
#: on one day. It is ONE point, so there is deliberately no Response for it:
#: Response refuses to be built from a single reading. What it establishes is
#: that a response is per-chain and not reusable, which is why nothing here
#: has a default.
ONEPLUS_RX_3000HZ_DB = -20.7

#: Every measured response, by name. The desktop bench chain is ABSENT on
#: purpose: its usable band is recorded as 1000-3000 Hz but its SHAPE inside
#: that band has never been swept, and today's operating point (tones
#: 1000/1400/1800/2200) therefore has no measured response at all. A sweep
#: that wants one has to measure it first.
MEASURED_RESPONSES = {PIXEL_RX_20260930.name: PIXEL_RX_20260930}


def pre_emphasis(samples, fs, tau_s=EMPHASIS_TAU_S):
    """Boost treble, as the transmitter does.

    Written as the exact inverse of de_emphasis() so that a signal passing
    through both comes back unchanged. That is deliberately kinder than
    reality -- two different radios do not cancel exactly -- and it keeps the
    model from blaming the modem for a mismatch this file invented.
    """
    k = math.exp(-1.0 / (fs * tau_s))
    out = []
    prev = 0.0
    for x in samples:
        out.append((x - k * prev) / (1.0 - k))
        prev = x
    return out


def de_emphasis(samples, fs, tau_s=EMPHASIS_TAU_S):
    """Cut treble back, as the receiver does. One-pole, matching the inverse
    above."""
    k = math.exp(-1.0 / (fs * tau_s))
    out = []
    y = 0.0
    for x in samples:
        y = (1.0 - k) * x + k * y
        out.append(y)
    return out


def clip(samples, limit=DEFAULT_CLIP):
    """Hard limiting in the transmitter.

    Hard rather than soft on purpose: it is the pessimistic case, and a modem
    that survives hard clipping survives a gentler limiter. The harmonics this
    generates are the point -- they fold back into the passband and interfere
    with the signal that produced them, which is why an over-driven audio
    interface degrades a link that looks fine on a level meter.
    """
    return [max(-limit, min(limit, x)) for x in samples]


def rms(samples):
    if not samples:
        return 0.0
    return math.sqrt(sum(x * x for x in samples) / len(samples))


def add_noise(samples, snr_db, threshold_db=DEFAULT_THRESHOLD_DB, rng=None):
    """Gaussian noise at `snr_db`, plus FM click noise once below threshold.

    The clicks are what make this worth having. Gaussian noise degrades a
    demodulator smoothly, so a modem tested only against it appears to have a
    long gentle tail of marginal-but-usable performance. A real FM link has no
    such tail: below threshold the limiter captures noise instead of signal
    and produces impulses that take out whole frames regardless of how good
    the demodulator is. Rate rises steeply below the knee, which is what
    produces a cliff rather than a slope.
    """
    noise = noise_for(samples, snr_db, threshold_db=threshold_db, rng=rng)
    return [x + n for x, n in zip(samples, noise)]


def noise_for(samples, snr_db, threshold_db=DEFAULT_THRESHOLD_DB, rng=None):
    """The noise add_noise() would add, on its own.

    Separated so the RECEIVER'S BAND LIMIT CAN BE APPLIED TO THE NOISE
    WITHOUT BEING APPLIED TO THE SIGNAL AGAIN. On the modelled path that
    distinction does not arise -- the same model owns the passband and the
    noise, so filtering the sum is right. On the measured path it matters a
    great deal: the signal has already been through a measured chain that
    CONTAINS that chain's passband, and running a modelled bandpass over it as
    well bends the band twice. Measured at 4.0 dB of invented tilt between
    1500 and 2250 Hz when this was filtering the sum, against a response that
    was flat by construction.
    """
    rng = rng or random
    signal = rms(samples)
    if signal <= 0.0:
        return [0.0] * len(samples)

    sigma = signal / (10.0 ** (snr_db / 20.0))
    out = [rng.gauss(0.0, sigma) for _ in samples]

    below = threshold_db - snr_db
    if below > 0:
        # Doubling roughly every 2 dB under the knee. The constant is chosen
        # so the cliff lands in the right few dB, not from measurement -- see
        # the module docstring on what this model is and is not for.
        rate = min(0.5, 0.002 * (2.0 ** (below / 2.0)))
        amp = signal * 4.0
        for i in range(len(out)):
            if rng.random() < rate:
                out[i] += rng.choice((-amp, amp))
    return out


def stage_drive(samples, drive=1.0, limit=DEFAULT_CLIP):
    """Set the audio level, the way an operator does when wiring a phone to a
    radio: turn it up until just short of clipping, then stop.

    This is not cosmetic. Pre-emphasis adds ~17 dB at 1.5 kHz, so a signal
    that looks safe going into the cable is far into the limiter by the time
    it reaches the modulator. Without this step the model clips every signal
    to ruin and blames the modem -- which is exactly what happened the first
    time this file was run.

    `drive` is the mis-set knob, and the reason this is a parameter rather
    than a normalisation: 1.0 is correctly adjusted, 2.0 is 6 dB hot. Being
    able to ASK what over-driving costs is worth more than being protected
    from it, because in the field it is the mistake people actually make.
    """
    peak = max((abs(x) for x in samples), default=0.0)
    if peak <= 0.0:
        return list(samples)
    return [x * (drive * limit / peak) for x in samples]


def through(samples, fs, snr_db, clip_limit=DEFAULT_CLIP,
            threshold_db=DEFAULT_THRESHOLD_DB, drive=1.0, rng=None,
            response=None):
    """One transmission, end to end: phone audio in, phone audio out.

    The order is the order the hardware imposes and is not rearrangeable.
    Noise is added after de-emphasis because it is the RECEIVER's noise, and
    putting it before would let de-emphasis filter away the very impairment
    being measured -- flattering every modem by several dB.

    WITH A MEASURED `response`, THREE MODELLED STAGES ARE DROPPED, and this is
    the whole reason the parameter exists rather than being another filter in
    the cascade. A Response is measured from audio-in to audio-out on a real
    chain, so it ALREADY CONTAINS that chain's passband and its pre/de-
    emphasis pair. Running the modelled versions as well would apply the
    passband twice and the emphasis twice -- roughly 6 dB/octave of tilt this
    file invented, on top of the tilt the measurement found, and a candidate
    judged against the sum of the two would be condemned for a chain that does
    not exist.

    What stays is what a magnitude response cannot carry: drive, clipping and
    the noise, all of which depend on LEVEL rather than frequency. The final
    bandpass stays too, because it is band-limiting the noise that was just
    added rather than the signal -- and it uses the measured chain's own
    usable band where the measurement states one.
    """
    if response is not None:
        s = stage_drive(samples, drive, clip_limit)
        s = clip(s, clip_limit)
        s = response.apply(s, fs)
        low, high = response.usable_hz or (PASSBAND_LOW_HZ, PASSBAND_HIGH_HZ)
        noise = bandpass(
            noise_for(s, snr_db, threshold_db=threshold_db, rng=rng),
            fs, low, high)
        return [a + b for a, b in zip(s, noise)]
    s = pre_emphasis(samples, fs)
    s = stage_drive(s, drive, clip_limit)
    s = clip(s, clip_limit)
    s = bandpass(s, fs)
    s = de_emphasis(s, fs)
    s = add_noise(s, snr_db, threshold_db=threshold_db, rng=rng)
    return bandpass(s, fs)


# ---------------------------------------------------------------------------
# VOX keying
#
# The channel model above says what happens to audio once the transmitter is
# on. This says WHEN it is on, which on a VOX-keyed link is most of the cost.
#
# A PTT line keys instantly and stops instantly. VOX does neither: it waits to
# be convinced there is audio, and it holds on afterwards in case there is
# more. Both ends of that are paid on every single transmission, and at
# 1200 bps a second of hang is 150 bytes of airtime spent on nothing.
#
# The hang time is the most valuable knob on the radio. Between a short and a
# long setting the channel's whole capacity roughly halves, which is a larger
# effect than anything achievable in software -- so the rig has to be able to
# measure it rather than assume it.
# ---------------------------------------------------------------------------

# Rough starting points, to be replaced by bench measurement. These are
# guesses and every number derived from them inherits that.
DEFAULT_VOX_ATTACK_S = 0.10
DEFAULT_VOX_HANG_S = 1.00
DEFAULT_VOX_THRESHOLD = 0.05


class Vox(object):
    """A VOX circuit, modelled as what the far end actually hears.

    Deliberately returns BOTH the audio and the occupancy: the two failures
    this exists to catch are different and a rig that reports only one will
    miss the other. Losing the front of a frame is a decode failure; holding
    the channel afterwards is an airtime failure that decodes perfectly and
    still starves the link.
    """

    def __init__(self, attack_s=DEFAULT_VOX_ATTACK_S,
                 hang_s=DEFAULT_VOX_HANG_S,
                 threshold=DEFAULT_VOX_THRESHOLD):
        self.attack_s = attack_s
        self.hang_s = hang_s
        self.threshold = threshold

    def transmit(self, samples, fs):
        """One keying cycle.

        Returns (heard, occupancy_s):
          heard      -- what the receiver gets. Samples before the transmitter
                        opened are GONE, not attenuated. This is why a lead-in
                        tone exists, and the rig should be able to show a frame
                        failing for want of one.
          occupancy_s-- how long the channel was held, attack through hang.
                        This is the number the airtime budget spends.
        """
        n = len(samples)
        if n == 0:
            return [], 0.0

        first = None
        last = None
        for i, x in enumerate(samples):
            if abs(x) >= self.threshold:
                if first is None:
                    first = i
                last = i

        if first is None:
            # Never loud enough to key. Silence out, and -- importantly -- no
            # airtime charged: a transmission that never happened is free, and
            # a rig that charged for it would hide the failure.
            return [0.0] * n, 0.0

        open_at = first + int(round(self.attack_s * fs))
        heard = [0.0 if i < open_at else x for i, x in enumerate(samples)]

        # Held from the moment it opens until hang expires after the last
        # audio. Anything the modem sent before open_at was still played into
        # a transmitter that was off, so it costs nothing and buys nothing.
        held_until = last + int(round(self.hang_s * fs))
        occupancy_s = max(0, held_until - open_at) / float(fs)
        return heard, occupancy_s

    def lost_lead_s(self, fs):
        """How much audio the front of a burst must waste to survive.

        A lead-in shorter than this loses real data; longer is pure airtime.
        Sizing it is the first thing the bench should settle.
        """
        return self.attack_s
