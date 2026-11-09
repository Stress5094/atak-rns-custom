"""
Tests for overture_tiles.py -- reading Overture's PMTiles on the phone.

No network: the PMTiles archives and vector tiles here are built in memory by
a minimal encoder below, so every decode path is checked against bytes whose
meaning is known exactly. The live archive was cross-checked once against
Overture's GeoParquet (same release, 760 buildings: heights within 0.05 m,
centroids within 0.85 m); that comparison needs pyarrow and the network and
is not repeated here.
"""

import gzip
import struct
import unittest

import overture_tiles as ot


# --- a minimal protobuf / MVT / PMTiles encoder --------------------------------

def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def zz(n):
    return (n << 1) ^ (n >> 63)


def field(num, wt, payload):
    key = varint((num << 3) | wt)
    if wt == 0:
        return key + varint(payload)
    if wt == 2:
        return key + varint(len(payload)) + payload
    raise ValueError(wt)


def packed(nums):
    return b"".join(varint(n) for n in nums)


def geom(parts, close):
    """MVT command stream for rings (close=True) or lines."""
    out = []
    x = y = 0
    for part in parts:
        px, py = part[0]
        out += [(1 << 3) | 1, zz(px - x), zz(py - y)]
        x, y = px, py
        rest = part[1:]
        out.append(((len(rest)) << 3) | 2)
        for px, py in rest:
            out += [zz(px - x), zz(py - y)]
            x, y = px, py
        if close:
            out.append((1 << 3) | 7)
    return out


def value(v):
    if isinstance(v, bool):
        return field(7, 0, int(v))
    if isinstance(v, str):
        return field(1, 2, v.encode())
    if isinstance(v, float):
        return varint((3 << 3) | 1) + struct.pack("<d", v)
    if isinstance(v, int):
        return field(6, 0, zz(v)) if v < 0 else field(5, 0, v)
    raise TypeError(v)


def layer(name, features, extent=4096):
    """features: [(type, props_dict, parts)]"""
    keys, vals = [], []
    body = field(15, 0, 2) + field(1, 2, name.encode())
    for ftype, props, parts in features:
        tags = []
        for k, v in props.items():
            if k not in keys:
                keys.append(k)
            if v not in vals or type(vals[vals.index(v)]) is not type(v):
                vals.append(v)
            tags += [keys.index(k), len(vals) - 1 - vals[::-1].index(v)]
        f = field(2, 2, packed(tags)) + field(3, 0, ftype) + \
            field(4, 2, packed(geom(parts, ftype == 3)))
        body += field(2, 2, f)
    for k in keys:
        body += field(3, 2, k.encode())
    for v in vals:
        body += field(4, 2, value(v))
    body += field(5, 0, extent)
    return field(3, 2, body)


def directory(entries):
    """entries: [(tile_id, offset, length, run_length)] sorted."""
    out = varint(len(entries))
    last = 0
    for tid, _, _, _ in entries:
        out += varint(tid - last)
        last = tid
    for e in entries:
        out += varint(e[3])
    for e in entries:
        out += varint(e[2])
    for i, e in enumerate(entries):
        if i > 0 and e[1] == entries[i - 1][1] + entries[i - 1][2]:
            out += varint(0)
        else:
            out += varint(e[1] + 1)
    return out


def archive(tiles, leaf_split=False):
    """A whole PMTiles v3 archive in bytes. tiles: {(z,x,y): raw_bytes}.
    leaf_split puts every entry in a leaf directory behind the root."""
    items = sorted((ot.zxy_to_tileid(*k), gzip.compress(v)) for k, v in tiles.items())
    data = b""
    entries = []
    for tid, blob in items:
        entries.append((tid, len(data), len(blob), 1))
        data += blob
    if leaf_split:
        leaf = gzip.compress(directory(entries))
        root = gzip.compress(directory([(entries[0][0], 0, len(leaf), 0)]))
        leaves = leaf
    else:
        root = gzip.compress(directory(entries))
        leaves = b""
    meta = gzip.compress(b"{}")
    root_off = 127
    meta_off = root_off + len(root)
    leaf_off = meta_off + len(meta)
    data_off = leaf_off + len(leaves)
    header = b"PMTiles" + bytes([3]) + struct.pack(
        "<11Q", root_off, len(root), meta_off, len(meta), leaf_off, len(leaves),
        data_off, len(data), len(items), len(items), len(items))
    header += bytes([1, 2, 2, 1, 0, 14]) + b"\0" * (127 - len(header) - 6)
    assert len(header) == 127
    return header + root + meta + leaves + data


class BytesSource:
    def __init__(self, blob):
        self.blob = blob
        self.reads = 0

    def read(self, offset, length):
        self.reads += 1
        return self.blob[offset:offset + length]


# --- tests ---------------------------------------------------------------------

class TileIdTest(unittest.TestCase):
    def test_spec_vectors(self):
        # From the PMTiles v3 spec's own test suite. A wrong Hilbert curve
        # reads a different tile and silently places it here.
        self.assertEqual(ot.zxy_to_tileid(0, 0, 0), 0)
        self.assertEqual(ot.zxy_to_tileid(1, 0, 0), 1)
        self.assertEqual(ot.zxy_to_tileid(1, 0, 1), 2)
        self.assertEqual(ot.zxy_to_tileid(1, 1, 1), 3)
        self.assertEqual(ot.zxy_to_tileid(1, 1, 0), 4)
        self.assertEqual(ot.zxy_to_tileid(2, 0, 0), 5)
        self.assertEqual(ot.zxy_to_tileid(12, 3423, 1763), 19078479)

    def test_ids_unique_at_a_zoom(self):
        ids = {ot.zxy_to_tileid(3, x, y) for x in range(8) for y in range(8)}
        self.assertEqual(len(ids), 64)
        self.assertEqual(min(ids), 1 + 4 + 16)


class TileMathTest(unittest.TestCase):
    def test_coverage_tile(self):
        # Example Ave & Example Rd at z14.
        self.assertEqual(ot.lonlat_to_tile(-140.5823, 20.9120, 14), (1793, 7218))

    def test_corner_round_trip(self):
        lon, lat = ot.tile_to_lonlat(14, 1793, 7218)
        self.assertEqual(ot.lonlat_to_tile(lon + 1e-9, lat - 1e-9, 14), (1793, 7218))

    def test_bbox_tiles(self):
        t = ot.tiles_for_bbox(20.9095, -140.5853, 20.9145, -140.5793, 14)
        self.assertTrue(all(z == 14 for z, _, _ in t))
        self.assertIn((14, 1793, 7218), t)


class MvtTest(unittest.TestCase):
    SQUARE = [(100, 100), (200, 100), (200, 200), (100, 200)]   # y-down: clockwise => exterior
    HOLE = [(120, 120), (120, 180), (180, 180), (180, 120)]

    def test_properties_and_geometry(self):
        tile = layer("building", [(3, {"id": "b1", "height": 12.5, "class": "office",
                                       "num_floors": 3, "is_underground": False},
                                   [self.SQUARE])])
        f = ot.decode_mvt(tile)["building"][0]
        self.assertEqual(f["type"], 3)
        self.assertEqual(f["props"], {"id": "b1", "height": 12.5, "class": "office",
                                      "num_floors": 3, "is_underground": False})
        self.assertEqual(f["parts"], [self.SQUARE])

    def test_layer_filter(self):
        tile = layer("building", [(3, {}, [self.SQUARE])]) + layer("other", [(3, {}, [self.SQUARE])])
        self.assertEqual(list(ot.decode_mvt(tile, ("building",))), ["building"])

    def test_negative_deltas(self):
        line = [(500, 500), (10, 400), (3000, 20)]
        f = ot.decode_mvt(layer("segment", [(2, {}, [line])]))["segment"][0]
        self.assertEqual(f["parts"], [line])

    def test_exterior_kept_hole_dropped(self):
        tile = layer("building", [(3, {"id": "x", "height": 6.0}, [self.SQUARE, self.HOLE])])
        out = ot.buildings_from_tile(ot.decode_mvt(tile), 14, 0, 0)
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0]["poly"]), 4)
        self.assertEqual(out[0]["id"], "x")

    def test_ring_not_closed_by_repeat(self):
        # MVT closes rings with a command, not a repeated vertex. coverage's
        # segment test was once broken on exactly that repeated vertex.
        tile = layer("building", [(3, {"height": 6.0}, [self.SQUARE])])
        poly = ot.buildings_from_tile(ot.decode_mvt(tile), 14, 0, 0)[0]["poly"]
        self.assertNotEqual(poly[0], poly[-1])


class BuildingFilterTest(unittest.TestCase):
    SQ = [(0, 0), (100, 0), (100, 100), (0, 100)]

    def _one(self, props):
        tile = layer("building", [(3, props, [self.SQ])])
        return ot.buildings_from_tile(ot.decode_mvt(tile), 14, 1793, 7218)

    def test_underground_and_open_structures_dropped(self):
        self.assertEqual(self._one({"is_underground": True, "height": 5.0}), [])
        for c in ("carport", "roof", "garage"):
            self.assertEqual(self._one({"class": c}), [], c)

    def test_height_order(self):
        self.assertEqual(ot.building_height({"height": 21.3, "num_floors": 2}), 21.3)
        self.assertEqual(ot.building_height({"num_floors": 4}), 12.0)
        self.assertEqual(ot.building_height({"class": "commercial"}), 9.0)
        self.assertEqual(ot.building_height({}), 5.0)
        self.assertEqual(ot.building_height({"height": 0.0, "num_floors": 2}), 6.0)

    def test_placed_in_its_tile(self):
        poly = self._one({"height": 5.0})[0]["poly"]
        w, n = ot.tile_to_lonlat(14, 1793, 7218)
        e, s = ot.tile_to_lonlat(14, 1794, 7219)
        for la, lo in poly:
            self.assertTrue(s - 1e-6 <= la <= n + 1e-6 and w - 1e-6 <= lo <= e + 1e-6)


class RoadFilterTest(unittest.TestCase):
    def test_class_and_subtype(self):
        line = [(0, 0), (100, 100)]
        tile = layer("segment", [
            (2, {"subtype": "road", "class": "residential"}, [line]),
            (2, {"subtype": "road", "class": "footway"}, [line]),
            (2, {"subtype": "rail", "class": "standard_gauge"}, [line]),
        ])
        out = ot.roads_from_tile(ot.decode_mvt(tile), 14, 0, 0, ot.ROAD_CLASSES)
        self.assertEqual(len(out), 1)

    def test_name_carried(self):
        line = [(0, 0), (100, 100)]
        tile = layer("segment", [
            (2, {"subtype": "road", "class": "residential", "@name": "West Sierra Street"}, [line]),
            (2, {"subtype": "road", "class": "residential"}, [line]),
        ])
        out = ot.roads_from_tile(ot.decode_mvt(tile), 14, 0, 0, ot.ROAD_CLASSES)
        self.assertEqual([n for _, n in out], ["West Sierra Street", ""])


class PMTilesTest(unittest.TestCase):
    def setUp(self):
        self.tiles = {(14, 1793, 7218): b"tile-a", (14, 1794, 7218): b"tile-b",
                      (0, 0, 0): b"world"}

    def test_reads_tiles_from_root(self):
        p = ot.PMTiles(BytesSource(archive(self.tiles)))
        for k, v in self.tiles.items():
            self.assertEqual(p.get(*k), v)

    def test_reads_tiles_through_a_leaf(self):
        src = BytesSource(archive(self.tiles, leaf_split=True))
        p = ot.PMTiles(src)
        self.assertEqual(p.get(14, 1794, 7218), b"tile-b")
        before = src.reads
        self.assertEqual(p.get(14, 1793, 7218), b"tile-a")
        self.assertEqual(src.reads, before + 1)    # leaf directory cached

    def test_missing_tile_is_none(self):
        p = ot.PMTiles(BytesSource(archive(self.tiles)))
        self.assertIsNone(p.get(14, 1, 1))
        self.assertIsNone(p.get(14, 1795, 7218))

    def test_run_length(self):
        entries = [(10, 0, 5, 3)]
        self.assertEqual(ot.find_entry(entries, 12), entries[0])
        self.assertIsNone(ot.find_entry(entries, 13))
        self.assertIsNone(ot.find_entry(entries, 9))

    def test_rejects_non_pmtiles(self):
        with self.assertRaises(ValueError):
            ot.parse_header(b"x" * 127)


class FetchAreaTest(unittest.TestCase):
    def setUp(self):
        self._saved = dict(ot._archives)

    def tearDown(self):
        ot._archives.clear()
        ot._archives.update(self._saved)

    def test_too_big_is_refused_before_any_download(self):
        ot._archives.clear()   # a download attempt would hit the network
        with self.assertRaises(ValueError):
            ot.fetch_area(20.3175, -141.4133, 21.3175, -140.4133)

    def test_area_from_in_memory_archives(self):
        sq = [(1000, 1000), (1100, 1000), (1100, 1100), (1000, 1100)]
        bt = layer("building", [(3, {"id": "b", "height": 7.0}, [sq])])
        rt = layer("segment", [(2, {"subtype": "road", "class": "primary"},
                                [[(0, 2048), (4096, 2048)]])])
        z, x, y = 14, 1793, 7218
        ot._archives["buildings"] = (ot.PMTiles(BytesSource(archive({(z, x, y): bt}))), "test")
        ot._archives["transportation"] = (ot.PMTiles(BytesSource(archive({(z, x, y): rt}))), "test")
        w, n = ot.tile_to_lonlat(z, x, y)
        e, s = ot.tile_to_lonlat(z, x + 1, y + 1)
        seen = []
        b, r, rel = ot.fetch_area(s + 1e-6, w + 1e-6, n - 1e-6, e - 1e-6,
                                  lambda d, t: seen.append((d, t)))
        self.assertEqual(rel, "test")
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["h"], 7.0)
        self.assertEqual(len(r), 1)
        self.assertEqual(seen[-1], (2, 2))


if __name__ == "__main__":
    unittest.main()
