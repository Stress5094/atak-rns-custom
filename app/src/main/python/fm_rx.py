"""The receive path: captured audio in, usable payloads out.

PCM -> levels -> de-emphasis -> demodulator -> unscramble/unframe -> health.

WHY THIS IS ITS OWN MODULE. The path this replaces was a single 70-line
function inside the 1045-line fm_interface.py, and it carried its own private
copy of the framing -- its own `_LEN_PREFIX`, its own length check, its own
capacity arithmetic -- alongside the transmit side's separate copy of the same
thing. The two agreeing was a matter of nobody editing one of them. Here the
framing is fm_framing's, the operating point is fm_config's, and this module is
only the ORDER the stages run in.

THE ORDER IS LOAD-BEARING, and every stage below is placed where it is for a
measured reason rather than for tidiness. In particular levels are measured
BEFORE de-emphasis and the modem instance outlives the chunks. Both have cost
real bench time when done the other way round; see the comments at each stage.

Deliberately free of RNS and Android imports, with the clock injected through
the health object, so the whole path is testable on a desktop against the real
libcodec2. The only thing a phone adds is where the bytes come from.
"""

import fm_airlog
import fm_burst
import fm_carrier
import fm_framing
import fm_health


class Deemphasis(object):
    """A one-pole low-pass correcting a TX/RX pre-emphasis mismatch, carried
    across chunks.

    NOT OPTIONAL ON EVERY CHAIN, and this is the most expensive thing Phase 0
    measured. With the K1 PRO receiving, codec2's FSK estimator would not reach
    even trial sync against the untreated tilt at ANY normalisation peak from
    0.1 to 0.99: 3 of 48 copies decoded without this filter, 34 of 40 with it.
    The tones were exactly on frequency the whole time -- a goertzel confirmed
    0 Hz shift on all four -- and the top tone simply carried ~70% more energy
    than the bottom one, which is the signature of FM pre-emphasis applied on
    one side without matching de-emphasis on the other.

    IT IS A PROPERTY OF THE CABLE, NOT THE RADIO PAIRING. The record first
    attributed the tilt to which radio transmitted; swapping in a second
    Digirig with both radios fixed took the same direction to 20/20 unfiltered.
    So the time constant comes from fm_config's Chain, via
    LinkConfig.deemphasis_us(), and a chain that needs none says so with a zero.

    THE STATE PERSISTS ACROSS CHUNKS, which is the whole reason this is a class
    and not the bench's plain function. tools/fm_bench_audio.deemphasis() resets
    its accumulator per call, which is correct there because it is handed one
    complete captured burst. A live receive path is handed a continuous stream
    in arbitrary chunks, and resetting per chunk puts a step discontinuity at
    every chunk boundary -- an impulse straight into the filter the demodulator
    is trying to track, several times a second, for free.

    The value is insensitive over 50-900 us, so this is far more a
    present/absent decision than a numeric one.
    """

    def __init__(self, tau_us, sample_rate_hz):
        self.tau_us = float(tau_us or 0.0)
        self.sample_rate_hz = sample_rate_hz
        # One-pole coefficient. At tau_us = 0 the filter is bypassed entirely
        # rather than computed with a degenerate coefficient.
        self._a = 0.0
        if self.tau_us > 0.0:
            dt = 1.0 / float(sample_rate_hz)
            self._a = dt / (self.tau_us * 1e-6 + dt)
        self._y = 0.0

    @property
    def active(self):
        return self._a > 0.0

    def __call__(self, samples):
        if not self.active:
            return samples
        a = self._a
        y = self._y
        out = []
        for x in samples:
            y += a * (x - y)
            out.append(y)
        self._y = y
        return out

    def reset(self):
        """Forget the accumulator -- between two unrelated captures, never
        between two chunks of one stream."""
        self._y = 0.0


class FmRx(object):
    """One persistent receive path for one link configuration.

    THE INSTANCE MUST OUTLIVE THE CHUNKS. codec2's demodulator carries its own
    sync state, and a modem rebuilt per chunk of audio throws that away and
    never acquires at all -- it is not a slow link, it is a dead one. The same
    goes for the de-emphasis accumulator above and for the health history. One
    object, fed forever.
    """

    def __init__(self, modem, config, health=None):
        self.modem = modem
        self.config = config
        self.health = health if health is not None else fm_health.LinkHealth()
        self.deemphasis = Deemphasis(config.deemphasis_us(),
                                     modem.sample_rate_hz)
        # Built from the same tone list and symbol rate the modem runs, so the
        # two cannot disagree about what a burst looks like.
        self.carrier = fm_carrier.CarrierDetector(
            config.profile.tone_list(), config.profile.symbol_rate_bd,
            modem.sample_rate_hz)
        # Why each frame was thrown away, newest last, bounded. Kept rather
        # than only counted because the COUNT tells an operator something is
        # wrong and the reason tells them it is the volume knob.
        self.rejections = []
        self.max_rejections = 16
        # One verdict per burst the carrier detector saw -- see fm_airlog. Sized
        # by the live burst, so a short-carrier verdict is judged against the
        # burst actually on the air and not against the ceiling.
        self.airlog = fm_airlog.BurstLedger(config.burst_duration_s())
        self._last_rejected = 0

    # -- the path -----------------------------------------------------------

    def feed_pcm(self, pcm_bytes):
        """One chunk of captured PCM16LE -> the usable payloads it completed.

        Usually returns [] -- a chunk is milliseconds and a burst is seconds,
        so most chunks complete nothing. That is a normal result and not a
        failure.
        """
        return self.feed(fm_burst.from_pcm16(pcm_bytes))

    def feed(self, samples):
        """One chunk of float samples -> the usable payloads it completed."""
        samples = list(samples)

        # STAGE 1: measure, BEFORE any filtering. This is the only place the
        # raw receive audio exists, and the level readout is meant to tell an
        # operator what their radio is actually delivering -- measuring after
        # de-emphasis would report a level the knob cannot be set against,
        # since the filter changes amplitude. tools/fm_bench_audio makes the
        # same ordering explicit for the same reason.
        peak, rms = fm_health.levels_of(samples)
        self.health.note_audio(peak, rms)

        # STAGE 2: correct the chain's tilt, if it has one.
        samples = self.deemphasis(samples)

        # STAGE 2b: the live carrier reading, from the same audio the modem is
        # about to see. Before the modem rather than after because it is the
        # modem's answer that arrives late -- see fm_carrier.
        self.carrier.feed(samples)

        # STAGE 3: the demodulator. Returns whole frames or nothing; codec2's
        # own CRC has already passed for anything it hands back.
        frames = self.modem.feed(samples)

        # STAGE 4: unscramble and unframe. fm_framing owns this -- the receive
        # path having its own copy is how the transmit and receive sides came
        # to be two independent implementations of one format.
        usable = self._unframe(frames)

        # STAGE 5: the per-burst verdict. Last, because it needs the carrier
        # reading, this chunk's levels and what this chunk decoded -- all of
        # which exist only after the stages above.
        self.airlog.observe(
            self.carrier.busy, self.carrier.level, peak, rms,
            decoded=len(usable), rejected=self._last_rejected,
            reason=self.rejections[-1] if self._last_rejected else "",
            strong=self.carrier.strong)
        return usable

    def _unframe(self, frames):
        frame_payload = self.modem.frame_payload_bytes
        usable = []
        rejected = 0
        for f in frames:
            content = fm_framing.unpack(f, frame_payload)
            if content is None:
                # NEVER A SILENT `continue`. A frame reaching here has already
                # passed codec2's CRC, so this is not ordinary noise -- it is a
                # payload corrupted in a way that still satisfied the physical
                # layer, which on this link overwhelmingly means the receive
                # level is wrong: clipping makes the LDPC decoder settle on a
                # wrong-but-CRC-valid codeword.
                #
                # A bare `continue` here is what let a bench spend an entire
                # evening watching a link report "Hearing" while delivering
                # precisely nothing, because the decode counter was fed the RAW
                # frame count. That is the whole reason fm_health has a GARBLED
                # state and the reason these are counted separately from
                # decodes: "something arrived" and "something arrived that we
                # could use" are different claims.
                rejected += 1
                why = fm_framing.describe_rejection(f, frame_payload)
                self.rejections.append(why)
                del self.rejections[:-self.max_rejections]
                continue
            usable.append(content)

        # Usable frames only, deliberately -- see note_decoded's docstring.
        self.health.note_decoded(len(usable))
        self.health.note_rejected(rejected)
        self._last_rejected = rejected
        return usable

    # -- the per-burst log --------------------------------------------------

    def note_tx(self, active):
        """This node started or stopped transmitting. The receive loop is not
        fed while it does, so this is the only way a burst that overlapped our
        own transmit can be told from one the channel lost."""
        self.airlog.note_tx(active)

    def pop_events(self):
        """Burst verdicts finished since the last call, as log lines."""
        return self.airlog.pop_events()

    def tx_within(self, seconds):
        """See fm_airlog.BurstLedger.tx_within."""
        return self.airlog.tx_within(seconds)

    # -- for listen-before-transmit -----------------------------------------

    def channel_busy(self):
        """Is a burst on the air right now?

        ASKS fm_carrier, NOT THE MODEM. `modem.has_sync()` was used here and it
        reads idle for the whole of a burst: codec2's status jumps when a frame
        DECODES, and a frame is 2.6 s of a 2.95 s burst. A node that listened
        while another was mid-burst heard a clear channel and keyed up over it
        (15 of 20 overlaps in the 2026-10-03 two-phone run). Measured, with the
        trace, in the project bench notes.

        MUST NOT BECOME AN ENERGY THRESHOLD. This project's receive audio sits
        at an RMS of roughly 0.65-0.75 with zero real traffic, so no level
        threshold can discriminate; the detector looks at how the energy is
        distributed across the tones instead.
        """
        return self.carrier.busy

    # -- reporting ----------------------------------------------------------

    def snapshot(self):
        """Plain JSON-ready data for get_status()."""
        out = dict(self.health.snapshot())
        out["deemphasis_us"] = self.deemphasis.tau_us
        out["rejection_reasons"] = list(self.rejections)
        out["bursts"] = self.airlog.summary()
        return out

    def summary_lines(self):
        """What to show an operator, most actionable first.

        The level line comes FIRST because it is the one thing on this screen
        they can act on -- the volume knob is the gain stage on an FM audio
        link -- and because a 2026-09-30 session spent setting both radios off
        a stale reading and then measuring a mis-levelled link for a run.
        """
        lines = [self.health.level_line(), self.health.summary()]
        if self.deemphasis.active:
            lines.append("rx de-emphasis: %.0f us (this chain needs it -- "
                         "3/48 copies decoded without it, 34/40 with)"
                         % self.deemphasis.tau_us)
        if self.rejections:
            lines.append("last rejection: %s" % self.rejections[-1])
        return lines
