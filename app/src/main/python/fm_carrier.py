"""Is somebody transmitting RIGHT NOW? A live detector for listen-before-transmit.

WHY THIS EXISTS. listen-before-transmit used to ask codec2's rx-status bitmask
(`Modem.has_sync`). Fed a real 2.95 s burst in 0.2 s chunks, that reading is
idle for the WHOLE burst and goes busy only after it has ended -- the frame is
2.6 s long and the status jumps when the frame DECODES, then holds for ~2.6 s.
(Measured 2026-10-06; it was validated in September against bursts a fraction of
a second long, where "after the burst" and "during the burst" were nearly the
same moment.) A node that checked the channel while another was mid-burst
therefore read "clear" and keyed up over it. That is the 15-of-20 overlaps in
the project bench notes that began 1.1-3.4 s INTO a burst already in progress.

WHAT IT MEASURES. In every symbol of 4FSK exactly one of the four tones is
present; in noise the energy is spread across them. For each symbol-length block
this takes the share of the tone energy held by the strongest tone, and averages
that over the last ~0.2 s (about 80 symbols). On a real over-the-air capture of
a phone's burst (results/2026-10-03_phone_tx_first_light):

    idle noise     0.577 +/- 0.016   (955 windows, worst 0.632)
    burst          0.968 - 0.990

so the two are separated by ~25 standard deviations, from a statistic that
needs no calibration and no knowledge of where the symbols start.

NOT AN ENERGY THRESHOLD, and that is the reason it can work at all. This
project's receive audio sits at an RMS of 0.65-0.75 with no traffic (a raw
discriminator tap), and the same capture reads 0.26 idle against 0.29 in a
burst, so no level threshold separates them. The SHAPE of the spectrum does.

WEAK SIGNALS. Against white noise the statistic tracks decodability, which is
the property that matters: a listener that hears less than the modem can decode
would let through exactly the bursts that matter.

    SNR       statistic     codec2 decodes (128 B code)
    +10 dB      0.97         yes
      0 dB      0.78         yes
     -3 dB      0.66-0.72    no
     -6 dB      0.59         no

and noise tilted toward the top of the band, as a discriminator's is, moves the
idle reading from 0.52 to 0.55 -- still far under the threshold.

LATENCY, which is the part that cannot be shaved. The mean crosses the threshold
once about a third of the window is signal (~70 ms), and the caller reads it once
per audio chunk, so a burst that began less than ~0.3 s before the check is not
yet visible. That is the unavoidable regime: two nodes that start within a
fraction of a second of each other cannot hear one another.

FREE OF NATIVE CODE. Pure Python, no libcodec2, so it is testable anywhere and
cannot disagree with the modem about the sample rate: it is built from the same
tone list and symbol rate as the modem is.

UPDATE 2026-10-11 -- READ THIS BEFORE TRUSTING THE NUMBERS ABOVE. The window is
now 1.0 s (was 0.2), the busy threshold sits 0.10 of the way from a TRACKED idle
floor to all-signal (was a fixed 0.66), and the hold is 0.5 s (was 1.0). The
0.97/0.577 figures and the SNR table above describe the 0.2 s statistic; the
mean is unchanged but its spread is ~2.2x smaller, which is what allows the
lower threshold. Evidence is 40 recorded over-the-air bursts (laptop <-> OnePlus,
one desk chain) with graded white noise added afterwards, old vs new:

    added noise    flagged (old -> new)   body read clear (old -> new)
    none           40 -> 40               4.3% -> 0.6%
    0 dB           31 -> 40               33%  -> 6.8%
    -3 dB          18 -> 31               68%  -> 31%
    -6 dB           0 -> 19               100% -> 68%

False busy on noisy idle gaps: 0.00% in every row. THE COST, measured: the delay
to the first busy reading. Clean it is unchanged at a median 0.31 s but its worst
case is 0.84 s (was 0.42); at 0 dB the median is 0.50 s (was 0.31) and the worst
1.64 s (was 0.49). A slower rise is more time in which a simultaneous start
cannot be heard. NOT measured on a phone chain (no raw phone audio exists; the
only phone evidence is the idle floors logged on 2026-10-06), NOT run on a
radio, and white noise added after the radio says nothing about FM click noise.
The tracked floor exists because chains differ (0.49 / 0.52 / 0.58 idle); for the
first FLOOR_MIN_S + FLOOR_UPDATE_S of a run it assumes white noise, and a chain
that idles above that can read busy until it has learned (measured: discriminator-
tilted noise, up to ~6 s). `strong` (the old 0.66 mark) is what fm_airlog times
carriers by. Modem73's coded lead-in tone flags 40/40 down to -6 dB too, but it
adds 0.85 s to every burst and covers only its first second; see
the project bench notes.
"""

import math
from collections import deque
from operator import mul

# How much audio the busy decision averages over. 1.0 s is 400 symbols at 400 Bd
# (it WAS 0.2 s, 80 symbols). Five times the audio makes the idle statistic
# roughly sqrt(5) = 2.2 times steadier, which is what lets the threshold sit
# close to the noise floor without false alarms. The cost is a slower fall: a
# burst's tail is remembered for up to this long.
#
# WHY IT CHANGED (2026-10-11). Replayed over 40 real over-the-air bursts with
# graded added noise, the 0.2 s window flagged 31/40 at 0 dB, 18/40 at -3 dB and
# 0/40 at -6 dB, and read the burst BODY as clear 33% / 68% / 100% of the time. A
# 1.0 s window with the threshold 0.10 of the way up flagged 40/40, 30/40, 16/40
# and left 6.5% / 31% / 70% clear. Strong signals: 4.3% of the body clear before,
# 0.3% after. ONE desk chain, white noise added after the radio -- re-check on
# the phones. Modem73's coded lead-in tone flags 40/40 down to -6 dB, but it
# costs 0.85 s of airtime per burst and covers only the first second.
WINDOW_S = 1.0

# How far up from the noise floor to the "all signal" ceiling the BUSY threshold
# sits: 0.10 is ~0.57 for 4FSK against a white-noise floor of 0.52. That is only
# safe with a window this long and a floor that is TRACKED (below), because real
# idle floors differ by chain (0.49 Pixel, 0.52 OnePlus, 0.58 desktop).
# A FRACTION OF THE RANGE IS NOT A SAFE MARGIN ON ITS OWN: with 2 tones the range
# above the floor is 0.25, so 0.10 of it is 0.025 -- 3.5 idle spreads, and white
# noise crossed it within 20 s. The margin is therefore also floored at
# MARGIN_SPREADS idle spreads (below), which is what protects the 2-tone case.
# History: 0.375 (0.70) missed a third of mid-burst checks on the K1 PRO / Mini
# chain; 0.29 (0.66) with a 0.2 s window fixed most of it.
THRESHOLD_FRACTION = 0.10

# The idle statistic's spread falls as 1/sqrt(symbols averaged). Measured at 400
# symbols (1.0 s at 400 Bd): sd 0.0057-0.0074 on white AND discriminator-tilted
# noise, 4 tones and 2 tones alike. So the spread is scaled:
# sd ~= SPREAD_AT_400 * sqrt(400 / symbols). The margin never goes under
# MARGIN_SPREADS of them (6 sd: 0.045 at 400 symbols, ~2 in 10^9 per reading for
# a Gaussian; the 60 s runs above peaked at 4 sd).
SPREAD_AT_400 = 0.0075
MARGIN_SPREADS = 6.0

# Where "a burst is really here" sits, as a fraction of the way from the floor to
# all-signal. This is the OLD busy threshold (0.29 -> 0.66). It no longer decides
# busy; it is what fm_rx/fm_airlog use to measure how long a carrier lasted, so a
# carrier episode is still about a burst and not about the window's memory of it.
STRONG_FRACTION = 0.29

# After the statistic last reached the threshold, the channel stays BUSY this
# long. It is what makes a mid-burst dip below the threshold -- 5% of mid-burst
# samples read under 0.614 on the 2026-10-08 chain -- not read as "clear".
# Counted in audio SYMBOLS, not clock time, because this module never sees a
# clock and the receive loop does not feed it while this phone transmits.
HOLD_S = 0.5

# The longest the reading can take to go idle after a burst ends: the window it
# averages over plus the hold. Added to the listen-before-transmit wait bound.
RELEASE_S = WINDOW_S + HOLD_S

# THE FLOOR IS TRACKED, not assumed. The idle statistic's level is a property of
# the chain (measured idle: Pixel 0.49, OnePlus 0.52, desktop 0.58), and a
# threshold 0.05 above the wrong floor is either always busy or deaf. The floor
# is a low percentile of the readings over the last FLOOR_SPAN_S -- a percentile
# of EVERYTHING, not "the readings that were not busy", because the second rule
# deadlocks: a floor set too low makes idle read busy, busy readings are never
# used, and the floor never rises. Bursts are a minority of the time (17% in the
# two-phone run), so a low percentile is the idle floor.
FLOOR_SPAN_S = 120.0
FLOOR_PERCENTILE = 0.20
FLOOR_MIN_S = 5.0        # trust the tracked value only after this much audio
FLOOR_SAMPLE_S = 0.2     # one reading kept per this much audio
FLOOR_UPDATE_S = 1.0     # recompute this often (sorting ~600 numbers: trivial)
# The tracked floor is kept within this far of the white-noise value. A channel
# that is busy most of the time drags the percentile up; the clamp turns that
# into a threshold no worse than the old 0.66-ish one instead of "never busy".
FLOOR_BELOW = 0.06
FLOOR_ABOVE = 0.10


def noise_baseline(tones):
    """The statistic's expected value on white noise: E[max/sum] over M equal
    exponentials, which is H(M)/M. 0.52 for 4 tones, 0.75 for 2."""
    return sum(1.0 / k for k in range(1, tones + 1)) / tones


def idle_spread(window_symbols):
    """The idle statistic's standard deviation for a window of this many symbols."""
    return SPREAD_AT_400 * math.sqrt(400.0 / max(1, window_symbols))


def default_threshold(tones, floor=None, window_symbols=400):
    """The busy threshold for a given idle floor (the white-noise value if none)."""
    if floor is None:
        floor = noise_baseline(tones)
    margin = max(THRESHOLD_FRACTION * (1.0 - floor),
                 MARGIN_SPREADS * idle_spread(window_symbols))
    return floor + margin


def strong_threshold(tones, floor=None):
    """Where the statistic says a burst is really here, not just remembered."""
    if floor is None:
        floor = noise_baseline(tones)
    return floor + STRONG_FRACTION * (1.0 - floor)


class CarrierDetector(object):
    """Feeds on audio, answers `busy`. One instance for one link configuration,
    fed forever, like the demodulator it sits beside."""

    def __init__(self, tones_hz, symbol_rate_bd, sample_rate_hz,
                 window_s=WINDOW_S, threshold=None, hold_s=HOLD_S):
        tones_hz = list(tones_hz)
        if len(tones_hz) < 2:
            raise ValueError("carrier detection needs at least two tones")
        if sample_rate_hz % symbol_rate_bd != 0:
            raise ValueError(
                "%d Hz is not a whole number of samples per symbol at %d Bd"
                % (sample_rate_hz, symbol_rate_bd))
        self.tones_hz = tones_hz
        self.block = sample_rate_hz // symbol_rate_bd
        # A threshold passed in is FIXED (tests, experiments). None means "track
        # the chain's own idle floor", which is what production runs.
        self._fixed_threshold = threshold
        self._baseline = noise_baseline(len(tones_hz))   # white noise; also silence
        self._idle_floor = self._baseline
        self._floor_hist = deque(maxlen=int(FLOOR_SPAN_S / FLOOR_SAMPLE_S))
        self._floor_every = max(1, int(round(FLOOR_SAMPLE_S * symbol_rate_bd)))
        self._floor_update_every = max(1, int(FLOOR_UPDATE_S / FLOOR_SAMPLE_S))
        self._floor_tick = 0
        self._floor_new = 0

        # One cos/sin table per tone over one symbol. Correlating against these
        # is a tone's energy in the block regardless of the phase it started at
        # (cos^2 + sin^2), which is why no symbol alignment is needed.
        step = 2.0 * math.pi / sample_rate_hz
        self._tables = [
            ([math.cos(step * f * n) for n in range(self.block)],
             [math.sin(step * f * n) for n in range(self.block)])
            for f in tones_hz]

        self._len = max(8, int(round(window_s * symbol_rate_bd)))
        self._hold_len = int(round(hold_s * symbol_rate_bd))
        self._since_hit = self._hold_len        # idle until a burst is seen
        self._window = deque()
        self._sum = 0.0
        self._rest = []

    @property
    def window_symbols(self):
        return self._len

    def feed(self, samples):
        """Take a chunk of any size. Whatever does not fill a symbol-length
        block waits for the next call, so the answer does not depend on how the
        audio happened to be chunked."""
        buf = self._rest + list(samples)
        n = self.block
        end = len(buf) - len(buf) % n
        for i in range(0, end, n):
            self._push(self._share(buf[i:i + n]))
        self._rest = buf[end:]

    def _share(self, blk):
        best = total = 0.0
        for cos_t, sin_t in self._tables:
            c = sum(map(mul, blk, cos_t))
            s = sum(map(mul, blk, sin_t))
            e = c * c + s * s
            total += e
            if e > best:
                best = e
        # A block of exact silence (a closed squelch is digital zero) carries no
        # information either way and must not read as signal.
        return best / total if total > 0.0 else self._baseline

    def _push(self, share):
        self._window.append(share)
        self._sum += share
        if len(self._window) > self._len:
            self._sum -= self._window.popleft()
        self._track_floor()
        if self.level >= self.threshold:
            self._since_hit = 0
        elif self._since_hit < self._hold_len:
            self._since_hit += 1

    def _track_floor(self):
        """Keep one reading per FLOOR_SAMPLE_S and refresh the floor from them.
        Only once the window is full, so a half-filled average is never taken for
        the channel's idle level."""
        if len(self._window) < self._len:
            return
        self._floor_tick += 1
        if self._floor_tick < self._floor_every:
            return
        self._floor_tick = 0
        self._floor_hist.append(self._sum / len(self._window))
        self._floor_new += 1
        if (self._floor_new >= self._floor_update_every
                and len(self._floor_hist) * FLOOR_SAMPLE_S >= FLOOR_MIN_S):
            self._floor_new = 0
            ordered = sorted(self._floor_hist)
            value = ordered[int(FLOOR_PERCENTILE * (len(ordered) - 1))]
            self._idle_floor = min(self._baseline + FLOOR_ABOVE,
                                   max(self._baseline - FLOOR_BELOW, value))

    @property
    def floor(self):
        """The idle level the thresholds are measured from: tracked per chain."""
        return self._idle_floor

    @property
    def threshold(self):
        """Busy at or above this."""
        if self._fixed_threshold is not None:
            return self._fixed_threshold
        return default_threshold(len(self.tones_hz), self._idle_floor, self._len)

    @property
    def strong(self):
        """Over the 'a burst is really here' mark NOW (not merely remembered)."""
        return self.level >= strong_threshold(len(self.tones_hz), self._idle_floor)

    @property
    def level(self):
        """The current statistic, or the noise floor until half a window of
        audio has been seen. For a log line and for tests."""
        if len(self._window) < self._len // 2:
            return self._baseline
        return self._sum / len(self._window)

    @property
    def busy(self):
        """Over the threshold now, or was within the last HOLD_S of audio."""
        return self.level >= self.threshold or self._since_hit < self._hold_len

    def reset(self):
        self._window.clear()
        self._sum = 0.0
        self._rest = []
        self._since_hit = self._hold_len
