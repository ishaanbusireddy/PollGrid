"""Transparent pollster scorecard: every pollster graded on real historical error
against certified results, published openly — and that same public grade is what
sets its house-effect weight in the average (modeling/averaging.py)."""
from __future__ import annotations

from core import db
from core.util import today

GRADE_BANDS = [(1.5, "A"), (2.5, "B"), (3.5, "C"), (5.0, "D")]


def _grade(avg_err: float) -> str:
    for ceiling, grade in GRADE_BANDS:
        if avg_err <= ceiling:
            return grade
    return "F"


def refresh(as_of: str | None = None) -> int:
    """Grade every pollster whose final-two-week polls can be compared to a
    certified (called) race result. Until a pollster has graded history it stays
    'provisional': zero house effect, full weight — honestly labeled."""
    as_of = as_of or today()
    n = 0
    for p in db.query("SELECT id, name FROM pollsters"):
        rows = db.query(
            """SELECT pr.party_code, pr.pct, ph.dem_pct, ph.rep_pct
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
        errs, dem_biases = [], []
        for r in rows:
            actual = r["dem_pct"] if r["party_code"] == "DEM" else r["rep_pct"]
            if actual is None:
                continue
            errs.append(abs(r["pct"] - actual))
            if r["party_code"] == "DEM":
                dem_biases.append(r["pct"] - actual)
        if not errs:
            continue
        avg_err = sum(errs) / len(errs)
        house_dem = sum(dem_biases) / len(dem_biases) if dem_biases else 0.0
        grade = _grade(avg_err)
        weight = {"A": 1.2, "B": 1.0, "C": 0.8, "D": 0.5, "F": 0.3}[grade]
        db.execute(
            "INSERT INTO pollster_ratings(pollster_id,as_of,avg_abs_error,n_graded,grade,"
            "house_effect_dem,weight_multiplier) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(pollster_id,as_of) DO UPDATE SET avg_abs_error=excluded.avg_abs_error, "
            "n_graded=excluded.n_graded, grade=excluded.grade, house_effect_dem=excluded.house_effect_dem, "
            "weight_multiplier=excluded.weight_multiplier",
            (p["id"], as_of, round(avg_err, 3), len(errs), grade, round(house_dem, 3), weight))
        n += 1
    return n
