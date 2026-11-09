"""Operator-entered FM settings: the "Custom" choice beside the presets.

WHY THIS EXISTS. The presets (fm_config.PROFILES) are each a placement
something was measured on, and that rule stays: nothing here adds to
PROFILES or changes what a preset builds. But the presets serve the radios
they were measured on, and the record's clearest finding is that a placement
does not transfer -- 1500/2000/2500/3000 Hz delivered 91% on one bench and 0
of 9 on the next. An operator with other radios needs to move the tones and
the rate, and without this the only way to do that is to edit fm_config.

WHAT AN OPERATOR CANNOT DO HERE: build a configuration codec2 would abort the
process on, or one over the burst ceiling. Every hard check is fm_config's
own (Profile._validate, LinkConfig._validate) -- this module adds none of its
own versions of them, only range checks on fields the presets never needed
to range-check, and WARNINGS: things that are legal but that the record has
evidence against. A warning never blocks; an error always does.

THE NET CODE. Two nodes that disagree on the tone COUNT, the symbol RATE or
the CODE recover nothing from each other -- silence, with no error at either
end. With three named presets operators could compare names; with free
settings they need something equally short. net_code() is that: one line
holding the settings the NET shares, and nothing that is one node's business
(lead-in, keying, de-emphasis and copies per burst -- the receiver demodulates
every copy independently and never sees the sender's lead-in or filter). A
custom spec equal to a preset gives the preset's code.

THE TONE FREQUENCIES ARE IN IT, AND THEY ARE A WEAKER REQUIREMENT than the
other three -- measured 2026-10-25 on clean synthetic loopback through the
real libcodec2 (test_fm_custom.TestWhatAMismatchActuallyDoes): codec2's
frequency estimator FINDS the tones rather than being told them, so a
receiver set 1200 Hz off, or at double the spacing, still decoded 3/3, and
`narrow` and `wide` (both 250 Bd) decode each other. The listen-before-
transmit detector also still fired on off-placement bursts, but reading
0.77-0.86 where matched tones read 1.00 -- margin that real bursts, which
read 0.59-0.63 on the air, may not have. NONE OF THIS HAS BEEN ON A RADIO.
The tones stay in the code because they are what the radios were chosen to
pass and because the on-air cost of a mismatch is unmeasured; the code
should not be read as "different tones means silence".

LAYERING: fm_framing <- fm_config <- fm_custom <- fm_link. No RNS, no native
library, so Kotlin's settings screen can call describe_spec() before the
bridge has started, and the desktop tests need neither.

Every Kotlin-facing function takes and returns JSON strings and never raises.
"""

import json
import re

import fm_config

# The name a custom configuration runs under. Not in fm_config.PROFILES, by
# design: PROFILES is "measured placements", and resolve()'s fallback runs
# through it.
CUSTOM_NAME = "custom"

# The mesh envelope every FM frame carries (mesh.OVERHEAD: 8 B header + 4 B
# tag). Restated rather than imported because mesh pulls in the AEAD backend,
# which this module must not need; test_fm_custom asserts the two agree.
MESH_ENVELOPE_B = 12

# What a position report needs from the codec, measured 2026-10-02 when the
# 32 B code was found unable to carry one (see fm_config.OPERATING_POINT):
# 26 B with identity stripped entirely, 55 B with a real ATAK uid + callsign.
POSITION_FLOOR_B = 26
POSITION_TYPICAL_B = 55

# Bursts above this are legal (the ceiling is fm_config.MAX_BURST_S) but sit
# where the old bench first saw whole attempts fail: 5.55 s is the longest
# configuration offered for the owed A/B, and ~7.4 s measured 0/10.
LONG_BURST_S = 5.6

# Range checks on fields the presets never needed. Wider than anything
# measured, narrower than nonsense: the point is to turn a typo into a
# message, not to second-guess a real radio.
#   * Below ~300 Hz an FM radio's audio is filtered to strip sub-audible
#     squelch tones, so a tone there is not attenuated but removed.
#   * Lead-in: the longest any chain has needed is 2.2 s (VOX handhelds).
#   * De-emphasis: measured insensitive over 50-900 us; past ~2000 the filter
#     removes the signal it is meant to level.
#   * Copies: 3 is the most any bench run used, and in-burst copies are not
#     reliability (see fm_config.DEFAULT_REPEATS).
MIN_FIRST_TONE_HZ = 300
MAX_LEAD_IN_S = 3.0
MAX_DEEMPHASIS_US = 2000.0
MAX_REPEATS = 3
MIN_SYMBOL_RATE_BD = 100

# The lead-in and de-emphasis a VOX cable gets when the spec does not say.
# The same figures the existing "cable has no PTT pin" option uses
# (fm_link.set_profile(vox=True)), so choosing VOX here and ticking that box
# build the same burst.
_DEFAULT_LEAD_IN_S = {fm_config.KEYING_CM108: fm_config.KEYING_LEAD_IN_S[fm_config.KEYING_CM108],
                      fm_config.KEYING_VOX: fm_config.VOX_CABLE_LEAD_IN_S}
_DEFAULT_DEEMPHASIS_US = {fm_config.KEYING_CM108: 0.0,
                          fm_config.KEYING_VOX: fm_config.VOX_CABLE_DEEMPHASIS_US}


class SpecError(fm_config.ProfileError):
    """A custom spec that cannot be built. A ProfileError subclass, so a
    caller asking only "can this link come up" catches it with the rest."""


# -- the spec -----------------------------------------------------------------

_FIELDS = ("tones", "first_tone_hz", "tone_spacing_hz", "symbol_rate_bd",
           "codename", "repeats", "keying", "lead_in_s", "deemphasis_us")

# The evidence a spec can carry, written by the in-app tuning test (Phase D)
# so evidence_line() reports what was measured instead of "unmeasured".
_EVIDENCE_FIELDS = ("measured_on", "delivered", "usable_band_hz",
                    "tone_spread_db", "radios", "cables")


class CustomSpec(object):
    """Every operator-settable field, unvalidated until built.

    `lead_in_s` and `deemphasis_us` may be None, meaning "the keying's
    default" -- stored as None rather than resolved, so a spec saved with VOX
    keying and switched to PTT does not keep VOX's 2.2 s lead-in.
    """

    def __init__(self, tones=4, first_tone_hz=1000, tone_spacing_hz=400,
                 symbol_rate_bd=400, codename=fm_config.DEFAULT_CODE,
                 repeats=1, keying=fm_config.KEYING_CM108, lead_in_s=None,
                 deemphasis_us=None, evidence=None):
        self.tones = tones
        self.first_tone_hz = first_tone_hz
        self.tone_spacing_hz = tone_spacing_hz
        self.symbol_rate_bd = symbol_rate_bd
        self.codename = codename
        self.repeats = repeats
        self.keying = keying
        self.lead_in_s = lead_in_s
        self.deemphasis_us = deemphasis_us
        self.evidence = dict(evidence or {})

    @classmethod
    def from_preset(cls, name):
        """The spec a preset builds, as a starting point for the editor.
        Raises KeyError on a name that is not a preset -- the editor's "start
        from" buttons must copy what they say, not the fallback."""
        if name not in fm_config.PROFILES:     # link_config would fall back
            raise KeyError(name)
        cfg = fm_config.link_config(name)
        prof = cfg.profile
        # The lead-in and filter are copied only where the preset's CHAIN
        # overrides the keying default -- `narrow` carries the handheld pair's
        # measured 1.6 s. Otherwise left None, so they keep following the
        # keying if the operator changes it.
        lead = cfg.lead_in_s
        if lead == _DEFAULT_LEAD_IN_S.get(cfg.keying):
            lead = None
        de = cfg.deemphasis_us()
        if de == _DEFAULT_DEEMPHASIS_US.get(cfg.keying):
            de = None
        return cls(tones=prof.tones, first_tone_hz=prof.first_tone_hz,
                   tone_spacing_hz=prof.tone_spacing_hz,
                   symbol_rate_bd=prof.symbol_rate_bd, codename=cfg.codename,
                   repeats=cfg.repeats, keying=cfg.keying, lead_in_s=lead,
                   deemphasis_us=de)

    def to_dict(self):
        d = {k: getattr(self, k) for k in _FIELDS}
        if self.evidence:
            d["evidence"] = dict(self.evidence)
        return d

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            raise SpecError("custom FM settings must be a JSON object, got %s"
                            % type(d).__name__)
        unknown = sorted(set(d) - set(_FIELDS) - {"evidence"})
        if unknown:
            # Refused rather than ignored: a misspelt field silently taking
            # its default is a setting the operator believes is in force.
            raise SpecError("unknown custom FM setting(s): %s"
                            % ", ".join(unknown))
        kw = {}
        for k in ("tones", "first_tone_hz", "tone_spacing_hz",
                  "symbol_rate_bd", "repeats"):
            if k in d:
                kw[k] = _as_int(k, d[k])
        for k in ("lead_in_s", "deemphasis_us"):
            if k in d and d[k] is not None:
                kw[k] = _as_float(k, d[k])
        for k in ("codename", "keying"):
            if k in d:
                kw[k] = str(d[k])
        ev = d.get("evidence") or {}
        if not isinstance(ev, dict):
            raise SpecError("evidence must be a JSON object")
        unknown = sorted(set(ev) - set(_EVIDENCE_FIELDS))
        if unknown:
            raise SpecError("unknown evidence field(s): %s" % ", ".join(unknown))
        kw["evidence"] = ev
        return cls(**kw)

    @classmethod
    def from_json(cls, text):
        try:
            d = json.loads(text)
        except (TypeError, ValueError) as e:
            raise SpecError("custom FM settings are not valid JSON: %s" % e)
        return cls.from_dict(d)

    def resolved_lead_in_s(self):
        if self.lead_in_s is not None:
            return self.lead_in_s
        return _DEFAULT_LEAD_IN_S.get(self.keying)

    def resolved_deemphasis_us(self):
        if self.deemphasis_us is not None:
            return self.deemphasis_us
        return _DEFAULT_DEEMPHASIS_US.get(self.keying, 0.0)

    def __eq__(self, other):
        return isinstance(other, CustomSpec) and self.to_dict() == other.to_dict()

    def __ne__(self, other):
        return not self == other

    def __repr__(self):
        return "<CustomSpec %s>" % self.to_json()


def _as_int(name, value):
    # bool is an int in Python; true/false for a frequency is a bug upstream.
    if isinstance(value, bool):
        raise SpecError("%s must be a whole number, got %r" % (name, value))
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise SpecError("%s must be a whole number, got %r" % (name, value))
    if f != int(f):
        raise SpecError("%s must be a whole number, got %r" % (name, value))
    return int(f)


def _as_float(name, value):
    if isinstance(value, bool):
        raise SpecError("%s must be a number, got %r" % (name, value))
    try:
        return float(value)
    except (TypeError, ValueError):
        raise SpecError("%s must be a number, got %r" % (name, value))


# -- building -----------------------------------------------------------------

def _chain_for(spec):
    """The Chain a custom placement stands on: the operator's say-so, unless
    the tuning test has written evidence into the spec.

    The ceiling is set to the requested rate, as PHONE_PAIR_TWOTONE_TRIAL
    does: the operator is the one asserting their cable tracks it, and the
    warnings say what the record knows about that. The band is checked only
    when MEASURED (evidence.usable_band_hz) -- an unswept chain gets no band
    check rather than a borrowed one, fm_config's own rule.
    """
    ev = spec.evidence
    band = ev.get("usable_band_hz")
    if band is not None:
        try:
            band = (int(band[0]), int(band[1]))
        except (TypeError, ValueError, IndexError):
            raise SpecError("evidence.usable_band_hz must be [low, high] Hz, "
                            "got %r" % (band,))
    measured = bool(ev.get("measured_on"))
    return fm_config.Chain(
        name="operator-entered" + ("" if measured else " (UNMEASURED)"),
        radios=ev.get("radios") or "the operator's radios",
        cables=ev.get("cables") or "the operator's cables",
        measured_on=ev.get("measured_on") or "NOT MEASURED",
        delivered=ev.get("delivered") or "nothing measured; entered by hand",
        tone_spread_db=ev.get("tone_spread_db"),
        sample_rate_hz=fm_config.SAMPLE_RATE_HZ,
        symbol_rate_ceiling_bd=spec.symbol_rate_bd,
        usable_band_hz=band,
        deemphasis_us=spec.resolved_deemphasis_us(),
    )


def _check_ranges(spec):
    """The checks fm_config never needed, because no preset can fail them."""
    if spec.keying not in _DEFAULT_LEAD_IN_S:
        raise SpecError("keying must be %s, got %r"
                        % (" or ".join(sorted(_DEFAULT_LEAD_IN_S)), spec.keying))
    if spec.codename not in fm_config.CODES:
        raise SpecError("unknown error-correction code %r; have %s"
                        % (spec.codename, ", ".join(sorted(fm_config.CODES))))
    if spec.first_tone_hz < MIN_FIRST_TONE_HZ:
        raise SpecError(
            "first tone %d Hz is below %d Hz. FM radios filter that region out "
            "to strip sub-audible squelch tones, so a tone there is removed, "
            "not weakened." % (spec.first_tone_hz, MIN_FIRST_TONE_HZ))
    if spec.symbol_rate_bd < MIN_SYMBOL_RATE_BD:
        raise SpecError("symbol rate %d Bd is below %d Bd"
                        % (spec.symbol_rate_bd, MIN_SYMBOL_RATE_BD))
    if not 1 <= spec.repeats <= MAX_REPEATS:
        raise SpecError("copies per burst must be 1-%d, got %d"
                        % (MAX_REPEATS, spec.repeats))
    lead = spec.resolved_lead_in_s()
    if not 0.0 <= lead <= MAX_LEAD_IN_S:
        raise SpecError("lead-in must be 0-%.1f s, got %.2f s"
                        % (MAX_LEAD_IN_S, lead))
    de = spec.resolved_deemphasis_us()
    if not 0.0 <= de <= MAX_DEEMPHASIS_US:
        raise SpecError("de-emphasis must be 0-%.0f us, got %.0f us"
                        % (MAX_DEEMPHASIS_US, de))


def link_config(spec, sample_rate_hz=fm_config.SAMPLE_RATE_HZ, vox=False):
    """The LinkConfig a spec builds. Raises SpecError / ProfileError with the
    message to show the operator.

    `vox=True` is the existing "cable has no PTT pin" option: a cable with no
    GPIO cannot be keyed by one whatever the spec says, so it forces VOX
    keying -- and, where the spec left them to default, VOX's lead-in and
    filter, exactly as the preset path does. Explicit spec values win.

    `sample_rate_hz` is for the desktop bench, which runs 48000; the phone is
    always fm_config.SAMPLE_RATE_HZ.
    """
    if vox and spec.keying != fm_config.KEYING_VOX:
        d = spec.to_dict()
        d["keying"] = fm_config.KEYING_VOX
        spec = CustomSpec.from_dict(d)
    _check_ranges(spec)
    prof = fm_config.Profile(
        name=CUSTOM_NAME, first_tone_hz=spec.first_tone_hz,
        tone_spacing_hz=spec.tone_spacing_hz, tones=spec.tones,
        symbol_rate_bd=spec.symbol_rate_bd, chain=_chain_for(spec),
        sample_rate_hz=sample_rate_hz)
    return fm_config.LinkConfig(
        name=CUSTOM_NAME, profile=prof, codename=spec.codename,
        repeats=spec.repeats, keying=spec.keying,
        lead_in_s=spec.resolved_lead_in_s(),
        deemphasis_us=spec.resolved_deemphasis_us())


# -- the net code -------------------------------------------------------------

def _code_token(codename):
    # "H_1024_2048_4f" -> "H1024.2048". The (data, codeword) pair is unique
    # across fm_config.CODES (asserted in the tests); the suffix is codec2's
    # internal variant name and means nothing read aloud.
    k, n = fm_config.CODES[codename]
    return "H%d.%d" % (k, n)


def _codename_for_token(token):
    for name in fm_config.CODES:
        if _code_token(name) == token:
            return name
    raise SpecError("unknown code %r in net code; have %s"
                    % (token, ", ".join(_code_token(c) for c in sorted(fm_config.CODES))))


def net_code_for(tones, first_tone_hz, tone_spacing_hz, symbol_rate_bd,
                 codename):
    """e.g. "4T-1000/400-400Bd-H1024.2048" -- tones, first tone / spacing,
    rate, code. Exactly the fields a receiver must share with the sender."""
    return "%dT-%d/%d-%dBd-%s" % (tones, first_tone_hz, tone_spacing_hz,
                                  symbol_rate_bd, _code_token(codename))


def net_code(cfg):
    """The net code of any LinkConfig, preset or custom."""
    p = cfg.profile
    return net_code_for(p.tones, p.first_tone_hz, p.tone_spacing_hz,
                        p.symbol_rate_bd, cfg.codename)


_NET_CODE_RE = re.compile(
    r"^\s*(\d+)T-(\d+)/(\d+)-(\d+)Bd-(H\d+\.\d+)\s*$", re.IGNORECASE)


def parse_net_code(code):
    """A spec holding the net-wide settings from `code`; the per-node ones
    (copies, keying, lead-in, filter) at their defaults. Raises SpecError.

    Only parses: whether the result is BUILDABLE is link_config()'s job, so a
    code typed from another node gets the same messages as the editor.
    """
    m = _NET_CODE_RE.match(code or "")
    if not m:
        raise SpecError(
            "%r is not a net code; expected something like %s"
            % (code, net_code(fm_config.OPERATING_POINT)))
    tones, first, spacing, rate = (int(m.group(i)) for i in range(1, 5))
    token = m.group(5).upper()
    return CustomSpec(tones=tones, first_tone_hz=first,
                      tone_spacing_hz=spacing, symbol_rate_bd=rate,
                      codename=_codename_for_token(token))


def matching_preset(cfg):
    """The preset with the same net code, or None. A custom spec that equals
    a preset on the air says so, because 'custom' alone would suggest it
    cannot talk to nodes on that preset."""
    code = net_code(cfg)
    for name in fm_config.PROFILES:
        if net_code(fm_config.link_config(name)) == code:
            return name
    return None


# -- what the editor shows ----------------------------------------------------

def legal_rates(tones=4, sample_rate_hz=fm_config.SAMPLE_RATE_HZ):
    """Symbol rates codec2 can construct at this sample rate, fastest first.

    Every rate that divides the sample rate exactly (codec2 asserts that and
    aborts) with at least MIN_SAMPLES_PER_SYMBOL per symbol, and whose
    alphabet at the tightest spacing still fits under Nyquist from the lowest
    allowed first tone. Whether a rate also fits the burst ceiling depends on
    the code and copies, which describe() reports.
    """
    top = sample_rate_hz // fm_config.MIN_SAMPLES_PER_SYMBOL
    out = []
    for r in range(top, MIN_SYMBOL_RATE_BD - 1, -1):
        if sample_rate_hz % r:
            continue
        if MIN_FIRST_TONE_HZ + (tones - 1) * r >= sample_rate_hz / 2.0:
            continue
        out.append(r)
    return out


def codes():
    """Every code a link can actually run, smallest first, with what it
    carries -- for the dropdown. Capacity is the real per-burst MTU, not the
    codec2 data size.

    A code fm_config cannot frame is LEFT OUT rather than offered and
    refused: H_4096_8192_3d's 509 B does not fit fm_framing's 1-byte length
    prefix, and widening that costs a byte on every frame.
    """
    rows = []
    for name in sorted(fm_config.CODES, key=lambda c: fm_config.CODES[c]):
        try:
            cap = fm_config.LinkConfig(
                name="probe", profile=fm_config.FAST, codename=name,
                repeats=1, max_burst_s=1e9).capacity_bytes()
        except fm_config.ProfileError:
            continue
        rows.append({"codename": name, "token": _code_token(name),
                     "rate": "1/%.0f" % (1.0 / fm_config.code_rate(name)),
                     "capacity_bytes": cap})
    return rows


def warnings_for(cfg):
    """Legal, but the record has evidence against it. Plain words, each with
    the measurement behind it."""
    out = []
    p = cfg.profile
    rate = p.symbol_rate_bd

    if rate > fm_config.DIGIRIG_RX_CEILING_BD:
        out.append(
            "%d Bd is faster than any cable has been measured to follow "
            "(500 Bd, Digirig Lite receiving). 600 Bd lost whole messages on "
            "the desk at full signal." % rate)
    elif (rate > fm_config.K1PRO_RX_CEILING_BD
          and cfg.keying == fm_config.KEYING_VOX):
        # Only on VOX: the K1 PRO is the VOX-class cable, and the one measured
        # this slow. Warning a Digirig user at 400 Bd would be noise on the
        # default.
        out.append(
            "%d Bd is above the 250 Bd a K1 PRO-class receiving cable "
            "measured; a Digirig Lite has followed 400-500 Bd." % rate)

    low, high = fm_config.MEASURED_USABLE_BAND_HZ
    if p.first_tone_hz < low or p.top_tone_hz() > high:
        out.append(
            "Tones %s Hz go outside %d-%d Hz, the band measured on the desk "
            "radios (the handheld pair measured narrower, 1250-2500). A tone "
            "outside the band is lost, not weakened: a 400 Hz first tone "
            "delivered 0%%." % ("/".join(str(t) for t in p.tone_list()),
                                low, high))

    budget = cfg.capacity_bytes() - MESH_ENVELOPE_B
    if budget < POSITION_FLOOR_B:
        out.append(
            "This code leaves %d B per burst for content. A position report "
            "needs at least %d B, so positions cannot be sent in one burst "
            "and fall back to many (28 s each on the 32 B code)."
            % (budget, POSITION_FLOOR_B))
    elif budget < POSITION_TYPICAL_B:
        out.append(
            "This code leaves %d B per burst for content; a typical position "
            "report with its ATAK identity is ~%d B." % (budget, POSITION_TYPICAL_B))

    burst = cfg.burst_duration_s()
    if burst > LONG_BURST_S:
        out.append(
            "%.1f s per burst. Longer bursts are where the bench first lost "
            "whole attempts (a ~7.4 s burst measured 0 of 10), and every "
            "burst holds the channel for everyone." % burst)

    if cfg.repeats > 1:
        out.append(
            "%d copies per burst: the copies share one burst, so they fail "
            "together in a bad patch. Resending later is what survives one."
            % cfg.repeats)

    if (cfg.keying == fm_config.KEYING_VOX
            and cfg.lead_in_s < fm_config.VOX_CABLE_LEAD_IN_S):
        out.append(
            "%.2f s lead-in on a VOX cable. The handheld radios measured "
            "started transmitting 0.34-0.72 s after the audio, and with too "
            "short a lead-in 0 of 3 bursts decoded." % cfg.lead_in_s)
    return out


def evidence_text(cfg):
    """What a custom configuration stands on, in a sentence. Profile's own
    evidence_line() is phrased for a measured chain and reads as nonsense for
    one that was not ("measured nothing ... on NOT MEASURED")."""
    chain = cfg.profile.chain
    if chain is None or not chain.measured_on or chain.measured_on == "NOT MEASURED":
        return ("Custom settings, not measured: entered by hand, so nothing "
                "on record says they work on any radio.")
    return cfg.evidence_line()


def describe(spec, vox=False):
    """Everything the editor's preview needs, as a dict. Never raises."""
    out = {"ok": False, "error": "", "warnings": [], "net_code": "",
           "matches_preset": None}
    try:
        cfg = link_config(spec, vox=vox)
    except fm_config.ProfileError as e:
        # fm_config prefixes the profile name ("custom: ..."), which on the
        # editor's own screen says nothing.
        msg = str(e)
        prefix = CUSTOM_NAME + ": "
        out["error"] = msg[len(prefix):] if msg.startswith(prefix) else msg
        # Still offer the net code if the fields parse, so the operator can
        # see what they typed even while it is illegal.
        try:
            out["net_code"] = net_code_for(spec.tones, spec.first_tone_hz,
                                           spec.tone_spacing_hz,
                                           spec.symbol_rate_bd, spec.codename)
        except Exception:
            pass
        return out
    p = cfg.profile
    out.update({
        "ok": True,
        "warnings": warnings_for(cfg),
        "net_code": net_code(cfg),
        "matches_preset": matching_preset(cfg),
        "tones_hz": p.tone_list(),
        "symbol_rate_bd": p.symbol_rate_bd,
        "raw_bps": p.raw_bps(),
        "burst_s": round(cfg.burst_duration_s(), 3),
        "capacity_bytes": cfg.capacity_bytes(),
        "content_bytes": cfg.capacity_bytes() - MESH_ENVELOPE_B,
        "keying": cfg.keying,
        "lead_in_s": cfg.lead_in_s,
        "deemphasis_us": cfg.deemphasis_us(),
        "budget": cfg.budget_lines(),
        "summary": cfg.summary_line(),
        "evidence": evidence_text(cfg),
    })
    return out


# -- Kotlin entry points: JSON in, JSON out, never raise ----------------------

def describe_json(spec_json, vox=False):
    try:
        spec = CustomSpec.from_json(spec_json)
    except fm_config.ProfileError as e:
        return json.dumps({"ok": False, "error": str(e), "warnings": [],
                           "net_code": "", "matches_preset": None})
    return json.dumps(describe(spec, vox=bool(vox)))


def preset_spec_json(name):
    """A preset as a spec, for the editor's "start from" buttons. "" on an
    unknown name."""
    try:
        return CustomSpec.from_preset(name).to_json()
    except KeyError:
        return ""


def spec_from_net_code_json(code):
    """{"spec": {...}} or {"error": "..."} for a typed or scanned net code."""
    try:
        return json.dumps({"spec": parse_net_code(code).to_dict()})
    except fm_config.ProfileError as e:
        return json.dumps({"error": str(e)})


def choices_json(tones=4):
    """The editor's dropdown contents. Kotlin keeps no list of its own, so
    there is nothing to drift."""
    return json.dumps({
        "rates": legal_rates(int(tones)),
        "codes": codes(),
        "keyings": sorted(_DEFAULT_LEAD_IN_S),
        "max_repeats": MAX_REPEATS,
        "min_first_tone_hz": MIN_FIRST_TONE_HZ,
        "max_lead_in_s": MAX_LEAD_IN_S,
        "default_spec": CustomSpec.from_preset(fm_config.DEFAULT_PROFILE).to_dict(),
    })
