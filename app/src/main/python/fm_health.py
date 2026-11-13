"""What the FM link is actually doing right now, graded rather than binary.

THE PROBLEM THIS EXISTS FOR is the project bench notes, item 0f, which this
project has flagged since 2026-08-16 and never fixed: "a half-deaf node
looks exactly like a node alone on an empty network". On the FM link that
is worse than on LoRa, for two reasons that compound.

First, there are no acknowledgements on this link at all (see
fm_resend.py), so a sender genuinely cannot tell the difference between
"nobody is there", "my transmissions are not arriving" and "everything is
fine and nobody has said anything". A binary connected/not indicator on
that link is not a simplification, it is a fabrication.

Second, this link's delivery rate swings between roughly 20% and 100% on a
timescale of minutes to tens of minutes for reasons nobody has identified,
and -- the part that matters for a status screen -- SHORT OBSERVATIONS
SYSTEMATICALLY OVERESTIMATE IT. The same configuration measured 4/4 and
24/24 back-to-back and 2/10 spread across an hour. This project has been
fooled by that repeatedly and wrote it down each time. A status screen that
turns a single recent decode into a green light would be making exactly the
error the bench log is a record of, on the user's behalf, in the field.

So this reports what was OBSERVED, over a stated window, and refuses to
convert that into a promise. The states are about the receive path, which
is the half a node can actually observe:

    down      nothing is feeding audio in at all -- the capture path is
              dead, or the bridge service is not running. Not a radio
              problem, and the one state here that is definitely a bug.
    deaf      audio is arriving but it is flat: squelch closed, volume at
              zero, wrong input selected, or the radio is off. Distinct
              from `quiet` because the audio path is provably alive.
    quiet     real audio energy, no decodes -- either nobody is
              transmitting, or they are and it is not getting through.
              THESE TWO ARE NOT DISTINGUISHABLE FROM HERE and the UI must
              not pretend otherwise.
    garbled   the demodulator IS recovering frames, and every one of them
              is unusable -- the payload arrives corrupted past the point
              the framing can read it. Distinct from `quiet` because the
              physical layer is provably working, and distinct from
              `hearing` because nothing reached the mesh. Added
              2026-09-30, when a bench spent an evening reading "Hearing"
              off a link that had delivered precisely nothing: the decode
              counter was fed the RAW frame count, before the framing
              check that was throwing all of them away. Almost always a
              receive LEVEL problem (clipping makes codec2 settle on a
              wrong-but-CRC-valid codeword), which is why the summary
              points at the level.
    hearing   decoded something USABLE within the window. The link works,
              or worked recently. Says nothing about whether the next
              transmission will arrive.

and one independent WARNING that overlays any of them, because it is the
one condition here that is both measurable and directly fixable by the
operator:

    saturated the receive level is pinned at the ceiling. Measured on this
              bench as a real, large effect -- 2/4 at a saturated level
              against 15/16 at any non-saturated one -- and the fix is to
              turn the RECEIVING radio's volume down, not to change
              anything in software. Worth saying out loud because the
              symptom (a strong signal!) reads as healthy.

Deliberately free of RNS/Android imports, with the clock injected, so it is
testable on a desktop. Same shape as reliable.py, tx_policy.py and
fm_resend.py.
"""

import collections
import time

import fm_config
import fm_resend


# The level constants come from fm_config, which owns the operating point.
#
# THEY USED TO BE REDEFINED HERE, with their own copies of the same four
# numbers. That is exactly the duplication the rebuild exists to remove: two
# modules each holding "what a good receive level is" is two places to update
# and one silent disagreement away from a status screen that grades a level the
# transport does not actually want. Imported instead, with the RMS derivation
# below still living here because grading is this module's job.
SILENT_PEAK = fm_config.SILENT_PEAK
SATURATED_PEAK = fm_config.SATURATED_PEAK
TARGET_PEAK_LOW = fm_config.TARGET_PEAK_LOW
TARGET_PEAK_HIGH = fm_config.TARGET_PEAK_HIGH

# AND THE BAND IS AN AIMING POINT, NOT A DIAGNOSIS. 2026-10-02 measured one
# direction running 100% per copy at an RMS of 0.18 AND at 0.55 -- both outside
# the band -- while the other direction swung 0-100% at a fixed level. So a
# reading inside the band does not mean the link is working and a reading
# outside it does not explain a failure. It is a safe operating region, which
# is a real and useful thing to point a volume knob at, and that is all. Every
# wording below is chosen to say "aim here" rather than "this is why".

# The same band expressed as RMS, which is what the readout actually
# grades, because a PEAK cannot be trusted for this -- see peak_of()'s own
# warning, and the 2026-09-30 bench session that proved it the hard way: a
# Pixel turned almost all the way DOWN still read "1.00 TOO HOT" off a
# single key-up transient, while its sustained level was simultaneously
# below SILENT_PEAK. One click pinned the reading and no amount of volume
# adjustment could move it.
#
# DERIVED, not separately measured: these bursts are constant-envelope
# 4-FSK, so rms = peak / sqrt(2) for the tone itself. Stated as a
# derivation rather than a bench number so nobody later mistakes it for
# one -- if a measured RMS band ever exists, replace these.
TARGET_RMS_LOW = TARGET_PEAK_LOW / (2 ** 0.5)
TARGET_RMS_HIGH = TARGET_PEAK_HIGH / (2 ** 0.5)

# A chunk this quiet in RMS carries no real signal, whatever a stray
# transient did to its peak.
SILENT_RMS = SILENT_PEAK / (2 ** 0.5)

# How long a level reading stays on screen before it is treated as stale.
# The instantaneous peak is the last chunk's, which between bursts is the
# noise floor -- useless for setting a level, since the thing being set is
# how loud a BURST arrives. So the reading persists between bursts, up to
# this long.
#
# NOT a rolling maximum, which is what this was for about ten minutes on
# 2026-09-30 before the bench caught it: a max-hold pinned at 1.00 and then
# ignored every subsequent burst for two minutes, so an operator turning
# the volume DOWN -- the whole reason they were looking at it -- saw no
# response at all. The reading is the most recent burst's peak instead, so
# every transmission moves it, in either direction.
DEFAULT_PEAK_WINDOW_S = 120.0

# Silence this long ends a burst, so the next loud chunk starts a fresh
# reading rather than extending the last one. Comfortably shorter than the
# gap between transmissions and longer than any within-burst dip.
DEFAULT_BURST_GAP_S = 2.0

# How long a decode "counts for", DERIVED from the sender's whole resend
# schedule rather than picked, so that a node exchanging traffic at the slowest
# cadence fm_resend produces cannot flicker between `hearing` and `quiet`
# between attempts.
#
# The previous value was a flat 300 s "sized against fm_resend's OLD 240s
# interval plus a margin", left unchanged after that interval dropped to 60 s.
# It happened to still be wide enough, which is luck rather than design: the
# same two-constants-drifting-apart failure that mis-sized the reassembly
# window (see rns_bridge._reassembly_window_s) and with the same shape of
# consequence, a status screen going quiet mid-schedule and telling an operator
# the link had died when it had not.
#
# A floor of 300 s is kept so this never gets SHORTER than the figure the bench
# sessions were read against; the span term is what makes it grow if the
# schedule does.
DEFAULT_WINDOW_S = max(300.0, 1.25 * fm_resend.span_s())

# How long without any audio chunk before the capture path is presumed
# dead. Kotlin's AudioRecord loop delivers continuously, so a gap this
# long is a real fault rather than a lull.
DEFAULT_AUDIO_TIMEOUT_S = 10.0

DOWN = "down"
DEAF = "deaf"
QUIET = "quiet"
GARBLED = "garbled"
HEARING = "hearing"


class LinkHealth(object):
    """Observations from the receive path, summarised honestly.

    Fed from fm_interface.feed_audio() on the audio thread and read from
    get_status() on the UI thread. Both are cheap and neither blocks: the
    counters are plain ints and the history is a bounded deque, so no lock
    is taken -- a status read that races an audio chunk sees the previous
    value, which is not worth a lock on a screen that repaints anyway.
    """

    def __init__(self, window_s=DEFAULT_WINDOW_S,
                 audio_timeout_s=DEFAULT_AUDIO_TIMEOUT_S,
                 peak_window_s=DEFAULT_PEAK_WINDOW_S,
                 burst_gap_s=DEFAULT_BURST_GAP_S,
                 clock=time.monotonic):
        self.window_s = window_s
        self.audio_timeout_s = audio_timeout_s
        self.peak_window_s = peak_window_s
        self.burst_gap_s = burst_gap_s
        self._clock = clock
        self._last_audio_at = None
        self._last_decode_at = None
        self._recent_peak = 0.0
        self._decodes = collections.deque()   # timestamps, pruned to window
        # Frames the demodulator recovered that the framing then threw
        # away. Tracked separately and never folded into _decodes: the
        # whole point is that "something arrived" and "something arrived
        # that we could use" are different claims.
        self._rejects = collections.deque()
        self._total_rejects = 0
        # The loudest chunk of the burst currently arriving, or of the last
        # one that did. Two scalars rather than any history: note_audio()
        # runs on the audio thread for every chunk, and this costs one
        # compare.
        self._burst_peak = 0.0
        self._burst_rms = 0.0
        self._recent_rms = 0.0
        self._last_loud_at = None
        self._chunks = 0
        self._total_decodes = 0
        self._saturated_chunks = 0

    def note_audio(self, peak, rms=None):
        """One chunk of captured audio arrived, with this peak level and
        (optionally) its RMS.

        `rms` is what the level readout grades. It is optional only so that
        callers which genuinely have nothing but a peak still work; without
        it the readout falls back to the peak and inherits all of peak's
        unreliability (see TARGET_RMS_LOW).
        """
        now = self._clock()
        self._last_audio_at = now
        self._chunks += 1
        self._recent_peak = peak
        if rms is None:
            rms = peak
        self._recent_rms = rms
        if peak >= SATURATED_PEAK:
            self._saturated_chunks += 1
        # Gated on RMS, not peak: a lone click is not a burst, and letting
        # one start (or extend) a reading is exactly how a nearly-silent
        # link came to report "TOO HOT".
        if rms >= SILENT_RMS:
            started_new_burst = (
                self._last_loud_at is None
                or (now - self._last_loud_at) > self.burst_gap_s)
            if started_new_burst:
                self._burst_peak = 0.0
                self._burst_rms = 0.0
            self._last_loud_at = now
            if peak > self._burst_peak:
                self._burst_peak = peak
            if rms > self._burst_rms:
                self._burst_rms = rms

    def _fresh(self):
        """Is the stored burst reading still current?"""
        return (self._last_loud_at is not None
                and (self._clock() - self._last_loud_at) <= self.peak_window_s)

    def recent_level(self):
        """The sustained (RMS) level of the burst currently arriving, or of
        the most recent one, going stale after peak_window_s.

        THIS is what a radio's volume gets set against. Not the
        instantaneous value, which between bursts is the noise floor; not a
        rolling maximum, which cannot show a level coming DOWN (see
        DEFAULT_PEAK_WINDOW_S); and not a peak, which one key-up click
        pins to full scale regardless of how the real signal sits (see
        TARGET_RMS_LOW).
        """
        return self._burst_rms if self._fresh() else 0.0

    def recent_peak(self):
        """The loudest single sample of the same burst. Reported alongside
        recent_level() because it is the honest way to spot real clipping,
        but never used on its own to grade the level."""
        return self._burst_peak if self._fresh() else 0.0

    def level_state(self):
        """Where the receive level sits relative to the target band:
        "none", "low", "good", "hot" or "saturated"."""
        rms = self.recent_level()
        if rms < SILENT_RMS:
            return "none"
        if rms > TARGET_RMS_HIGH:
            # Sustained-loud is "hot"; only a level that is ALSO hitting
            # the ceiling has actually run out of headroom.
            return "saturated" if self.recent_peak() >= SATURATED_PEAK \
                else "hot"
        if rms < TARGET_RMS_LOW:
            return "low"
        return "good"

    def level_line(self):
        """The receive level as a number AND which way to turn the knob.

        A number alone would not be actionable (nothing on the screen says
        what a good one looks like), and a verdict alone would not let the
        operator see the level respond as they adjust it -- which is the
        whole point: the 2026-09-30 session overshot the target twice
        because the only feedback was a warning that appeared and vanished.
        """
        rms = self.recent_level()
        state = self.level_state()
        advice = {
            "none": "nothing heard yet -- no level to set against",
            "saturated": "TOO HOT, and clipping -- turn the receiving "
                         "radio down",
            "hot": "a little hot -- ease the receiving radio down",
            "low": "too low -- turn the receiving radio up",
            "good": "in band",
        }[state]
        if state == "none":
            return "rx level: %s" % advice
        # The AGE matters as much as the number. This reading persists
        # between bursts, so seconds after turning a knob it still shows
        # the level from BEFORE the adjustment -- which on 2026-09-30 had
        # an operator set both radios by a stale "in band" reading while
        # they were really well above it, and the bench then spent a run
        # measuring a mis-set link. Saying how old it is makes "wait for
        # the next burst" obvious instead of something you have to know.
        return "rx level %.2f as of %s (aim %.2f-%.2f; peak %.2f) -- %s" % (
            rms, self._age_phrase(), TARGET_RMS_LOW, TARGET_RMS_HIGH,
            self.recent_peak(), advice)

    def _age_phrase(self):
        age = self.level_age_s()
        if age is None:
            return "unknown"
        if age < 2.0:
            return "now"
        return "%.0fs ago" % age

    def level_age_s(self):
        """How long since the burst this reading describes, or None if
        nothing has been heard."""
        if self._last_loud_at is None:
            return None
        return self._clock() - self._last_loud_at

    def note_decoded(self, count=1):
        """`count` frames came out of the demodulator AND survived framing
        -- i.e. were actually handed onward.

        Usable frames only, deliberately. This used to be fed the raw
        demodulator count, which meant a link delivering nothing at all
        still displayed "Hearing"; see GARBLED above.
        """
        if count <= 0:
            return
        now = self._clock()
        self._last_decode_at = now
        self._total_decodes += count
        for _ in range(count):
            self._decodes.append(now)
        self._prune(now)

    def note_rejected(self, count=1):
        """`count` frames came out of the demodulator and were then thrown
        away by the framing as unreadable."""
        if count <= 0:
            return
        now = self._clock()
        self._total_rejects += count
        for _ in range(count):
            self._rejects.append(now)
        self._prune(now)

    def _prune(self, now):
        cutoff = now - self.window_s
        while self._decodes and self._decodes[0] < cutoff:
            self._decodes.popleft()
        while self._rejects and self._rejects[0] < cutoff:
            self._rejects.popleft()

    def state(self):
        now = self._clock()
        self._prune(now)
        if self._last_audio_at is None or (
                now - self._last_audio_at) > self.audio_timeout_s:
            return DOWN
        if self._decodes:
            return HEARING
        # Before the level checks: a demodulator recovering frames is
        # proof the physical layer works, whatever the current chunk's
        # level happens to be between bursts.
        if self._rejects:
            return GARBLED
        if self._recent_peak < SILENT_PEAK:
            return DEAF
        return QUIET

    def saturated(self):
        """True when the most recent audio was at the ceiling. Reported
        alongside the state rather than as one, since a saturated link can
        still be decoding -- just worse than it would otherwise."""
        return self._recent_peak >= SATURATED_PEAK

    def summary(self):
        """One line for a human, phrased so it cannot be read as a promise.

        Every wording here is deliberate. "decoded N in the last M min" is
        a count over a stated window; "link is good" would be a claim about
        the next transmission, which nothing here can support.
        """
        state = self.state()
        mins = self.window_s / 60.0
        if state == DOWN:
            return "no audio reaching the decoder -- capture path is down"
        if state == DEAF:
            return ("audio path alive but silent -- check the radio is on, "
                    "unsquelched, and its volume is up")
        if state == QUIET:
            base = ("hearing audio, nothing decoded in the last %.0f min -- "
                    "either nobody is transmitting or it is not getting "
                    "through, which this link cannot tell apart" % mins)
        elif state == GARBLED:
            base = ("recovered %d frame(s) in the last %.0f min but NONE "
                    "were usable -- the signal is arriving and decoding, "
                    "and the payload is corrupt, which is usually the "
                    "receive level" % (len(self._rejects), mins))
        else:
            base = ("decoded %d frame(s) in the last %.0f min"
                    % (len(self._decodes), mins))
            if self._rejects:
                base += (" (%d more recovered but unusable)"
                         % len(self._rejects))
        if self.saturated():
            base += ("; receive level is SATURATED -- turn the receiving "
                     "radio's volume down")
        return base

    def snapshot(self):
        """For get_status(). Plain JSON-ready data."""
        now = self._clock()
        self._prune(now)
        return {
            "state": self.state(),
            "summary": self.summary(),
            "saturated": self.saturated(),
            "peak": round(self._recent_peak, 3),
            "peak_recent": round(self.recent_peak(), 3),
            "level_recent": round(self.recent_level(), 3),
            "level_age_s": (None if self.level_age_s() is None
                            else round(self.level_age_s(), 1)),
            "peak_window_s": self.peak_window_s,
            "level_state": self.level_state(),
            "level_line": self.level_line(),
            "decodes_in_window": len(self._decodes),
            "rejects_in_window": len(self._rejects),
            "rejects_total": self._total_rejects,
            "window_s": self.window_s,
            "decodes_total": self._total_decodes,
            "chunks": self._chunks,
            "last_decode_age_s": (
                None if self._last_decode_at is None
                else round(now - self._last_decode_at, 1)),
        }


def levels_of(samples):
    """(peak, rms) for a block of floats in -1.0..1.0, in one pass.

    Both, in one loop, because fm_interface calls this on the audio thread
    for every chunk and two separate passes would double that cost for no
    reason. Peak answers "did anything clip"; RMS answers "how loud is the
    signal actually sitting", and only the second is a level worth setting
    a radio against -- see peak_of()'s own warning below.
    """
    best = 0.0
    total = 0.0
    n = 0
    for s in samples:
        a = -s if s < 0.0 else s
        if a > best:
            best = a
        total += s * s
        n += 1
    if n == 0:
        return 0.0, 0.0
    return best, (total / n) ** 0.5


def peak_of(samples):
    """Peak absolute level of a block of floats in -1.0..1.0.

    Its own function because fm_interface calls it on the audio thread for
    every chunk, and because a peak is NOT proof of anything on its own --
    this project has twice mistaken a single loud transient (a PTT click)
    for a saturated signal. Callers wanting "is this clipping" should look
    at how long the level stays there, which is what the saturated-chunk
    count in LinkHealth is for.
    """
    best = 0.0
    for s in samples:
        a = -s if s < 0.0 else s
        if a > best:
            best = a
    return best
