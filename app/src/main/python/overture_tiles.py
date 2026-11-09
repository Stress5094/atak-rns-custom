"""
Overture Maps buildings and streets, read ON THE PHONE.

The Coverage tab's range prediction is only as good as its building
footprints. OpenStreetMap's are hand-mapped and miss the machine-detected
buildings (stadiums, big-box stores, warehouses) that Overture conflates in
from Microsoft/Google/Esri, and OSM rarely carries a height. Overture has
both. Until this module the only way to get Overture data onto the phone was
`tools/overture_to_geojson.py` on a laptop, because Overture's primary
distribution is GeoParquet and Chaquopy has no pyarrow.

The way round that: Overture ALSO publishes every theme as a PMTiles
archive -- one static file of Mapbox Vector Tiles, addressed by HTTP range
requests. Both formats are simple enough to read with the standard library:

    PMTiles v3   a 127-byte header, varint-coded directories, gzip
    MVT          protobuf (varints + length-delimited fields), zigzag deltas

so this module fetches only the zoom-14 tiles an area touches -- a few
hundred KB each -- and turns them into the same bundle format the desktop
converter writes. coverage.py then reads that bundle exactly as it reads a
sideloaded one.

What the tiles carry (read from the archive's own metadata, 2026-09-23.1):
the `building` layer has height, num_floors, class, is_underground at
z14; the transportation `segment` layer has subtype + class. Full detail is
z14 only -- lower zooms drop and simplify features -- so buildings are
always read at MAX_ZOOM, and only the street basemap uses a coarser zoom.

Two artefacts of tiling, both handled:

  * A building that straddles a tile edge arrives as one clipped piece per
    tile, plus the tile buffer's overlap. Pieces keep the feature's Overture
    `id`, so coverage.py counts a crossed building once however many pieces
    the ray touches.
  * Polygon holes (courtyards) are dropped. A ray at 1.5 m that crosses the
    courtyard also crosses the walls around it, so the hole never changes
    whether the building blocks the ray.

Pure standard library and Python 3.9, like coverage.py, for the same reason:
it runs inside Chaquopy and its tests run on a bare desktop Python.
"""

import gzip
import json
import math
import struct
import threading
import time
import urllib.request

STAC_CATALOG = "https://stac.overturemaps.org/catalog.json"
TILES_BASE = ("https://overturemaps-extras-us-west-2.s3.us-west-2.amazonaws.com"
              "/tiles/%s/%s.pmtiles")
# Used when the catalog cannot be read. Tiles for a release stay published
# for a while after the next one, so a stale fallback still works.
FALLBACK_RELEASE = "2026-09-23.1"
_UA = "rtak-fm-coverage/1.0 (field planning)"

MAX_ZOOM = 14          # the only zoom with full building detail and heights
BASEMAP_ZOOM = 13      # streets for orienting the map; ~4x fewer tiles
MAX_AREA_TILES = 64    # hard cap on one download (~5 x 5 km at z14 at 33 N)
MAX_BASEMAP_TILES = 16

# Streets drawn on the map, the same classes coverage.py asks Overpass for.
ROAD_CLASSES = frozenset((
    "motorway", "trunk", "primary", "secondary", "tertiary", "residential",
    "unclassified", "living_street",
))
# Coarser basemap: enough to recognise where you are, cheap to draw.
BASEMAP_ROAD_CLASSES = frozenset((
    "motorway", "trunk", "primary", "secondary", "tertiary",
))

# Classes Overture uses for structures that do not wall off a 1.5 m ray --
# the same exclusions coverage.fetch_data() makes for OSM.
_OPEN_CLASSES = frozenset(("carport", "roof", "shed", "garage", "garages"))


# --- tile maths ---------------------------------------------------------------

def lonlat_to_tile(lon, lat, z):
    """Web-Mercator tile (x, y) containing a point, clamped to the grid."""
    n = 1 << z
    lat = max(min(lat, 85.0511), -85.0511)
    x = int((lon + 180.0) / 360.0 * n)
    r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_to_lonlat(z, x, y):
    """(lon, lat) of a tile coordinate's top-left corner; x, y may be
    fractional, which is how a vertex inside a tile is placed."""
    n = float(1 << z)
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lon, lat


def tiles_for_bbox(south, west, north, east, z):
    x0, y0 = lonlat_to_tile(west, north, z)
    x1, y1 = lonlat_to_tile(east, south, z)
    return [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def zxy_to_tileid(z, x, y):
    """PMTiles v3 tile id: tiles of all lower zooms, then the Hilbert index.

    Getting this wrong does not fail -- it silently returns a DIFFERENT tile,
    whose features are then placed at the requested tile's position. The
    tests pin it against the spec's published vectors for that reason.
    """
    acc = 0
    for i in range(z):
        acc += (1 << i) * (1 << i)
    n = 1 << z
    d = 0
    s = n >> 1
    while s > 0:
        rx = 1 if (x & s) else 0
        ry = 1 if (y & s) else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = n - 1 - x
                y = n - 1 - y
            x, y = y, x
        s >>= 1
    return acc + d


# --- protobuf / varints -------------------------------------------------------

def _varint(buf, pos):
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def _zigzag(n):
    return (n >> 1) ^ -(n & 1)


def _fields(buf):
    """Yield (field_number, wire_type, value) over one protobuf message.
    Length-delimited values come back as memoryview slices, unparsed."""
    pos = 0
    end = len(buf)
    while pos < end:
        key, pos = _varint(buf, pos)
        field, wt = key >> 3, key & 7
        if wt == 0:
            val, pos = _varint(buf, pos)
        elif wt == 2:
            ln, pos = _varint(buf, pos)
            val = buf[pos:pos + ln]
            pos += ln
        elif wt == 5:
            val = buf[pos:pos + 4]
            pos += 4
        elif wt == 1:
            val = buf[pos:pos + 8]
            pos += 8
        else:
            raise ValueError("unsupported protobuf wire type %d" % wt)
        yield field, wt, val


def _packed(buf):
    out = []
    pos = 0
    while pos < len(buf):
        v, pos = _varint(buf, pos)
        out.append(v)
    return out


# --- MVT ----------------------------------------------------------------------

def _mvt_value(buf):
    for f, wt, v in _fields(buf):
        if f == 1:
            return bytes(v).decode("utf-8", "replace")
        if f == 2:
            return struct.unpack("<f", bytes(v))[0]
        if f == 3:
            return struct.unpack("<d", bytes(v))[0]
        if f in (4, 5):
            return v
        if f == 6:
            return _zigzag(v)
        if f == 7:
            return bool(v)
    return None


def _geometry(cmds):
    """Decode an MVT command stream into rings/lines of (x, y) tile units."""
    parts = []
    cur = None
    x = y = 0
    i = 0
    while i < len(cmds):
        cmd = cmds[i]
        i += 1
        op, count = cmd & 7, cmd >> 3
        if op == 1:          # MoveTo
            for _ in range(count):
                x += _zigzag(cmds[i])
                y += _zigzag(cmds[i + 1])
                i += 2
                cur = [(x, y)]
                parts.append(cur)
        elif op == 2:        # LineTo
            for _ in range(count):
                x += _zigzag(cmds[i])
                y += _zigzag(cmds[i + 1])
                i += 2
                cur.append((x, y))
        elif op == 7:        # ClosePath -- the ring is closed implicitly
            pass
        else:
            raise ValueError("bad MVT command %d" % op)
    return parts


def _ring_area(ring):
    """Shoelace area in tile units. In MVT's y-down space an EXTERIOR ring
    has positive area and a hole negative (spec 4.3.4.4)."""
    a = 0
    n = len(ring)
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return a / 2.0


def decode_mvt(data, want_layers=None):
    """{layer_name: [feature, ...]} for one vector tile.

    Each feature is {"type": 1|2|3, "props": {...}, "parts": [[(x, y)...]],
    "extent": int}. Only layers in want_layers are decoded when given.
    """
    buf = memoryview(data)
    layers = {}
    for f, _, lbuf in _fields(buf):
        if f != 3:
            continue
        name = None
        extent = 4096
        keys = []
        values = []
        raw_features = []
        for lf, _, lv in _fields(lbuf):
            if lf == 1:
                name = bytes(lv).decode("utf-8")
            elif lf == 2:
                raw_features.append(lv)
            elif lf == 3:
                keys.append(bytes(lv).decode("utf-8"))
            elif lf == 4:
                values.append(lv)
            elif lf == 5:
                extent = lv
        if want_layers is not None and name not in want_layers:
            continue
        decoded_values = [_mvt_value(v) for v in values]
        feats = []
        for fbuf in raw_features:
            ftype = 0
            tags = []
            geom = []
            for ff, _, fv in _fields(fbuf):
                if ff == 2:
                    tags = _packed(fv)
                elif ff == 3:
                    ftype = fv
                elif ff == 4:
                    geom = _packed(fv)
            props = {}
            for k in range(0, len(tags) - 1, 2):
                props[keys[tags[k]]] = decoded_values[tags[k + 1]]
            feats.append({"type": ftype, "props": props,
                          "parts": _geometry(geom), "extent": extent})
        layers.setdefault(name, []).extend(feats)
    return layers


# --- PMTiles ------------------------------------------------------------------

def _decompress(data, kind):
    if kind in (0, 1):       # unknown / none
        return data
    if kind == 2:
        return gzip.decompress(data)
    raise ValueError("unsupported PMTiles compression %d" % kind)


def parse_header(h):
    if len(h) < 127 or h[:7] != b"PMTiles":
        raise ValueError("not a PMTiles archive")
    if h[7] != 3:
        raise ValueError("PMTiles version %d unsupported (need 3)" % h[7])
    f = struct.unpack("<11Q", h[8:96])
    return {
        "root_offset": f[0], "root_length": f[1],
        "metadata_offset": f[2], "metadata_length": f[3],
        "leaf_offset": f[4], "leaf_length": f[5],
        "data_offset": f[6], "data_length": f[7],
        "internal_compression": h[97], "tile_compression": h[98],
        "tile_type": h[99], "min_zoom": h[100], "max_zoom": h[101],
    }


def parse_directory(buf):
    """List of (tile_id, offset, length, run_length), sorted by tile_id.
    run_length 0 marks a pointer to a leaf directory."""
    pos = 0
    n, pos = _varint(buf, pos)
    ids = []
    last = 0
    for _ in range(n):
        d, pos = _varint(buf, pos)
        last += d
        ids.append(last)
    runs = []
    for _ in range(n):
        v, pos = _varint(buf, pos)
        runs.append(v)
    lengths = []
    for _ in range(n):
        v, pos = _varint(buf, pos)
        lengths.append(v)
    offsets = []
    for i in range(n):
        v, pos = _varint(buf, pos)
        if v == 0 and i > 0:
            offsets.append(offsets[i - 1] + lengths[i - 1])
        else:
            offsets.append(v - 1)
    return list(zip(ids, offsets, lengths, runs))


def find_entry(entries, tile_id):
    """The entry covering tile_id, or None (binary search, as the spec)."""
    lo, hi = 0, len(entries) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        c = entries[mid][0]
        if c < tile_id:
            lo = mid + 1
        elif c > tile_id:
            hi = mid - 1
        else:
            return entries[mid]
    if hi >= 0:
        e = entries[hi]
        if e[3] == 0:
            return e                    # leaf directory: descend
        if tile_id - e[0] < e[3]:
            return e                    # inside a run of identical tiles
    return None


# Per THREAD, so a peer's download is not charged for the basemap thread's
# requests running beside it: how many range requests, how many bytes, and
# how long they took. Read by fetch_area() for last_fetch_stats().
_net = threading.local()


def _net_totals():
    return (getattr(_net, "reads", 0), getattr(_net, "bytes", 0),
            getattr(_net, "secs", 0.0))


def last_fetch_stats():
    """This thread's last fetch_area(): {tiles, http_reads, http_kb, http_s,
    decode_s}, or {} if it has not run one."""
    return dict(getattr(_net, "last", {}))


class HttpSource:
    """Byte ranges of one URL. Swappable for a test double."""

    def __init__(self, url, timeout=60):
        self.url = url
        self.timeout = timeout

    def read(self, offset, length):
        req = urllib.request.Request(self.url, headers={
            "User-Agent": _UA,
            "Range": "bytes=%d-%d" % (offset, offset + length - 1),
        })
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = r.read()
        finally:
            _net.reads = getattr(_net, "reads", 0) + 1
            _net.secs = getattr(_net, "secs", 0.0) + (time.monotonic() - t0)
        _net.bytes = getattr(_net, "bytes", 0) + len(data)
        if len(data) != length:
            raise IOError("short read from %s: wanted %d bytes, got %d"
                          % (self.url, length, len(data)))
        return data


class PMTiles:
    """Reads tiles from one archive, caching directories (not tiles)."""

    def __init__(self, source):
        self.src = source
        self.header = parse_header(source.read(0, 127))
        h = self.header
        self._root = parse_directory(_decompress(
            source.read(h["root_offset"], h["root_length"]),
            h["internal_compression"]))
        self._leaves = {}

    def _leaf(self, offset, length):
        key = (offset, length)
        if key not in self._leaves:
            h = self.header
            self._leaves[key] = parse_directory(_decompress(
                self.src.read(h["leaf_offset"] + offset, length),
                h["internal_compression"]))
        return self._leaves[key]

    def get(self, z, x, y):
        """Decompressed tile bytes, or None where the archive has no tile
        (open desert, ocean -- not an error)."""
        tid = zxy_to_tileid(z, x, y)
        entries = self._root
        for _ in range(4):           # spec: at most 3 levels of leaves
            e = find_entry(entries, tid)
            if e is None:
                return None
            _, offset, length, run = e
            if run > 0:
                h = self.header
                return _decompress(self.src.read(h["data_offset"] + offset, length),
                                   h["tile_compression"])
            entries = self._leaf(offset, length)
        raise ValueError("PMTiles directory deeper than the spec allows")


# --- releases -----------------------------------------------------------------

_release = None
_archives = {}


def latest_release():
    """The newest Overture release name, from the STAC catalog; the fallback
    constant if the catalog cannot be read."""
    global _release
    if _release is None:
        try:
            req = urllib.request.Request(STAC_CATALOG, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                cat = json.loads(r.read().decode("utf-8"))
            _release = cat.get("latest") or FALLBACK_RELEASE
        except Exception:  # noqa: BLE001 -- the fallback is the answer
            _release = FALLBACK_RELEASE
    return _release


def archive(theme):
    """The cached PMTiles reader for a theme. Falls back to FALLBACK_RELEASE
    when the latest release has no tiles published yet."""
    if theme not in _archives:
        rel = latest_release()
        try:
            _archives[theme] = (PMTiles(HttpSource(TILES_BASE % (rel, theme))), rel)
        except Exception:  # noqa: BLE001
            if rel == FALLBACK_RELEASE:
                raise
            _archives[theme] = (PMTiles(HttpSource(TILES_BASE % (FALLBACK_RELEASE, theme))),
                                FALLBACK_RELEASE)
    return _archives[theme]


# --- features -> bundle -------------------------------------------------------

def _to_latlon(part, z, tx, ty, extent):
    out = []
    for px, py in part:
        lon, lat = tile_to_lonlat(z, tx + px / float(extent), ty + py / float(extent))
        out.append([round(lat, 7), round(lon, 7)])
    return out


def building_height(props):
    """Overture height, else floors x 3 m, else a class default -- the same
    order tools/overture_to_geojson.py uses, so a bundle built on the phone
    and one built on a laptop agree."""
    h = props.get("height")
    if isinstance(h, (int, float)) and h > 0:
        return float(h)
    nf = props.get("num_floors")
    if isinstance(nf, (int, float)) and nf > 0:
        return nf * 3.0
    c = str(props.get("class") or "").lower()
    if c in ("commercial", "industrial", "institutional", "education", "retail",
             "office", "apartments"):
        return 9.0
    return 5.0


def buildings_from_tile(layers, z, tx, ty):
    out = []
    for f in layers.get("building", ()):
        if f["type"] != 3:
            continue
        p = f["props"]
        if p.get("is_underground") is True:
            continue
        if str(p.get("class") or "").lower() in _OPEN_CLASSES:
            continue
        h = round(building_height(p), 1)
        bid = p.get("id")
        for ring in f["parts"]:
            if len(ring) < 3 or _ring_area(ring) <= 0:
                continue                 # a hole, or degenerate
            b = {"poly": _to_latlon(ring, z, tx, ty, f["extent"]), "h": h}
            if bid:
                b["id"] = bid
            out.append(b)
    return out


def roads_from_tile(layers, z, tx, ty, classes):
    """[(line, name)] for the streets in a tile; name is "" when unnamed.
    Overture's tiles carry a ready-made display name in `@name` (the full
    `names` property is a JSON string of every variant)."""
    out = []
    for f in layers.get("segment", ()):
        if f["type"] != 2:
            continue
        p = f["props"]
        if p.get("subtype") != "road" or p.get("class") not in classes:
            continue
        name = p.get("@name") or ""
        for line in f["parts"]:
            if len(line) >= 2:
                out.append((_to_latlon(line, z, tx, ty, f["extent"]), name))
    return out


def _in_bbox(points, south, west, north, east):
    """Does the feature's extent overlap the area? Overlap, not "a vertex
    inside": a straight arterial is one segment whose two vertices can both
    lie outside the area it runs straight across."""
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return (min(lats) <= north and max(lats) >= south and
            min(lons) <= east and max(lons) >= west)


def fetch_area(south, west, north, east, progress=None):
    """Buildings + streets inside a bbox, from Overture's z14 tiles.

    Returns (buildings, roads, release); roads are (line, name) pairs. Raises ValueError when the area
    needs more than MAX_AREA_TILES tiles -- a phone on mobile data should be
    told the area is too big, not left downloading for ten minutes.
    progress(done, total) is called after each tile if given.
    """
    tiles = tiles_for_bbox(south, west, north, east, MAX_ZOOM)
    if len(tiles) > MAX_AREA_TILES:
        raise ValueError("area needs %d map tiles, the limit is %d -- select a "
                         "smaller area" % (len(tiles), MAX_AREA_TILES))
    t0 = time.monotonic()
    n0 = _net_totals()
    _net.last = {}
    bld_arc, release = archive("buildings")
    trn_arc, _ = archive("transportation")
    buildings = []
    roads = []
    total = 2 * len(tiles)
    done = 0
    for z, x, y in tiles:
        data = bld_arc.get(z, x, y)
        if data:
            for b in buildings_from_tile(decode_mvt(data, ("building",)), z, x, y):
                if _in_bbox(b["poly"], south, west, north, east):
                    buildings.append(b)
        done += 1
        if progress:
            progress(done, total)
        data = trn_arc.get(z, x, y)
        if data:
            for r in roads_from_tile(decode_mvt(data, ("segment",)), z, x, y,
                                     ROAD_CLASSES):
                if _in_bbox(r[0], south, west, north, east):
                    roads.append(r)
        done += 1
        if progress:
            progress(done, total)
    n1 = _net_totals()
    http_s = n1[2] - n0[2]
    _net.last = {"tiles": len(tiles), "http_reads": n1[0] - n0[0],
                 "http_kb": (n1[1] - n0[1]) // 1024, "http_s": round(http_s, 2),
                 "decode_s": round(time.monotonic() - t0 - http_s, 2)}
    return buildings, roads, release


def fetch_basemap(south, west, north, east):
    """Major streets over a viewport, for orienting the map before an area
    is chosen. Coarser zoom and classes than fetch_area; if the view needs
    more than MAX_BASEMAP_TILES tiles the zoom steps down until it fits."""
    z = BASEMAP_ZOOM
    tiles = tiles_for_bbox(south, west, north, east, z)
    while len(tiles) > MAX_BASEMAP_TILES and z > 8:
        z -= 1
        tiles = tiles_for_bbox(south, west, north, east, z)
    if len(tiles) > MAX_BASEMAP_TILES:
        return []
    arc, _ = archive("transportation")
    roads = []
    for key in tiles:
        hit = _basemap_cache.pop(key, None)
        if hit is None:
            data = arc.get(*key)
            hit = (roads_from_tile(decode_mvt(data, ("segment",)), key[0], key[1],
                                   key[2], BASEMAP_ROAD_CLASSES) if data else [])
        _basemap_cache[key] = hit           # re-insert: most recent last
        while len(_basemap_cache) > _BASEMAP_CACHE_MAX:
            del _basemap_cache[next(iter(_basemap_cache))]
        roads.extend(hit)
    return roads


# Decoded basemap tiles, least recently used first (dicts keep insertion
# order). Panning asks for mostly the same tiles again.
_basemap_cache = {}
_BASEMAP_CACHE_MAX = 128
