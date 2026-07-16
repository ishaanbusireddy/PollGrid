"""Shared areal-interpolation machinery: apportion a county's quantity to the
congressional districts that carve it up, weighted by the share of the county's
AREA inside each district. Used by both district_history.py (partisan lean) and
district_demographics.py — one implementation so they can never diverge.

Every share is a pure function of the vendored county/district geometry (it does
NOT depend on any election cycle or demographic variable), so it is computed once
per county and PERSISTED in county_district_area_shares. The cache is invalidated
only when the geometry files change (tracked by a fingerprint in app_meta), which
turns the nightly district steps from an O(counties x grid^2 x polygon) recompute
into a table read after the first run.

Zero GIS dependency: reuses the gazetteer's pure-Python point-in-polygon. The grid
is deterministic (a fixed cell-centered n x n grid over each county's bbox), so the
whole thing is reproducible."""
from __future__ import annotations

import os

from core import db
from core.config import ROOT
from core.config import cfg
from core.gazetteer import _load_features, _point_in_geometry

_DATA = os.path.join(ROOT, "frontend", "static", "data")
_FINGERPRINT_KEY = "areal_geom_fingerprint"


def _bbox(geom: dict) -> tuple[float, float, float, float]:
    xs, ys = [], []
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        for x, y in poly[0]:
            xs.append(x); ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def _grid_points(bbox: tuple[float, float, float, float], n: int) -> list[tuple[float, float]]:
    """Cell-centered n x n grid over the bbox — centered so sample points never sit
    exactly on a boundary edge (which point-in-polygon treats as ambiguous)."""
    minx, miny, maxx, maxy = bbox
    dx, dy = (maxx - minx) or 1e-9, (maxy - miny) or 1e-9
    return [(minx + (i + 0.5) * dx / n, miny + (j + 0.5) * dy / n)
            for i in range(n) for j in range(n)]


def _state_district_index() -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = {}
    for f in _load_features("us_districts.json"):
        idx.setdefault(f["properties"].get("state_fips"), []).append(f)
    return idx


def _county_geom_index() -> dict[str, dict]:
    return {f.get("id"): f["geometry"] for f in _load_features("us_counties.json") if f.get("id")}


def _compute_shares(county_geom: dict, districts: list[dict], n: int) -> dict[str, float]:
    """→ {district_geoid: fraction of the county's area inside that district}.
    Normalized over the sample points that land in SOME district, so shares sum to
    1 across districts (a point inside the county but in no district polygon is a
    county/district file-alignment artifact, not genuinely unrepresented land, and
    is excluded rather than allowed to deflate every share)."""
    tally: dict[str, int] = {}
    assigned = 0
    for (x, y) in _grid_points(_bbox(county_geom), n):
        if not _point_in_geometry(x, y, county_geom):
            continue
        for d in districts:
            if _point_in_geometry(x, y, d["geometry"]):
                g = d.get("id") or d["properties"].get("geoid")
                tally[g] = tally.get(g, 0) + 1
                assigned += 1
                break
    if not assigned:
        return {}
    return {g: c / assigned for g, c in tally.items()}


def _geom_fingerprint() -> str:
    """Cheap change-detector for the two boundary files (size+mtime). Any edit to a
    vendored geometry file changes this and forces a share recompute."""
    parts = []
    for name in ("us_counties.json", "us_districts.json"):
        try:
            st = os.stat(os.path.join(_DATA, name))
            parts.append(f"{name}:{st.st_size}:{int(st.st_mtime)}")
        except OSError:
            parts.append(f"{name}:missing")
    return "|".join(parts)


def _ensure_fresh_cache() -> None:
    """Drop the cached shares if the geometry files changed since they were computed."""
    fp = _geom_fingerprint()
    if db.meta_get(_FINGERPRINT_KEY) != fp:
        with db.write() as conn:
            conn.execute("DELETE FROM county_district_area_shares")
        db.meta_set(_FINGERPRINT_KEY, fp)


class AreaShareResolver:
    """Lazily resolves + persists a county's district area-shares, holding the
    geometry indexes in memory for the life of one derivation run. Construct once,
    call shares_for(county_geoid) per county."""

    def __init__(self) -> None:
        _ensure_fresh_cache()
        self._n = cfg("modeling.district_interpolation.grid_n")
        self._county_geoms = _county_geom_index()
        self._state_districts = _state_district_index()
        self._mem: dict[str, dict[str, float]] = {}

    def shares_for(self, county_geoid: str) -> dict[str, float]:
        if county_geoid in self._mem:
            return self._mem[county_geoid]
        cached = db.query(
            "SELECT district_geoid, share FROM county_district_area_shares WHERE county_geoid=?",
            (county_geoid,))
        if cached:
            shares = {r["district_geoid"]: r["share"] for r in cached}
            self._mem[county_geoid] = shares
            return shares
        geom = self._county_geoms.get(county_geoid)
        if geom is None:
            self._mem[county_geoid] = {}
            return {}
        districts = self._state_districts.get(county_geoid[:2], [])
        shares = _compute_shares(geom, districts, self._n) if districts else {}
        with db.write() as conn:
            for dg, sh in shares.items():
                conn.execute("INSERT OR IGNORE INTO county_district_area_shares"
                             "(county_geoid,district_geoid,share) VALUES(?,?,?)",
                             (county_geoid, dg, sh))
        self._mem[county_geoid] = shares
        return shares


def district_version_id(geoid: str) -> int | None:
    row = db.query_one(
        "SELECT district_version_id FROM congressional_districts WHERE geoid=? AND effective_to IS NULL",
        (geoid,))
    return row["district_version_id"] if row else None
