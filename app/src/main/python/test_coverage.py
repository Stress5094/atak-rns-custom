"""
Tests for coverage.py -- the Coverage tab's engine.

Everything here exercises the pure geometry and link-budget logic with
synthetic buildings and flat terrain; the network fetch (Overpass/SRTM) is
not touched, so these run on a plain desktop Python with no network and no
third-party packages, the same way the module runs inside Chaquopy.
"""

import gzip
import json
import math
import os
import random
import tempfile
import unittest

import coverage


class FlatTerrain:
    """A terrain stub with a constant elevation, standing in for the SRTM
    reader so the geometry can be tested without downloading a tile."""

    def __init__(self, elev=0.0):
        self._elev = elev

    def elev(self, lat, lon):
        return self._elev


def square(cx, cy, half=8.0):
    return [[cx - half, cy - half], [cx + half, cy - half],
            [cx + half, cy + half], [cx - half, cy + half]]


def bldg(cx, cy, h=5.0, gelev=370.0):
    return {"poly": square(cx, cy), "h": h, "gelev": gelev}


def row(n, spacing=50.0, start=50.0):
    """A row of buildings up the y-axis (azimuth 0), the residential-grid
    case: one house every `spacing` metres."""
    return [bldg(0.0, start + i * spacing) for i in range(n)]


class LinkBudgetTest(unittest.TestCase):
    def test_known_budgets(self):
        self.assertAlmostEqual(coverage.link_budget_db(coverage.RADIOS["lora"]),
                               24.15 + 2.15 + 124.0, places=6)
        self.assertAlmostEqual(coverage.link_budget_db(coverage.RADIOS["fm"]),
                               42.15 + 2.15 + 120.0, places=6)

    def test_fm_has_more_budget_than_short_slow_lora(self):
        # At Short Slow / 22 dBm the 10 W handheld has ~14 dB more budget.
        # The old LongFast / 28 dBm entry had this the other way round.
        self.assertGreater(coverage.link_budget_db(coverage.RADIOS["fm"]),
                           coverage.link_budget_db(coverage.RADIOS["lora"]))


class TwoRayTest(unittest.TestCase):
    def test_frequency_independent_above_breakpoint(self):
        # The two-ray model's defining (and surprising) property: past the
        # breakpoint the lambda^2 cancels, so loss stops depending on frequency.
        self.assertAlmostEqual(coverage.two_ray_loss_db(1000.0, 146.0),
                               coverage.two_ray_loss_db(1000.0, 915.0), places=6)

    def test_increases_with_distance(self):
        self.assertLess(coverage.two_ray_loss_db(500.0, 915.0),
                        coverage.two_ray_loss_db(1000.0, 915.0))

    def test_positive(self):
        self.assertGreater(coverage.two_ray_loss_db(50.0, 146.0), 0.0)


class KnifeEdgeTest(unittest.TestCase):
    def test_clear_air_is_free(self):
        self.assertEqual(coverage.knife_edge_loss_db(0.0, 0.33, 100.0, 100.0), 0.0)
        self.assertEqual(coverage.knife_edge_loss_db(-5.0, 0.33, 100.0, 100.0), 0.0)

    def test_grows_with_obstruction_height(self):
        lo = coverage.knife_edge_loss_db(2.0, 0.33, 100.0, 100.0)
        hi = coverage.knife_edge_loss_db(20.0, 0.33, 100.0, 100.0)
        self.assertGreater(hi, lo)
        self.assertGreater(lo, 0.0)

    def test_closer_obstruction_blocks_more(self):
        near = coverage.knife_edge_loss_db(5.0, 0.33, 20.0, 200.0)
        far = coverage.knife_edge_loss_db(5.0, 0.33, 100.0, 200.0)
        self.assertGreater(near, far)

    def test_higher_frequency_diffracts_worse(self):
        # Shorter wavelength -> larger v -> more loss for the same screen.
        self.assertGreater(coverage.knife_edge_loss_db(5.0, 0.33, 100.0, 100.0),
                           coverage.knife_edge_loss_db(5.0, 2.05, 100.0, 100.0))


class GeometryTest(unittest.TestCase):
    def test_point_in_poly(self):
        sq = square(0.0, 0.0, 5.0)
        self.assertTrue(coverage.point_in_poly(0.0, 0.0, sq))
        self.assertFalse(coverage.point_in_poly(20.0, 20.0, sq))

    def test_segment_hits_poly(self):
        sq = square(0.0, 100.0, 5.0)
        self.assertTrue(coverage.segment_hits_poly(0.0, 0.0, 0.0, 200.0, sq))
        self.assertFalse(coverage.segment_hits_poly(100.0, 0.0, 100.0, 200.0, sq))


class SegmentEdgeCasesTest(unittest.TestCase):
    """The corner/collinear branches of the segment test.

    These once took their arguments in the wrong order, and a CLOSED ring
    (last vertex repeating the first, as every OSM way and every shapely
    exterior is) has a zero-length closing edge on which the wrong-order
    check always passed -- so every building in the ray's index cells counted
    as crossed, in every direction. Ranges on OSM data came out ~10 m."""

    BLDG = [(6.5, 14.4), (6.5, 22.8), (-9.6, 22.8), (-9.6, 11.9),
            (-0.5, 11.9), (-0.5, 14.4), (6.5, 14.4)]   # real OSM shape, closed

    def test_closed_ring_off_the_ray_is_not_hit(self):
        self.assertFalse(coverage.segment_hits_poly(0, 0, 100, 0, self.BLDG))
        self.assertFalse(coverage.segment_hits_poly(0, 0, 0, -100, self.BLDG))

    def test_closed_ring_on_the_ray_is_still_hit(self):
        self.assertTrue(coverage.segment_hits_poly(0, 0, 0, 100, self.BLDG))

    def test_closed_and_open_rings_agree(self):
        open_ring = self.BLDG[:-1]
        for bx, by in ((100, 0), (0, 100), (-100, 0), (0, -100), (70, 70), (-70, 70)):
            self.assertEqual(coverage.segment_hits_poly(0, 0, bx, by, self.BLDG),
                             coverage.segment_hits_poly(0, 0, bx, by, open_ring),
                             (bx, by))

    def test_ray_through_a_corner_exactly_is_a_hit(self):
        # The 45-degree-through-a-grid case the collinear branches exist for.
        sq = [(10.0, 10.0), (20.0, 10.0), (20.0, 0.0), (30.0, 0.0), (30.0, 30.0), (10.0, 30.0)]
        self.assertTrue(coverage._segs_intersect((0, 0), (50, 50), (10.0, 10.0), (20.0, 10.0)))
        self.assertTrue(coverage.segment_hits_poly(0, 0, 50, 50, sq))

    def test_collinear_but_disjoint_is_not_a_hit(self):
        self.assertFalse(coverage._segs_intersect((0, 0), (10, 0), (20, 0), (30, 0)))
        self.assertFalse(coverage._segs_intersect((0, 0), (10, 0), (5, 5), (5, 5)))

    def test_collinear_overlap_is_a_hit(self):
        self.assertTrue(coverage._segs_intersect((0, 0), (10, 0), (5, 0), (30, 0)))


class ObstaclesTest(unittest.TestCase):
    def _obs(self, azimuth, buildings):
        terrain = FlatTerrain(370.0)
        idx = coverage._build_index(buildings, coverage._CELL)
        return coverage._obstacles(azimuth, buildings, idx, terrain, 370.0, 20.8175, -140.4133)

    def test_empty(self):
        self.assertEqual(self._obs(0.0, []), [])

    def test_building_in_its_direction_only(self):
        obs = self._obs(0.0, [bldg(0.0, 100.0)])
        self.assertEqual(len(obs), 1)
        self.assertAlmostEqual(obs[0][0], 100.0, delta=coverage.RAY_STEP_M)
        self.assertGreater(obs[0][1], 0.0)  # roof pokes above the ray

    def test_side_direction_clear(self):
        self.assertEqual(self._obs(90.0, [bldg(0.0, 100.0)]), [])

    def test_sorted_by_distance(self):
        obs = self._obs(0.0, [bldg(0.0, 200.0), bldg(0.0, 50.0)])
        self.assertEqual([int(o[0]) for o in obs], [50, 200])


class MaxRangeTest(unittest.TestCase):
    def _run(self, azimuth, buildings, radio_key="lora", params=None):
        terrain = FlatTerrain(370.0)
        idx = coverage._build_index(buildings, coverage._CELL)
        return coverage._max_range_az(
            azimuth, coverage.resolve_radio(radio_key, params), buildings, idx, terrain,
            370.0, 20.8175, -140.4133)

    def test_unobstructed_reaches_radius(self):
        reach, los = self._run(0.0, [])
        self.assertGreaterEqual(reach, coverage.RADIUS_M - 1.0)
        self.assertEqual(los, coverage.RADIUS_M)  # nothing in the way

    def test_los_is_first_building(self):
        reach, los = self._run(0.0, row(5))
        self.assertAlmostEqual(los, 50.0, delta=coverage.RAY_STEP_M)

    def test_row_of_buildings_limits_range(self):
        # One house is ~20 dB; a whole street of them eats any budget. The
        # reach must come down well short of the unobstructed radius.
        reach, _ = self._run(0.0, row(40))
        self.assertLess(reach, 1000.0)

    def test_denser_grid_means_less_range(self):
        # Same number of houses, closer together: the signal crosses more of
        # them per kilometre, so it dies sooner.
        dense = [bldg(0.0, 50.0 + i * 50.0) for i in range(40)]   # 50 m lots
        sparse = [bldg(0.0, 200.0 + i * 200.0) for i in range(10)]  # 200 m lots
        r_dense, _ = self._run(0.0, dense)
        r_sparse, _ = self._run(0.0, sparse)
        self.assertLess(r_dense, r_sparse)

    def test_vhf_reaches_further_than_uhf_through_buildings(self):
        # Same 10 W and sensitivity; the only difference is the wavelength,
        # and VHF diffracts around rooftops better.
        r_vhf, _ = self._run(0.0, row(40), "fm", {"fm_mhz": 146.0})
        r_uhf, _ = self._run(0.0, row(40), "fm", {"fm_mhz": 440.0})
        self.assertGreater(r_vhf, r_uhf)


class ComputeTest(unittest.TestCase):
    def test_bad_radio_reports_error(self):
        self.assertIn("error", coverage.compute("20.8175", "-140.4133", "bogus"))

    def test_bad_coordinate_reports_error(self):
        self.assertIn("error", coverage.compute("not-a-number", "-140.4133", "lora"))

    def test_tile_key(self):
        # A point at -140.5133 falls in the tile whose SW corner is 141 W.
        self.assertEqual(coverage.Terrain._tile_key(20.5, -140.1), "N20W141")
        self.assertEqual(coverage.Terrain._tile_key(20.5, -139.9), "N20W140")
        self.assertEqual(coverage.Terrain._tile_key(-33.5, 112.1), "S34E112")


class BundledDataTest(unittest.TestCase):
    """The pre-built bundle path: real Overture heights, read offline."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = coverage._BUNDLED_DIR

    def tearDown(self):
        coverage._BUNDLED_DIR = self._old_dir
        self._tmp.cleanup()

    def _write(self, name, clat, clon, radius, buildings, roads):
        path = os.path.join(self._tmp.name, name)
        with open(path, "w") as f:
            json.dump({"center_lat": clat, "center_lon": clon, "radius_m": radius,
                       "buildings": buildings, "roads": roads}, f)

    def test_returns_covering_bundle(self):
        coverage.set_bundled_dir(self._tmp.name)
        bldg = {"poly": [[20.8175, -140.4133], [20.8185, -140.4133], [20.8185, -140.4143]], "h": 7.5}
        self._write("a.json", 20.8175, -140.4133, 2000, [bldg], [[[20.8175, -140.4133], [20.8275, -140.4233]]])
        b, r = coverage.load_bundled(20.8275, -140.4233)
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["h"], 7.5)   # the real height is preserved
        self.assertEqual(len(r), 1)

    def test_ignores_non_covering_bundle(self):
        coverage.set_bundled_dir(self._tmp.name)
        self._write("a.json", 20.8175, -140.4133, 1000, [], [])
        b, r = coverage.load_bundled(21.8175, -139.0)
        self.assertIsNone(b)

    def test_none_when_no_dir_set(self):
        coverage.set_bundled_dir(None)
        self.assertEqual(coverage.load_bundled(20.8175, -140.4133), (None, None))

    def test_covers_uses_center_and_radius(self):
        bundle = {"center_lat": 20.8175, "center_lon": -140.4133, "radius_m": 2000}
        self.assertTrue(coverage._covers(bundle, 20.8275, -140.4233))
        self.assertFalse(coverage._covers(bundle, 20.9175, -140.5133))


class SplitBuildingTest(unittest.TestCase):
    """Overture tiles cut a building at tile edges into pieces sharing one id;
    it must cost ONE knife edge, not one per piece."""

    def test_pieces_with_one_id_count_once(self):
        a = bldg(0.0, 100.0)
        b = {"poly": square(0.0, 116.0), "h": 5.0, "gelev": 370.0}
        a["id"] = b["id"] = "same"
        idx = coverage._build_index([a, b], coverage._CELL)
        obs = coverage._building_obstacles(0.0, [a, b], idx, 370.0, coverage.RADIUS_M)
        self.assertEqual(len(obs), 1)

    def test_buildings_without_id_still_count_separately(self):
        bs = [bldg(0.0, 100.0), bldg(0.0, 116.0)]
        idx = coverage._build_index(bs, coverage._CELL)
        self.assertEqual(len(coverage._building_obstacles(0.0, bs, idx, 370.0,
                                                          coverage.RADIUS_M)), 2)

    def test_the_nearest_piece_is_charged_whatever_the_order(self):
        # The distance charged decides where the knife edge sits, so it must
        # not depend on which piece the index happens to yield first.
        near = bldg(0.0, 100.0)
        far = {"poly": square(0.0, 116.0), "h": 5.0, "gelev": 370.0}
        near["id"] = far["id"] = "same"
        for bs in ([near, far], [far, near]):
            idx = coverage._build_index(bs, coverage._CELL)
            obs = coverage._building_obstacles(0.0, bs, idx, 370.0, coverage.RADIUS_M)
            self.assertEqual(len(obs), 1)
            self.assertAlmostEqual(obs[0][0], 100.0)


def _prefix_walk_obstacles(az_deg, buildings, index, tx_elev, max_d):
    """The pre-2026-10-20 _building_obstacles, kept as the reference the fast
    one must agree with: re-test the growing prefix every RAY_STEP_M, with
    candidates from cells sampled every half-cell along it."""
    th = math.radians(az_deg)
    out, seen = [], set()
    d = coverage.RAY_STEP_M
    while d <= max_d:
        bx, by = d * math.sin(th), d * math.cos(th)
        cands = set()
        n = max(1, int(math.hypot(bx, by) // (coverage._CELL * 0.5)))
        for i in range(n + 1):
            t = i / n
            cands.update(index.get((int(bx * t // coverage._CELL),
                                    int(by * t // coverage._CELL)), ()))
        for bi in cands:
            b = buildings[bi]
            if bi in seen:
                continue
            if coverage.segment_hits_poly(0.0, 0.0, bx, by, b["poly"]):
                seen.add(bi)
                excess = (b["gelev"] + b["h"]) - (tx_elev + coverage.ANT_HEIGHT_M)
                if excess > 0.0:
                    out.append((b["cdist"], excess))
        d += coverage.RAY_STEP_M
    out.sort()
    return out


class SingleTestPerBuildingTest(unittest.TestCase):
    """_building_obstacles tests each building once against the whole ray
    instead of re-testing every 20 m prefix (~20x faster on real data). It
    must find the same buildings."""

    def test_agrees_with_the_prefix_walk_on_a_random_town(self):
        rnd = random.Random(7)
        bs = [bldg(rnd.uniform(-1900, 1900), rnd.uniform(-1900, 1900),
                   h=rnd.uniform(3, 12)) for _ in range(1500)]
        idx = coverage._build_index(bs, coverage._CELL)
        azs = [i * 5.0 for i in range(72)] + [rnd.uniform(0, 360) for _ in range(40)]
        for az in azs:
            self.assertEqual(
                coverage._building_obstacles(az, bs, idx, 370.0, coverage.RADIUS_M),
                _prefix_walk_obstacles(az, bs, idx, 370.0, coverage.RADIUS_M),
                "azimuth %.2f" % az)

    def test_cell_walk_matches_dense_sampling(self):
        rnd = random.Random(3)
        cell = coverage._CELL
        for _ in range(300):
            ax, ay = rnd.uniform(-500, 500), rnd.uniform(-500, 500)
            bx, by = rnd.uniform(-2000, 2000), rnd.uniform(-2000, 2000)
            walked = set(coverage._cells_on_segment(ax, ay, bx, by, cell))
            n = 20000
            sampled = {(int((ax + (bx - ax) * i / n) // cell),
                        int((ay + (by - ay) * i / n) // cell)) for i in range(n + 1)}
            self.assertTrue(sampled <= walked, (ax, ay, bx, by, sampled - walked))
            # And it walks, it does not flood: corners add at most 2 a step.
            self.assertLessEqual(len(walked), 3 * len(sampled))

    def test_a_45_degree_ray_keeps_the_cells_beside_each_corner(self):
        # From the origin (itself a cell corner) at 45 degrees the ray goes
        # through a corner every cell. A building sitting only in a side
        # cell, touching the ray at that corner, must still be a candidate.
        # (Whether it then counts as CROSSED is float noise -- a 45-degree
        # ray misses an exact corner by ~1e-13 m -- so only the candidacy,
        # which this function owns, is asserted.)
        cells = set(coverage._cells_on_segment(0.0, 0.0, 1000.0, 1000.0, 100.0))
        for k in range(1, 9):
            self.assertIn((k, k), cells)
            self.assertIn((k, k - 1), cells)
            self.assertIn((k - 1, k), cells)

    def test_axis_aligned_and_degenerate_rays(self):
        self.assertEqual(coverage._cells_on_segment(0.0, 0.0, 0.0, 350.0, 100.0),
                         [(0, 0), (0, 1), (0, 2), (0, 3)])
        self.assertEqual(coverage._cells_on_segment(0.0, 0.0, -250.0, 0.0, 100.0),
                         [(0, 0), (-1, 0), (-2, 0), (-3, 0)])
        self.assertEqual(coverage._cells_on_segment(5.0, 5.0, 5.0, 5.0, 100.0), [(0, 0)])


# A valid 2 x 2 SRTM tile: four big-endian int16 samples, gzipped.
TINY_HGT_GZ = gzip.compress(b"\x01\x00" * 4)


class FakeTerrainDownload:
    """Stands in for Terrain._download: counts calls, never touches the
    network, and leaves the in-memory cache empty on the way in and out."""

    def __enter__(self):
        self.calls = []
        self._old = coverage.Terrain._download
        self._old_cache = dict(coverage.Terrain._data_cache)
        coverage.Terrain._data_cache.clear()
        coverage.Terrain._download = staticmethod(
            lambda key: (self.calls.append(key), TINY_HGT_GZ)[1])
        return self

    def __exit__(self, *exc):
        coverage.Terrain._download = self._old
        coverage.Terrain._data_cache.clear()
        coverage.Terrain._data_cache.update(self._old_cache)


class TerrainDiskCacheTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = coverage._BUNDLED_DIR
        coverage.set_bundled_dir(self._tmp.name)

    def tearDown(self):
        coverage._BUNDLED_DIR = self._old_dir
        self._tmp.cleanup()

    def test_second_session_reads_disk_not_network(self):
        with FakeTerrainDownload() as fake:
            coverage.Terrain._load("N20W141")
            coverage.Terrain._data_cache.clear()      # an app restart
            tile = coverage.Terrain._load("N20W141")
        self.assertEqual(fake.calls, ["N20W141"])
        self.assertEqual(tile["n"], 2)
        self.assertTrue(os.path.exists(os.path.join(
            coverage.terrain_dir(), "N20W141.hgt.gz")))

    def test_corrupt_cache_file_is_replaced(self):
        os.makedirs(coverage.terrain_dir())
        path = os.path.join(coverage.terrain_dir(), "N20W141.hgt.gz")
        with open(path, "wb") as f:
            f.write(b"not gzip")
        with FakeTerrainDownload() as fake:
            tile = coverage.Terrain._load("N20W141")
        self.assertEqual(fake.calls, ["N20W141"])
        self.assertEqual(tile["n"], 2)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), TINY_HGT_GZ)

    def test_bad_download_is_not_cached(self):
        with FakeTerrainDownload():
            coverage.Terrain._download = staticmethod(lambda key: gzip.compress(b"abc"))
            with self.assertRaises(ValueError):
                coverage.Terrain._load("N20W141")
        self.assertFalse(os.path.exists(os.path.join(
            coverage.terrain_dir(), "N20W141.hgt.gz")))

    def test_no_storage_dir_still_works(self):
        coverage.set_bundled_dir(None)
        with FakeTerrainDownload() as fake:
            self.assertEqual(coverage.Terrain._load("N20W141")["n"], 2)
        self.assertEqual(fake.calls, ["N20W141"])

    def test_keys_for_box(self):
        self.assertEqual(coverage.terrain_keys_for_box(20.5, -140.2, 20.7, -140.1),
                         ["N20W141"])
        self.assertEqual(sorted(coverage.terrain_keys_for_box(20.9, -140.1, 21.1, -139.9)),
                         ["N20W140", "N20W141", "N21W140", "N21W141"])


class AreaBundleTest(unittest.TestCase):
    """Areas downloaded on the phone: bbox bundles, chosen and managed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = coverage._BUNDLED_DIR
        self._old_fetch = coverage.overture_tiles.fetch_area
        coverage.set_bundled_dir(self._tmp.name)
        self._terrain = FakeTerrainDownload().__enter__()

    def tearDown(self):
        self._terrain.__exit__(None, None, None)
        coverage._BUNDLED_DIR = self._old_dir
        coverage.overture_tiles.fetch_area = self._old_fetch
        coverage._bundle_cache.clear()
        self._tmp.cleanup()

    def _write(self, name, s, w, n, e, nb=0):
        with open(os.path.join(self._tmp.name, name), "w") as f:
            json.dump({"south": s, "west": w, "north": n, "east": e, "source": name,
                       "buildings": [{"poly": [[s, w], [s, e], [n, e]], "h": 5.0}] * nb,
                       "roads": []}, f)

    def test_bbox_covers_point_and_disc(self):
        bundle = {"south": 20.8175, "west": -140.5133, "north": 20.9175, "east": -140.4133}
        self.assertTrue(coverage._covers(bundle, 20.8675, -140.4633))
        self.assertTrue(coverage._covers(bundle, 20.8675, -140.4633, coverage.RADIUS_M))
        # 1 km inside the south edge: holds the point, not the 2 km disc.
        self.assertTrue(coverage._covers(bundle, 20.8265, -140.4633))
        self.assertFalse(coverage._covers(bundle, 20.8265, -140.4633, coverage.RADIUS_M))

    def test_complete_bundle_preferred_over_partial(self):
        # "a" (read first) only just holds the point; "b" holds the disc.
        self._write("a.json", 20.8175, -140.5133, 20.8775, -140.4133)
        self._write("b.json", 20.7175, -140.6133, 21.0175, -140.3133)
        bundle, complete = coverage.find_bundle(20.8725, -140.4633)
        self.assertTrue(complete)
        self.assertEqual(bundle["source"], "b.json")

    def test_partial_bundle_reported_as_partial(self):
        self._write("a.json", 20.8175, -140.5133, 20.8775, -140.4133)
        bundle, complete = coverage.find_bundle(20.8725, -140.4633)
        self.assertIsNotNone(bundle)
        self.assertFalse(complete)

    def test_download_writes_bundle_and_drops_stale_results(self):
        got = {}

        def fake_fetch(s, w, n, e, progress=None):
            got["box"] = (s, w, n, e)
            if progress:
                progress(1, 1)
            return [{"poly": [[20.8675, -140.4633], [20.8685, -140.4633], [20.8685, -140.4643]],
                     "h": 8.0, "id": "x"}], [], "2026-test"

        coverage.overture_tiles.fetch_area = fake_fetch
        coverage._coverage_cache.put((1, 2, "lora"), {"stale": True})
        coverage._buildings_cache.put((1, 2), "stale")
        coverage._peer_cache.put((1, 2, "lora"), {"stale": True})
        r = json.loads(coverage.download_area("20.8575", "-140.4733", "20.8775", "-140.4533"))
        self.assertNotIn("error", r)
        self.assertEqual(r["buildings"], 1)
        # Terrain came down with the area, so it works offline.
        self.assertEqual(self._terrain.calls, ["N20W141"])
        self.assertTrue(os.path.exists(os.path.join(coverage.terrain_dir(), "N20W141.hgt.gz")))
        self.assertIsNone(coverage._coverage_cache.get((1, 2, "lora")))
        self.assertIsNone(coverage._buildings_cache.get((1, 2)))
        self.assertIsNone(coverage._peer_cache.get((1, 2, "lora")))
        # The download is the box plus RADIUS_M all round, so a point at the
        # box's corner still has its whole disc.
        s, w, n, e = got["box"]
        self.assertAlmostEqual((20.8575 - s) * 110540.0, coverage.RADIUS_M + 10.0, delta=1.0)
        bundle, complete = coverage.find_bundle(20.8575, -140.4733)
        self.assertTrue(complete)
        self.assertEqual(bundle["buildings"][0]["id"], "x")
        self.assertFalse(json.loads(coverage.download_progress())["active"])
        areas = json.loads(coverage.list_areas())
        self.assertEqual(len(areas), 1)
        self.assertAlmostEqual(areas[0]["south"], 20.8575)   # the box drawn, not the margin
        self.assertEqual(json.loads(coverage.delete_area(areas[0]["file"]))["deleted"],
                         areas[0]["file"])
        self.assertEqual(json.loads(coverage.list_areas()), [])

    def test_download_failure_is_a_reason_not_a_raise(self):
        def boom(*a, **k):
            raise ValueError("area needs 99 map tiles")
        coverage.overture_tiles.fetch_area = boom
        r = json.loads(coverage.download_area("20.8575", "-140.4733", "20.8775", "-140.4533"))
        self.assertIn("99 map tiles", r["error"])
        self.assertFalse(json.loads(coverage.download_progress())["active"])
        self.assertEqual(os.listdir(self._tmp.name), [])
        self.assertEqual(self._terrain.calls, [])

    def test_delete_refuses_paths(self):
        for bad in ("../x.json", "/etc/passwd", "sub/x.json", "x.txt"):
            self.assertIn("error", json.loads(coverage.delete_area(bad)), bad)

    def test_tile_count_reports_limit(self):
        r = json.loads(coverage.area_tile_count("20.9075", "-140.5833", "20.9175", "-140.5733"))
        self.assertLessEqual(r["tiles"], r["limit"])
        big = json.loads(coverage.area_tile_count("20.3175", "-141.4133", "21.3175", "-140.4133"))
        self.assertGreater(big["tiles"], big["limit"])


class DefaultSourceTest(unittest.TestCase):
    """With no saved area, Overture is fetched (and saved); Overpass only
    when Overture fails."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = coverage._BUNDLED_DIR
        self._old = (coverage.overture_tiles.fetch_area, coverage.fetch_data,
                     coverage.Terrain)
        coverage.set_bundled_dir(self._tmp.name)
        coverage._coverage_cache.clear()
        coverage._buildings_cache.clear()
        self.overpass_calls = 0
        self.overture_calls = 0

        def overture(s, w, n, e, progress=None):
            self.overture_calls += 1
            return [], [], "2026-test"

        def overpass(lat, lon, r):
            self.overpass_calls += 1
            return [], [], []

        coverage.overture_tiles.fetch_area = overture
        coverage.fetch_data = overpass
        coverage.Terrain = type("T", (FlatTerrain,), {
            "__init__": lambda self, *a: FlatTerrain.__init__(self, 370.0),
            "_load": classmethod(lambda cls, key: None),
            "_tile_key": staticmethod(coverage.Terrain._tile_key)})

    def tearDown(self):
        (coverage.overture_tiles.fetch_area, coverage.fetch_data,
         coverage.Terrain) = self._old
        coverage._BUNDLED_DIR = self._old_dir
        coverage._coverage_cache.clear()
        coverage._buildings_cache.clear()
        coverage._bundle_cache.clear()
        self._tmp.cleanup()

    def test_overture_is_default_and_saved(self):
        r = json.loads(coverage.compute("20.9120", "-140.5823", "fm"))
        self.assertNotIn("error", r)
        self.assertEqual(r["data_source"], "overture 2026-test (auto)")
        self.assertTrue(r["data_complete"])
        self.assertEqual((self.overture_calls, self.overpass_calls), (1, 0))
        # Saved: a fresh session (caches cleared) at a nearby point reads
        # the auto area instead of downloading again.
        coverage._coverage_cache.clear()
        coverage._buildings_cache.clear()
        r = json.loads(coverage.compute("20.9145", "-140.5823", "lora"))
        self.assertEqual(r["data_source"], "overture 2026-test (auto)")
        self.assertEqual(self.overture_calls, 1)

    def test_overpass_when_overture_fails(self):
        def boom(*a, **k):
            raise OSError("no route")
        coverage.overture_tiles.fetch_area = boom
        r = json.loads(coverage.compute("20.9120", "-140.5823", "fm"))
        self.assertNotIn("error", r)
        self.assertEqual(self.overpass_calls, 1)
        self.assertIn("Overture failed", r["data_source"])
        self.assertFalse(json.loads(coverage.download_progress())["active"])


class AreaReplacementTest(unittest.TestCase):
    """A new area replaces the old one of its kind; a failed one does not."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = coverage._BUNDLED_DIR
        self._old_fetch = coverage.overture_tiles.fetch_area
        coverage.set_bundled_dir(self._tmp.name)
        self._terrain = FakeTerrainDownload().__enter__()
        line = [[20.8675, -140.4633], [20.8685, -140.4633]]
        coverage.overture_tiles.fetch_area = \
            lambda *a, **k: ([], [(line, "West Sierra Street")], "2026-test")

    def tearDown(self):
        self._terrain.__exit__(None, None, None)
        coverage._BUNDLED_DIR = self._old_dir
        coverage.overture_tiles.fetch_area = self._old_fetch
        coverage._bundle_cache.clear()
        self._tmp.cleanup()

    def _files(self, prefix):
        return sorted(n for n in os.listdir(self._tmp.name) if n.startswith(prefix))

    def test_new_selection_replaces_old(self):
        coverage.download_area("20.8575", "-140.4733", "20.8775", "-140.4533")
        first = self._files("overture_")
        coverage.download_area("20.9175", "-140.5133", "20.9375", "-140.4933")
        second = self._files("overture_")
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first, second)

    def test_selection_and_auto_kept_separately(self):
        coverage.download_area("20.8575", "-140.4733", "20.8775", "-140.4533")
        coverage._auto_overture(21.0175, -140.6133)
        coverage._auto_overture(21.1175, -140.7133)
        self.assertEqual(len(self._files("overture_")), 1)
        self.assertEqual(len(self._files("auto_")), 1)
        # The NEWER automatic area survives.
        newest_south = coverage._disc_bbox(21.1175, -140.7133,
                                           coverage.RADIUS_M + coverage.AUTO_MARGIN_M)[0]
        self.assertTrue(self._files("auto_")[0].startswith("auto_%.4f_" % newest_south))

    def test_failed_download_keeps_the_old_area(self):
        coverage.download_area("20.8575", "-140.4733", "20.8775", "-140.4533")
        before = self._files("overture_")

        def boom(*a, **k):
            raise OSError("no route")
        coverage.overture_tiles.fetch_area = boom
        r = json.loads(coverage.download_area("20.9175", "-140.5133", "20.9375", "-140.4933"))
        self.assertIn("error", r)
        self.assertEqual(self._files("overture_"), before)

    def test_auto_area_without_names_is_refetched(self):
        with open(os.path.join(self._tmp.name, "auto_old.json"), "w") as f:
            json.dump({"south": 20.3175, "west": -141.4133, "north": 21.3175, "east": -140.4133,
                       "buildings": [], "roads": []}, f)
        self.assertIsNone(coverage.find_bundle(20.8175, -140.9133)[0])
        # A user-selected area from before names is still used as it is.
        os.rename(os.path.join(self._tmp.name, "auto_old.json"),
                  os.path.join(self._tmp.name, "overture_old.json"))
        self.assertIsNotNone(coverage.find_bundle(20.8175, -140.9133)[0])

    def test_list_areas_says_what_kind_each_area_is(self):
        coverage.download_area("20.8575", "-140.4733", "20.8775", "-140.4533")
        coverage._auto_overture(21.0175, -140.6133)
        coverage._auto_overture(21.1175, -140.7133, "peer", 3, False)
        with open(os.path.join(self._tmp.name, "coverage_desktop.json"), "w") as f:
            json.dump({"center_lat": 20.8175, "center_lon": -140.4133, "radius_m": 2000,
                       "buildings": [], "roads": []}, f)
        kinds = sorted(a["kind"] for a in json.loads(coverage.list_areas()))
        self.assertEqual(kinds, ["auto", "bundle", "overture", "peer"])

    def test_names_saved_with_roads(self):
        coverage.download_area("20.8575", "-140.4733", "20.8775", "-140.4533")
        bundle, _ = coverage.find_bundle(20.8675, -140.4633)
        self.assertEqual(bundle["road_names"], ["West Sierra Street"])
        self.assertEqual(len(bundle["road_names"]), len(bundle["roads"]))


class StreetNameTest(unittest.TestCase):
    def test_abbreviation(self):
        self.assertEqual(coverage.short_street_name("West Sierra Street"), "W Sierra St")
        self.assertEqual(coverage.short_street_name("North Example Avenue"), "N Example Ave")
        self.assertEqual(coverage.short_street_name("Loop 101"), "Loop 101")
        self.assertEqual(coverage.short_street_name(""), "")
        self.assertEqual(coverage.short_street_name(None), "")

    def test_names_reach_the_map_aligned(self):
        tmp = tempfile.TemporaryDirectory()
        old_dir, old_terrain = coverage._BUNDLED_DIR, coverage.Terrain
        try:
            coverage.set_bundled_dir(tmp.name)
            near = [[20.9121, -140.5823], [20.9125, -140.5823]]
            far = [[21.0175, -140.7133], [21.0275, -140.7133]]     # beyond the disc: dropped
            with open(os.path.join(tmp.name, "a.json"), "w") as f:
                json.dump({"south": 20.8175, "west": -140.7133, "north": 21.0175, "east": -140.4133,
                           "buildings": [], "roads": [far, near],
                           "road_names": ["Far Road", "North Example Avenue"]}, f)
            coverage.Terrain = type("T", (FlatTerrain,), {
                "__init__": lambda self, *a: FlatTerrain.__init__(self, 370.0)})
            coverage._coverage_cache.clear()
            coverage._buildings_cache.clear()
            r = json.loads(coverage.compute("20.9120", "-140.5823", "lora"))
            self.assertEqual(r["road_names"], ["N Example Ave"])
            self.assertEqual(len(r["roads"]), 1)
        finally:
            coverage._BUNDLED_DIR, coverage.Terrain = old_dir, old_terrain
            coverage._coverage_cache.clear()
            coverage._buildings_cache.clear()
            coverage._bundle_cache.clear()
            tmp.cleanup()

    def test_old_bundle_without_names(self):
        tmp = tempfile.TemporaryDirectory()
        old_dir, old_terrain = coverage._BUNDLED_DIR, coverage.Terrain
        try:
            coverage.set_bundled_dir(tmp.name)
            with open(os.path.join(tmp.name, "a.json"), "w") as f:
                json.dump({"center_lat": 20.9120, "center_lon": -140.5823, "radius_m": 5000,
                           "buildings": [], "roads": [[[20.9121, -140.5823], [20.9125, -140.5823]]]}, f)
            coverage.Terrain = type("T", (FlatTerrain,), {
                "__init__": lambda self, *a: FlatTerrain.__init__(self, 370.0)})
            coverage._coverage_cache.clear()
            coverage._buildings_cache.clear()
            r = json.loads(coverage.compute("20.9120", "-140.5823", "lora"))
            self.assertEqual(r["road_names"], [""])
        finally:
            coverage._BUNDLED_DIR, coverage.Terrain = old_dir, old_terrain
            coverage._coverage_cache.clear()
            coverage._buildings_cache.clear()
            coverage._bundle_cache.clear()
            tmp.cleanup()


class MergeStreetLinesTest(unittest.TestCase):
    def test_pieces_chain_whatever_their_direction(self):
        # One street in three pieces, the middle one stored backwards and
        # listed out of order.
        a = [[0, 0], [100, 0]]
        b = [[250, 0], [100, 0]]
        c = [[250, 0], [400, 0]]
        out = coverage.merge_street_lines([c, a, b], ["Main St"] * 3, 2.0)
        self.assertEqual(len(out), 1)
        name, line = out[0]
        self.assertEqual(name, "Main St")
        xs = [p[0] for p in line]
        self.assertIn(xs, (sorted(xs), sorted(xs, reverse=True)))   # one continuous run
        self.assertEqual({xs[0], xs[-1]}, {0, 400})
        self.assertEqual(len(line), 4)            # shared ends not repeated

    def test_different_names_and_gaps_stay_apart(self):
        out = coverage.merge_street_lines(
            [[[0, 0], [100, 0]], [[100, 0], [200, 0]], [[300, 0], [400, 0]]],
            ["A St", "B St", "A St"], 2.0)
        self.assertEqual(sorted(n for n, _ in out), ["A St", "A St", "B St"])

    def test_unnamed_dropped(self):
        self.assertEqual(coverage.merge_street_lines([[[0, 0], [1, 1]]], [""], 2.0), [])


class PartialAreaTest(unittest.TestCase):
    """A saved area that only partly covers the disc triggers a complete
    automatic fetch; it is used only if that fetch fails."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = (coverage._BUNDLED_DIR, coverage.overture_tiles.fetch_area,
                     coverage.fetch_data, coverage.Terrain)
        coverage.set_bundled_dir(self._tmp.name)
        for c in (coverage._coverage_cache, coverage._buildings_cache, coverage._bundle_cache):
            c.clear()
        # Holds the point, ends 300 m north of it: partial.
        with open(os.path.join(self._tmp.name, "overture_old.json"), "w") as f:
            json.dump({"south": 20.8675, "west": -140.6133, "north": 20.9147, "east": -140.5133,
                       "source": "old selection", "buildings": [], "roads": []}, f)
        coverage.Terrain = type("T", (FlatTerrain,), {
            "__init__": lambda self, *a: FlatTerrain.__init__(self, 370.0),
            "_load": classmethod(lambda cls, key: None),
            "_tile_key": staticmethod(self._old[3]._tile_key)})
        coverage.fetch_data = lambda *a: self.fail("Overpass must not be asked")

    def tearDown(self):
        (coverage._BUNDLED_DIR, coverage.overture_tiles.fetch_area,
         coverage.fetch_data, coverage.Terrain) = self._old
        for c in (coverage._coverage_cache, coverage._buildings_cache, coverage._bundle_cache):
            c.clear()
        self._tmp.cleanup()

    def test_partial_area_is_replaced_by_a_complete_fetch(self):
        coverage.overture_tiles.fetch_area = lambda *a, **k: ([], [], "2026-test")
        r = json.loads(coverage.compute("20.9120", "-140.5823", "lora"))
        self.assertEqual(r["data_source"], "overture 2026-test (auto)")
        self.assertTrue(r["data_complete"])

    def test_partial_area_used_when_fetch_fails(self):
        def boom(*a, **k):
            raise OSError("offline")
        coverage.overture_tiles.fetch_area = boom
        r = json.loads(coverage.compute("20.9120", "-140.5823", "lora"))
        self.assertEqual(r["data_source"], "old selection")
        self.assertFalse(r["data_complete"])


if __name__ == "__main__":
    unittest.main()


class PeerCoverageTest(unittest.TestCase):
    """compute_peer(): another node's footprint, drawn from its reported
    position. Its downloads must never displace the operator's own areas or
    show in the operator's progress line, and are rate-limited."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = (coverage._BUNDLED_DIR, coverage.overture_tiles.fetch_area,
                     coverage.fetch_data, coverage.Terrain, coverage._last_peer_fetch[0])
        coverage.set_bundled_dir(self._tmp.name)
        for c in (coverage._coverage_cache, coverage._buildings_cache,
                  coverage._bundle_cache, coverage._peer_cache):
            c.clear()
        coverage._last_peer_fetch[0] = None
        coverage.Terrain = type("T", (FlatTerrain,), {
            "__init__": lambda self, *a: FlatTerrain.__init__(self, 370.0),
            "_load": classmethod(lambda cls, key: None),
            "_tile_key": staticmethod(self._old[3]._tile_key)})
        coverage.fetch_data = lambda *a: self.fail("Overpass must not be asked for a peer")
        self.fetches = 0
        line = [[20.8675, -140.4633], [20.8685, -140.4633]]

        def fetch(*a, **k):
            self.fetches += 1
            return [], [(line, "West Sierra Street")], "2026-test"
        coverage.overture_tiles.fetch_area = fetch

    def tearDown(self):
        (coverage._BUNDLED_DIR, coverage.overture_tiles.fetch_area,
         coverage.fetch_data, coverage.Terrain, coverage._last_peer_fetch[0]) = self._old
        for c in (coverage._coverage_cache, coverage._buildings_cache,
                  coverage._bundle_cache, coverage._peer_cache):
            c.clear()
        self._tmp.cleanup()

    def _files(self, prefix):
        return sorted(n for n in os.listdir(self._tmp.name) if n.startswith(prefix))

    def _peer(self, lat, lon):
        return json.loads(coverage.compute_peer(str(lat), str(lon), "lora"))

    def test_result_is_coarse_and_carries_no_map_geometry(self):
        r = self._peer(20.8675, -140.4633)
        self.assertNotIn("error", r)
        self.assertEqual(len(r["coverage"]), 36)
        self.assertEqual([c["az"] for c in r["coverage"][:3]], [0.0, 10.0, 20.0])
        for k in ("roads", "road_names", "street_labels", "buildings"):
            self.assertNotIn(k, r)
        self.assertTrue(r["data_complete"])

    def test_own_map_is_unchanged_at_full_detail(self):
        r = json.loads(coverage.compute("20.8675", "-140.4633", "lora"))
        self.assertEqual(len(r["coverage"]), 72)
        self.assertIn("buildings", r)

    def test_a_peer_download_does_not_replace_the_operators_areas(self):
        coverage._auto_overture(20.7175, -140.8133)
        coverage.download_area("20.6175", "-140.7133", "20.6275", "-140.7033")
        own = self._files("auto_") + self._files("overture_")
        self._peer(20.8675, -140.4633)
        self.assertEqual(self._files("auto_") + self._files("overture_"), own)
        self.assertEqual(len(self._files("peer_")), 1)

    def test_only_the_newest_peer_areas_are_kept(self):
        made = []
        for i in range(coverage.PEER_AREAS_KEPT + 2):
            coverage._last_peer_fetch[0] = None        # lift the rate limit
            self._peer(20.3175 + i * 0.1, -140.4633)
            new = [n for n in self._files("peer_") if n not in made]
            made += new
            path = os.path.join(self._tmp.name, new[0])
            os.utime(path, (1000 + i, 1000 + i))       # deterministic order
        self.assertEqual(self._files("peer_"), sorted(made[-coverage.PEER_AREAS_KEPT:]))

    def test_peer_downloads_never_show_in_the_operators_progress(self):
        coverage._set_progress(active=True, done=3, total=9, stage="Downloading map tiles")
        try:
            self._peer(20.8675, -140.4633)
            p = json.loads(coverage.download_progress())
            self.assertEqual((p["done"], p["total"], p["stage"]),
                             (3, 9, "Downloading map tiles"))
            self.assertTrue(p["active"])
        finally:
            coverage._set_progress(active=False)

    def test_downloads_are_rate_limited_across_peers(self):
        self.assertNotIn("error", self._peer(20.8675, -140.4633))
        r = self._peer(21.2675, -140.8633)                 # elsewhere, right away
        self.assertTrue(r.get("pending"), r)
        self.assertEqual(self.fetches, 1)
        # Inside the area already fetched is no download, so not limited.
        self.assertNotIn("error", self._peer(20.8685, -140.4643))
        self.assertEqual(self.fetches, 1)

    def test_a_failed_download_is_charged_too(self):
        def boom(*a, **k):
            self.fetches += 1
            raise OSError("no route")
        coverage.overture_tiles.fetch_area = boom
        self.assertIn("error", self._peer(20.8675, -140.4633))
        self.assertTrue(self._peer(20.8675, -140.4633).get("pending"))
        self.assertEqual(self.fetches, 1)

    def test_partial_area_used_while_rate_limited_and_not_cached(self):
        # Holds the point, ends 300 m north of it: partial.
        with open(os.path.join(self._tmp.name, "overture_old.json"), "w") as f:
            json.dump({"south": 20.8675, "west": -140.6133, "north": 20.9147, "east": -140.5133,
                       "source": "old selection", "buildings": [], "roads": []}, f)
        coverage._note_peer_fetch()
        r = self._peer(20.9120, -140.5633)
        self.assertFalse(r["data_complete"])
        self.assertEqual(self.fetches, 0)
        # Once allowed, the whole disc is fetched rather than the partial
        # answer being served from a cache.
        coverage._last_peer_fetch[0] = None
        self.assertTrue(self._peer(20.9120, -140.5633)["data_complete"])
        self.assertEqual(self.fetches, 1)


class TransmitterHeightTest(unittest.TestCase):
    """The transmit antenna's height is the operator's to set; the receiver
    stays a handheld at ANT_HEIGHT_M. At ANT_HEIGHT_M the model must be
    exactly what it was before heights could be set."""

    def _run(self, buildings, h_t, azimuth=0.0, radio_key="lora"):
        idx = coverage._build_index(buildings, coverage._CELL)
        return coverage._max_range_az(
            azimuth, coverage.RADIOS[radio_key], buildings, idx, FlatTerrain(370.0),
            370.0, 20.8175, -140.4133, h_t, coverage.ANT_HEIGHT_M)

    def test_the_default_height_is_the_old_model_exactly(self):
        rnd = random.Random(11)
        bs = [bldg(rnd.uniform(-1900, 1900), rnd.uniform(-1900, 1900),
                   h=rnd.uniform(3, 12)) for _ in range(1500)]
        idx = coverage._build_index(bs, coverage._CELL)
        for az in [i * 15.0 for i in range(24)]:
            self.assertEqual(
                self._run(bs, coverage.ANT_HEIGHT_M, az),
                coverage._max_range_az(az, coverage.RADIOS["lora"], bs, idx,
                                       FlatTerrain(370.0), 370.0, 20.8175, -140.4133),
                "azimuth %g" % az)

    def test_a_higher_antenna_reaches_further_over_a_street(self):
        street = row(40)                        # 5 m houses every 50 m
        r = [self._run(street, h)[0] for h in (1.5, 4.0, 10.0, 25.0)]
        self.assertEqual(r, sorted(r))
        self.assertLess(r[0], 1000.0)
        self.assertGreater(r[-1], 2 * r[0])

    def test_a_roof_below_a_tall_antenna_still_blocks_a_receiver_behind_it(self):
        # A 10 m block at 100 m, antenna at 20 m: the roof is 10 m BELOW the
        # antenna, so a level-line model would ignore it. The line down to a
        # 1.5 m receiver just behind it passes ~5 m up, so it is in the way.
        block = [bldg(0.0, 100.0, h=10.0)]
        _, los = self._run(block, 20.0)
        self.assertAlmostEqual(los, 100.0, delta=coverage.RAY_STEP_M)
        _, los_clear = self._run([], 20.0)
        self.assertEqual(los_clear, coverage.RADIUS_M)

    def test_roofs_below_a_tall_antenna_still_cost_signal(self):
        # 10 m blocks every 50 m under a 20 m antenna: every roof is BELOW
        # the antenna, so measured from a level line none would cost
        # anything and the reach would be the whole radius. A receiver among
        # them is under the roofline, and the line down to it crosses them.
        street = [bldg(0.0, 50.0 + i * 50.0, h=10.0) for i in range(40)]
        reach, _ = self._run(street, 20.0)
        self.assertLess(reach, coverage.RADIUS_M - 1.0)
        self.assertGreater(reach, self._run(row(40), 1.5)[0])

    def test_a_low_wall_never_blocks(self):
        # Lower than both antennas: the line between them never dips to it.
        wall = [bldg(0.0, 100.0, h=1.0)]
        reach, los = self._run(wall, 20.0)
        self.assertEqual(los, coverage.RADIUS_M)
        self.assertEqual((reach, los), self._run([], 20.0))

    def test_compute_takes_the_height_and_does_not_reuse_the_other_result(self):
        tmp = tempfile.TemporaryDirectory()
        old = (coverage._BUNDLED_DIR, coverage.Terrain)
        try:
            coverage.set_bundled_dir(tmp.name)
            with open(os.path.join(tmp.name, "overture_t.json"), "w") as f:
                json.dump({"south": 20.7175, "west": -140.6133, "north": 20.9175, "east": -140.2133,
                           # A street of 5 m houses northward, 50 m apart: enough
                           # to cut the reach at 1.5 m, so a stale cached answer
                           # for the other height would show.
                           "buildings": [{"poly": [[20.8175 + d - 7e-5, -140.41338],
                                                   [20.8175 + d - 7e-5, -140.41322],
                                                   [20.8175 + d + 7e-5, -140.41322],
                                                   [20.8175 + d + 7e-5, -140.41338]],
                                          "h": 5.0}
                                         for d in [(50.0 + 50.0 * i) / 110540.0
                                                   for i in range(38)]],
                           "roads": [], "road_names": []}, f)
            coverage.Terrain = type("T", (FlatTerrain,), {
                "__init__": lambda self, *a: FlatTerrain.__init__(self, 370.0)})
            for c in (coverage._coverage_cache, coverage._buildings_cache, coverage._bundle_cache):
                c.clear()
            low = json.loads(coverage.compute("20.8175", "-140.4133", "lora"))
            high = json.loads(coverage.compute("20.8175", "-140.4133", "lora",
                                               json.dumps({"tx_height_m": 12})))
            self.assertEqual(low["tx_height_m"], 1.5)
            self.assertEqual(high["tx_height_m"], 12.0)
            self.assertEqual(high["rx_height_m"], 1.5)
            self.assertGreater(high["coverage"][0]["range"], low["coverage"][0]["range"])
        finally:
            coverage._BUNDLED_DIR, coverage.Terrain = old
            for c in (coverage._coverage_cache, coverage._buildings_cache, coverage._bundle_cache):
                c.clear()
            tmp.cleanup()

    def test_out_of_range_heights_are_refused_with_a_reason(self):
        for bad in (0.1, 400, "tall", float("nan")):
            r = json.loads(coverage.compute("20.8175", "-140.4133", "lora",
                                            json.dumps({"tx_height_m": bad})))
            self.assertIn("Transmitter height", r.get("error", ""), bad)


class RadioSettingsTest(unittest.TestCase):
    """LoRa comes from the app's radio section; FM frequencies are the
    Coverage tab's own settings."""

    def test_short_slow_sensitivity_is_the_figure_radios_carried(self):
        self.assertEqual(coverage.lora_sensitivity_dbm(8, 250000),
                         coverage.RADIOS["lora"]["sens_dbm"])

    def test_every_preset_has_a_sensitivity_and_slower_hears_further(self):
        import radio
        sens = [coverage.resolve_radio("lora", {"lora_preset": p})["sens_dbm"]
                for p in radio.preset_names()]
        # radio.PRESETS is ordered fastest to slowest.
        self.assertEqual(sens, sorted(sens, reverse=True))
        self.assertEqual(coverage.resolve_radio("lora", {"lora_preset": "Long Fast"})["sens_dbm"],
                         -131.5)

    def test_the_radio_sections_frequency_and_power_are_used(self):
        r = coverage.resolve_radio("lora", {"lora_preset": "Medium Slow",
                                            "lora_freq_mhz": "906.875", "lora_tx_dbm": "17"})
        self.assertEqual((r["freq_mhz"], r["tx_dbm"]), (906.875, 17.0))
        self.assertIn("906.875 MHz Medium Slow (SF10/250 kHz, 17 dBm)", r["label"])
        self.assertLess(coverage.link_budget_db(r),
                        coverage.link_budget_db(coverage.RADIOS["lora"]) + 6.0)

    def test_fm_frequency(self):
        self.assertEqual(coverage.resolve_radio("fm", {"fm_mhz": 151.94})["freq_mhz"], 151.94)
        self.assertEqual(coverage.resolve_radio("fm", {"fm_mhz": "462.5625"})["freq_mhz"],
                         462.5625)
        self.assertIn("462.562 MHz", coverage.resolve_radio("fm", {"fm_mhz": 462.5625})["label"])
        # LoRa's settings are not FM's.
        self.assertEqual(coverage.resolve_radio("fm", {"lora_freq_mhz": "915"})["freq_mhz"], 146.0)

    def test_one_fm_entry_and_the_band_is_read_off_the_frequency(self):
        self.assertEqual(sorted(coverage.RADIOS), ["fm", "lora"])
        for mhz, band in ((146.52, "VHF"), (299.9, "VHF"), (300, "UHF"), (446.0, "UHF")):
            self.assertTrue(coverage.resolve_radio("fm", {"fm_mhz": mhz})["label"]
                            .startswith("FM %s " % band), mhz)
        # The two entries it replaced are gone, not aliased.
        for old in ("fm_vhf", "fm_uhf"):
            self.assertIn("error", json.loads(coverage.compute("20.8175", "-140.4133", old)))

    def test_bad_settings_are_refused_with_a_reason(self):
        for key, params, why in (
                ("fm", {"fm_mhz": "14.6"}, "FM frequency"),
                ("fm", {"fm_mhz": "abc"}, "FM frequency"),
                ("lora", {"lora_preset": "Hyper Fast"}, "unknown LoRa preset"),
                ("lora", {"lora_tx_dbm": "99"}, "LoRa TX power")):
            r = json.loads(coverage.compute("20.8175", "-140.4133", key, json.dumps(params)))
            self.assertIn(why, r.get("error", ""), params)

    def test_no_settings_is_the_old_behaviour(self):
        for key in coverage.RADIOS:
            r = coverage.resolve_radio(key, None)
            for f in ("freq_mhz", "tx_dbm", "sens_dbm"):
                self.assertEqual(r[f], coverage.RADIOS[key][f])

    def test_a_peer_takes_the_radio_but_not_the_operators_height(self):
        seen = {}

        def fake(lat, lon, radio_key, **kw):
            seen.update(kw)
            return {"data_complete": False}
        orig = coverage.compute_coverage
        coverage.compute_coverage = fake
        try:
            coverage.compute_peer("20.8175", "-140.4133", "fm",
                                  json.dumps({"fm_mhz": 446.0, "tx_height_m": 30}))
        finally:
            coverage.compute_coverage = orig
        self.assertEqual(seen["radio"]["freq_mhz"], 446.0)
        self.assertNotIn("tx_height_m", seen)


class CheckSettingsTest(unittest.TestCase):
    def test_good_settings_pass(self):
        self.assertEqual(coverage.check_settings(json.dumps(
            {"fm_mhz": "462.5625", "fm_w": "4", "tx_height_m": "6"})), "")
        self.assertEqual(coverage.check_settings(""), "")

    def test_each_bad_field_is_named(self):
        for params, why in (({"fm_mhz": "1460"}, "FM frequency"),
                            ({"fm_mhz": ""}, "FM frequency"),
                            ({"tx_height_m": "0"}, "Transmitter height")):
            self.assertIn(why, coverage.check_settings(json.dumps(params)))
        self.assertTrue(coverage.check_settings("not json"))


class FmPowerTest(unittest.TestCase):
    def test_watts_become_dbm_and_ten_watts_is_the_old_figure(self):
        self.assertEqual(coverage.resolve_radio("fm", {"fm_w": "10"})["tx_dbm"],
                         coverage.RADIOS["fm"]["tx_dbm"])
        self.assertAlmostEqual(coverage.resolve_radio("fm", {"fm_w": "5"})["tx_dbm"],
                               36.99, places=2)
        self.assertIn("(4 W)", coverage.resolve_radio("fm", {"fm_w": 4})["label"])

    def test_less_power_reaches_less_far_through_a_street(self):
        # Houses 200 m apart: at 50 m the reach stops in the same house's
        # shadow at every power (the 20 m-step artefact in the coverage model).
        idx_b = [bldg(0.0, 200.0 + i * 200.0) for i in range(10)]
        idx = coverage._build_index(idx_b, coverage._CELL)

        def reach(w):
            r = coverage.resolve_radio("fm", {"fm_mhz": 440.0, "fm_w": w})
            return coverage._max_range_az(0.0, r, idx_b, idx, FlatTerrain(370.0),
                                          370.0, 20.8175, -140.4133)[0]
        # Never less reach for more power, and some over the whole range --
        # but in STEPS: each house costs ~10-20 dB, so a few dB more usually
        # buys no extra house (measured: 0.5, 1 and 4 W all stop at 800 m).
        rs = [reach(w) for w in (0.1, 0.5, 1, 4, 10, 100)]
        self.assertEqual(rs, sorted(rs))
        self.assertLess(rs[0], rs[-1])

    def test_bad_power_is_refused_and_named(self):
        for bad in ("0", "500", "lots"):
            self.assertIn("FM power", coverage.check_settings(json.dumps({"fm_w": bad})))

    def test_the_radio_in_use_is_named_in_the_lora_label(self):
        r = coverage.resolve_radio("lora", {"lora_tx_dbm": 17, "lora_source": "reported by the RNode"})
        self.assertTrue(r["label"].endswith("[reported by the RNode]"), r["label"])
