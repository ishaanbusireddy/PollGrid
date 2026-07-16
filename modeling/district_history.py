"""Derived congressional-district partisan history via population-weighted areal
interpolation of REAL county-level presidential results onto the CURRENT district
lines. This is exactly how district PVI is computed everywhere (Daily Kos, DRA,
Cook): take the current district boundaries and ask how they would have voted in
past presidential elections, apportioning each county's real result to the
districts that carve it up, weighted by how much of the county's population sits
in each.

Nothing here is fabricated. Every input is real: county dem/rep percentages
imported from certified OpenElections results (political_history, is_synthetic=0),
county total population from the Census ACS, and the vendored boundary geometry.
Every OUTPUT row is written confidence='derived' and source
'derived:areal_pop_interpolation' — the same honesty contract precinct figures
use — so nobody can mistake an interpolated estimate for a measured count.

The area-share machinery lives in modeling/areal.py (shared with
district_demographics.py and persistently cached)."""
from __future__ import annotations

from core import db
from core.util import today
from modeling.areal import AreaShareResolver, district_version_id

SOURCE = "derived:areal_pop_interpolation"


def _county_population(geoid: str) -> float:
    row = db.query_one(
        "SELECT value FROM demographics WHERE tier='county_equivalent' AND entity_id=? "
        "AND variable='total_population' ORDER BY is_synthetic, as_of DESC LIMIT 1", (geoid,))
    return float(row["value"]) if row and row["value"] else 0.0


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
    rows written. Safe to re-run: area shares are cached (modeling/areal.py) and
    rows upsert. A state with no real county history contributes nothing (honest —
    its districts stay uncolored rather than guessed)."""
    as_of = as_of or today()
    cycles = [r["cycle_year"] for r in db.query(
        "SELECT DISTINCT cycle_year FROM political_history WHERE tier='county_equivalent' "
        "AND office='president' AND is_synthetic=0 AND dem_pct IS NOT NULL ORDER BY cycle_year")]
    if not cycles:
        return 0
    resolver = AreaShareResolver()
    written = 0
    for cycle in cycles:
        # district geoid -> weighted [dem, rep, other, total_weight] accumulators
        acc: dict[str, list[float]] = {}
        rows = db.query(
            "SELECT entity_id geoid, dem_pct, rep_pct, other_pct FROM political_history "
            "WHERE tier='county_equivalent' AND office='president' AND cycle_year=? "
            "AND is_synthetic=0 AND dem_pct IS NOT NULL", (cycle,))
        for r in rows:
            shares = resolver.shares_for(r["geoid"])
            if not shares:
                continue
            pop = _county_population(r["geoid"]) or 1.0   # equal weight if population is missing
            dem, rep, other = r["dem_pct"] or 0.0, r["rep_pct"] or 0.0, r["other_pct"] or 0.0
            for dgeoid, share in shares.items():
                w = pop * share
                a = acc.setdefault(dgeoid, [0.0, 0.0, 0.0, 0.0])
                a[0] += w * dem; a[1] += w * rep; a[2] += w * other; a[3] += w
        for dgeoid, (dem, rep, other, wsum) in acc.items():
            if wsum <= 0:
                continue
            dvid = district_version_id(dgeoid)
            if dvid is None:
                continue
            # accumulators hold weight*percentage; dividing by total weight recovers
            # the population-weighted average percentage, which is what we store
            _write_district_row(dvid, cycle, dem / wsum, rep / wsum, other / wsum)
            written += 1
    return written
