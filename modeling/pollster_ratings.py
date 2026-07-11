"""Transparent pollster scorecard: every pollster graded on real historical error
against certified results, published openly — and that same public grade is what
sets its house-effect weight in the average (modeling/averaging.py).

Regional rows (addendum §1.3): a pollster's error profile often differs by
Census region, so past a graded-count threshold we also publish per-region
grades, and the average prefers the regional row for races in that region.
Below the threshold no regional row exists — a grade is never invented from
thin data; the national row is always written and is always the fallback."""
from __future__ import annotations

from core import db
from core.config import cfg
from core.util import today

GRADE_BANDS = [(1.5, "A"), (2.5, "B"), (3.5, "C"), (5.0, "D")]
GRADE_WEIGHTS = {"A": 1.2, "B": 1.0, "C": 0.8, "D": 0.5, "F": 0.3}


def _grade(avg_err: float) -> str:
    for ceiling, grade in GRADE_BANDS:
        if avg_err <= ceiling:
            return grade
    return "F"


def _write_rating(pollster_id: int, as_of: str, region: str,
                  errs: list[float], dem_biases: list[float]) -> None:
    avg_err = sum(errs) / len(errs)
    house_dem = sum(dem_biases) / len(dem_biases) if dem_biases else 0.0
    grade = _grade(avg_err)
    db.execute(
        "INSERT INTO pollster_ratings(pollster_id,as_of,avg_abs_error,n_graded,grade,"
        "house_effect_dem,weight_multiplier,region) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(pollster_id,as_of,region) DO UPDATE SET avg_abs_error=excluded.avg_abs_error, "
        "n_graded=excluded.n_graded, grade=excluded.grade, house_effect_dem=excluded.house_effect_dem, "
        "weight_multiplier=excluded.weight_multiplier",
        (pollster_id, as_of, round(avg_err, 3), len(errs), grade,
         round(house_dem, 3), GRADE_WEIGHTS[grade], region))


def refresh(as_of: str | None = None) -> int:
    """Grade every pollster whose final-two-week polls can be compared to a
    certified (called) race result. Until a pollster has graded history it stays
    'provisional': zero house effect, full weight — honestly labeled."""
    from domain.geography import CENSUS_REGION, STATES
    as_of = as_of or today()
    min_regional = cfg("polling.regional_ratings_min_graded")
    n = 0
    for p in db.query("SELECT id, name FROM pollsters"):
        rows = db.query(
            """SELECT pr.party_code, pr.pct, ph.dem_pct, ph.rep_pct, r.state_fips
               FROM polls po
               JOIN poll_results pr ON pr.poll_id = po.id
               JOIN races r ON r.id = po.race_id
               JOIN race_calls rc ON rc.race_id = r.id
               JOIN political_history ph ON ph.tier='state' AND ph.entity_id=r.state_fips
                    AND ph.office=r.race_type AND ph.cycle_year=r.cycle_year
               WHERE po.pollster_id=? AND pr.party_code IN ('DEM','REP')
                 AND julianday(rc.called_at) - julianday(po.field_end) BETWEEN 0 AND 14""",
            (p["id"],))
        if not rows:
            db.execute("INSERT OR IGNORE INTO pollster_ratings(pollster_id,as_of,n_graded,grade,"
                       "house_effect_dem,weight_multiplier) VALUES(?,?,0,'provisional',0,1.0)",
                       (p["id"], as_of))
            continue
        # (err, dem_bias|None, region|None) per graded topline
        samples: list[tuple[float, float | None, str | None]] = []
        for r in rows:
            actual = r["dem_pct"] if r["party_code"] == "DEM" else r["rep_pct"]
            if actual is None:
                continue
            usps = STATES.get(r["state_fips"], (None,))[0] if r["state_fips"] else None
            region = CENSUS_REGION.get(usps) if usps else None
            samples.append((abs(r["pct"] - actual),
                            r["pct"] - actual if r["party_code"] == "DEM" else None, region))
        if not samples:
            continue
        _write_rating(p["id"], as_of, "national",
                      [s[0] for s in samples], [s[1] for s in samples if s[1] is not None])
        by_region: dict[str, list[tuple[float, float | None, str | None]]] = {}
        for s in samples:
            if s[2]:
                by_region.setdefault(s[2], []).append(s)
        for region, group in by_region.items():
            if len(group) >= min_regional:
                _write_rating(p["id"], as_of, region,
                              [s[0] for s in group], [s[1] for s in group if s[1] is not None])
        n += 1
    return n
