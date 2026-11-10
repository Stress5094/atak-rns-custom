"""
RF coverage map for the Coverage tab.

Computes how far a radio reaches from the phone's location, from real
building footprints (Overture Maps, falling back to OpenStreetMap Overpass)
and SRTM terrain, plus the
radio's own link budget. The model: line-of-sight with the ground-reflection
two-ray loss up to the first building or hill, then knife-edge diffraction
over that obstruction, and "covered" wherever the received signal is still
above the radio's sensitivity.

Pure standard library (urllib, json, gzip, struct, math) on purpose: this
runs inside Chaquopy without importing RNS or any third-party package, so it
can answer before the mesh starts and its tests run on a plain desktop
Python with no dependencies.

Called from Kotlin via Chaquopy:

    py.getModule("coverage").callAttr("compute", lat, lon, radio_key)

`compute` returns a JSON string and never raises -- a failure (no network,
bad data) comes back as {"error": "..."} so the tab can say why instead of
crashing.

Three deliberate simplifications, stated because a coverage map is only as
honest as its caveats:

  - Building HEIGHTS come from Overture (real where Overture has them, else
    floors x 3 m or a class default). Only the OSM fallback estimates every
    height, since OSM rarely tags them. Either way the
    line-of-sight blockage -- the dominant effect at street level -- does not
    depend on height at all.
  - Every building the ray crosses is charged a full knife-edge loss, summed.
    One that only grazes the ray is over-charged, so ranges along streets
    come out roughly 2x short of the hand analysis. A "graze discount" is
    the open fix.
  - The two-ray model assumes flat ground between the antennas; terrain is
    folded in as the obstruction check rather than a full irregular-terrain
    integral. Good enough for "which way and how far", not a survey.
"""

import array
import collections
import gzip
import io
import json
import math
import os
import sys
import threading
import time
import urllib.request
import zlib

import overture_tiles
import radio as lora_presets

# --- radios ----------------------------------------------------------------

# The two the Coverage tab offers. TX power is what the operator's actual
# hardware transmits; sensitivity is the receiver floor (LoRa Short Slow vs
# the analog-FM 12 dB SINAD figure). See the field notes in the repo docs for
# where the FM numbers came from.
#
# LoRa is the preset every radio runs as of 2026-10-15: Short Slow (SF8,
# 250 kHz) at 22 dBm. -124 dBm is CALCULATED, not measured:
# -174 + 10*log10(250e3) + NF 6 - SNR limit 10 (SF8) = -124. SX1276's
# datasheet gives -123 for SF8/250k; Meshtastic's site planner says -131.5,
# ~7.5 dB more optimistic than either. This replaced 28 dBm / -139 (LongFast),
# which overstated the budget by ~21 dB and made LoRa look better than FM.
RADIOS = {
    "lora":   {"label": "LoRa 915 MHz (Short Slow, 22 dBm)", "freq_mhz": 915.0, "tx_dbm": 22.0, "sens_dbm": -124.0},
    # One FM entry, at whatever frequency the operator sets (fm_mhz): VHF or
    # UHF is only a label, read off the frequency (fm_band). Until 2026-10-20
    # there were two, fm_vhf and fm_uhf, each with its own frequency and power.
    "fm":     {"label": "FM 146 MHz",            "freq_mhz": 146.0, "tx_dbm": 40.0, "sens_dbm": -120.0},
}

ANT_GAIN_DBI = 2.15   # dipole on both ends
ANT_HEIGHT_M = 1.5    # body-worn / handheld; the receiver always, the
                      # transmitter unless the operator sets its height
TX_HEIGHT_RANGE_M = (0.5, 300.0)
# What the FM frequency fields accept: wider than any one radio's band on
# purpose (the radio decides what it can tune); the model only needs a
# wavelength. Outside this it is a typo, not a radio.
FM_FREQ_RANGE_MHZ = (30.0, 1000.0)
# Watts, as a handheld's power menu states it (a Baofeng's 1/4/5 W).
FM_POWER_RANGE_W = (0.1, 100.0)
# Where VHF stops and UHF starts, for the label only: the model itself
# needs nothing but the wavelength.
FM_UHF_FROM_MHZ = 300.0


def fm_band(freq_mhz):
    return "UHF" if freq_mhz >= FM_UHF_FROM_MHZ else "VHF"

# LoRa receive sensitivity is CALCULATED from the radio section's preset:
# thermal noise over the bandwidth, the receiver's noise figure, and the SNR
# each spreading factor still demodulates at (Semtech SX1276/SX1262
# datasheets). Short Slow (SF8/250 kHz) gives the -124 dBm RADIOS["lora"]
# has carried since 2026-10-15; the datasheet tables are ~1 dB either side.
LORA_NOISE_FIGURE_DB = 6.0
LORA_SNR_LIMIT_DB = {6: -5.0, 7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0,
                     11: -17.5, 12: -20.0}


def lora_sensitivity_dbm(sf, bw_hz):
    return round(-174.0 + 10.0 * math.log10(bw_hz) + LORA_NOISE_FIGURE_DB
                 + LORA_SNR_LIMIT_DB[sf], 1)


def _number(params, key, lo, hi, what):
    try:
        v = float(params[key])
    except (TypeError, ValueError):
        raise ValueError("%s must be a number" % what)
    if not (lo <= v <= hi) or v != v:
        raise ValueError("%s must be between %g and %g" % (what, lo, hi))
    return v


def resolve_radio(radio_key, params=None):
    """The radio to model: RADIOS[radio_key] with the operator's settings
    applied. Raises ValueError, with a reason written for the operator, on a
    setting the model cannot use.

    params (all optional; a missing one keeps RADIOS' value):
      lora_preset, lora_freq_mhz, lora_tx_dbm, lora_source
                        -- the LoRa radio in use (rns_bridge.coverage_radio:
                           what the RNode reports or the Meshtastic board is
                           set to); lora_source is shown in the label
      fm_mhz, fm_w      -- the Coverage tab's own
    """
    if radio_key not in RADIOS:
        raise ValueError("unknown radio %r" % radio_key)
    params = params or {}
    r = dict(RADIOS[radio_key])
    if radio_key == "lora":
        preset = params.get("lora_preset")
        if preset is not None:
            sfbw = lora_presets.preset_params(preset)
            if sfbw is None:
                raise ValueError("unknown LoRa preset %r" % preset)
            sf, bw, _ = sfbw
            r["sens_dbm"] = lora_sensitivity_dbm(sf, bw)
        else:
            preset, sf, bw = "Short Slow", 8, 250000
        if "lora_freq_mhz" in params:
            r["freq_mhz"] = _number(params, "lora_freq_mhz", 100.0, 3000.0,
                                    "LoRa frequency (MHz)")
        if "lora_tx_dbm" in params:
            r["tx_dbm"] = _number(params, "lora_tx_dbm", -10.0, 40.0, "LoRa TX power (dBm)")
        r["label"] = "LoRa %.3f MHz %s (SF%d/%g kHz, %g dBm)" % (
            r["freq_mhz"], preset, sf, bw / 1000.0, r["tx_dbm"])
        if params.get("lora_source"):
            r["label"] += " [%s]" % params["lora_source"]
    else:
        if "fm_mhz" in params:
            r["freq_mhz"] = _number(params, "fm_mhz", FM_FREQ_RANGE_MHZ[0],
                                    FM_FREQ_RANGE_MHZ[1], "FM frequency (MHz)")
        if "fm_w" in params:
            watts = _number(params, "fm_w", FM_POWER_RANGE_W[0], FM_POWER_RANGE_W[1],
                            "FM power (W)")
            r["tx_dbm"] = round(30.0 + 10.0 * math.log10(watts), 2)
        r["label"] = "FM %s %.3f MHz (%g W)" % (fm_band(r["freq_mhz"]), r["freq_mhz"],
                                               round(10 ** ((r["tx_dbm"] - 30) / 10.0), 2))
    return r


def tx_height_from(params):
    if params and "tx_height_m" in params:
        return _number(params, "tx_height_m", TX_HEIGHT_RANGE_M[0], TX_HEIGHT_RANGE_M[1],
                       "Transmitter height (m)")
    return ANT_HEIGHT_M


def _params(params_json):
    if not params_json:
        return {}
    p = json.loads(params_json)
    if not isinstance(p, dict):
        raise ValueError("settings must be a JSON object")
    return p


def check_settings(params_json):
    """Kotlin entry point: "" when the Coverage tab's settings can be used,
    else the reason, written for the operator -- asked BEFORE they are
    saved, so a typo is never persisted to come back after every reboot."""
    try:
        params = _params(params_json)
        resolve_radio("fm", params)
        tx_height_from(params)
        return ""
    except ValueError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return "%s: %s" % (type(e).__name__, e)


def _model_key(radio, h_t):
    """What a cached result depends on besides the place."""
    return (radio["freq_mhz"], radio["tx_dbm"], radio["sens_dbm"], h_t)
RADIUS_M = 2000.0     # how far out to compute and draw
AZ_STEP_DEG = 5.0     # angular resolution of the coverage boundary
RAY_STEP_M = 20.0     # how finely to walk each direction
_CELL = 100.0         # building spatial-index cell (m)

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
# Per mirror. Overpass is now only the fallback when Overture fails, and on
# 2026-10-19 all three mirrors timed out from the OnePlus at 90 s each --
# four minutes to say "failed". 30 s each bounds that at ~1.5 min.
OVERPASS_TIMEOUT_S = 30
SRTM_BASE = "https://s3.amazonaws.com/elevation-tiles-prod/skadi"
_UA = "rtak-fm-coverage/1.0 (field planning)"

class _Lru(object):
    """A small bounded dict, safe across the two coverage threads (the
    operator's own map and the peers'). Unbounded, these caches grew with
    every 110 m moved, and an own result holds every building projected."""

    def __init__(self, size):
        self.size = size
        self._d = collections.OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            v = self._d.get(key)
            if v is not None:
                self._d.move_to_end(key)
            return v

    def put(self, key, value):
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self.size:
                self._d.popitem(last=False)

    def clear(self):
        with self._lock:
            self._d.clear()


# Caches so a radio change recomputes without refetching, and a small move
# reuses the same download. Keyed on location rounded to ~110 m.
_coverage_cache = _Lru(8)
_buildings_cache = _Lru(16)

# Directory holding bundled coverage files (see tools/overture_to_geojson.py).
# Kotlin sets this to the app's assets/coverage or files/coverage directory at
# startup; None means "nowhere to save": Overture is still fetched, unsaved.
_BUNDLED_DIR = None


# --- link budget -----------------------------------------------------------

def link_budget_db(radio):
    """Most path loss this radio can tolerate, receiver input referred."""
    eirp = radio["tx_dbm"] + ANT_GAIN_DBI
    return eirp + ANT_GAIN_DBI - radio["sens_dbm"]


def two_ray_loss_db(d_m, freq_mhz, h_t=ANT_HEIGHT_M, h_r=ANT_HEIGHT_M):
    """Ground-reflection (two-ray) LOS loss; P.1411's breakpoint form.

    Above the breakpoint (a few metres at handheld heights) this is
    frequency-independent, which is the real, and slightly surprising,
    property of the two-ray model -- the free-space lambda^2 cancels the
    ground-reflection phase term.
    """
    lam = 299.792458 / freq_mhz  # metres
    rbp = max(4.0 * h_t * h_r / lam, 1.0)
    d_m = max(d_m, 1.0)
    lbp = abs(20.0 * math.log10(lam * lam / (8.0 * math.pi * h_t * h_r)))
    if d_m <= rbp:
        return lbp + 20.0 * math.log10(d_m / rbp)
    return lbp + 40.0 * math.log10(d_m / rbp)


def knife_edge_loss_db(h_above_m, lam_m, d1_m, d2_m):
    """Single knife-edge diffraction over an obstruction.

    h_above_m is how far the obstruction pokes above the straight-line ray;
    a value <= 0 is clear air (no loss). Classic Fresnel parameter v and the
    ITU knife-edge curve.
    """
    if h_above_m <= 0.0 or d1_m <= 0.0 or d2_m <= 0.0:
        return 0.0
    v = h_above_m * math.sqrt(2.0 / lam_m * (1.0 / d1_m + 1.0 / d2_m))
    if v <= -0.7:
        return 0.0
    return 6.9 + 20.0 * math.log10(math.sqrt(v * v + 1.0) + v)


# --- geometry --------------------------------------------------------------

def point_in_poly(x, y, poly):
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _on_segment(p, q, r):
    """Is point q on segment p-r, assuming q is collinear with p and r."""
    return (min(p[0], r[0]) <= q[0] <= max(p[0], r[0]) and
            min(p[1], r[1]) <= q[1] <= max(p[1], r[1]))


def _segs_intersect(p1, p2, p3, p4):
    d1 = _cross(p3, p4, p1)
    d2 = _cross(p3, p4, p2)
    d3 = _cross(p1, p2, p3)
    d4 = _cross(p1, p2, p4)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    # Collinear or corner-touching cases, which the strict test above misses.
    # A 45-degree ray through an axis-aligned street grid hits building
    # corners exactly, so this is not an edge case -- it is the common case.
    # _on_segment(p, q, r) asks whether q lies on p-r, so the point under
    # test goes in the MIDDLE. These once passed it last (the textbook's
    # argument order, not this helper's), which on a closed ring's
    # zero-length closing edge was always true: every building near the ray
    # counted as crossed. test_coverage.SegmentEdgeCasesTest pins it.
    if d1 == 0 and _on_segment(p3, p1, p4):
        return True
    if d2 == 0 and _on_segment(p3, p2, p4):
        return True
    if d3 == 0 and _on_segment(p1, p3, p2):
        return True
    if d4 == 0 and _on_segment(p1, p4, p2):
        return True
    return False


def segment_hits_poly(ax, ay, bx, by, poly):
    if point_in_poly(ax, ay, poly) or point_in_poly(bx, by, poly):
        return True
    n = len(poly)
    for i in range(n):
        if _segs_intersect((ax, ay), (bx, by), poly[i], poly[(i + 1) % n]):
            return True
    return False


# --- terrain ---------------------------------------------------------------

class Terrain:
    """SRTM 1-arc-second elevation, loaded lazily from the AWS open tiles.

    Only the tile(s) the area touches are downloaded, once, and cached in the
    module-level _data_cache. Instances are cheap; the download is shared.

    The compressed tile is also kept on disk (terrain_dir(), beside the
    downloaded areas), so terrain survives an app restart and a downloaded
    area works with no network at all. ~6 MB per 1-degree square, kept
    until deleted; a 2 km disc touches one to four squares.
    """

    _data_cache = {}

    def __init__(self, lat, lon, radius_m):
        self._tiles = {}
        dlat = radius_m / 110540.0
        dlon = radius_m / (111320.0 * math.cos(math.radians(lat)))
        for la in (lat - dlat, lat + dlat):
            for lo in (lon - dlon, lon + dlon):
                key = self._tile_key(la, lo)
                if key not in self._tiles:
                    self._tiles[key] = self._load(key)

    @staticmethod
    def _tile_key(lat, lon):
        n = "N" if lat >= 0 else "S"
        w = "W" if lon < 0 else "E"
        return "%s%02d%s%03d" % (n, abs(int(math.floor(lat))), w, abs(int(math.floor(lon))))

    @staticmethod
    def _download(key):
        url = "%s/%s/%s.hgt.gz" % (SRTM_BASE, key[:3], key)
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()

    @staticmethod
    def _decode(gz, key):
        raw = gzip.GzipFile(fileobj=io.BytesIO(gz)).read()
        n = int(math.sqrt(len(raw) / 2))
        if n * n * 2 != len(raw):
            raise ValueError("unexpected SRTM tile size for %s" % key)
        return raw, n

    @classmethod
    def _load(cls, key):
        if key in cls._data_cache:
            return cls._data_cache[key]
        path = _terrain_path(key)
        raw = None
        if path and os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    raw, n = cls._decode(f.read(), key)
            except (OSError, EOFError, ValueError, zlib.error):
                # A truncated or corrupt cache file must not wedge the tab
                # forever: drop it and fetch again.
                raw = None
                try:
                    os.remove(path)
                except OSError:
                    pass
        if raw is None:
            gz = cls._download(key)
            raw, n = cls._decode(gz, key)   # validate BEFORE caching it
            if path:
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    tmp = path + ".part"
                    with open(tmp, "wb") as f:
                        f.write(gz)
                    os.replace(tmp, path)
                except OSError:
                    pass                    # a full disk costs a re-download, nothing more
        # array('h') keeps 12.9M samples in 26 MB of C shorts instead of a
        # ~350 MB tuple of Python ints -- the difference between loading and
        # an OutOfMemoryError on a phone. The tiles are big-endian on disk.
        grid = array.array("h")
        grid.frombytes(raw)
        if sys.byteorder != "big":
            grid.byteswap()
        lat_s = int(key[1:3]) * (1 if key[0] == "N" else -1)
        lon_w = int(key[4:7]) * (-1 if key[3] == "W" else 1)
        tile = {"n": n, "lat_s": lat_s, "lon_w": lon_w, "grid": grid}
        cls._data_cache[key] = tile
        return tile

    def elev(self, lat, lon):
        key = self._tile_key(lat, lon)
        t = self._tiles.get(key)
        if t is None:
            return 0.0
        n = t["n"]
        row = (t["lat_s"] + 1.0 - lat) * n
        col = (lon - t["lon_w"]) * n
        r0 = int(math.floor(row))
        c0 = int(math.floor(col))
        fr = row - r0
        fc = col - c0
        vals = []
        for r in (r0, r0 + 1):
            for c in (c0, c0 + 1):
                if 0 <= r < n and 0 <= c < n:
                    v = t["grid"][r * n + c]
                    vals.append(None if v <= -32768 else float(v))
                else:
                    vals.append(None)
        good = [v for v in vals if v is not None]
        if not good:
            return 0.0
        base = good[0]
        vals = [base if v is None else v for v in vals]
        top = vals[0] * (1 - fc) + vals[1] * fc
        bot = vals[2] * (1 - fc) + vals[3] * fc
        return top * (1 - fr) + bot * fr


_STREET_ABBREV = {
    "north": "N", "south": "S", "east": "E", "west": "W",
    "street": "St", "avenue": "Ave", "drive": "Dr", "road": "Rd",
    "boulevard": "Blvd", "lane": "Ln", "court": "Ct", "place": "Pl",
    "parkway": "Pkwy", "highway": "Hwy", "circle": "Cir", "terrace": "Ter",
    "trail": "Trl", "freeway": "Fwy", "expressway": "Expy",
}


def short_street_name(name):
    """'West Sierra Street' -> 'W Sierra St': the postal abbreviations, so a
    label fits along a street a few hundred metres long on a phone screen."""
    if not name:
        return ""
    return " ".join(_STREET_ABBREV.get(w.lower(), w) for w in str(name).split())


def merge_street_lines(lines, names, tol):
    """[(name, line)] with each street's pieces chained end to end.

    Overture cuts a street at every intersection, so a piece is often a
    couple of hundred metres -- shorter on screen than its own name, and the
    map skips a label that does not fit (2026-10-19: no label drew at all at
    the default zoom). Pieces of one name whose ends meet within `tol` (same
    units as the points) are joined, reversing a piece where needed. Greedy:
    a junction of three same-named pieces yields two lines, which is fine
    for placing a label. Unnamed pieces are dropped.
    """
    groups = {}
    for line, name in zip(lines, names):
        if name and len(line) >= 2:
            groups.setdefault(name, []).append([tuple(p) for p in line])
    out = []
    for name, pieces in groups.items():
        def cell(pt):
            return (int(math.floor(pt[0] / tol)), int(math.floor(pt[1] / tol)))
        ends = {}
        for i, pc in enumerate(pieces):
            for pt in (pc[0], pc[-1]):
                ends.setdefault(cell(pt), []).append(i)
        used = [False] * len(pieces)

        def take_starting_at(pt):
            """An unused piece with an end at pt, oriented to START there."""
            cx, cy = cell(pt)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for i in ends.get((cx + dx, cy + dy), ()):
                        if used[i]:
                            continue
                        pc = pieces[i]
                        if math.hypot(pc[0][0] - pt[0], pc[0][1] - pt[1]) <= tol:
                            used[i] = True
                            return pc
                        if math.hypot(pc[-1][0] - pt[0], pc[-1][1] - pt[1]) <= tol:
                            used[i] = True
                            return pc[::-1]
            return None

        for i in range(len(pieces)):
            if used[i]:
                continue
            used[i] = True
            chain = list(pieces[i])
            while True:                       # grow forward from the end
                nxt = take_starting_at(chain[-1])
                if nxt is None:
                    break
                chain.extend(nxt[1:])
            while True:                       # and backward from the start
                prv = take_starting_at(chain[0])
                if prv is None:
                    break
                chain[:0] = prv[::-1][:-1]
            out.append((name, [list(p) for p in chain]))
    return out


def terrain_dir():
    """Where terrain tiles are kept on disk, or None when no storage
    directory has been set (desktop tests, or before Kotlin's startup)."""
    if not _BUNDLED_DIR:
        return None
    return os.path.join(_BUNDLED_DIR, "terrain")


def _terrain_path(key):
    d = terrain_dir()
    return os.path.join(d, key + ".hgt.gz") if d else None


def terrain_keys_for_box(south, west, north, east):
    """Every 1-degree SRTM square the box touches."""
    keys = []
    for la in range(int(math.floor(south)), int(math.floor(north)) + 1):
        for lo in range(int(math.floor(west)), int(math.floor(east)) + 1):
            k = Terrain._tile_key(la + 0.5, lo + 0.5)
            if k not in keys:
                keys.append(k)
    return keys


# --- data fetch ------------------------------------------------------------

def _overpass(query):
    # Overpass is a single volunteer-run service that rate-limits and returns
    # 5xx under load, so try a couple of public mirrors before giving up. A
    # coverage map that silently fails because one mirror is down is worse
    # than a map that tried the next one.
    last = None
    for url in OVERPASS_URLS:
        try:
            req = urllib.request.Request(url, data=query.encode("utf-8"),
                                         headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=OVERPASS_TIMEOUT_S) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 -- try the next mirror
            last = e
    raise last


def estimate_height(tags):
    def num(s):
        try:
            return float(str(s).replace("m", "").strip())
        except (TypeError, ValueError):
            return None
    if "height" in tags:
        v = num(tags["height"])
        if v:
            return v
    for k in ("building:levels", "levels"):
        if k in tags:
            v = num(tags[k])
            if v:
                return v * 3.0
    b = tags.get("building", "yes")
    if b in ("commercial", "retail", "industrial", "apartments", "school",
             "church", "university", "supermarket"):
        return 9.0
    return 5.0


def fetch_data(lat, lon, radius_m):
    """Buildings (footprints + estimated height) and roads from Overpass.

    Returns (buildings, roads, road_names): each building is {poly: [[lat,lon]..], h:
    float}, each road a list of [lat, lon] points. Raises on network failure
    -- the caller decides how to report it.
    """
    query = (
        "[out:json][timeout:25];("
        "nwr(around:%d,%f,%f)[building];"
        "way(around:%d,%f,%f)[highway~\"^(motorway|trunk|primary|secondary|"
        "tertiary|residential|unclassified|living_street)$\"];"
        ");out geom;"
        % (radius_m, lat, lon, radius_m, lat, lon)
    )
    data = _overpass(query)
    buildings = []
    roads = []
    names = []
    for el in data.get("elements", []):
        geom = el.get("geometry") or []
        if el.get("type") == "way" and len(geom) < 2:
            continue
        tags = el.get("tags", {})
        if "building" in tags:
            # Open-sided or open-topped structures don't block a 1.5 m ray the
            # way a walled building does, and they are everywhere in a
            # residential suburb -- counting them would over-shorten coverage.
            if tags["building"] in ("carport", "roof", "shed", "garage", "garages"):
                continue
            if len(geom) >= 3:
                buildings.append({
                    "poly": [[p["lat"], p["lon"]] for p in geom],
                    "h": estimate_height(tags),
                })
        else:
            roads.append([[p["lat"], p["lon"]] for p in geom])
            names.append(tags.get("name", ""))
    return buildings, roads, names


# --- bundled data ----------------------------------------------------------

def set_bundled_dir(path):
    """Point coverage at a directory of pre-built bundles (see
    tools/overture_to_geojson.py). Called from Kotlin at startup with the
    app's assets/coverage or files/coverage path; None disables bundles."""
    global _BUNDLED_DIR
    _BUNDLED_DIR = path


def _bundle_bbox(bundle):
    """(south, west, north, east) of a bundle, or None for the older
    centre+radius bundles the desktop converter writes."""
    try:
        return (float(bundle["south"]), float(bundle["west"]),
                float(bundle["north"]), float(bundle["east"]))
    except (KeyError, TypeError, ValueError):
        return None


def _disc_bbox(lat, lon, radius_m):
    dlat = radius_m / 110540.0
    dlon = radius_m / (111320.0 * math.cos(math.radians(lat)))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def _covers(bundle, lat, lon, radius_m=0.0):
    """Does the bundle hold data for the whole disc of radius_m around the
    point? radius_m=0 asks only about the point itself."""
    bb = _bundle_bbox(bundle)
    if bb is not None:
        s, w, n, e = _disc_bbox(lat, lon, radius_m)
        return bb[0] <= s and bb[1] <= w and bb[2] >= n and bb[3] >= e
    clat = bundle.get("center_lat")
    clon = bundle.get("center_lon")
    radius = bundle.get("radius_m")
    if clat is None or clon is None or radius is None:
        return False
    dy = abs(lat - clat) * 110540.0
    dx = abs(lon - clon) * 111320.0 * math.cos(math.radians(clat))
    return math.hypot(dx, dy) + radius_m <= radius


# Parsed bundles by path, invalidated by mtime. A downloaded area can be
# tens of MB of JSON; re-parsing every one on every location change is the
# slow part of opening the tab, not the sweep.
_bundle_cache = {}


def _read_bundle(path):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _bundle_cache.get(path)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            bundle = json.load(f)
    except (OSError, ValueError):
        return None
    _bundle_cache[path] = (mtime, bundle)
    return bundle


def _bundle_paths():
    if not _BUNDLED_DIR:
        return []
    try:
        names = sorted(n for n in os.listdir(_BUNDLED_DIR) if n.endswith(".json"))
    except OSError:
        return []
    return [os.path.join(_BUNDLED_DIR, n) for n in names]


def find_bundle(lat, lon):
    """(bundle, complete) for the best bundle at (lat, lon), or (None, False).

    A bundle that holds the WHOLE computation disc wins over one that merely
    contains the point: the second leaves the far side of the map empty,
    which reads as open ground and over-reports range there.
    """
    partial = None
    for path in _bundle_paths():
        bundle = _read_bundle(path)
        if bundle is None:
            continue
        # An automatic area is only a cache. One saved before street names
        # were kept (2026-10-19) is skipped, so it is fetched again -- with
        # names -- and the new one replaces it.
        if os.path.basename(path).startswith("auto_") and "road_names" not in bundle:
            continue
        if _covers(bundle, lat, lon, RADIUS_M):
            return bundle, True
        if partial is None and _covers(bundle, lat, lon):
            partial = bundle
    return partial, False


def load_bundled(lat, lon):
    """Find a bundled file covering (lat, lon), return (buildings, roads) or
    (None, None). Bundles carry real Overture heights, so they are preferred
    over the Overpass fetch -- the point of building them in the first place.
    """
    bundle, _ = find_bundle(lat, lon)
    if bundle is None:
        return None, None
    return bundle.get("buildings", []), bundle.get("roads", [])


# --- the computation -------------------------------------------------------

def _project(lat, lon, lat0, lon0):
    x = (lon - lon0) * 111320.0 * math.cos(math.radians(lat0))
    y = (lat - lat0) * 110540.0
    return x, y


def _build_index(buildings, cell):
    index = {}
    for bi, b in enumerate(buildings):
        xs = [p[0] for p in b["poly"]]
        ys = [p[1] for p in b["poly"]]
        b["minx"], b["maxx"] = min(xs), max(xs)
        b["miny"], b["maxy"] = min(ys), max(ys)
        b["cx"], b["cy"] = sum(xs) / len(xs), sum(ys) / len(ys)
        b["cdist"] = math.hypot(b["cx"], b["cy"])
        for cx in range(int(b["minx"] // cell), int(b["maxx"] // cell) + 1):
            for cy in range(int(b["miny"] // cell), int(b["maxy"] // cell) + 1):
                index.setdefault((cx, cy), []).append(bi)
    return index


def _dist_to_latlon(d, az_rad, lat0, lon0):
    lat = lat0 + d * math.cos(az_rad) / 110540.0
    lon = lon0 + d * math.sin(az_rad) / (111320.0 * math.cos(math.radians(lat0)))
    return lat, lon


def _cells_on_segment(ax, ay, bx, by, cell):
    """Every spatial-index cell the segment (ax,ay)-(bx,by) passes through,
    walked exactly (Amanatides-Woo) rather than sampled.

    Sampling misses a cell the line only clips at a corner, and a building
    can sit in nothing else. Where the line crosses a cell corner (exactly,
    or within float noise -- a 45-degree ray from the origin, which is itself
    a cell corner, does it every cell), both side cells are kept too, so a
    building touching only that corner is still a candidate. Extra cells
    cost a few tests; segment_hits_poly decides, so they never add a hit.
    """
    cx, cy = int(ax // cell), int(ay // cell)
    ex, ey = int(bx // cell), int(by // cell)
    cells = [(cx, cy)]
    dx, dy = bx - ax, by - ay
    sx = 1 if dx > 0 else -1
    sy = 1 if dy > 0 else -1
    inf = float("inf")
    if dx != 0:
        tx = ((cx + (1 if sx > 0 else 0)) * cell - ax) / dx
        tdx = cell / abs(dx)
    else:
        tx, tdx = inf, inf
    if dy != 0:
        ty = ((cy + (1 if sy > 0 else 0)) * cell - ay) / dy
        tdy = cell / abs(dy)
    else:
        ty, tdy = inf, inf
    left = abs(ex - cx) + abs(ey - cy)
    while left > 0:
        if abs(tx - ty) < 1e-9:
            if left < 2:
                # Float noise put the end a step short of the corner; take
                # the one step that remains toward the end cell.
                if cx != ex:
                    cx += sx
                else:
                    cy += sy
                cells.append((cx, cy))
                break
            cells.append((cx + sx, cy))
            cells.append((cx, cy + sy))
            cx += sx
            cy += sy
            tx += tdx
            ty += tdy
            left -= 2
        elif tx < ty:
            cx += sx
            tx += tdx
            left -= 1
        else:
            cy += sy
            ty += tdy
            left -= 1
        cells.append((cx, cy))
    return cells


def _building_obstacles(az_deg, buildings, index, tx_elev, max_d,
                        tx_height=ANT_HEIGHT_M, min_excess=0.0):
    """Every building the ray crosses, as (dist_m, excess_m), sorted by dist.

    excess_m is how far the roof pokes above a level line at the transmit
    antenna's height -- flat ground is assumed for the excess, which is
    within ~0.3 m of the curved-ray truth over a 2 km run. Buildings with
    excess_m > min_excess are kept: 0 when both antennas are at one height,
    negative when the transmitter is higher, because the line then slopes
    down to the receiver and a roof below the transmit antenna can still
    cross it (see _max_range_az).

    Each nearby building is tested ONCE against the whole ray. This used to
    walk the ray in RAY_STEP_M steps and re-test every candidate against
    each growing prefix -- the same answer (a building crossed by any prefix
    is crossed by the whole ray, and the last prefix IS the whole ray) at
    ~100x the work: 769,000 polygon tests for 72 directions, 94% of a
    10.3 s computation on 2026-10-20's coverage profile.
    """
    th = math.radians(az_deg)
    bx, by = max_d * math.sin(th), max_d * math.cos(th)
    tx_ant = tx_elev + tx_height
    tested = set()
    nearest = {}            # building key -> the nearest crossed piece
    for cell in _cells_on_segment(0.0, 0.0, bx, by, _CELL):
        for bi in index.get(cell, ()):
            if bi in tested:
                continue
            tested.add(bi)
            b = buildings[bi]
            if not segment_hits_poly(0.0, 0.0, bx, by, b["poly"]):
                continue
            # Overture tiles cut a building at every tile edge, and the tile
            # buffer repeats the cut pieces; they share the building's id, so
            # the building is charged ONE knife edge however many pieces the
            # ray crosses -- the nearest piece, which is where the ray meets
            # it. OSM buildings have no id and stay one per polygon.
            key = b.get("id", bi)
            have = nearest.get(key)
            if have is None or b["cdist"] < have["cdist"]:
                nearest[key] = b
    out = []
    for b in nearest.values():
        excess = (b["gelev"] + b["h"]) - tx_ant
        if excess > min_excess:
            out.append((b["cdist"], excess))
    out.sort()
    return out


def _terrain_obstacle(az_deg, terrain, tx_elev, lat0, lon0, max_d,
                      tx_height=ANT_HEIGHT_M, min_excess=0.0):
    """The single highest point where terrain pokes above the level line at
    the transmit antenna's height (or above min_excess relative to it; see
    _building_obstacles).

    A hill is one dominant ridge, not a series of knife edges, so only the
    worst point is kept.
    """
    th = math.radians(az_deg)
    tx_ant = tx_elev + tx_height
    best_x, best_excess = None, min_excess
    x = RAY_STEP_M
    while x <= max_d:
        plat, plon = _dist_to_latlon(x, th, lat0, lon0)
        excess = terrain.elev(plat, plon) - tx_ant
        if excess > best_excess:
            best_excess = excess
            best_x = x
        x += RAY_STEP_M
    if best_x is None:
        return None
    return (best_x, best_excess)


def _obstacles(az_deg, buildings, index, terrain, tx_elev, lat0, lon0,
               tx_height=ANT_HEIGHT_M, min_excess=0.0):
    """All obstructions along one direction, sorted by distance.

    Buildings are many (one per crossed roof); terrain is one (the dominant
    ridge). Returns ([(dist_m, excess_m), ...]), excess relative to a level
    line at the transmit antenna.
    """
    obs = _building_obstacles(az_deg, buildings, index, tx_elev, RADIUS_M,
                              tx_height, min_excess)
    t = _terrain_obstacle(az_deg, terrain, tx_elev, lat0, lon0, RADIUS_M,
                          tx_height, min_excess)
    if t is not None:
        obs.append(t)
    obs.sort()
    return obs


def _max_range_az(az_deg, radio, buildings, index, terrain, tx_elev, lat0, lon0,
                  h_t=ANT_HEIGHT_M, h_r=ANT_HEIGHT_M):
    """(signal-reach_m, line-of-sight_m) in one direction.

    Signal reach walks the link budget out: two-ray loss plus the summed
    knife-edge diffraction of every obstruction crossed, cut off where the
    total exceeds what the radio can tolerate. Line of sight is the distance
    to the first point where an obstruction blocks the path (or RADIUS_M).

    h_t is the transmit antenna's height above its ground, h_r the
    receiver's. The path is the straight line between them over LEVEL
    ground at the transmitter's elevation -- the receiver's own ground is
    not looked up, the same flat-ground assumption the excess always made --
    so an obstruction at od clears it by oe - (h_r - h_t) * od / d for a
    receiver at d. With h_t == h_r the line is level, the slope term is
    exactly zero, and this is the model as it was before heights could be
    set (pinned against it by test_coverage.TransmitterHeightTest).
    """
    budget = link_budget_db(radio)
    lam = 299.792458 / radio["freq_mhz"]
    slope = h_r - h_t
    obstacles = _obstacles(az_deg, buildings, index, terrain, tx_elev, lat0, lon0,
                           h_t, min(0.0, slope))

    def blocked(d):
        for od, oe in obstacles:
            if od >= d:
                return False
            if oe - slope * od / d > 0.0:
                return True
        return False

    if slope == 0.0:
        los = obstacles[0][0] if obstacles else RADIUS_M
    else:
        # A higher transmitter sees over a roof to a receiver far behind it
        # but not to one right behind it, so "first blocked" is searched for
        # rather than read off the first obstacle.
        los = RADIUS_M
        d, prev = RAY_STEP_M, 0.0
        while d <= RADIUS_M:
            if blocked(d):
                lo, hi = prev, d
                for _ in range(20):
                    mid = (lo + hi) / 2
                    if blocked(mid):
                        hi = mid
                    else:
                        lo = mid
                los = lo
                break
            prev = d
            d += RAY_STEP_M

    def loss(d):
        total = two_ray_loss_db(d, radio["freq_mhz"], h_t, h_r)
        for od, oe in obstacles:
            if od >= d:
                break
            total += knife_edge_loss_db(oe - slope * od / d, lam, od, max(d - od, 1.0))
        return total

    # If the budget survives the whole radius, there is nothing to cut off.
    if loss(RADIUS_M) <= budget:
        return RADIUS_M, los

    d = RAY_STEP_M
    prev = 0.0
    while d <= RADIUS_M:
        if loss(d) > budget:
            lo, hi = prev, d
            for _ in range(20):
                mid = (lo + hi) / 2
                if loss(mid) <= budget:
                    lo = mid
                else:
                    hi = mid
            return lo, los
        prev = d
        d += RAY_STEP_M
    return RADIUS_M, los


class AreaPending(Exception):
    """No data here yet and fetching is not allowed right now (peer
    footprints rate-limit their downloads); ask again later."""


def compute_coverage(lat, lon, radio_key, az_step=AZ_STEP_DEG, detail=True,
                     area_prefix="auto", areas_kept=1, report=True,
                     allow_fetch=True, overpass=True, timing=None,
                     radio=None, tx_height_m=ANT_HEIGHT_M):
    """Full computation. Returns a dict (JSON-serialisable), raises on failure.

    The defaults are the operator's own map. A peer's footprint passes a
    coarser az_step, detail=False (no streets or buildings in the result --
    the map already has them, and they are ~1.4 MB of JSON a time), its own
    area_prefix so its downloads never replace the operator's automatic
    area, report=False so they never show in the operator's progress line,
    and overpass=False because a 90 s volunteer-server fallback is not worth
    it for somebody else's outline. allow_fetch=False with no complete saved
    area uses a partial one, or raises AreaPending if there is none.

    `timing`, a dict, gets seconds per stage: data_s (finding or fetching
    the buildings -- with the fetch's own stages when there was one),
    terrain_s and sweep_s.

    `radio` is resolve_radio()'s answer (RADIOS[radio_key] when None);
    tx_height_m the transmit antenna's height, the receiver staying at
    ANT_HEIGHT_M.
    """
    radio = RADIOS[radio_key] if radio is None else radio
    timing = {} if timing is None else timing
    t0 = time.monotonic()

    # Buildings and roads depend only on where you are, so cache them across
    # radio changes (the expensive part); terrain tiles are already cached in
    # Terrain. A radio switch then just re-runs the sweep, a couple of seconds.
    loc_key = (round(lat, 3), round(lon, 3))
    hit = _buildings_cache.get(loc_key)
    if hit is not None:
        buildings_raw, roads_raw, names_raw, source, complete = hit
    else:
        # A saved area (downloaded, automatic, or sideloaded) wins when it
        # covers the spot; otherwise fetch Overture now, then Overpass.
        bundle, complete = find_bundle(lat, lon)
        got = None
        cacheable = True
        if (bundle is None or not complete) and not allow_fetch:
            if bundle is None:
                raise AreaPending("no saved area here yet")
            cacheable = False       # fetch the whole disc when allowed
        elif bundle is None or not complete:
            # No saved area holds the whole 2 km disc: fetch one. Overture is
            # the default (real heights; a static file on S3 that has not
            # failed once), saved so the next look here is offline.
            try:
                got = _auto_overture(lat, lon, area_prefix, areas_kept, report, timing)
                complete = True
            except Exception as e:  # noqa: BLE001 -- fall back, and say so
                if bundle is None and not overpass:
                    raise
                if bundle is None:
                    # OSM Overpass is the last resort: a volunteer service
                    # that timed out on all three mirrors from the phone on
                    # 2026-10-19.
                    b, r, nm = fetch_data(lat, lon, RADIUS_M)
                    got = (b, r, nm, "openstreetmap (Overture failed: %s)"
                           % type(e).__name__)
                    complete = True
                # else: the partial saved area is better than OSM; use it,
                # and data_complete=False makes the tab say so.
        if got is not None:
            buildings_raw, roads_raw, names_raw, source = got
        else:
            buildings_raw = bundle.get("buildings", [])
            roads_raw = bundle.get("roads", [])
            # Older bundles (desktop converter, before 2026-10-19) have none.
            names_raw = bundle.get("road_names") or [""] * len(roads_raw)
            source = bundle.get("source") or "bundle"
        if cacheable:
            _buildings_cache.put(loc_key, (buildings_raw, roads_raw, names_raw,
                                           source, complete))

    t1 = time.monotonic()
    timing["data_s"] = round(t1 - t0, 2)
    terrain = Terrain(lat, lon, RADIUS_M)
    tx_elev = terrain.elev(lat, lon)
    timing["terrain_s"] = round(time.monotonic() - t1, 2)
    # The sweep is the slow part on a phone once data is local; say so
    # rather than leave the last download stage showing.
    if report:
        _set_progress(active=True, done=0, total=0, stage="Computing coverage")

    # A downloaded area can be far bigger than the disc computed here, and
    # Terrain only loaded tiles for the disc -- a building outside it would
    # sit at elevation 0 and tower over every ray. Keep the disc plus slack.
    keep = RADIUS_M * 1.1
    buildings = []
    for br in buildings_raw:
        # br["poly"] is [[lat, lon], ...] -- unpack la=lat, lo=lon, matching
        # _project(lat, lon, ...). Swapping these is the classic silent bug:
        # the map comes out millions of metres off and still "works".
        poly = [_project(la, lo, lat, lon) for la, lo in br["poly"]]
        if len(poly) < 3:
            continue
        if min(math.hypot(px, py) for px, py in poly) > keep:
            continue
        cx = sum(p[0] for p in poly) / len(poly)
        cy = sum(p[1] for p in poly) / len(poly)
        glat = lat + cy / 110540.0
        glon = lon + cx / (111320.0 * math.cos(math.radians(lat)))
        b = {"poly": poly, "h": br["h"], "gelev": terrain.elev(glat, glon)}
        if br.get("id"):
            b["id"] = br["id"]
        buildings.append(b)

    roads = []
    road_names = []
    for i, rd in enumerate(roads_raw if detail else ()):
        pts = [_project(la, lo, lat, lon) for la, lo in rd]
        if pts and min(math.hypot(px, py) for px, py in pts) <= keep:
            roads.append(pts)
            road_names.append(short_street_name(names_raw[i] if i < len(names_raw) else ""))
    index = _build_index(buildings, _CELL)

    coverage = []
    steps = int(round(360.0 / az_step))
    for i in range(steps):
        az = i * 360.0 / steps
        r, los = _max_range_az(az, radio, buildings, index, terrain, tx_elev, lat, lon,
                               tx_height_m, ANT_HEIGHT_M)
        coverage.append({"az": round(az, 1), "range": round(r, 1),
                         "los": round(los, 1)})

    timing["sweep_s"] = round(time.monotonic() - t1 - timing["terrain_s"], 2)
    ranges = [c["range"] for c in coverage]
    loses = [c["los"] for c in coverage]
    result = {
        "radio": radio_key,
        "label": radio["label"],
        "freq_mhz": radio["freq_mhz"],
        "tx_dbm": radio["tx_dbm"],
        "sens_dbm": radio["sens_dbm"],
        "tx_height_m": tx_height_m,
        "rx_height_m": ANT_HEIGHT_M,
        "lat": lat,
        "lon": lon,
        "elev_m": round(tx_elev, 0),
        "radius_m": RADIUS_M,
        # Where the buildings came from, and whether they fill the whole
        # disc. An incomplete area leaves its far side looking like open
        # ground, so the tab has to be able to say so.
        "data_source": source,
        "data_complete": complete,
        "coverage": coverage,
        "roads": [[[round(x, 1), round(y, 1)] for x, y in rd] for rd in roads],
        # Aligned with "roads"; "" for an unnamed street. For labelling the
        # map, so the reach can be read against streets the operator knows.
        "road_names": road_names,
        # The same streets chained into whole named lines, for the labels.
        "street_labels": [[n, [[round(x, 1), round(y, 1)] for x, y in ln]]
                          for n, ln in merge_street_lines(roads, road_names, 2.0)],
        "buildings": [[[round(x, 1), round(y, 1)] for x, y in b["poly"]]
                      for b in buildings],
        "stats": {
            "max_km": round(max(ranges) / 1000.0, 2),
            "min_km": round(min(ranges) / 1000.0, 2),
            "max_los_km": round(max(loses) / 1000.0, 2),
        },
    }
    if not detail:
        for k in ("roads", "road_names", "street_labels", "buildings"):
            del result[k]
    return result


def compute(lat_s, lon_s, radio_key, params_json=""):
    """Kotlin entry point. Returns a JSON string, never raises. params_json
    carries the operator's settings (see resolve_radio, tx_height_from)."""
    try:
        lat = float(lat_s)
        lon = float(lon_s)
        params = _params(params_json)
        radio = resolve_radio(radio_key, params)
        h_t = tx_height_from(params)
        key = (round(lat, 3), round(lon, 3), radio_key) + _model_key(radio, h_t)
        hit = _coverage_cache.get(key)
        if hit is not None:
            return json.dumps(hit)
        result = compute_coverage(lat, lon, radio_key, radio=radio, tx_height_m=h_t)
        _coverage_cache.put(key, result)
        return json.dumps(result)
    except Exception as e:  # noqa: BLE001 -- the tab must get a reason, not a trace
        return json.dumps({"error": "%s: %s" % (type(e).__name__, e)})
    finally:
        _set_progress(active=False)


# --- other nodes' footprints -------------------------------------------------
#
# The same model run from where a PEER last said it was. Half the directions
# (10 degrees, not 5): the position is minutes old and the outline is a
# guide, so the halved cost buys more than the detail would. Each peer's area
# is downloaded once and kept with its own prefix -- the newest
# PEER_AREAS_KEPT of them -- so it never displaces the operator's own.

PEER_AZ_STEP_DEG = 10.0
PEER_AREAS_KEPT = 3
# One peer download at most this often, across all peers: a driving peer
# leaves its area every ~500 m. Measured on the OnePlus 2026-10-20, an area
# is 2-7.5 MB in ~25 requests and 12-20 s. Was 120 s, which queued new peers
# ~2 min apart (the third outside every saved area waited ~4 min); 30 s
# still caps it at ~2 downloads a minute. The Kotlin side serves the
# longest-waiting peer first (PeerQueue), since the first to ask when the
# window opens gets it.
PEER_FETCH_MIN_INTERVAL_S = 30.0

_peer_cache = _Lru(64)
_peer_fetch_lock = threading.Lock()
_last_peer_fetch = [None]       # time.monotonic() of the last download allowed


def _peer_fetch_allowed():
    with _peer_fetch_lock:
        now = time.monotonic()
        last = _last_peer_fetch[0]
        if last is not None and now - last < PEER_FETCH_MIN_INTERVAL_S:
            return False
        return True


def _note_peer_fetch():
    with _peer_fetch_lock:
        _last_peer_fetch[0] = time.monotonic()


def compute_peer(lat_s, lon_s, radio_key, params_json=""):
    """Kotlin entry point: a peer's footprint as JSON -- compute()'s result
    without streets or buildings, at PEER_AZ_STEP_DEG -- or {"error": ...},
    with "pending": true when there is no data there yet and the download
    rate limit says wait. Never raises, never touches download_progress().

    The radio settings apply (every node on the mesh runs the same preset);
    the transmitter height does NOT -- it describes the operator's own
    antenna, and a peer is modelled as a handheld at ANT_HEIGHT_M."""
    try:
        lat = float(lat_s)
        lon = float(lon_s)
        radio = resolve_radio(radio_key, _params(params_json))
        key = (round(lat, 3), round(lon, 3), radio_key) + _model_key(radio, ANT_HEIGHT_M)
        hit = _peer_cache.get(key)
        if hit is not None:
            return json.dumps(hit)
        allow = _peer_fetch_allowed()
        bundle, complete = find_bundle(lat, lon)
        fetching = allow and not complete
        if fetching:
            # Charged before the attempt: a failing download must not be
            # retried every time the peer list refreshes.
            _note_peer_fetch()
        timing = {}
        result = compute_coverage(lat, lon, radio_key, az_step=PEER_AZ_STEP_DEG,
                                  detail=False, area_prefix="peer",
                                  areas_kept=PEER_AREAS_KEPT, report=False,
                                  allow_fetch=allow, overpass=False, timing=timing,
                                  radio=radio)
        if result["data_complete"]:
            _peer_cache.put(key, result)
        return json.dumps(dict(result, timing=timing))
    except AreaPending as e:
        return json.dumps({"error": str(e), "pending": True})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "%s: %s" % (type(e).__name__, e)})


# --- downloading an area on the phone ----------------------------------------
#
# The Coverage tab's "select an area" flow. The operator drags a box on the
# map; download_area() pulls Overture's buildings (real heights) and streets
# for it straight from Overture's PMTiles (overture_tiles.py, no laptop, no
# pyarrow) and saves them as an ordinary bundle, which compute() then prefers
# over the OSM fetch. Offline from then on.

_progress_lock = threading.Lock()
_progress = {"active": False, "done": 0, "total": 0, "stage": ""}


def _set_progress(**kw):
    with _progress_lock:
        _progress.update(kw)


class DownloadStopped(Exception):
    """The operator tapped Stop download. Raised from the progress callback,
    so the fetch unwinds before anything is saved and the old area stays."""


# Set by stop_download() from the UI thread, read between tiles by the
# download thread. Only the operator's own download (download_area) checks
# it: the automatic and other-node fetches are not theirs to stop.
_stop_download = threading.Event()


def stop_download():
    """Kotlin entry point: ask a running download_area() to stop. It stops
    after the tile in flight, at most a few seconds. JSON {"ok": true}."""
    _stop_download.set()
    _set_progress(stage="Stopping")
    return json.dumps({"ok": True})


def download_progress():
    """JSON {active, done, total, stage}. Polled by Kotlin while
    download_area() runs on another thread -- Chaquopy calls cannot stream."""
    with _progress_lock:
        return json.dumps(_progress)


def _area_bbox_with_margin(south, west, north, east):
    """The selected box grown by RADIUS_M on every side, so a transmitter
    placed ANYWHERE inside the box has buildings for its whole computation
    disc. Without the margin a point near the box's edge would see open
    ground beyond it and over-report range in exactly that direction.
    The extra 10 m absorbs the bundle's 6-decimal rounding, which could
    otherwise pull an edge in by centimetres and leave a point on the drawn
    box's own corner reported as incomplete."""
    margin = RADIUS_M + 10.0
    s, w, _, _ = _disc_bbox(south, west, margin)
    _, _, n, e = _disc_bbox(north, east, margin)
    return s, w, n, e


def area_tile_count(south_s, west_s, north_s, east_s):
    """JSON {tiles, limit} for a selected box, so the tab can refuse an area
    that is too big before downloading anything."""
    try:
        s, w, n, e = _normalise_box(south_s, west_s, north_s, east_s)
        tiles = overture_tiles.tiles_for_bbox(
            *_area_bbox_with_margin(s, w, n, e), z=overture_tiles.MAX_ZOOM)
        return json.dumps({"tiles": len(tiles), "limit": overture_tiles.MAX_AREA_TILES})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "%s: %s" % (type(e).__name__, e)})


def _normalise_box(south_s, west_s, north_s, east_s):
    s, w, n, e = float(south_s), float(west_s), float(north_s), float(east_s)
    s, n = min(s, n), max(s, n)
    w, e = min(w, e), max(w, e)
    if not (-85.0 <= s < n <= 85.0 and -180.0 <= w < e <= 180.0):
        raise ValueError("not a valid area: %r" % ((s, w, n, e),))
    return s, w, n, e


def _fetch_and_save(S, W, N, E, selected, prefix, label, keep=1, report=True,
                    timing=None, stoppable=False):
    """Download Overture buildings + streets + terrain for (S, W, N, E) and
    save them as a bundle. Returns (file name or None, buildings, roads,
    road_names, release). With no storage directory the data is returned
    unsaved.

    A saved area REPLACES the previous one of the same kind (prefix): one
    selected area, one automatic area, the newest `keep` peer areas. The old
    file is removed only after the new one is safely on disk, so a failed
    download loses nothing. report=False keeps it out of download_progress()
    (a peer's download is not the operator's)."""
    say = _set_progress if report else (lambda **kw: None)
    timing = {} if timing is None else timing
    t0 = time.monotonic()
    say(active=True, done=0, total=0, stage="Reading Overture index")

    def check_stop():
        if stoppable and _stop_download.is_set():
            raise DownloadStopped()

    def progress(done, total):
        check_stop()
        say(done=done, total=total, stage="Downloading map tiles")

    buildings, pairs, release = overture_tiles.fetch_area(S, W, N, E, progress)
    t1 = time.monotonic()
    timing["overture_s"] = round(t1 - t0, 2)
    timing.update(overture_tiles.last_fetch_stats())
    roads = [line for line, _ in pairs]
    road_names = [name for _, name in pairs]
    # Terrain too, so the area needs no network at all from here on.
    # Saved to disk by Terrain._load; it is shared between areas, so
    # deleting an area leaves it in place.
    keys = terrain_keys_for_box(S, W, N, E)
    for i, key in enumerate(keys):
        check_stop()
        say(done=i, total=len(keys), stage="Downloading terrain")
        Terrain._load(key)
    check_stop()
    t2 = time.monotonic()
    timing["terrain_fetch_s"] = round(t2 - t1, 2)
    if not _BUNDLED_DIR:
        return None, buildings, roads, road_names, release
    say(done=0, total=0, stage="Saving")
    bundle = {
        "source": "overture %s (%s)" % (release, label),
        "created": int(time.time()),
        "south": round(S, 6), "west": round(W, 6),
        "north": round(N, 6), "east": round(E, 6),
        # The box the operator drew, kept for drawing it back on the map.
        "selected": [round(v, 6) for v in selected],
        # centre/radius for anything that only understands the older
        # desktop format: the INSCRIBED circle, so it never over-claims.
        "center_lat": round((S + N) / 2, 6),
        "center_lon": round((W + E) / 2, 6),
        "radius_m": round(min((N - S) * 110540.0,
                              (E - W) * 111320.0 * math.cos(math.radians((S + N) / 2)))
                          / 2.0, 0),
        "buildings": buildings,
        "roads": roads,
        "road_names": road_names,
    }
    os.makedirs(_BUNDLED_DIR, exist_ok=True)
    name = "%s_%.4f_%.4f_%.4f_%.4f.json" % ((prefix,) + tuple(selected))
    path = os.path.join(_BUNDLED_DIR, name)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(bundle, f, separators=(",", ":"))
    os.replace(tmp, path)   # a half-written bundle must never be read
    timing["save_s"] = round(time.monotonic() - t2, 2)
    others = []
    for old in os.listdir(_BUNDLED_DIR):
        if old != name and old.startswith(prefix + "_") and old.endswith(".json"):
            try:
                others.append((os.path.getmtime(os.path.join(_BUNDLED_DIR, old)), old))
            except OSError:
                pass
    others.sort(reverse=True)               # newest first; the new one is kept
    for _, old in others[max(0, keep - 1):]:
        try:
            os.remove(os.path.join(_BUNDLED_DIR, old))
        except OSError:
            pass
        _bundle_cache.pop(os.path.join(_BUNDLED_DIR, old), None)
    return name, buildings, roads, road_names, release


def download_area(south_s, west_s, north_s, east_s):
    """Kotlin entry point: download Overture for a selected box, save it as a
    bundle, return JSON {file, buildings, roads, release, south, west, north,
    east} or {"error": ...}. Never raises. Blocks for the whole download --
    call it off the UI thread and poll download_progress()."""
    try:
        if not _BUNDLED_DIR:
            return json.dumps({"error": "no storage directory for downloaded areas"})
        s, w, n, e = _normalise_box(south_s, west_s, north_s, east_s)
        S, W, N, E = _area_bbox_with_margin(s, w, n, e)
        _stop_download.clear()
        name, buildings, roads, _, release = _fetch_and_save(
            S, W, N, E, (s, w, n, e), "overture", "phone", stoppable=True)
        # Anything computed before this area existed used OSM (or nothing);
        # drop it so the next compute() at these spots reads the new data.
        _coverage_cache.clear()
        _buildings_cache.clear()
        _peer_cache.clear()
        return json.dumps({
            "file": name, "buildings": len(buildings), "roads": len(roads),
            "release": release,
            "south": s, "west": w, "north": n, "east": e,
            "size_kb": int(os.path.getsize(os.path.join(_BUNDLED_DIR, name)) / 1024),
        })
    except DownloadStopped:
        # Nothing was saved, so the previously downloaded area is untouched.
        return json.dumps({"stopped": True})
    except Exception as e:  # noqa: BLE001 -- the tab must get a reason, not a trace
        return json.dumps({"error": "%s: %s" % (type(e).__name__, e)})
    finally:
        _stop_download.clear()
        _set_progress(active=False)


# How far past the computation disc an automatic download reaches, so a
# short walk stays inside the area just fetched instead of fetching again.
AUTO_MARGIN_M = 500.0


def _auto_overture(lat, lon, prefix="auto", keep=1, report=True, timing=None):
    """Overture for the disc around (lat, lon), saved as an automatic area
    ("auto", or "peer" for another node's). Returns (buildings, roads,
    road_names, source). Raises on failure."""
    S, W, N, E = _disc_bbox(lat, lon, RADIUS_M + AUTO_MARGIN_M)
    try:
        _, buildings, roads, names, release = _fetch_and_save(
            S, W, N, E, (S, W, N, E), prefix, prefix, keep, report, timing)
    finally:
        if report:
            _set_progress(active=False)
    return buildings, roads, names, "overture %s (%s)" % (release, prefix)


def list_areas():
    """Kotlin entry point: JSON list of downloaded areas, for outlining them
    on the map so the operator can see where offline data already exists."""
    out = []
    for path in _bundle_paths():
        bundle = _read_bundle(path)
        if bundle is None:
            continue
        sel = bundle.get("selected")
        bb = _bundle_bbox(bundle)
        if sel is None and bb is not None:
            sel = list(bb)
        if sel is None:
            # A desktop bundle: describe its circle by its bounding box.
            try:
                sel = list(_disc_bbox(bundle["center_lat"], bundle["center_lon"],
                                      bundle["radius_m"]))
            except (KeyError, TypeError, ValueError):
                continue
        name = os.path.basename(path)
        prefix = name.split("_", 1)[0]
        out.append({"file": name,
                    # "overture" = downloaded by the operator; "auto" and
                    # "peer" are the app's own, replaced without asking;
                    # "bundle" = anything else (a desktop-converted file).
                    "kind": prefix if prefix in ("overture", "auto", "peer") else "bundle",
                    "source": bundle.get("source") or "bundle",
                    "south": sel[0], "west": sel[1], "north": sel[2], "east": sel[3],
                    "buildings": len(bundle.get("buildings", []))})
    return json.dumps(out)


def delete_area(name):
    """Kotlin entry point: remove one downloaded area by file name. Only a
    plain file name inside the bundle directory is accepted."""
    try:
        if not _BUNDLED_DIR or os.path.basename(name) != name or not name.endswith(".json"):
            return json.dumps({"error": "bad area name %r" % name})
        path = os.path.join(_BUNDLED_DIR, name)
        os.remove(path)
        _bundle_cache.pop(path, None)
        _coverage_cache.clear()
        _buildings_cache.clear()
        _peer_cache.clear()
        return json.dumps({"deleted": name})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "%s: %s" % (type(e).__name__, e)})


def basemap(south_s, west_s, north_s, east_s):
    """Kotlin entry point: major streets over the map's current view, as
    JSON {"roads": [[[lat, lon], ...], ...], "names": [...]}, names aligned
    with roads. Lets the operator find the area
    they want before anything has been downloaded or computed there."""
    try:
        s, w, n, e = _normalise_box(south_s, west_s, north_s, east_s)
        pairs = overture_tiles.fetch_basemap(s, w, n, e)
        lines = [line for line, _ in pairs]
        names = [short_street_name(n) for _, n in pairs]
        # 2e-5 degrees is ~2 m, the same joining tolerance as the model's.
        return json.dumps({"roads": lines, "names": names,
                           "labels": merge_street_lines(lines, names, 2e-5)})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "%s: %s" % (type(e).__name__, e)})
