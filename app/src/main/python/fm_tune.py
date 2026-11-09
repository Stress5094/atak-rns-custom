"""Measure a radio pair's audio path and recommend FM settings for it.

Phase D of the custom-settings plan, first slice: the TONE SWEEP. One phone
transmits a ladder of tones; the other records it and measures how each
frequency arrived. From that comes the usable band, the tilt, and a short
list of settings that fit -- each one buildable by fm_custom, so a
recommendation can never be something the modem would refuse.

WHY THIS EXISTS. The finding that fixed phone-to-phone delivery in the first
place was a passband one: tones at 1500/2000/2500/3000 Hz delivered 91% on
the desk bench and 0 of 9 between the phones, because the phones' radios
pass a narrower band. Every new radio pair is that question again, and the
answer has only ever come from a laptop.

THE SWEEP:
  * A 2.0 s lead tone first. Not VOX margin only: the handhelds measured a
    received level collapsing ~0.9 s into every transmission and then
    holding (fm_config.KEYING_LEAD_IN_S), so tones measured before that
    would read loud for a reason that has nothing to do with frequency.
  * 33 tones, 300-3500 Hz in 100 Hz steps, 0.24 s each (12 blocks of 20 ms).
  * In a FIXED SHUFFLED ORDER, not low-to-high. A slow level drift during
    the sweep (an AGC still settling) would otherwise read as a frequency
    tilt; shuffled, it scatters across the band instead.
  * EVERY SLOT ALSO CARRIES A REFERENCE TONE (REF_HZ), and a tone's level is
    its ratio to the reference IN THE SAME 20 ms. Found on the first real
    run (Pixel <- OnePlus, 2026-10-25): with one tone at a time the levels
    jumped 20 dB between neighbouring frequencies, and inside each 0.24 s
    slot the level was still CLIMBING (500 Hz 0.05 -> 0.10, 400 Hz 0.36 ->
    0.74) -- a gain somewhere in the chain moving over seconds, so a tone's
    level said when it was played, not how its frequency passed. A gain
    change scales both tones of a slot alike, so the ratio cancels it.
    Every ladder frequency is a multiple of 50 Hz, i.e. a whole number of
    cycles in a 20 ms block, so the two tones do not leak into each other's
    measurement.

THE ANALYSIS needs no clock sync: it finds where the 2 s lead tone ends,
then tries the ladder at that point give or take a few 20 ms blocks, and
keeps the offset where each slot's expected tone carries the largest share
of that slot's energy. One frequency per block, never all 33 -- the first
version measured every frequency in every block and did not finish within
a minute on the OnePlus (2026-10-25). Pure Python (the app has no numpy).

WHAT IT CANNOT TELL YOU, and the result says so: the radio's own automatic
level control flattens real differences, so this is the SHAPE of the path
at one volume setting, not an absolute gain; and a desk measurement says
nothing about range.

LAYERING: fm_config <- fm_custom <- fm_tune. No RNS, no native library.
"""

import json
import math
import random
from operator import mul

import fm_config
import fm_custom

FS = fm_config.SAMPLE_RATE_HZ
LADDER_HZ = tuple(range(300, 3501, 100))
BLOCK = FS // 50                 # 20 ms
SEG_BLOCKS = 12                  # 0.24 s per tone
SEG_S = SEG_BLOCKS * BLOCK / float(FS)
LEAD_S = 2.0
LEAD_HZ = 1000
AMP = 0.5
# The reference: in the middle of every band measured so far (desk
# 1000-3000, handhelds 1250-2500), so it is the tone most sure to arrive.
REF_HZ = 1800
# Each of the two tones in a slot; the sum peaks at AMP.
SLOT_AMP = AMP / 2.0
# The middle of each segment only: a tone change smears across a block or
# two through the radios' filters, and that smear is not the tone.
EDGE_BLOCKS = 2

# Fixed, so sender and receiver agree with nothing exchanged. Seeded once;
# changing the seed changes what every phone sends AND expects, so it is a
# wire format, effectively -- leave it.
_rng = random.Random(1025)
ORDER = list(range(len(LADDER_HZ)))
_rng.shuffle(ORDER)

SWEEP_S = LEAD_S + len(ORDER) * SEG_S

# The bar for a usable tone: within this of the best one, and arriving as
# its own frequency rather than as energy somewhere else (a weak fundamental
# under its own harmonics was measured on the desk bench at 800 Hz).
BAND_DB = 6.0
MIN_PURITY = 0.5
# The best alignment's mean purity below this means no sweep was heard.
# Purity is each tone's share of its own slot's energy (1 = clean, ~0.01 =
# noise); a narrow radio path leaves most of the 33 slots low, so this is
# deliberately well under what a good sweep scores.
FOUND_SCORE = 0.20
# Tone spread for a placement: ~5 dB delivered 7 of 9, 13-21 dB delivered
# nothing (2026-09-30). 6 dB is the bar a recommendation must clear.
SPREAD_DB = 6.0


class TuneError(Exception):
    """Something the operator needs to hear, in their words."""


# -- the sweep ----------------------------------------------------------------

def sweep_samples(fs=FS, gain=None):
    """Float samples peaking at AMP: the lead tone, then the shuffled ladder,
    each slot its test tone plus REF_HZ (the REF_HZ slot carries the
    reference alone). Phase-continuous per tone, so no change clicks.

    `gain(hz)` scales each tone -- for tests, to build what a radio path of
    known shape would deliver."""
    g = gain or (lambda hz: 1.0)
    out = []
    lead = int(round(LEAD_S * fs))
    step = 2.0 * math.pi * LEAD_HZ / fs
    a = AMP * g(LEAD_HZ)
    out.extend(a * math.sin(step * i) for i in range(lead))
    seg = int(round(SEG_S * fs))
    ref_step = 2.0 * math.pi * REF_HZ / fs
    ref_a = SLOT_AMP * g(REF_HZ)
    ref_phase = 0.0
    for k in ORDER:
        hz = LADDER_HZ[k]
        t_step = 2.0 * math.pi * hz / fs
        t_a = 0.0 if hz == REF_HZ else SLOT_AMP * g(hz)
        for i in range(seg):
            out.append(ref_a * math.sin(ref_phase) + t_a * math.sin(t_step * i))
            ref_phase += ref_step
    return out


def sweep_pcm16(fs=FS):
    import struct
    s = sweep_samples(fs)
    return struct.pack("<%dh" % len(s), *(int(round(v * 32767)) for v in s))


# -- the analysis -------------------------------------------------------------

# How far either side of the lead tone's end to search for the ladder. The
# lead's end is found to a block or two; a VOX radio that cut the START of
# the lead does not move its end.
SEARCH_BLOCKS = 5
# A block counts as lead tone when this much of its energy is at LEAD_HZ.
# Above 0.5: the ladder slot whose test tone is LEAD_HZ carries it at half
# the energy, beside the reference.
LEAD_SHARE = 0.7
# ...for at least this long: shorter than the 2 s sent, so a VOX radio
# switching on late still leaves a lead to find; longer than any single
# ladder segment (0.24 s), one of which is also at 1000 Hz.
LEAD_MIN_BLOCKS = 25

_TABLES = {}


def _table(hz):
    t = _TABLES.get(hz)
    if t is None:
        t = ([math.cos(2 * math.pi * hz * i / FS) for i in range(BLOCK)],
             [math.sin(2 * math.pi * hz * i / FS) for i in range(BLOCK)])
        _TABLES[hz] = t
    return t


def _purity(blk, hz, energy):
    """(power at hz, share of the block's energy that is at hz).

    The share needs no other frequency: a pure tone of amplitude A over N
    samples gives power (A*N/2)^2 against energy A^2*N/2, so 2*P/(N*E) is 1
    for a clean tone and ~2/N for noise. This is what keeps the analysis
    affordable on a phone -- one frequency per block, not thirty-three.
    """
    c, s = _table(hz)
    x = sum(map(mul, blk, c))
    y = sum(map(mul, blk, s))
    p = x * x + y * y
    return p, (2.0 * p / (BLOCK * energy)) if energy > 0 else 0.0


def _to_floats(pcm):
    if isinstance(pcm, (bytes, bytearray)):
        import array
        a = array.array("h")
        a.frombytes(bytes(pcm[:len(pcm) - len(pcm) % 2]))
        return [v / 32768.0 for v in a]
    return list(pcm)


def _lead_ends(blocks, energies):
    """Block indices where a run of lead tone ends -- candidate ladder starts."""
    ends, run = [], 0
    for b, (blk, e) in enumerate(zip(blocks, energies)):
        _, share = _purity(blk, LEAD_HZ, e)
        if share >= LEAD_SHARE:
            run += 1
        else:
            if run >= LEAD_MIN_BLOCKS:
                ends.append(b)
            run = 0
    if run >= LEAD_MIN_BLOCKS:
        ends.append(len(blocks))
    return ends


def analyse(pcm):
    """A recording (PCM16 bytes at FS, or float samples) -> the measurement.

    Returns a dict: found, offset_s (where the ladder starts), score, points
    [{hz, level_db, purity}], band_hz [lo, hi] or None, tilt_db_per_khz,
    notes. Raises TuneError when no sweep can be found -- with the reason an
    operator can act on.
    """
    samples = _to_floats(pcm)
    need = len(ORDER) * SEG_BLOCKS
    nb = len(samples) // BLOCK
    if nb < need:
        raise TuneError(
            "The recording is %.1f s; a sweep takes %.1f s. Listen for longer."
            % (len(samples) / float(FS), SWEEP_S))
    blocks = [samples[b * BLOCK:(b + 1) * BLOCK] for b in range(nb)]
    energies = [sum(map(mul, blk, blk)) for blk in blocks]
    peak = max(max(abs(v) for v in blk) for blk in blocks)

    mids = range(EDGE_BLOCKS, SEG_BLOCKS - EDGE_BLOCKS)
    cache = {}
    ref_k = LADDER_HZ.index(REF_HZ)

    def at(b, k):
        """(test power, reference power, test purity) for block b.

        Purity is the test tone's share of what is left once the reference
        is taken out: ~1 for a clean tone whatever its level, ~0 when the
        slot holds something else. For the REF_HZ slot it is the
        reference's own share."""
        key = (b, k)
        if key not in cache:
            p_ref, _ = _purity(blocks[b], REF_HZ, energies[b])
            if k == ref_k:
                p_t = p_ref
                rest = energies[b]
            else:
                p_t, _ = _purity(blocks[b], LADDER_HZ[k], energies[b])
                rest = energies[b] - 2.0 * p_ref / BLOCK
            pu = (2.0 * p_t / (BLOCK * rest)) if rest > 0 else 0.0
            cache[key] = (p_t, p_ref, pu)
        return cache[key]

    best, best_o = -1.0, None
    for end in _lead_ends(blocks, energies):
        for o in range(end - SEARCH_BLOCKS, end + SEARCH_BLOCKS + 1):
            if o < 0 or o + need > nb:
                continue
            sc = sum(at(o + i * SEG_BLOCKS + m, k)[2]
                     for i, k in enumerate(ORDER) if k != ref_k for m in mids)
            if sc > best:
                best, best_o = sc, o

    if best_o is None:
        if peak < 0.01:
            raise TuneError(
                "No audio came in at all (the loudest moment was %.3f of full "
                "scale). Check the receiving radio's volume and squelch, and "
                "that the other phone actually transmitted." % peak)
        raise TuneError(
            "Audio came in, but not the sweep's opening tone (a steady %d Hz "
            "for 2 s). Was the other phone sending the sweep, on the same "
            "channel?" % LEAD_HZ)
    score = best / ((len(ORDER) - 1) * len(mids))
    if score < FOUND_SCORE:
        raise TuneError(
            "The opening tone came through but the tones after it did not "
            "(clean %.0f%% where a good sweep scores 60-100%%). The audio "
            "path may be very narrow, or something else was on the channel."
            % (100 * score))

    # Level = test power over reference power, summed over the slot's
    # middle blocks: the ratio is what cancels a gain moving under the sweep.
    # The REF_HZ slot is 1 by definition (and its tone was sent at the same
    # amplitude as every test tone).
    raw = {}
    for i, k in enumerate(ORDER):
        base = best_o + i * SEG_BLOCKS
        vals = [at(base + m, k) for m in mids]
        ref = sum(v[1] for v in vals)
        ratio = 1.0 if k == ref_k else (sum(v[0] for v in vals) / ref if ref > 0 else 0.0)
        raw[LADDER_HZ[k]] = (ratio, sum(v[2] for v in vals) / len(vals))
    top = max(pw for pw, _ in raw.values()) or 1e-30
    points = [{"hz": hz,
               "level_db": round(10 * math.log10(max(raw[hz][0], 1e-30) / top), 1),
               "purity": round(min(raw[hz][1], 1.0), 2)}
              for hz in LADDER_HZ]

    band = _band(points)
    out = {"found": True, "offset_s": round(best_o * BLOCK / float(FS), 2),
           "score": round(score, 2), "points": points, "band_hz": band,
           "tilt_db_per_khz": _tilt(points, band), "notes": []}
    if band is None:
        out["notes"].append("No stretch of tones arrived evenly enough to use.")
    out["notes"].append(
        "This is the SHAPE of the audio path at this volume setting, not an "
        "absolute level: the radios' own level control evens out real "
        "differences. Measured on a desk, it says nothing about range.")
    return out


def _band(points):
    """The longest contiguous run of usable tones, as [lo, hi] Hz."""
    good = [p["level_db"] >= -BAND_DB and p["purity"] >= MIN_PURITY
            for p in points]
    best = (0, -1)
    i = 0
    while i < len(points):
        if good[i]:
            j = i
            while j + 1 < len(points) and good[j + 1]:
                j += 1
            if j - i > best[1] - best[0]:
                best = (i, j)
            i = j + 1
        else:
            i += 1
    if best[1] < best[0] or best[1] == best[0]:
        return None
    return [points[best[0]]["hz"], points[best[1]]["hz"]]


def _tilt(points, band):
    if band is None:
        return None
    xs = [p["hz"] / 1000.0 for p in points if band[0] <= p["hz"] <= band[1]]
    ys = [p["level_db"] for p in points if band[0] <= p["hz"] <= band[1]]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if not den:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den, 1)


# -- the recommendation -------------------------------------------------------

def _level_at(points, hz):
    """Measured level at hz, interpolated in dB; None outside the ladder."""
    if hz < points[0]["hz"] or hz > points[-1]["hz"]:
        return None
    for a, b in zip(points, points[1:]):
        if a["hz"] <= hz <= b["hz"]:
            t = (hz - a["hz"]) / float(b["hz"] - a["hz"])
            return a["level_db"] + t * (b["level_db"] - a["level_db"])
    return points[-1]["level_db"]


def _spread(points, tones_hz):
    levels = [_level_at(points, f) for f in tones_hz]
    if any(v is None for v in levels):
        return None, levels
    return round(max(levels) - min(levels), 1), levels


def recommend(measured, keying=fm_config.KEYING_CM108, limit=3):
    """Candidate settings for a measured path, best first, plus the closest
    preset as a control. Each is a dict with a buildable fm_custom spec.

    Shortest burst first among placements whose tones arrive within
    SPREAD_DB of each other; a rate above what any cable has been measured
    to follow is marked unproven rather than left out -- the verification
    run is what says whether it holds.
    """
    points = measured["points"]
    band = measured.get("band_hz")
    if not band:
        raise TuneError("No usable band was measured, so nothing can be "
                        "recommended. Check levels and measure again.")
    lo, hi = band
    found = []
    for tones in (4, 2):
        for rate in fm_custom.legal_rates(tones):
            span = (tones - 1) * rate
            if span > hi - lo:
                continue
            best = None
            f = lo
            while f + span <= hi:
                tl = [f + i * rate for i in range(tones)]
                sp, _ = _spread(points, tl)
                if sp is not None:
                    centre = abs((f + span / 2.0) - (lo + hi) / 2.0)
                    key = (sp, centre)
                    if best is None or key < best[0]:
                        best = (key, f, sp)
                f += 50
            if best is None:
                continue
            _, first, sp = best
            spec = fm_custom.CustomSpec(
                tones=tones, first_tone_hz=first, tone_spacing_hz=rate,
                symbol_rate_bd=rate, codename=fm_config.DEFAULT_CODE,
                repeats=1, keying=keying)
            try:
                cfg = fm_custom.link_config(spec)
            except fm_config.ProfileError:
                continue
            found.append(_candidate(spec, cfg, sp, keying))
    even = [c for c in found if c["spread_db"] <= SPREAD_DB]
    # Within what has been MEASURED first, shortest burst first. Then at most
    # one faster option that has not -- offered, labelled, never on top: the
    # fastest candidates are exactly the ones with the least behind them.
    proven = sorted((c for c in even if not c["unproven"]),
                    key=lambda c: (c["burst_s"], c["spread_db"]))
    unproven = sorted((c for c in even if c["unproven"]),
                      key=lambda c: (c["burst_s"], c["spread_db"]))
    picks = proven[:limit]
    if unproven and (not picks or unproven[0]["burst_s"] < picks[0]["burst_s"]):
        picks = picks[:limit - 1] + unproven[:1]
    if not picks:
        # Nothing clears the bar: say so, and offer the evenest anyway.
        picks = sorted(found, key=lambda c: c["spread_db"])[:limit]
        for c in picks:
            c["notes"].insert(0, "Tones arrive %.1f dB apart -- above the "
                              "~5 dB that has delivered before." % c["spread_db"])
    control = _closest_preset(points, keying)
    return {"candidates": picks, "control": control,
            "deemphasis_hint": _deemphasis_hint(measured)}


def _candidate(spec, cfg, spread, keying):
    notes = []
    rate = spec.symbol_rate_bd
    unproven = False
    if rate > fm_config.DIGIRIG_RX_CEILING_BD:
        unproven = True
        notes.append("Unproven: faster than any cable has been measured to "
                     "follow (500 Bd).")
    elif keying == fm_config.KEYING_VOX and rate > fm_config.K1PRO_RX_CEILING_BD:
        unproven = True
        notes.append("Unproven on a K1 PRO-class cable (measured to 250 Bd).")
    if spec.tones == 2 and rate == 1000:
        notes.append("Measured AGAINST: 2 tones at 1000 Bd delivered 20/42 "
                     "where Fast delivered 39/42, on two phones (2026-10-12).")
    return {"net_code": fm_custom.net_code(cfg), "spec": spec.to_dict(),
            "unproven": unproven,
            "tones_hz": cfg.profile.tone_list(), "spread_db": spread,
            "burst_s": round(cfg.burst_duration_s(), 2),
            "content_bytes": cfg.capacity_bytes() - fm_custom.MESH_ENVELOPE_B,
            "notes": notes}


def _closest_preset(points, keying):
    """The preset whose tones arrive most evenly here -- the control every
    verification run should include, so it measures 'better than what we
    had' and not merely 'works'."""
    best = None
    for name in fm_config.PROFILES:
        cfg = fm_config.link_config(name)
        sp, _ = _spread(points, cfg.profile.tone_list())
        if sp is None:
            continue
        if best is None or sp < best[0]:
            best = (sp, name, cfg)
    if best is None:
        return None
    sp, name, cfg = best
    spec = fm_custom.CustomSpec.from_preset(name)
    spec.keying = keying
    c = _candidate(spec, fm_custom.link_config(spec), sp, keying)
    c["preset"] = name
    return c


def _deemphasis_hint(measured):
    """A GUESS from the shape, said as one: a path rising steeply with
    frequency is what the K1 PRO receive tilt looked like, and that chain
    needed ~250 us of de-emphasis (3/48 copies without, 34/40 with)."""
    tilt = measured.get("tilt_db_per_khz")
    if tilt is not None and tilt > 4.0:
        return ("The level RISES %.1f dB per kHz. A receive path that tilts "
                "up like this needed 250 us of de-emphasis on the K1 PRO -- "
                "worth trying on the receiving phone." % tilt)
    return None


# -- JSON entry points (Kotlin), never raise ---------------------------------

def sweep_info_json():
    return json.dumps({"seconds": round(SWEEP_S, 2), "lead_s": LEAD_S,
                       "tones": len(LADDER_HZ), "low_hz": LADDER_HZ[0],
                       "high_hz": LADDER_HZ[-1], "sample_rate_hz": FS})


def analyse_and_recommend(pcm, keying=fm_config.KEYING_CM108):
    """Everything the result screen shows, as a dict; {"error": ...} on a
    sweep that could not be measured."""
    try:
        m = analyse(pcm)
        try:
            m["recommendation"] = recommend(m, keying)
        except TuneError as e:
            m["recommendation"] = {"error": str(e)}
        return m
    except TuneError as e:
        return {"found": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 -- the screen needs a reason
        return {"found": False, "error": "%s: %s" % (type(e).__name__, e)}
