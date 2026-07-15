"""Derived congressional-district partisan history via population-weighted areal
interpolation of REAL county-level presidential results onto the CURRENT district
lines. This is exactly how district PVI is computed everywhere (Daily Kos, DRA,
Cook): take the current district boundaries and ask how they would have voted in
past presidential elections, apportioning each county's real result to the
districts that carve it up, weighted by how much of the county's population sits
in each.

Nothing here is fabricated. Every input is real: county dem/rep percentages
imported from certified OpenElections results (political_history, is_synthetic=0),
county total population from the Census ACS, and the vendored district/county
boundary geometry. Every OUTPUT row is written confidence='derived' and source
'derived:areal_pop_interpolation' — the same honesty contract precinct figures
use — so nobody can mistake an interpolated estimate for a measured count.

Zero GIS dependency: reuses the gazetteer's pure-Python point-in-polygon. The
per-county area shares are grid-sampled deterministically (a fixed cell-centered
grid over each county's bounding box), so the whole derivation is reproducible."""
from __future__ import annotations

from core import db
from core.config import cfg
from core.gazetteer import _load_features, _point_in_geometry
from core.util import today

SOURCE = "derived:areal_pop_interpolation"


def _bbox(geom: dict) -> tuple[float, float, float, float]:
    xs, ys = [], []
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        for x, y in poly[0]:
            xs.append(x); ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def _grid_points(bbox: tuple[float, float, float, float], n: int) -> list[tuple[float, float]]:
    """Cell-centered n×n grid over the bbox — centered so sample points never sit
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


def _area_shares(county_geom: dict, districts: list[dict], n: int) -> dict[str, float]:
    """→ {district_geoid: fraction of the county's area inside that district}.
    Normalized over the sample points that land in SOME district, so shares sum
    to 1 across districts (a point inside the county but in no district polygon is
    a county/district file-alignment artifact, not genuinely unrepresented land,
    and is excluded rather than allowed to deflate every share)."""
    bbox = _bbox(county_geom)
    tally: dict[str, int] = {}
    assigned = 0
    for (x, y) in _grid_points(bbox, n):
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


def _county_population(geoid: str) -> float:
    row = db.query_one(
        "SELECT value FROM demographics WHERE tier='county_equivalent' AND entity_id=? "
        "AND variable='total_population' ORDER BY is_synthetic, as_of DESC LIMIT 1", (geoid,))
    return float(row["value"]) if row and row["value"] else 0.0


def _district_version_id(geoid: str) -> int | None:
    row = db.query_one(
        "SELECT district_version_id FROM congressional_districts WHERE geoid=? AND effective_to IS NULL",
        (geoid,))
    return row["district_version_id"] if row else None


def _write_district_row(dvid: int, cycle: int, dem: float, rep: float, other: float) -> None:
    total = dem + rep + other
    if total <= 0:
        return
    dem_pct, rep_pct, other_pct = 100 * dem / total, 100 * rep / total, 100 * other / total
    winner = "DEM" if dem >= rep and dem >= other else ("REP" if rep >= other else "OTH")
    # derived beats synthetic, but must NEVER overwrite a genuinely measured row
    db.execute(
        "INSERT INTO political_history(tier,entity_id,office,seat,cycle_year,winner_party,"
        "dem_pct,rep_pct,other_pct,margin_pct,confidence,source,is_synthetic) "
        "VALUES('congressional_district',?,'president','regular',?,?,?,?,?,?,'derived',?,0) "
        "ON CONFLICT(tier,entity_id,office,seat,cycle_year) DO UPDATE SET "
        "winner_party=excluded.winner_party, dem_pct=excluded.dem_pct, rep_pct=excluded.rep_pct, "
        "other_pct=excluded.other_pct, margin_pct=excluded.margin_pct, confidence='derived', "
        "source=excluded.source, is_synthetic=0 WHERE political_history.confidence!='measured'",
        (str(dvid), cycle, winner, round(dem_pct, 2), round(rep_pct, 2), round(other_pct, 2),
         round(abs(dem_pct - rep_pct), 2), SOURCE))


def derive_all(as_of: str | None = None) -> int:
    """Interpolate current-line district presidential leans from every cycle of
    real county presidential history on hand. Returns the number of district-cycle
    rows written. Safe to re-run: area shares are recomputed deterministically and
    rows upsert. A state with no real county history contributes nothing (honest —
    its districts stay uncolored rather than guessed)."""
    as_of = as_of or today()
    n = cfg("modeling.district_interpolation.grid_n")
    cycles = [r["cycle_year"] for r in db.query(
        "SELECT DISTINCT cycle_year FROM political_history WHERE tier='county_equivalent' "
        "AND office='president' AND is_synthetic=0 AND dem_pct IS NOT NULL ORDER BY cycle_year")]
    if not cycles:
        return 0
    state_districts = _state_district_index()
    county_geoms = _county_geom_index()
    shares_cache: dict[str, dict[str, float]] = {}   # county geoid -> {district geoid: share}; cycle-independent
    written = 0

    for cycle in cycles:
        # district geoid -> weighted [dem, rep, other] accumulators + total weight
        acc: dict[str, list[float]] = {}
        rows = db.query(
            "SELECT entity_id geoid, dem_pct, rep_pct, other_pct FROM political_history "
            "WHERE tier='county_equivalent' AND office='president' AND cycle_year=? "
            "AND is_synthetic=0 AND dem_pct IS NOT NULL", (cycle,))
        for r in rows:
            geoid = r["geoid"]
            geom = county_geoms.get(geoid)
            if geom is None:
                continue
            if geoid not in shares_cache:
                dists = state_districts.get(geoid[:2], [])
                shares_cache[geoid] = _area_shares(geom, dists, n) if dists else {}
            shares = shares_cache[geoid]
            if not shares:
                continue
            pop = _county_population(geoid) or 1.0   # equal weight if population is missing
            dem, rep, other = r["dem_pct"] or 0.0, r["rep_pct"] or 0.0, r["other_pct"] or 0.0
            for dgeoid, share in shares.items():
                w = pop * share
                a = acc.setdefault(dgeoid, [0.0, 0.0, 0.0, 0.0])
                a[0] += w * dem; a[1] += w * rep; a[2] += w * other; a[3] += w
        for dgeoid, (dem, rep, other, wsum) in acc.items():
            if wsum <= 0:
                continue
            dvid = _district_version_id(dgeoid)
            if dvid is None:
                continue
            # accumulators hold weight*percentage; dividing by weight recovers the
            # population-weighted average percentage, which is what we store
            _write_district_row(dvid, cycle, dem / wsum, rep / wsum, other / wsum)
            written += 1
    return written
