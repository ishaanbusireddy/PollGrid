#!/usr/bin/env python3
"""Offline boundary build. States + counties ship vendored (frontend/static/
data/). This script fetches the Census-derived 2022-vintage congressional
district shapes (cartographic 500k, public domain — the loganpowell/
census-geojson mirror of the Census cb_2022_us_cd118_500k file), simplifies
every ring with Douglas-Peucker, and writes
frontend/static/data/us_districts.json.

Feature normalization: properties -> {geoid, state_fips, district_number},
feature.id = the 4-digit GEOID (state FIPS + district number, at-large = 00,
non-voting delegate seats = 98) so the frontend can join rows by id.

Rebuilding from a different vintage later: point --source at any GeoJSON
FeatureCollection carrying STATEFP + CD###FP (or GEOID) properties — e.g. a
TIGER/cartographic file converted with `ogr2ogr -f GeoJSON out.json
cb_2024_us_cd119_500k.shp`. --source accepts a URL or a local path.

Also home to the point-in-polygon twin used by the backend gazetteer — zero
GIS dependency at runtime, ever.

Usage: python scripts/build_boundaries.py [--tolerance 0.008] [--source URL|PATH]
"""
import argparse
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "frontend", "static", "data", "us_districts.json")
DEFAULT_SOURCE = ("https://raw.githubusercontent.com/loganpowell/census-geojson/"
                  "master/GeoJSON/500k/2022/congressional-district.json")
_CD_FIELD = re.compile(r"^CD\d+FP$")  # CD118FP today; CD119FP etc. on rebuilds
_PRECISION = 4  # ~11 m at the equator; plenty for a national overlay


def douglas_peucker(points: list, tol: float) -> list:
    if len(points) < 3:
        return points
    def perp(pt, a, b):
        (x, y), (x1, y1), (x2, y2) = pt, a, b
        dx, dy = x2 - x1, y2 - y1
        if dx == dy == 0:
            return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
        t = max(0, min(1, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
        px, py = x1 + t * dx, y1 + t * dy
        return ((x - px) ** 2 + (y - py) ** 2) ** 0.5
    dmax, idx = 0.0, 0
    for i in range(1, len(points) - 1):
        d = perp(points[i], points[0], points[-1])
        if d > dmax:
            dmax, idx = d, i
    if dmax > tol:
        left = douglas_peucker(points[:idx + 1], tol)
        right = douglas_peucker(points[idx:], tol)
        return left[:-1] + right
    return [points[0], points[-1]]


def _round_ring(ring: list) -> list:
    """Fixed-precision coords, consecutive duplicates (rounding artifacts)
    collapsed; ring closure (first == last) preserved."""
    out: list = []
    for x, y in ring:
        pt = [round(x, _PRECISION), round(y, _PRECISION)]
        if not out or pt != out[-1]:
            out.append(pt)
    if len(out) > 1 and out[0] != out[-1]:
        out.append(list(out[0]))
    return out


def simplify_geometry(geom: dict, tol: float) -> dict:
    def simp_ring(ring):
        out = douglas_peucker([tuple(p) for p in ring], tol)
        return _round_ring([list(p) for p in (out if len(out) >= 4 else ring)])
    if geom["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [simp_ring(r) for r in geom["coordinates"]]}
    if geom["type"] == "MultiPolygon":
        return {"type": "MultiPolygon",
                "coordinates": [[simp_ring(r) for r in poly] for poly in geom["coordinates"]]}
    return geom


def _load_source(source: str) -> dict:
    if os.path.exists(source):
        with open(source, encoding="utf-8") as fh:
            return json.load(fh)
    with urllib.request.urlopen(source, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_feature(feature: dict, tol: float) -> dict | None:
    """Census CD feature -> {id: geoid, properties: {geoid, state_fips,
    district_number}} with a simplified geometry. Returns None for the ZZ
    'undefined' placeholder rows some vintages carry."""
    props = feature.get("properties") or {}
    state_fips = (props.get("STATEFP") or props.get("state_fips") or "").strip()
    cd = next((str(v).strip() for k, v in props.items() if _CD_FIELD.match(k)), "")
    if not cd:
        cd = str(props.get("district_number", "")).strip()
    geoid = (props.get("GEOID") or props.get("geoid") or "").strip()
    if not cd and len(geoid) == 4:
        cd = geoid[2:]
    if not state_fips and len(geoid) == 4:
        state_fips = geoid[:2]
    if cd.upper() == "ZZ" or not cd.isdigit() or not state_fips:
        return None
    district_number = int(cd)
    if not geoid:
        geoid = f"{state_fips}{district_number:02d}"
    return {"type": "Feature", "id": geoid,
            "properties": {"geoid": geoid, "state_fips": state_fips,
                           "district_number": district_number},
            "geometry": simplify_geometry(feature["geometry"], tol)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tolerance", type=float, default=0.008,
                    help="Douglas-Peucker tolerance in degrees (default 0.008)")
    ap.add_argument("--source", default=DEFAULT_SOURCE,
                    help="GeoJSON FeatureCollection of congressional districts "
                         "(URL or local path; default: census-geojson 2022 500k mirror). "
                         "Use this to rebuild from a newer TIGER-derived file later.")
    args = ap.parse_args()
    sys.setrecursionlimit(20000)  # DP recursion depth on 500k-resolution rings

    print(f"loading {args.source} …")
    gj = _load_source(args.source)
    raw_features = gj.get("features") or []
    features = []
    for feature in raw_features:
        norm = normalize_feature(feature, args.tolerance)
        if norm:
            features.append(norm)
    features.sort(key=lambda f: f["id"])
    if len(features) < 435:
        sys.exit(f"only {len(features)} districts normalized (need >= 435) — wrong source file?")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": features}, fh,
                  separators=(",", ":"))
    size = os.path.getsize(OUT)
    print(f"wrote {len(features)} districts ({size / 1e6:.2f} MB, tolerance {args.tolerance}) → {OUT}")


# ---- point-in-polygon twin (used by the gazetteer; no GIS dependency) ----

def point_in_ring(lon: float, lat: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def point_in_geometry(lon: float, lat: float, geom: dict) -> bool:
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        if point_in_ring(lon, lat, poly[0]) and not any(point_in_ring(lon, lat, h) for h in poly[1:]):
            return True
    return False


if __name__ == "__main__":
    main()
