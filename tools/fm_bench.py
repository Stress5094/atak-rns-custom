"""Measure the REBUILT FM transport over real radios, and count what arrives.

This is the instrument the rebuild's Phase 0 needs: the whole 250 Bd /
750 Hz / 30-byte-code architecture hangs off one cable's analog envelope
follower, and nothing in the new `fm_config` / `fm_framing` / `fm_modem` /
`fm_burst` has ever been on the air. It drives those four modules -- not the
deleted ones -- through `fm_bench_audio`, which is the proven device and
keying layer recovered from the old `otair_test.py`.

    python3 tools/fm_bench.py --devices
    python3 tools/fm_bench.py --loopback                      # no radio
    python3 tools/fm_bench.py --run --trials 10 --tx 5 --rx 6
    python3 tools/fm_bench.py --sweep 250,320,400,500 --rounds 6 --tx 5 --rx 6
    python3 tools/fm_bench.py --sweep 250,500 --both-directions --rounds 10 \
            --gap 10 --tx 0 --rx 1        # a direction comparison, properly

    # The burst-duration comparison the 2026-10-02 operating-point switch
    # left owing. Three cells because the code and the copy count moved
    # TOGETHER, so two cannot attribute a weak result to either.
    python3 tools/fm_bench.py --tx 0 --rx 1 --rounds 30 --gap 10 \
            --cell code=H_256_768_22,repeats=2 \
            --cell code=H_1024_2048_4f,repeats=1 \
            --cell code=H_1024_2048_4f,repeats=2 \
            --csv results/burst_ab.csv --note "both knobs at 11 o'clock" \
            --setting rx_volume=11oclock --setting squelch=open

    # A SURVIVAL CURVE, with no radio at all: the real modules through
    # fm_channel's modelled handheld, at four signal levels, interleaved.
    python3 tools/fm_bench.py --channel --response pixel-rx-2026-09-30 \
            --fs 8000 --rs 250 --first-tone 1500 --spacing 250 --rounds 20 \
            --cell snr_db=-6 --cell snr_db=-3 --cell snr_db=0 \
            --cell snr_db=3 --csv results/survival.csv

WHY --channel EXISTS, AND WHAT IT IS FOR. Two radios on a desk cannot be made
weak. 2026-10-03 put 210 bursts over the air across three configurations and
lost exactly one, to an impulse -- so NOTHING was ranked, and the three-cell
comparison above came back 90/90 and could not separate its own cells. The
transmit level does not help either: 13 dB of it moved the far radio's output
by about 5%, because the radio's microphone limiter pins the deviation. The
model is the only place on this bench where a configuration can be turned
down until it breaks, which is the only place one can be ranked.

What comes out of it RANKS; it does not predict. Absolute survival rates from
a model are not field numbers and must not be quoted as any -- see
fm_channel's own docstring. --response is mandatory and has no default for
the same reason --note is: the flat 300-3000 Hz model is flat where no real
chain is, and a figure is only valid for the chain it was taken on.

THE SYMBOL-RATE CEILING IS THE QUESTION, so this tool is allowed to exceed
it, and says so every time. `fm_config.MAX_SYMBOL_RATE_BD = 250` is a
measured property of one receiving cable's LM358 envelope follower, not of
FM -- and `Profile` already takes a per-chain ceiling so it can be raised
with evidence. `--sweep` raises it for the duration of the run and prints
that it has. A rate that delivers here has earned a new ceiling for THIS
chain; one that does not has reconfirmed the old one.

RUN AT THE RATE THE RECORD WAS TAKEN AT. The bench ran 48000 Hz and the
phone ran 8000 Hz, and `codec2` requires `Fs % Rs == 0` -- so 375 and 480 Bd,
two of the three rates the record says failed, cannot even be CONSTRUCTED at
8000 Hz. They divide 48000 fine. `--fs` defaults to the bench's 48000 for
that reason; see fm_bench_audio's docstring.

WHAT IT REPORTS, AND WHY BOTH NUMBERS. Delivery (did any copy of the message
arrive byte-exact) AND per-copy rate (what fraction of individual frames
decoded). Redundancy masks a degrading channel: this bench has recorded 100%
delivery at a configuration whose per-copy rate was 56-69%, and reading only
the first number would have called that healthy. Per-copy is the leading
indicator.

THE DEFAULT CONFIGURATION IS THE OPERATING POINT, asked of `fm_config`
rather than restated. It has not always been: `--code` was a literal
"H_256_768_22" and `--repeats` was `fm_config.DEFAULT_REPEATS` (2), so a bare
`--run` measured a 32 B code at 2 copies -- neither the operating point nor
the one it superseded. A figure is only valid for the configuration it was
measured in, and the configuration nobody types is the one nobody checks.
`--fs` is the deliberate exception; see above.

GROUND RULES THIS TOOL ENFORCES RATHER THAN DOCUMENTS:

  * A run is a list of CELLS -- complete configuration points -- and every
    round visits every cell, never all of A then all of B. A configuration
    measured in its own block is measured against its own slice of a channel
    whose bad patches last minutes. `--cell` sets them on any axis
    (code, copies, rate, tones, lead-in, payload); `--sweep` is sugar for
    cells that differ only in rate, so there is one mechanism and not two.
    The axis used to be the rate alone, which is why the outstanding
    code-and-copies comparison could only be run as blocks.
  * `--both-directions` does the same for the two DIRECTIONS, alternating the
    tx/rx roles inside each round. Interleaving the rates and leaving the
    directions as separate blocks is how a direction asymmetry came to be
    claimed at p = 0.00022 on this bench and retracted the same day -- a bad
    patch over one block reproduces it exactly. If it is being compared, it
    belongs inside the round.
  * `--gap` spreads a batch over time, and the CSV carries wall-clock. The
    same configuration measured 4/4 and 24/24 back-to-back and 2/10 across an
    hour; a back-to-back batch is an upper bound, not a measurement.
  * `--note` is mandatory for an over-the-air run and goes in the log. Three
    investigations on this bench were confounded by an unrecorded volume-knob
    position.
  * A trial that could not be RUN is not a trial that FAILED. Hardware errors
    abort; an empty decode is a result and gets recorded.
"""

import argparse
import datetime
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "src", "main", "python"))

import fm_bench_audio as audio
import fm_channel
import ptt_test

import fm_burst
import fm_config
import fm_framing
import fm_modem


# The lead-in now lives in fm_config.KEYING_LEAD_IN_S, keyed by keying method,
# with the measurements that produced each figure. It used to be here because
# fm_burst had a single DEFAULT_LEAD_IN_S of 1.6 s -- the handheld chain's
# number -- with nothing tying it to how the radio was keyed. A LinkConfig now
# resolves it from the keying method, or from the chain when the chain measured
# its own.
# THE DEFAULT CONFIGURATION IS THE OPERATING POINT, asked of fm_config rather
# than restated here, and that is a correction rather than a tidy-up.
#
# Three of these were already derived from fm_config.FAST, with a comment
# saying restating them is how the previous transport's numbers came to
# disagree across modules -- and the other two were not. `--code` was the
# literal "H_256_768_22" and `--repeats` was fm_config.DEFAULT_REPEATS (2), so
# `fm_bench.py --run` with no flags measured a 32 B code at 2 copies: NEITHER
# the operating point (128 B, 1 copy, 2.95 s) nor the one it superseded
# (32 B, 2 copies, 2.35 s). The help text still described the 32 B code as
# "the rate-1/3 30 B code the rebuild plan picked", which the plan abandoned
# on 2026-10-02 when the CoT codec showed 29 B cannot carry a position.
#
# That is the measurement trap this project keeps paying for -- a figure is
# only valid for the configuration it was measured in -- sitting in the
# instrument that is about to take the deciding burst-duration measurement.
#
# `--fs` IS DELIBERATELY NOT DERIVED FROM HERE. The operating point's profile
# runs at the phone's 8000 Hz and this bench's cards run at 48000, two of the
# rates in the record cannot be constructed at 8000 at all, and a figure is
# only comparable at the rate it was taken at. See the module docstring.
_OP = fm_config.OPERATING_POINT

DEFAULT_FIRST_TONE_HZ = _OP.profile.first_tone_hz          # 1000
DEFAULT_SYMBOL_RATE_BD = _OP.profile.symbol_rate_bd        # 400
DEFAULT_TONES = _OP.profile.tones                          # 4
DEFAULT_SPACING_HZ = _OP.profile.tone_spacing_hz           # 400
DEFAULT_CODE = _OP.codename                                # H_1024_2048_4f
DEFAULT_REPEATS = _OP.repeats                              # 1


# ---------------------------------------------------------------------------
# Cells: the thing a round interleaves.
#
# WHY THIS IS NOT A LIST OF SYMBOL RATES ANY MORE. `--sweep` interleaved rates
# and only rates, which was right for Phase 0 -- the rate was the question --
# and is wrong for the question actually outstanding. The operating-point
# switch of 2026-10-02 left a comparison owing in which the rate is FIXED and
# the code and the copy count vary:
#
#     2.35 s   32 B code (H_256_768_22),   2 copies
#     2.95 s   128 B code (H_1024_2048_4f), 1 copy     <- in force
#     5.55 s   128 B code,                  2 copies
#
# Those two changes are COUPLED, so three cells are needed or a weak result
# cannot be attributed to either. With a rate-only sweep the only way to run
# it was three separate invocations -- that is, three BLOCKS -- and this
# bench's dominant failure is a bad patch lasting minutes, which sits over one
# block and reproduces any effect you like. That is exactly how a direction
# asymmetry came to be claimed at p = 0.00022 here and retracted the same day.
#
# So the interleave axis is a CELL: one complete configuration point. `--sweep`
# is now sugar for a list of cells that differ only in rate, which keeps every
# documented Phase 0 invocation working and leaves one interleave mechanism
# instead of two.
# ---------------------------------------------------------------------------

#: What a --cell spec may set, and how to read each value. Anything else is
#: refused by name rather than ignored: a typo'd field silently measuring the
#: baseline three times is a run that looks like a null result.
CELL_FIELDS = {
    "rs": int,
    "code": str,
    "repeats": int,
    "first_tone": int,
    "spacing": int,
    "tones": int,
    "lead_in": float,
    "payload": int,
    # TRANSMIT LEVEL, in dB on the playback mixer control of whichever card is
    # about to transmit. The only cell field that is not part of the waveform,
    # and it is here for the reason every other axis is: it has to be
    # INTERLEAVED to be measurable.
    #
    # On 2026-10-03 this level was compared as two blocks of time -- 0/13 at
    # -31 dB, then 10/10 at -20 dB after changing it -- and a Fisher exact
    # p = 8.7e-07 was quoted off that. The comparison was worthless: this
    # bench's AtoB direction swings between 0% and 100% at fixed settings
    # within minutes while BtoA holds at 100%, so a bad patch over one block
    # reproduces any effect wanted. A later run at the SAME -20 dB went back
    # to 0/5, which is what exposed it. Same error as the retracted
    # p = 0.00022 direction asymmetry, made again with the tool that had just
    # been rebuilt to prevent it -- because the level was the one axis the
    # tool could not interleave.
    "tx_level": float,
    # SIMULATED CHANNEL (--channel only). SNR in dB at the demodulator and
    # the audio drive into the limiter. Cell fields for the same reason every
    # other axis is one: a survival curve is a COMPARISON across SNR, and the
    # tool already knows how to interleave cells, summarise them side by side
    # and write one CSV row per trial. A separate sweep script would be a
    # second mechanism that has to re-learn all of it -- which is the mistake
    # --sweep was folded into --cell to undo.
    "snr_db": float,
    "drive": float,
    # 1 to drop the demodulator's carried-over sync state before decoding this
    # burst, 0 to leave it. Also not part of the waveform, and also here
    # because it only means anything INTERLEAVED -- the thing it is suspected
    # of causing is the unexplained 0%-or-100% swing, which is precisely what
    # a blocked comparison cannot see past.
    #
    # FreeDV's own GUI resets its receivers at the end of every burst, because
    # "the receive path does not run while transmitting, and a demodulator left
    # 'in sync' from before keying would miss the next burst". This transport
    # has that gap and has never reset anything.
    "rx_reset": int,
}

#: How a field is printed in a cell's label. Rate keeps the "400 Bd" form
#: every existing sweep output and bench note uses.
_LABEL_FORMS = {
    "rs": lambda v: "%d Bd" % v,
    "tx_level": lambda v: "tx %+.0f dB" % v,
    "rx_reset": lambda v: "rx-reset" if v else "no-rx-reset",
    "lead_in": lambda v: "lead-in %.2fs" % v,
    "payload": lambda v: "payload %dB" % v,
    "snr_db": lambda v: "SNR %+.0f dB" % v,
    "drive": lambda v: "drive x%.2f" % v,
}


class CellSpecError(ValueError):
    """A --cell argument that cannot be read. Raised before any radio is keyed."""


def parse_cell_spec(text):
    """'code=H_256_768_22,repeats=2' -> {'code': ..., 'repeats': 2}."""
    out = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise CellSpecError(
                "%r is not field=value. A cell is a comma-separated list of "
                "overrides on the baseline configuration, e.g. "
                "'code=H_1024_2048_4f,repeats=1'. Fields: %s"
                % (part, ", ".join(sorted(CELL_FIELDS))))
        name, value = part.split("=", 1)
        name = name.strip().replace("-", "_")
        if name not in CELL_FIELDS:
            raise CellSpecError(
                "unknown cell field %r. Have: %s"
                % (name, ", ".join(sorted(CELL_FIELDS))))
        if name in out:
            raise CellSpecError(
                "%r set twice in one cell (%r then %r) -- one of them is "
                "being silently discarded"
                % (name, out[name], value.strip()))
        try:
            out[name] = CELL_FIELDS[name](value.strip())
        except ValueError:
            raise CellSpecError(
                "cell field %s=%r is not a %s"
                % (name, value.strip(), CELL_FIELDS[name].__name__))
    if not out:
        raise CellSpecError("empty cell spec: it would just repeat the baseline")
    return out


class Cell(object):
    """One complete configuration point in a run, and its identity.

    `values` is fully resolved -- the baseline with this cell's overrides
    applied -- so nothing downstream has to know which fields were typed and
    which were inherited. `overrides` is kept only to report what the operator
    actually asked for.
    """

    def __init__(self, index, values, overrides=None):
        self.index = index
        self.values = dict(values)
        self.overrides = dict(overrides or {})

    def __getitem__(self, name):
        return self.values[name]

    @property
    def slug(self):
        """Filename-safe and UNIQUE PER CELL, which the rate is not.

        The 3-cell comparison above has two cells at 400 Bd, and the capture
        path used to be keyed by rate -- so with two same-rate cells in one
        round the staleness check would compare a capture against the OTHER
        cell's file and pass off one cell's recording as the other's. The
        tool already had this hazard for directions and names it there; this
        is the same hazard on the new axis.
        """
        return "c%d" % self.index

    def label(self, varying):
        """Names only what VARIES in this run, because that is what is being
        compared. A label carrying every field is one nobody reads, and the
        CSV keeps the full configuration per row regardless."""
        if not varying:
            return "config"
        return " ".join(
            _LABEL_FORMS.get(f, lambda v, _f=f: "%s=%s" % (_f, v))(self[f])
            for f in varying)


def varying_fields(cells):
    """Which fields differ across the cells, in CELL_FIELDS order.

    Derived from the RESOLVED values rather than from what was typed, so two
    cells that spell the same configuration differently are correctly reported
    as one -- and so a label cannot claim a difference that is not there.
    """
    return [f for f in CELL_FIELDS
            if len({c[f] for c in cells}) > 1]


def build_cells(specs, sweep, base):
    """The run's cells, from --cell specs or --sweep, over a baseline.

    --sweep IS REIMPLEMENTED HERE rather than kept as a parallel path: one
    interleave mechanism, two front doors. Every Phase 0 invocation in the
    module docstring produces exactly the cells it used to.
    """
    if specs and sweep:
        raise CellSpecError(
            "--cell and --sweep both given. --sweep IS a list of cells that "
            "differ only in rate, so passing both is two ways of saying what "
            "gets interleaved -- write the rates as cells "
            "(--cell rs=250 --cell rs=400) if they need other overrides too")
    # EVERY FIELD MUST BE IN THE BASELINE. Without this, adding a key to
    # CELL_FIELDS and forgetting to give it a baseline value surfaces as a
    # KeyError inside a set comprehension in varying_fields -- which is a long
    # way from the one-line omission that caused it. Checked once, here, where
    # the baseline arrives.
    missing = [f for f in CELL_FIELDS if f not in base]
    if missing:
        raise CellSpecError(
            "the baseline configuration has no value for %s. Every name in "
            "CELL_FIELDS needs one, even if it is None -- see main()'s `base`."
            % ", ".join(sorted(missing)))
    if specs:
        cells = []
        for i, text in enumerate(specs):
            over = parse_cell_spec(text)
            cells.append(Cell(i, dict(base, **over), over))
        return cells
    if sweep:
        rates = [r.strip() for r in sweep.split(",") if r.strip()]
        if not rates:
            raise CellSpecError("--sweep listed no symbol rates")
        cells = []
        for i, r in enumerate(rates):
            try:
                rs = int(r)
            except ValueError:
                raise CellSpecError("--sweep rate %r is not an integer" % r)
            cells.append(Cell(i, dict(base, rs=rs), {"rs": rs}))
        return cells
    return [Cell(0, base)]


# ---------------------------------------------------------------------------
# The transmit level, as a thing a cell can set.
#
# APPLIED TO WHICHEVER CARD IS ABOUT TO TRANSMIT, not to a fixed one. Under
# --both-directions the roles swap inside every round, so a cell that means
# "transmit at -31 dB" has to follow the playing card the same way PTT already
# does (see fm_bench_audio.transmit_and_record's cm108_path derivation).
#
# EVERY SET IS READ BACK. Setting a level and assuming it took is how this
# bench spent forty minutes measuring at a railed level; the read-back is
# compared and a disagreement ABORTS rather than being logged, because a run
# at a level nobody can name afterwards is not data.
# ---------------------------------------------------------------------------

DEFAULT_TX_LEVEL_CONTROL = "Speaker"

#: A mixer step on these C-Media cards is 1 dB, so a request that lands
#: further than this away was not honoured at all.
_TX_LEVEL_TOLERANCE_DB = 1.0

_DB_RE = re.compile(r"\[(-?\d+(?:\.\d+)?)dB\]")


def alsa_card_index(dev):
    """The card NUMBER behind a --tx/--rx value, for amixer.

    `dev` may be an index, an "hw:N,0" string or a card name, so it is
    resolved the same way the audio layer resolves it and the index read off
    the result -- rather than assuming the operator typed a number.
    """
    resolved = audio.resolve_alsa_device(dev, "playback")
    m = re.search(r"hw:(\d+)", resolved or "")
    if not m:
        raise audio.BenchError(
            "cannot work out which card %r is for the mixer (resolved to %r). "
            "tx_level needs a card it can set a level on." % (dev, resolved))
    return int(m.group(1))


def read_tx_level(card, control=DEFAULT_TX_LEVEL_CONTROL):
    """Current playback level in dB on `control`, or None if unreadable."""
    try:
        out = subprocess.run(["amixer", "-c", str(card), "sget", control],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    found = _DB_RE.findall(out)
    return float(found[0]) if found else None


def set_tx_level(card, db, control=DEFAULT_TX_LEVEL_CONTROL):
    """Set `control` on `card` to `db`, confirm it, and return what it reads.

    The `--` is load-bearing: amixer parses a leading "-31dB" as options and
    fails with "invalid option -- 'B'", which looks like a broken mixer rather
    than a quoting problem.
    """
    try:
        subprocess.run(["amixer", "-c", str(card), "--", "sset", control,
                        "%gdB" % db],
                       capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise audio.BenchError(
            "could not set %s on card %d to %g dB: %s" % (control, card, db, exc))
    actual = read_tx_level(card, control)
    if actual is None:
        raise audio.BenchError(
            "set %s on card %d to %g dB but cannot read it back -- refusing to "
            "measure at a level that cannot be named" % (control, card, db))
    if abs(actual - db) > _TX_LEVEL_TOLERANCE_DB:
        raise audio.BenchError(
            "asked card %d's %s for %g dB and it reads %g dB. The control did "
            "not honour it (check its range with `amixer -c %d sget %s`); "
            "refusing to measure at a level that is not the one requested"
            % (card, control, db, actual, card, control))
    return actual


def bench_chain(note, tx_desc, rx_desc):
    """The Chain this run is evidence about.

    `Profile` REFUSES to be built without one, deliberately -- a tone
    placement is a property of a specific radio+cable+radio path, and the
    same placement measured 91% delivered on one bench and 0 of 9 on another.
    Building it from what the operator typed, rather than borrowing a stored
    Chain, keeps that honest: this run's numbers belong to this run's
    hardware and nothing else.
    """
    return fm_config.Chain(
        name="bench run",
        radios=rx_desc or "unrecorded",
        cables=tx_desc or "unrecorded",
        measured_on=datetime.date.today().isoformat(),
        delivered="in progress",
        notes=note or "",
    )


def make_profile(rs, first_tone, spacing, tones, fs, ceiling, chain,
                 slug=None):
    """One Profile for a bench point, with the ceiling under test.

    `spacing` defaults to the symbol rate, which is the TIGHTEST LEGAL
    placement -- spacing must be >= the symbol rate for the tones to stay
    orthogonal. That default is what keeps a faster rate inside the measured
    1250-2500 Hz usable band for as long as arithmetic allows: at 320 Bd the
    top tone lands at 2460 Hz, at 500 Bd it is forced out to 3000 Hz.
    """
    return fm_config.Profile(
        # The cell slug is in the name because two cells in one run can share
        # a rate: "rs400" twice over tells an operator reading a summary_line
        # nothing about which cell it describes.
        name="rs%d" % rs if slug is None else "%s-rs%d" % (slug, rs),
        first_tone_hz=first_tone,
        tone_spacing_hz=spacing or rs,
        tones=tones,
        symbol_rate_bd=rs,
        chain=chain,
        sample_rate_hz=fs,
        symbol_rate_ceiling_bd=ceiling,
    )


class Trial(object):
    """One burst's outcome, in the shape the CSV and the summary both need."""

    def __init__(self, rs, code, repeats, payload_b, burst_s, direction="",
                 tx_level_db=None, rx_reset=0, snr_db=None):
        # The level READ BACK off the card, not the one requested -- those
        # differ when a control snaps to its nearest step, and the CSV has to
        # say what was on the air.
        self.tx_level_db = tx_level_db
        self.rx_reset = rx_reset
        # None over the air: the SNR there is whatever the radios were doing
        # and is not known. A number here means the channel was SIMULATED, so
        # the column also says which rows are field data and which are not.
        self.snr_db = snr_db
        self.rs = rs
        self.code = code
        self.repeats = repeats
        self.payload_b = payload_b
        self.burst_s = burst_s
        self.direction = direction
        self.frames = 0          # frames the physical layer handed back
        self.exact = 0           # of those, copies byte-identical to what was sent
        self.rejected = 0        # passed the physical CRC, failed framing
        self.peak = 0.0
        self.rms = 0.0
        self.at = ""
        self.error = ""

    @property
    def delivered(self):
        """Did the MESSAGE arrive -- any one byte-exact copy is enough."""
        return self.exact > 0

    def row(self, note, settings=""):
        return [self.at, self.direction, "%d" % self.rs, self.code,
                "%d" % self.repeats,
                "%d" % self.payload_b, "%.3f" % self.burst_s,
                "1" if self.delivered else "0",
                "%d" % self.frames, "%d" % self.exact, "%d" % self.rejected,
                "%.4f" % self.peak, "%.4f" % self.rms,
                "" if self.tx_level_db is None else "%g" % self.tx_level_db,
                "1" if self.rx_reset else "0",
                "" if self.snr_db is None else "%g" % self.snr_db,
                settings, self.error, note]


#: `direction` is its own column rather than buried in `note`, so a
#: direction comparison can be re-derived from the CSV without parsing prose.
#: Not having it is how a direction asymmetry got claimed at p = 0.00022 from
#: runs that were never interleaved across directions -- see the RETRACTION
#: entry in docs/open-questions.md.
CSV_HEADER = ["utc", "direction", "rs_bd", "code", "repeats", "payload_b",
              "burst_s", "delivered", "frames_out", "exact_copies",
              "rejected", "peak", "rms", "tx_level_db", "rx_reset",
              "snr_db", "settings", "error", "note"]


def score(modem, payload, frames, trial):
    """Count what came back, separating the two ways a frame can be wrong.

    A frame reaching here has ALREADY passed the physical layer's CRC, so a
    framing rejection is not noise that slipped through -- it is
    overwhelmingly a receive level that is wrong, because clipping makes the
    decoder settle on wrong-but-CRC-valid codewords. Counting those
    separately from clean misses is the difference between "the channel ate
    it" and "turn the volume down", and a silent `continue` here once let a
    link report Hearing while delivering nothing for an evening.
    """
    trial.frames = len(frames)
    for frame in frames:
        content = fm_framing.unpack(frame, modem.frame_payload_bytes)
        if content is None:
            trial.rejected += 1
        elif content == payload:
            trial.exact += 1
    return trial


def run_one(modem, config, payload, args, scratch,
            tx=None, rx=None, direction="", slug="", tx_level=None,
            rx_reset=0, snr_db=None, drive=1.0, response=None):
    """One burst: build, transmit, record, decode, score.

    `tx`/`rx` override args.tx/args.rx so one invocation can alternate
    directions inside a round. See --both-directions. `slug` identifies the
    cell, so its capture cannot be confused with another cell's -- see
    Cell.slug.
    """
    tx = args.tx if tx is None else tx
    rx = args.rx if rx is None else rx

    # SET BEFORE ANYTHING IS BUILT OR KEYED, on the card about to transmit,
    # and read back. A failure here raises BenchError, which the caller treats
    # as "the trial did not happen" rather than as a failed trial -- a mixer
    # that cannot be set is a dead instrument, not a dead channel.
    level_db = None
    if tx_level is not None and not args.loopback:
        level_db = set_tx_level(alsa_card_index(tx), tx_level,
                                args.tx_level_control)

    trial = Trial(modem.profile.symbol_rate_bd, modem.codename,
                  config.repeats, len(payload), config.burst_duration_s(),
                  direction=direction, tx_level_db=level_db,
                  rx_reset=rx_reset,
                  snr_db=snr_db if args.channel else None)
    trial.at = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")

    samples = fm_burst.build(modem, payload, config)

    if args.loopback:
        # Same build path, no sound card and no radio. Proves the code before
        # any of the hardware's many ways to look like a software bug.
        frames = modem.demodulate(samples)
        trial.peak = trial.rms = 0.0
        return score(modem, payload, frames, trial)

    if args.channel:
        # The real modules through the modelled channel: no sound card, no
        # radio, and a signal that can be turned DOWN, which is the one thing
        # two radios on a desk cannot do. Everything measured on this bench so
        # far arrives far too strong -- 210 bursts on 2026-10-03 lost one, to
        # an impulse -- so the configurations cannot be ranked there at all.
        #
        # The peak and rms columns are filled from the channel's output for
        # the same reason they are filled over the air: a run whose level
        # columns are blank cannot be compared against one whose are not. They
        # are NOT a volume knob here and level_warning() is not printed.
        got = fm_channel.through(
            samples, modem.sample_rate_hz, snr_db,
            drive=drive, response=response)
        trial.peak = max((abs(x) for x in got), default=0.0)
        trial.rms = fm_channel.rms(got)
        if rx_reset:
            modem.reset_rx_state()
        return score(modem, payload, modem.demodulate(got), trial)

    wav = os.path.join(scratch, "fm_bench_tx.wav")
    # The recording path carries the CELL and the direction, not the rate.
    # Both are things that alternate inside one round, and a shared filename
    # means the staleness check -- which is what stops a failed recorder
    # passing off the previous capture as this one's -- compares against some
    # other trial's file. The rate alone was not enough: two cells in one run
    # can share a rate and differ in the code or the copy count, which is the
    # comparison the cell axis exists for.
    rec = os.path.join(scratch, "fm_bench_rx_%s_%s.wav"
                       % (direction or "one", slug or "rs%d" % trial.rs))
    audio.transmit_and_record(
        samples, modem.sample_rate_hz, wav, rec,
        play_dev=tx, rec_dev=rx,
        line=args.ptt_line, dev=args.ptt_dev, gpio=args.ptt_gpio,
        vox=args.vox, tx_gain=args.tx_gain, backend=args.backend,
        cm108_path=args.cm108_path, quiet=args.quiet)

    got, peak, rms = audio.capture_to_samples(
        rec, modem.sample_rate_hz, deemphasis_us=args.rx_deemphasis_us)
    trial.peak, trial.rms = peak, rms
    # AFTER the transmission and BEFORE decoding, which is the boundary
    # FreeDV's own GUI resets at. Resetting earlier would also drop state the
    # modulator just used; later would be after the thing it is meant to
    # affect.
    if rx_reset:
        modem.reset_rx_state()
    return score(modem, payload, modem.demodulate(got), trial)


def level_warning(peak):
    """What the receive level says, in the words an operator can act on.

    The volume knob IS the gain stage on an FM audio link, and a level
    outside the band is the most common fixable fault here -- so this is
    printed per trial rather than left to the summary, where a run that was
    never going to work would already be over.
    """
    if peak <= 0.0:
        return "SILENT -- nothing recorded at all; wrong card, or no transmission"
    if peak < fm_config.SILENT_PEAK:
        return "silent (peak %.3f < %.2f) -- the path is dead, turn it UP" % (
            peak, fm_config.SILENT_PEAK)
    if peak > fm_config.SATURATED_PEAK:
        return ("SATURATED (peak %.3f > %.2f) -- the decoder will settle on "
                "wrong-but-CRC-valid codewords; turn it DOWN"
                % (peak, fm_config.SATURATED_PEAK))
    if peak < fm_config.TARGET_PEAK_LOW:
        return "low (peak %.3f, want %.2f-%.2f) -- turn it UP" % (
            peak, fm_config.TARGET_PEAK_LOW, fm_config.TARGET_PEAK_HIGH)
    if peak > fm_config.TARGET_PEAK_HIGH:
        return "hot (peak %.3f, want %.2f-%.2f) -- turn it DOWN" % (
            peak, fm_config.TARGET_PEAK_LOW, fm_config.TARGET_PEAK_HIGH)
    return "in band (peak %.3f)" % peak


def summarise(label, trials, width=10):
    """Delivery AND per-copy, always together.

    Per-copy is the leading indicator and delivery is the lagging one: 100%
    delivery over a channel chewing through half of every burst is a
    configuration about to fall off a cliff, and only the second number says
    so. Printing one without the other is how this bench talked itself into a
    healthy-looking config twice.
    """
    if not trials:
        return "%-*s no trials" % (width, label)
    n = len(trials)
    delivered = sum(1 for t in trials if t.delivered)
    copies = sum(t.repeats for t in trials)
    exact = sum(t.exact for t in trials)
    rejected = sum(t.rejected for t in trials)
    peaks = [t.peak for t in trials if t.peak > 0]
    out = ("%-*s delivered %2d/%-2d = %3.0f%%   per-copy %3d/%-3d = %3.0f%%   "
           "burst %.2f s" % (
               width, label, delivered, n, 100.0 * delivered / n,
               exact, copies, 100.0 * exact / copies if copies else 0.0,
               trials[0].burst_s))
    if rejected:
        out += "   rejected %d" % rejected
    if peaks:
        out += "   peak %.2f-%.2f" % (min(peaks), max(peaks))
    return out


#: WHAT THE BENCH WAS SET TO, as data rather than as prose in --note.
#:
#: Every key here names something that has actually confounded a measurement
#: in this project's record, and the value is whatever the operator can read
#: off the hardware -- "11oclock", "-20dB", "open", "digirig-lite-a". The
#: tool does not interpret them; it records them in their own column so two
#: runs can be compared mechanically instead of by reading two paragraphs.
#:
#: --note still exists and is still required over the air. It is for the
#: things that have no key. The lesson the record keeps repeating is not
#: "write more prose", it is that a figure is only valid for the
#: configuration it was taken in, and prose cannot be joined on.
RUN_SETTINGS = {
    "rx_volume": "the RECEIVING radio's volume knob -- the dominant fixable "
                 "fault on this link, and the one three investigations were "
                 "lost to",
    "tx_volume": "the transmitting radio's volume knob, where it has one",
    "squelch": "open or closed, and on which end",
    "tx_power": "the transmitting radio's power setting (high/low)",
    "tx_radio": "which radio was transmitting",
    "rx_radio": "which radio was receiving",
    "tx_cable": "which dongle was transmitting",
    "rx_cable": "which dongle was RECEIVING -- the cleanest finding in seven "
                "weeks is that the receiving cable decides whether the link "
                "works at all",
    "antenna": "antennas and how they were placed",
    "position": "where the radios physically were, and how far apart",
}

#: Required for an over-the-air run. `unknown` is an accepted value and is
#: RECORDED as unknown: the point is that it cannot be forgotten, only
#: declined on the record. A blank column and a column saying "nobody looked"
#: are different facts, and the first has repeatedly been read as the second.
REQUIRED_SETTINGS = ("rx_volume",)


class SettingError(ValueError):
    """A --setting that cannot be read. Raised before any radio is keyed."""


def parse_settings(items):
    """['rx_volume=11oclock', ...] -> 'rx_volume=11oclock;...', normalised.

    Sorted and semicolon-joined so the column is comparable between runs
    rather than ordered by whatever the operator typed.
    """
    out = {}
    for item in items or ():
        text = item.strip()
        if "=" not in text:
            raise SettingError(
                "--setting %r is not key=value. Keys: %s"
                % (item, ", ".join(sorted(RUN_SETTINGS))))
        key, value = text.split("=", 1)
        key, value = key.strip().lower(), value.strip()
        if key not in RUN_SETTINGS:
            raise SettingError(
                "--setting %r is not a known key. Have: %s"
                % (key, ", ".join(sorted(RUN_SETTINGS))))
        if not value:
            raise SettingError(
                "--setting %s= has no value. Use 'unknown' to record that it "
                "was not read, which is a different fact from leaving it "
                "out." % key)
        if ";" in value or "," in value:
            raise SettingError(
                "--setting %s=%r contains a separator the column uses "
                "(';' or ','). Use a plain word." % (key, value))
        if key in out and out[key] != value:
            raise SettingError(
                "--setting %s given twice, as %r and %r" % (key, out[key],
                                                            value))
        out[key] = value
    return ";".join("%s=%s" % (k, out[k]) for k in sorted(out))


class CsvLog(object):
    """Append one row per trial, flushed, as the trial finishes.

    WRITTEN PER TRIAL AND NOT AT THE END, because the end may never come. A
    40-minute run was interrupted on 2026-10-02 about four minutes in and the
    whole batch -- roughly twenty bursts already on the air and decoded --
    went with it, because the rows only existed in a list that the exception
    unwound past. Every other check in this tool is about refusing to record
    data that cannot be trusted; this one is about not losing data that can.

    Flushed rather than merely buffered: an interrupt kills the process, so
    anything sitting in Python's buffer is lost exactly when it matters. Not
    fsync'd -- that defends against losing power, which is a different and
    much rarer problem than the operator stopping a run.
    """

    def __init__(self, path, note, settings=""):
        self.path = path
        self.note = note
        self.settings = settings
        self.rows = 0
        new = not os.path.exists(path)
        if not new:
            # APPENDING TO A FILE WITH A DIFFERENT HEADER IS REFUSED. The
            # columns have changed before (tx_level_db was added 2026-10-03),
            # and appending new rows under an old header produces a file whose
            # every column is silently shifted from that point on -- which is
            # worse than losing the run, because it corrupts the rows that
            # were already good.
            with open(path) as fh:
                first = fh.readline().strip()
            if first and first != ",".join(CSV_HEADER):
                raise ValueError(
                    "%s already exists with a different header.\n"
                    "  its:  %s\n  ours: %s\n"
                    "Write to a new file rather than appending rows whose "
                    "columns do not line up." % (path, first,
                                                 ",".join(CSV_HEADER)))
        self.fh = open(path, "a")
        if new:
            self.fh.write(",".join(CSV_HEADER) + "\n")
            self.fh.flush()

    def write(self, trial):
        self.fh.write(",".join(f.replace(",", ";")
                               for f in trial.row(self.note, self.settings)) + "\n")
        self.fh.flush()
        self.rows += 1

    def close(self):
        if self.fh is not None:
            self.fh.close()
            self.fh = None


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)

    ap.add_argument("--devices", action="store_true",
                    help="list the sound cards ALSA can see, and stop")
    ap.add_argument("--loopback", action="store_true",
                    help="modem only: no sound card, no radio. Run this "
                         "first -- it separates a code fault from the "
                         "hardware's many ways of imitating one")
    ap.add_argument("--ptt-check", action="store_true",
                    help="key --tx's radio and MEASURE the carrier on --rx. "
                         "Run this before any data: it is the only check "
                         "that separates 'PTT keys nothing' from the dozen "
                         "other faults a failed data run looks like. Needs "
                         "SQUELCH OPEN on the receiving radio")
    ap.add_argument("--channel", action="store_true",
                    help="the real modules through tools/fm_channel.py's "
                         "modelled radio: no sound card, no radio, and a "
                         "signal that can be turned DOWN. Two radios on a "
                         "desk cannot be made weak, so this is the only "
                         "place a configuration can be RANKED. Use "
                         "--cell snr_db=N to get a survival curve, and see "
                         "--response")
    ap.add_argument("--snr", type=float, default=12.0,
                    help="SNR in dB for --channel (default 12, a few dB "
                         "above the model's 10 dB FM threshold). Set it per "
                         "CELL to sweep it")
    ap.add_argument("--drive", type=float, default=1.0,
                    help="audio drive into the limiter for --channel; 1.0 is "
                         "correctly adjusted, 2.0 is 6 dB hot")
    ap.add_argument("--response",
                    help="which MEASURED chain response --channel runs "
                         "through: " + ", ".join(sorted(
                             fm_channel.MEASURED_RESPONSES)) +
                         ", or 'flat' for the idealised 300-3000 Hz model. "
                         "REQUIRED with --channel and deliberately has no "
                         "default: the flat model is flat where no real "
                         "chain is, so it flatters every candidate, and "
                         "which chain a survival number belongs to is the "
                         "question this project most often cannot answer "
                         "afterwards")
    ap.add_argument("--allow-unmeasured", action="store_true",
                    help="run --channel even where the chosen response never "
                         "measured the configuration's tones. The response is "
                         "then HELD FLAT outside its measured span, which is "
                         "a placeholder and not a model -- so the result "
                         "ranks nothing")
    ap.add_argument("--run", action="store_true",
                    help="transmit over the air at one configuration")
    ap.add_argument("--sweep",
                    help="comma-separated symbol rates to compare, "
                         "INTERLEAVED round by round (e.g. 250,320,400,500). "
                         "Raises the per-chain ceiling for the run, which is "
                         "the whole point of Phase 0. Sugar for --cell rs=N, "
                         "so the two cannot be combined")
    ap.add_argument("--cell", action="append", default=[], metavar="SPEC",
                    help="one configuration to compare, as comma-separated "
                         "field=value overrides on the baseline flags; repeat "
                         "the option per cell. Cells are INTERLEAVED round by "
                         "round, so a comparison on ANY axis is spread over "
                         "the channel rather than measured in blocks. Fields: "
                         + ", ".join(sorted(CELL_FIELDS))
                         + ". The burst-duration comparison the "
                         "operating-point switch left owing is: "
                         "--cell code=H_256_768_22,repeats=2 "
                         "--cell code=H_1024_2048_4f,repeats=1 "
                         "--cell code=H_1024_2048_4f,repeats=2")

    ap.add_argument("--both-directions", action="store_true",
                    help="alternate --tx and --rx roles INSIDE each round, so "
                         "the two directions are interleaved rather than "
                         "measured as separate blocks. Required for any claim "
                         "about a direction difference: a bad patch on this "
                         "channel lasts minutes and will sit over one block")
    ap.add_argument("--trials", type=int, default=10,
                    help="bursts per configuration (default %(default)s)")
    ap.add_argument("--rounds", type=int, default=6,
                    help="interleaved rounds for --sweep (default %(default)s)")
    ap.add_argument("--gap", type=float, default=0.0,
                    help="seconds between trials. A back-to-back batch is an "
                         "UPPER BOUND, not a measurement -- the same config "
                         "measured 24/24 back-to-back and 2/10 across an hour")

    ap.add_argument("--rs", type=int, default=DEFAULT_SYMBOL_RATE_BD,
                    help="symbol rate, Bd (default %(default)s, Phase 0's)")
    ap.add_argument("--first-tone", type=int, default=DEFAULT_FIRST_TONE_HZ,
                    help="first tone Hz (default %(default)s, the measured LOW "
                         "EDGE of the usable band -- 400 Hz delivered 0%%)")
    ap.add_argument("--spacing", type=int, default=0,
                    help="tone spacing in Hz; 0 (the default) follows the "
                         "symbol rate, which is the tightest legal placement "
                         "and what the operating point uses (%d Hz at %d Bd)"
                         % (DEFAULT_SPACING_HZ, DEFAULT_SYMBOL_RATE_BD))
    ap.add_argument("--tones", type=int, default=DEFAULT_TONES)
    ap.add_argument("--fs", type=int, default=audio.BENCH_FS,
                    help="sample rate (default %(default)s, what the cards "
                         "run at). The phone ran 8000; a figure is only "
                         "comparable at the rate it was taken at")
    ap.add_argument("--code", default=DEFAULT_CODE,
                    help="LDPC code (default %s, the operating point's -- "
                         "%d B of capacity). Have: "
                         % (DEFAULT_CODE, _OP.capacity_bytes())
                         + ", ".join(sorted(fm_modem.CODES)))
    ap.add_argument("--repeats", type=int, default=DEFAULT_REPEATS,
                    help="copies sharing one preamble (default %(default)s, "
                         "the operating point's -- NOT fm_config."
                         "DEFAULT_REPEATS, which is 2 and which the operating "
                         "point deliberately disagrees with)")
    ap.add_argument("--payload", type=int, default=0,
                    help="payload bytes; defaults to filling the frame")
    ap.add_argument("--lead-in", type=float, default=None,
                    help="lead-in tone seconds; defaults by keying method "
                         "(%.2f s CM108 PTT, %.2f s VOX -- see "
                         "fm_config.KEYING_LEAD_IN_S)"
                         % (fm_config.KEYING_LEAD_IN_S[fm_config.KEYING_CM108],
                            fm_config.KEYING_LEAD_IN_S[fm_config.KEYING_VOX]))

    ap.add_argument("--tx", help="playback card (index, hw:N,0, or a name "
                                 "substring -- see --devices)")
    ap.add_argument("--rx", help="capture card, the receiving end")
    ap.add_argument("--vox", action="store_true",
                    help="let the audio key the radio instead of CM108 PTT")
    ap.add_argument("--ptt-line", default=ptt_test.CM108_LINE,
                    choices=list(ptt_test.ALL_LINES))
    ap.add_argument("--ptt-dev", default=ptt_test.DEFAULT_DEV)
    ap.add_argument("--ptt-gpio", type=int, default=3)
    ap.add_argument("--cm108-path",
                    help="pin PTT to one hidraw node, for a bench with more "
                         "than one Digirig plugged in")
    ap.add_argument("--tx-gain", type=float, default=1.0,
                    help="scale the played audio: a real way to weaken the "
                         "signal the far radio hears. Use PTT, not --vox")
    ap.add_argument("--rx-reset", type=int, default=0, choices=(0, 1),
                    help="1 to drop the demodulator's carried-over sync state "
                         "after every burst, before decoding it. Set it per "
                         "CELL (--cell rx_reset=1) to A/B it against leaving "
                         "the state, interleaved -- the only shape that can "
                         "see past the 0%%-or-100%% swing it is suspected of "
                         "causing")
    ap.add_argument("--tx-level", type=float, default=None,
                    help="playback level in dB on the transmitting card's "
                         "mixer control, applied before every burst and read "
                         "back. Omitted, the mixer is left alone. Set it per "
                         "CELL (--cell tx_level=-31) to INTERLEAVE two levels "
                         "round by round, which is the only way this axis is "
                         "measurable -- see CELL_FIELDS")
    ap.add_argument("--tx-level-control", default=DEFAULT_TX_LEVEL_CONTROL,
                    help="mixer control the transmit level lives on "
                         "(default %(default)s, which is what feeds the "
                         "radio's mic input on a CM108 cable)")
    ap.add_argument("--rx-deemphasis-us", type=float, default=0.0,
                    help="one-pole de-emphasis, us. OFF by default -- one "
                         "radio pairing needed 150-400, the other needed none")
    ap.add_argument("--backend", default=audio.DEFAULT_BACKEND,
                    choices=list(audio.ALL_BACKENDS))

    ap.add_argument("--setting", action="append", default=[],
                    metavar="KEY=VALUE",
                    help="what the bench was set to, as DATA rather than as "
                         "prose in --note; repeat the option per setting. "
                         "Keys: " + ", ".join(sorted(RUN_SETTINGS)) + ". "
                         + ", ".join(REQUIRED_SETTINGS) + " is required for "
                         "an over-the-air run and accepts 'unknown', which "
                         "records that it was not read -- a different fact "
                         "from leaving it out")
    ap.add_argument("--note", default="",
                    help="volume-knob positions, gain settings, which cable "
                         "is on which end. REQUIRED for an over-the-air run "
                         "and written to the CSV")
    ap.add_argument("--csv", help="append results here")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.devices:
        print(audio.describe_devices())
        return 0

    if args.ptt_check:
        if not (args.tx and args.rx):
            ap.error("--ptt-check needs --tx (the cable to key) and --rx "
                     "(the cable to listen on)")
        scratch = os.environ.get("TMPDIR") or "/tmp"
        rec = os.path.join(scratch, "fm_bench_ptt.wav")
        print("keying %s, listening on %s.\n"
              "SQUELCH MUST BE OPEN on the receiving radio -- the carrier is "
              "found as the QUIET\nstretch inside the hiss, and with squelch "
              "closed there is nothing to measure.\n" % (args.tx, args.rx))
        try:
            path, held = audio.key_and_record(
                rec, args.fs, play_dev=args.tx, rec_dev=args.rx,
                line=args.ptt_line, dev=args.ptt_dev, gpio=args.ptt_gpio,
                cm108_path=args.cm108_path)
        except audio.BenchError as exc:
            print("ABORTED: %s" % exc, file=sys.stderr)
            return 3
        samples, fs = audio.read_wav(path)
        print(ptt_test.report_carrier(
            ptt_test.analyze_carrier(samples, fs, held)))
        peak, rms = audio.levels(samples)
        print("\nrecorded peak %.3f  rms %.3f" % (peak, rms))
        return 0

    if not (args.loopback or args.channel or args.run or args.sweep
            or args.cell or args.both_directions):
        ap.error("pick one of --devices, --loopback, --channel, --ptt-check, "
                 "--run, --sweep, --cell or --both-directions")
    if args.channel and args.loopback:
        ap.error("--channel and --loopback are different experiments: "
                 "loopback is the modem with NO channel at all, --channel is "
                 "the modem through a modelled one. Pick one")
    if args.channel and args.both_directions:
        ap.error("--both-directions alternates two SOUND CARDS, and "
                 "--channel has none. A direction asymmetry is a property of "
                 "the hardware; the model has one chain by construction")
    if args.channel and not args.response:
        ap.error("--channel needs --response: " + ", ".join(
            sorted(fm_channel.MEASURED_RESPONSES)) + ", or 'flat'. There is "
            "no default because the flat model is flat where no measured "
            "chain is, and a survival figure is meaningless without the "
            "chain it belongs to")
    if args.response and not args.channel:
        ap.error("--response only means anything with --channel; over the "
                 "air the chain IS the response")
    if args.both_directions and args.vox:
        ap.error("--both-directions needs deterministic PTT on BOTH ends; "
                 "under --vox the audio keys whichever radio hears it and the "
                 "roles cannot be swapped per trial")

    if not fm_modem.available():
        print("libcodec2 is not loadable here -- nothing can be measured.",
              file=sys.stderr)
        return 2

    # The note is enforced, not requested. An unrecorded level is how three
    # separate investigations on this bench ended up unattributable.
    # EVERY run mode that is not --loopback goes over the air, --both-
    # directions included. It was omitted here, so `--both-directions` on its
    # own reached the point of keying a radio with neither --note nor --tx/--rx
    # required -- transmitting with no recorded volume-knob position, which is
    # precisely what the module docstring says three investigations were lost
    # to. The list is now "not loopback" rather than an enumeration, so a mode
    # added later cannot be left out of it the same way.
    # --channel joins --loopback here: it keys nothing, so there is no knob
    # position to record and no card to name. The list stays "not simulated"
    # rather than an enumeration of the real modes, which is the shape that
    # let --both-directions slip past these guards once already.
    over_the_air = bool(args.run or args.sweep or args.cell
                        or args.both_directions) \
        and not (args.loopback or args.channel)
    if over_the_air and not args.note.strip():
        ap.error("--note is required for an over-the-air run: record the "
                 "volume-knob positions and which cable is on which end. "
                 "Three investigations here were confounded without it")
    if over_the_air and not (args.tx and args.rx):
        ap.error("--tx and --rx are required for an over-the-air run "
                 "(see --devices)")

    try:
        settings = parse_settings(args.setting)
    except SettingError as exc:
        ap.error(str(exc))
    if over_the_air:
        given = {kv.split("=", 1)[0] for kv in settings.split(";") if kv}
        missing = [k for k in REQUIRED_SETTINGS if k not in given]
        if missing:
            ap.error(
                "an over-the-air run needs --setting %s. %s\n"
                "  It cannot be forgotten, only declined: --setting "
                "%s=unknown records that it was not read, which the CSV can "
                "tell apart from a run where nobody was asked."
                % (", ".join("%s=..." % k for k in missing),
                   " ".join(RUN_SETTINGS[k] for k in missing),
                   missing[0]))

    # The baseline every cell overrides: the operating point, as adjusted by
    # whatever bare flags the operator passed.
    base = {
        "rs": args.rs,
        "code": args.code,
        "repeats": args.repeats,
        "first_tone": args.first_tone,
        "spacing": args.spacing,
        "tones": args.tones,
        "lead_in": args.lead_in,
        "payload": args.payload,
        # None means "leave the mixer alone", which is what every invocation
        # that predates this field does.
        "tx_level": args.tx_level,
        "rx_reset": args.rx_reset,
        "snr_db": args.snr,
        "drive": args.drive,
    }
    try:
        cells = build_cells(args.cell, args.sweep, base)
    except CellSpecError as exc:
        ap.error(str(exc))
    varying = varying_fields(cells)
    rates = sorted({c["rs"] for c in cells})

    # The ceiling is raised to whatever is being tested, and said out loud.
    # fm_config's default 250 Bd is a measured property of one receiving
    # cable; Phase 0 exists to find out whether it belongs to this one.
    ceiling = max(rates + [fm_config.MAX_SYMBOL_RATE_BD])
    if ceiling > fm_config.MAX_SYMBOL_RATE_BD:
        print("NOTE: testing above fm_config.MAX_SYMBOL_RATE_BD = %d Bd "
              "(ceiling raised to %d for this run).\n"
              "      That ceiling is an ANALOG limit measured on one "
              "receiving cable's LM358 envelope\n"
              "      follower, not a software setting. A rate that delivers "
              "here has earned a new\n"
              "      ceiling for THIS chain only -- record which cable "
              "received.\n" % (fm_config.MAX_SYMBOL_RATE_BD, ceiling))

    keying = fm_config.KEYING_VOX if args.vox else fm_config.KEYING_CM108

    chain = bench_chain(args.note, args.tx, args.rx)
    scratch = os.environ.get("TMPDIR") or "/tmp"

    # WHAT THE MIXER WAS, so the bench is handed back as it was found. A run
    # that leaves the transmit level wherever its last cell happened to put it
    # silently changes the configuration of whatever runs next -- and since
    # the level is not part of the waveform, nothing downstream would report
    # the difference.
    restore_levels = {}
    if any(c["tx_level"] is not None for c in cells) and args.channel:
        print("configuration refused:\n  tx_level sets a SOUND CARD's mixer "
              "and --channel has no sound card. Use drive=, which is the "
              "model's audio level into the limiter.", file=sys.stderr)
        return 2
    if any(c["tx_level"] is not None for c in cells) and not args.loopback:
        for dev in (args.tx, args.rx):
            if not dev:
                continue
            try:
                card = alsa_card_index(dev)
            except audio.BenchError as exc:
                print("configuration refused:\n  %s" % exc, file=sys.stderr)
                return 2
            was = read_tx_level(card, args.tx_level_control)
            if was is None:
                print("configuration refused:\n  cannot read %s on card %d, "
                      "so a tx_level run could not be undone afterwards"
                      % (args.tx_level_control, card), file=sys.stderr)
                return 2
            restore_levels[card] = was
        print("transmit level will be set per burst on %s; restoring "
              "afterwards: %s"
              % (args.tx_level_control,
                 ", ".join("card %d -> %g dB" % (c, v)
                           for c, v in sorted(restore_levels.items()))))

    # Every modem built up front, so an illegal configuration is an error
    # before a radio is keyed rather than halfway through a sweep. Profile
    # validates on construction and codec2 ABORTS THE PROCESS on two of the
    # same constraints, so this is also what keeps a bad sweep argument from
    # core-dumping the run.
    # A LinkConfig per rate as well as a modem, so the whole operating point
    # -- code, rate, tones, repeats, lead-in -- is validated together before a
    # radio is keyed, and so the burst duration this run reports comes from the
    # same object the transport will use rather than from the bench's own
    # arithmetic.
    # KEYED BY CELL, not by rate: two cells can share a rate and differ in
    # the code or the copy count, which is precisely the comparison this is
    # for. Keyed by rate, the second such cell would silently reuse the
    # first's modem and configuration and both would report the same burst.
    modems = {}
    configs = {}
    try:
        for cell in cells:
            prof = make_profile(cell["rs"], cell["first_tone"],
                                cell["spacing"], cell["tones"],
                                args.fs, ceiling, chain, slug=cell.slug)
            configs[cell.index] = fm_config.LinkConfig(
                name="bench-%s" % cell.slug, profile=prof,
                codename=cell["code"], repeats=cell["repeats"],
                keying=keying, lead_in_s=cell["lead_in"])
            modems[cell.index] = fm_modem.Codec2Fsk(prof, cell["code"])
    except (fm_config.ProfileError, fm_modem.ModemError) as exc:
        for m in modems.values():
            m.close()
        print("configuration refused:\n  %s" % exc, file=sys.stderr)
        return 2

    response = None
    unmeasured = []
    if args.channel and args.response != "flat":
        response = fm_channel.MEASURED_RESPONSES.get(args.response)
        if response is None:
            for m in modems.values():
                m.close()
            print("configuration refused:\n  no measured response named %r. "
                  "Have: %s, or 'flat'." % (
                      args.response,
                      ", ".join(sorted(fm_channel.MEASURED_RESPONSES))),
                  file=sys.stderr)
            return 2
        # ASKED BEFORE THE RUN, not discovered inside it. Response.taps()
        # HOLDS the response flat outside its measured span rather than
        # extrapolating, which is the right default for a filter and the
        # wrong thing to rank a tone placement on: the held region is a
        # placeholder, so a candidate that puts tones there would be scored
        # against no evidence at all. Today's desktop operating point (tones
        # from 1000 Hz) is exactly that case against the only measured
        # response, which spans 1500-3000 Hz.
        unmeasured = [c for c in cells
                      if not response.covers(
                          *configs[c.index].profile.tone_list())]
        if unmeasured and not args.allow_unmeasured:
            for m in modems.values():
                m.close()
            print("configuration refused:\n  %s was measured over "
                  "%.0f-%.0f Hz, and %d of %d cells place tones outside it "
                  "(%s). Outside its span the response is HELD FLAT, which "
                  "is a placeholder and not a measurement, so a survival "
                  "figure from those cells would rank nothing. Measure the "
                  "chain (tools/passband_sweep.py), choose tones inside the "
                  "span, or pass --allow-unmeasured and do not quote the "
                  "result." % (
                      response.name, response.low_hz, response.high_hz,
                      len(unmeasured), len(cells),
                      "; ".join(
                          "/".join(str(t) for t in
                                   configs[c.index].profile.tone_list())
                          for c in unmeasured)),
                  file=sys.stderr)
            return 2

    try:
        if args.channel:
            print("SIMULATED CHANNEL -- no radio. Absolute survival rates "
                  "from here are NOT field predictions;\n"
                  "               this model exists to RANK configurations "
                  "under one consistent insult.")
            print("response: %s\n     from %s"
                  % ("flat 300-3000 Hz MODEL (no measured chain -- it is "
                     "flat where no real chain is)" if response is None
                     else "%s, %.0f-%.0f Hz" % (response.name,
                                                response.low_hz,
                                                response.high_hz),
                     "tools/fm_channel.py's idealised passband"
                     if response is None else response.source))
            if unmeasured:
                print("     WARNING: %d cell(s) place tones outside the "
                      "measured span; held flat there, ranking nothing"
                      % len(unmeasured))
        print("%s  Fs %d Hz  %d cell%s, interleaved round by round"
              % ("simulated channel" if args.channel else
                 ("VOX" if args.vox else args.ptt_line) + " keying", args.fs,
                 len(cells), "" if len(cells) == 1 else "s"))
        for cell in cells:
            cfg = configs[cell.index]
            m = modems[cell.index]
            # EVERY field per cell, not just the varying ones. The label names
            # what is being compared, for reading a summary at a glance; this
            # is the record of what was actually on the air, and "which
            # configuration was this figure taken in" is the question this
            # project has had to re-answer most often.
            print("  %-34s %4d Bd  tones %-24s  %s  %d B/frame  %d cop%s  "
                  "lead-in %.2f s  burst %.2f s"
                  % (cell.label(varying) if varying else "baseline",
                     cell["rs"],
                     "/".join(str(t) for t in cfg.profile.tone_list()),
                     cell["code"], m.frame_payload_bytes, cell["repeats"],
                     "y" if cell["repeats"] == 1 else "ies",
                     cfg.lead_in_s, cfg.burst_duration_s()))
        # Where the airtime goes, itemised. The 4.4x this rebuild recovered
        # came from the code size, the repeat count and the lead-in rather
        # than from the waveform, and that was invisible while the burst was a
        # single number. Named for the cell it describes: in a multi-cell run
        # an unlabelled breakdown reads as if it covered all of them.
        print("  airtime, %s:"
              % (cells[0].label(varying) if varying else "baseline"))
        for line in configs[cells[0].index].budget_lines():
            print("      %s" % line)
        print()

        # EVERY comparison this run makes is interleaved at the finest
        # granularity available, not just the symbol rates.
        #
        # Interleaving the rates and leaving the DIRECTIONS as separate blocks
        # of runs is how a direction asymmetry came to be claimed at
        # p = 0.00022 on this bench and then retracted the same day: a bad
        # patch sitting over one direction's block reproduces the effect
        # exactly. If a thing is being compared, it belongs inside the round.
        if args.both_directions:
            directions = [("AtoB", args.tx, args.rx), ("BtoA", args.rx, args.tx)]
        else:
            directions = [("", args.tx, args.rx)]

        results = {(c.index, d[0]): [] for c in cells for d in directions}
        # Keyed on whether this run is a COMPARISON at all, rather than on
        # which flag produced it: more than one cell, or two directions, means
        # rounds are the unit and --rounds says how many. A single cell in one
        # direction is a plain batch and --trials says how long.
        rounds = args.rounds if (len(cells) > 1 or args.both_directions) \
            else args.trials
        log = CsvLog(args.csv, args.note, settings) \
            if args.csv else None
        interrupted = False
        try:
            for rnd in range(1, rounds + 1):
                for cell in cells:
                    m = modems[cell.index]
                    # PER CELL, because a cell's code decides its frame size:
                    # the 32 B and 128 B cells of the pending comparison carry
                    # 29 B and 125 B respectively when left to fill. That is
                    # the honest unit here -- airtime on this link is FLAT, so
                    # a frame costs its code's whole burst whatever is in it,
                    # and pinning a common --payload would shorten neither.
                    size = cell["payload"] or fm_framing.capacity_for(
                        m.frame_payload_bytes)
                    payload = audio.payload_for(size)
                    for dname, dtx, drx in directions:
                        label = "round %d/%d  %s%s" % (
                            rnd, rounds, cell.label(varying),
                            "  " + dname if dname else "")
                        if not args.quiet:
                            print("%s ..." % label)
                        try:
                            t = run_one(m, configs[cell.index], payload,
                                        args, scratch, tx=dtx, rx=drx,
                                        direction=dname, slug=cell.slug,
                                        tx_level=cell["tx_level"],
                                        rx_reset=cell["rx_reset"],
                                        snr_db=cell["snr_db"],
                                        drive=cell["drive"],
                                        response=response)
                        except audio.BenchError as exc:
                            # Hardware could not be driven: the trial did not
                            # happen. Reported as an error rather than counted
                            # as a failure -- a dead card is not a dead
                            # channel.
                            print("  ABORTED: %s" % exc, file=sys.stderr)
                            return 3
                        except (fm_burst.BurstTooLong,
                                fm_framing.FramingError) as exc:
                            print("  refused: %s" % exc, file=sys.stderr)
                            return 2
                        results[(cell.index, dname)].append(t)
                        # Banked before anything else can go wrong with it.
                        if log:
                            log.write(t)
                        if not args.quiet:
                            detail = "  -> %s  frames=%d exact=%d/%d" % (
                                "DELIVERED" if t.delivered else "lost",
                                t.frames, t.exact, t.repeats)
                            if t.rejected:
                                detail += " rejected=%d" % t.rejected
                            print(detail)
                            if not (args.loopback or args.channel):
                                print("     level: %s" % level_warning(t.peak))
                        if args.gap > 0:
                            import time
                            time.sleep(args.gap)
        except KeyboardInterrupt:
            # A stopped run is a SHORT run, not a lost one. Every row is
            # already on disk; summarise what was actually measured and say
            # plainly that it is partial, so a half-length batch cannot be
            # mistaken later for a full one.
            interrupted = True
            print("\n\nINTERRUPTED after %d trials -- rows already on disk."
                  % sum(len(v) for v in results.values()), file=sys.stderr)
        finally:
            if log:
                log.close()

        print()
        # One column width for the whole block, from the longest label in it:
        # the point of these lines is reading three cells against each other,
        # and ragged columns are how a per-copy rate gets compared against a
        # delivery figure by eye.
        labels = {(c.index, d[0]): c.label(varying)
                  + (" " + d[0] if d[0] else "")
                  for c in cells for d in directions}
        width = max(len(l) for l in labels.values())
        for cell in cells:
            for dname, _, _ in directions:
                print(summarise(labels[(cell.index, dname)],
                                results[(cell.index, dname)], width=width))

        all_trials = [t for c in cells for d in directions
                      for t in results[(c.index, d[0])]]
        if log:
            print("\n%s %d rows to %s"
                  % ("wrote (PARTIAL)" if interrupted else "wrote",
                     log.rows, args.csv))
        if interrupted:
            print("PARTIAL RUN: %d of %d rounds. Do not compare these cells "
                  "against a full batch without saying so." % (rnd, rounds))
            return 4

        # Exit non-zero when nothing at all arrived, so a wrapper script can
        # tell "measured, badly" from "measured nothing" -- but NOT on
        # partial loss, which is the normal shape of a result here.
        #
        # NOT IN --channel. Over the air, nothing arriving means the
        # instrument failed: a dead card, a closed squelch, a line that never
        # reached PTT. In the model there is no instrument to fail, and a
        # cell at 0% is not an error -- it is the BOTTOM OF THE SURVIVAL
        # CURVE, which is the whole thing a sweep is run to find. Returning 1
        # there would make every sweep that reached the cliff look like a
        # broken run, and the first sweep written against this mode did
        # exactly that.
        if args.channel:
            return 0
        return 0 if any(t.delivered for t in all_trials) else 1
    finally:
        for m in modems.values():
            m.close()
        for card, was in sorted(restore_levels.items()):
            try:
                back = set_tx_level(card, was, args.tx_level_control)
                print("restored card %d %s to %g dB"
                      % (card, args.tx_level_control, back))
            except audio.BenchError as exc:
                # Said loudly rather than swallowed: the next run on this
                # bench would otherwise inherit a level nobody chose.
                print("WARNING: could not restore card %d %s to %g dB: %s"
                      % (card, args.tx_level_control, was, exc),
                      file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
