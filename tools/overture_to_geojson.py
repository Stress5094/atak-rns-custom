#!/usr/bin/env python3
"""
Build a coverage-data bundle from Overture Maps for the app's Coverage tab.

The Coverage tab predicts range from real building footprints. It normally
fetches those from OpenStreetMap's Overpass API, which is hand-mapped and
MISSES the machine-detected buildings (stadiums, big-box stores, warehouses)
that Overture conflates in from Microsoft/Google/Esri. This tool downloads
Overture's buildings -- footprints AND real LiDAR-derived heights -- for the
area you choose, plus the streets, and writes one JSON file the app can read
offline. That closes the "app says 2 km, reality says you're standing at a
stadium wall" mismatch.

Why a desktop tool and not the app itself: Overture ships GeoParquet, and
reading Parquet needs pyarrow, which the app's on-phone Python (Chaquopy)
does not have. The conversion therefore happens HERE, on a laptop, and the
plain-JSON result is bundled or sideloaded to the phone.

The one dependency is the official Overture client (desktop only, not added
to the app's build):

    uv pip install overturemaps     # or: pip install overturemaps

Usage:

    # By place name (geocoded):
    python3 tools/overture_to_geojson.py --place "a US suburb" --radius 3 \
        -o coverage_area.json

    # By centre + radius:
    python3 tools/overture_to_geojson.py --center 20.9115 -140.5823 --radius 2

    # By bounding box (west, south, east, north):
    python3 tools/overture_to_geojson.py --bbox -140.6133,20.8875,-140.5533,20.9375

Output is the app's native bundle format:

    { "center_lat", "center_lon", "radius_m",
      "buildings": [ {"poly": [[lat, lon], ...], "h": 5.2}, ... ],
      "roads":    [ [[lat, lon], ...], ... ] }

Sideload it to the phone (see docs/operations.md for where the app looks) or
drop it in the app's assets to be bundled.
"""

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request

# Overpass mirrors, tried in order -- the main one rate-limits and 5xx's.
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
_UA = "rtak-fm-coverage-builder/1.0"


def geocode(place):
    """Turn a place name into (lat, lon) via Nominatim. Raises on failure."""
    q = urllib.parse.quote(place)
    url = ("https://nominatim.openstreetmap.org/search?q=%s&format=json&limit=1" % q)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        hits = json.loads(r.read().decode("utf-8"))
    if not hits:
        raise SystemExit("no geocode result for %r" % place)
    return float(hits[0]["lat"]), float(hits[0]["lon"])


def bbox_from_center(lat, lon, radius_km):
    dlat = radius_km / 110.54
    dlon = radius_km / (111.32 * math.cos(math.radians(lat)))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def overture_buildings(bbox):
    """Buildings with real heights from Overture, as the app's native format."""
    try:
        from overturemaps import core
        from shapely import from_wkb
    except ImportError:
        raise SystemExit(
            "the 'overturemaps' package is not installed -- run "
            "'uv pip install overturemaps' first (desktop only)"
        )

    reader = core.record_batch_reader('building', bbox=bbox)
    out = []
    for batch in reader:
        h = batch.column('height').to_pylist()
        nf = batch.column('num_floors').to_pylist()
        cls = batch.column('class').to_pylist()
        geom = batch.column('geometry').to_pylist()
        for i in range(len(h)):
            ht = h[i]
            if ht is None:
                ht = nf[i] * 3.0 if nf[i] else _default_height(cls[i])
            g = from_wkb(geom[i])
            polys = list(g.geoms) if g.geom_type == 'MultiPolygon' else [g]
            for p in polys:
                ext = list(p.exterior.coords)  # (lon, lat)
                out.append({
                    "poly": [[round(c[1], 7), round(c[0], 7)] for c in ext],
                    "h": round(float(ht), 1),
                })
    return out


def _default_height(cls):
    c = (cls or '').lower()
    if c in ('commercial', 'industrial', 'institutional', 'education', 'retail',
             'office', 'apartments'):
        return 9.0
    if c == 'residential':
        return 5.0
    return 5.0


def overpass_roads(bbox):
    """Streets for the map, via Overpass (with mirror fallback)."""
    w, s, e, n = bbox
    query = (
        '[out:json][timeout:60];'
        'way(%f,%f,%f,%f)[highway~"^(motorway|trunk|primary|secondary|'
        'tertiary|residential|unclassified|living_street)$"];'
        'out geom;' % (s, w, n, e)
    )
    last = None
    for url in OVERPASS_URLS:
        try:
            req = urllib.request.Request(url, data=query.encode('utf-8'),
                                         headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read().decode('utf-8'))
            return [[[p['lat'], p['lon']] for p in el.get('geometry', [])]
                    for el in data.get('elements', [])
                    if el.get('type') == 'way' and len(el.get('geometry', [])) >= 2]
        except Exception as e:  # noqa: BLE001 -- try the next mirror
            last = e
    raise SystemExit("could not fetch roads from any Overpass mirror: %s" % last)


def main():
    ap = argparse.ArgumentParser(description="Build a coverage-data bundle from Overture.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--place", help="place name to geocode, e.g. 'a US suburb'")
    g.add_argument("--center", nargs=2, type=float, metavar=("LAT", "LON"))
    g.add_argument("--bbox", help="west,south,east,north")
    ap.add_argument("--radius", type=float, default=2.0,
                    help="radius in km around --place/--center (default 2)")
    ap.add_argument("-o", "--out", help="output JSON path (default: auto-named)")
    args = ap.parse_args()

    if args.bbox:
        w, s, e, n = [float(x) for x in args.bbox.split(",")]
        lat, lon = (s + n) / 2, (w + e) / 2
        radius_km = math.hypot((n - s) / 2 * 110.54,
                               (e - w) / 2 * 111.32 * math.cos(math.radians((s + n) / 2)))
    elif args.place:
        lat, lon = geocode(args.place)
        radius_km = args.radius
    else:
        lat, lon = args.center
        radius_km = args.radius
    bbox = bbox_from_center(lat, lon, radius_km)

    print("downloading Overture buildings for bbox %.4f,%.4f,%.4f,%.4f ..."
          % bbox, file=sys.stderr, flush=True)
    buildings = overture_buildings(bbox)
    print("  %d buildings" % len(buildings), file=sys.stderr, flush=True)

    print("downloading streets ...", file=sys.stderr, flush=True)
    roads = overpass_roads(bbox)
    print("  %d roads" % len(roads), file=sys.stderr, flush=True)

    out = {
        "center_lat": round(lat, 6),
        "center_lon": round(lon, 6),
        "radius_m": round(radius_km * 1000.0, 0),
        "buildings": buildings,
        "roads": roads,
    }
    path = args.out or "coverage_%.4f_%.4f.json" % (lat, lon)
    with open(path, "w") as f:
        json.dump(out, f)
    print("wrote %s (%d buildings, %d roads)" % (path, len(buildings), len(roads)),
          file=sys.stderr)


if __name__ == "__main__":
    main()
