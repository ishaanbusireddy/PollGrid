#!/usr/bin/env python3
"""Offline boundary build. States + counties ship vendored (frontend/static/
data/). This script fetches current congressional-district shapes (public
domain, the unitedstates/districts mirror of Census TIGER), simplifies them
with Douglas-Peucker, and writes frontend/static/data/us_districts.json.

Also home to the point-in-polygon twin used by the backend gazetteer — zero
GIS dependency at runtime, ever.

Usage: python scripts/build_boundaries.py [--tolerance 0.01]
"""
import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domain.geography import HOUSE_SEATS  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "frontend", "static", "data", "us_districts.json")
BASE = "https://theunitedstates.io/districts/cds/2022/{code}/shape.geojson"


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


def simplify_geometry(geom: dict, tol: float) -> dict:
    def simp_ring(ring):
        out = douglas_peucker([tuple(p) for p in ring], tol)
        return [list(p) for p in (out if len(out) >= 4 else ring)]
    if geom["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [simp_ring(r) for r in geom["coordinates"]]}
    if geom["type"] == "MultiPolygon":
        return {"type": "MultiPolygon",
                "coordinates": [[simp_ring(r) for r in poly] for poly in geom["coordinates"]]}
    return geom


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tolerance", type=float, default=0.01)
    args = ap.parse_args()
    features = []
    for usps, seats in sorted(HOUSE_SEATS.items()):
        codes = [f"{usps}-AL"] if seats == 1 else [f"{usps}-{n}" for n in range(1, seats + 1)]
        for code in codes:
            url = BASE.format(code=code)
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    gj = json.loads(resp.read().decode())
            except Exception as e:
                print(f"skip {code}: {e}")
                continue
            geom = gj["geometry"] if gj.get("type") == "Feature" else gj["features"][0]["geometry"]
            features.append({"type": "Feature", "id": code,
                             "properties": {"code": code, "state": usps},
                             "geometry": simplify_geometry(geom, args.tolerance)})
            print(f"ok {code}")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": features}, fh)
    print(f"wrote {len(features)} districts → {OUT}")


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
