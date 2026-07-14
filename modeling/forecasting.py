"""Forecasting: deterministic blend of the poll average and the fundamentals
composite → win probability, hash-chained into predictions. A race-type only
earns VISIBLE forecasts after the nightly backtest clears the Brier ceiling
over a minimum number of graded predictions (pure SQL and arithmetic — no LLM,
no peeking at outcomes in the replay). The scorecard shows every category,
including the ones still failing."""
from __future__ import annotations

import json
import math

from core import db
from core.config import cfg
from core.util import today
from modeling import fundamentals as fdx
from modeling.audit import record
from modeling.averaging import latest_average

# Logistic scale (config: forecasting.margin_scale): ~6 pts of margin ≈ 80%
# win probability by default — a conservative mapping stated in every audit row.
def MARGIN_SCALE() -> float:
    return cfg("forecasting.margin_scale")


def _probability(margin_pts: float) -> float:
    return 1.0 / (1.0 + math.exp(-margin_pts / MARGIN_SCALE()))


def compute(race_id: int, as_of: str | None = None) -> dict | None:
    as_of = as_of or today()
    race = db.query_one("SELECT * FROM races WHERE id=?", (race_id,))
    if race is None or race["race_type"] == "generic_ballot":
        return None
    avg = latest_average(race_id, as_of)
    fund = fdx.latest(race_id, as_of) or fdx.compute(race_id, as_of)
    if avg is None and fund is None:
        return None
    poll_margin = None
    if avg:
        poll_margin = avg["parties"].get("DEM", 0) - avg["parties"].get("REP", 0)
    fund_margin = (fund["dem_score"] * 10.0) if fund else 0.0  # score [-1,1] → margin pts
    if poll_margin is None:
        blended = fund_margin
        formula = "margin = fundamentals_score*10 (no qualifying polls)"
    else:
        w = cfg("forecasting.fundamentals_weight_with_polls")
        blended = (1 - w) * poll_margin + w * fund_margin
        formula = f"margin = {1-w:.2f}*poll_margin + {w:.2f}*fundamentals_margin"
    dem_prob = round(_probability(blended), 4)
    metric_id = record(
        "forecast", f"race:{race_id}",
        formula + f"; p = 1/(1+exp(-margin/{MARGIN_SCALE()}))",
        {"as_of": as_of, "poll_margin": poll_margin, "fund_margin": fund_margin,
         "avg_metric": avg and avg["metric_id"], "fund_metric": fund and fund["metric_id"]},
        {"dem_prob": dem_prob})
    probs = {"DEM": dem_prob, "REP": round(1 - dem_prob, 4)}
    with db.write() as conn:
        conn.execute("INSERT OR IGNORE INTO forecasts(race_id,as_of,model,dem_prob,rep_prob,other_prob,metric_id) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (race_id, as_of, "quantitative", dem_prob, 1 - dem_prob, 0, metric_id))
    from core import provenance
    provenance.chained_insert("predictions", {
        "race_id": race_id, "as_of": as_of, "model": "quantitative", "probs_json": json.dumps(probs)})
    return {"race_id": race_id, "as_of": as_of, "model": "quantitative",
            "dem_prob": dem_prob, "rep_prob": round(1 - dem_prob, 4), "metric_id": metric_id}


def latest(race_id: int, model: str = "quantitative", as_of: str | None = None) -> dict | None:
    as_of = as_of or today()
    return db.query_one("SELECT * FROM forecasts WHERE race_id=? AND model=? AND as_of<=? "
                        "ORDER BY as_of DESC LIMIT 1", (race_id, model, as_of))


# ------------------------- the Brier backtest gate -------------------------

def grade_predictions() -> int:
    """Grade every ungraded prediction against ground truth. Two truth sources,
    pure SQL + arithmetic, no peeking (a prediction only grades against an
    outcome dated after it): (1) human race calls; (2) the certified archive —
    a real (never synthetic) political_history row for the race's office/
    state/cycle, which is how OpenElections-imported certified results grade
    the replay even when nobody clicked CALL."""
    n = 0
    for r in db.query(
            "SELECT p.id, p.probs_json, rc.winner_party FROM predictions p "
            "JOIN race_calls rc ON rc.race_id=p.race_id WHERE p.graded_outcome IS NULL "
            "AND p.as_of < date(rc.called_at)"):
        db.execute("UPDATE predictions SET graded_outcome=?, graded_at=datetime('now') WHERE id=?",
                   (r["winner_party"], r["id"]))
        n += 1
    for r in db.query(
            """SELECT p.id, ph.winner_party FROM predictions p
               JOIN races r ON r.id = p.race_id
               JOIN political_history ph ON ph.tier='state' AND ph.entity_id=r.state_fips
                    AND ph.office=r.race_type AND ph.cycle_year=r.cycle_year
                    AND ph.is_synthetic=0 AND ph.confidence='measured'
               WHERE p.graded_outcome IS NULL AND ph.winner_party IS NOT NULL
                 AND r.race_type IN ('president','senate','governor')
                 AND p.as_of <= (r.cycle_year || '-11-30')"""):
        db.execute("UPDATE predictions SET graded_outcome=?, graded_at=datetime('now') WHERE id=?",
                   (r["winner_party"], r["id"]))
        n += 1
    return n


def backtest(as_of: str | None = None) -> list[dict]:
    """Nightly: Brier per category per model over all graded predictions.
    passed = brier <= ceiling AND n >= minimum. Category visibility is earned,
    never asserted."""
    as_of = as_of or today()
    ceiling = cfg("forecasting.brier_ceiling")
    min_n = cfg("forecasting.min_graded_predictions")
    out = []
    cats = db.query("SELECT DISTINCT r.race_type cat, p.model FROM predictions p "
                    "JOIN races r ON r.id=p.race_id WHERE p.graded_outcome IS NOT NULL")
    for c in cats:
        rows = db.query(
            "SELECT p.probs_json, p.graded_outcome FROM predictions p JOIN races r ON r.id=p.race_id "
            "WHERE r.race_type=? AND p.model=? AND p.graded_outcome IS NOT NULL", (c["cat"], c["model"]))
        briers = []
        for r in rows:
            probs = json.loads(r["probs_json"])
            p_dem = probs.get("DEM", 0.5)
            outcome = 1.0 if r["graded_outcome"] == "DEM" else 0.0
            briers.append((p_dem - outcome) ** 2)
        if not briers:
            continue
        brier = sum(briers) / len(briers)
        passed = int(brier <= ceiling and len(briers) >= min_n)
        db.execute("INSERT INTO backtest_results(category,as_of,model,brier,n_graded,passed) VALUES(?,?,?,?,?,?) "
                   "ON CONFLICT(category,as_of,model) DO UPDATE SET brier=excluded.brier, "
                   "n_graded=excluded.n_graded, passed=excluded.passed",
                   (c["cat"], as_of, c["model"], round(brier, 4), len(briers), passed))
        out.append({"category": c["cat"], "model": c["model"], "brier": round(brier, 4),
                    "n_graded": len(briers), "passed": bool(passed)})
    return out


def category_visible(category: str, model: str = "quantitative") -> tuple[bool, str]:
    """Is this race-type's forecast earned-visible? Scope honesty (review §2.8):
    with no pre-launch polling archive, categories earn visibility from live
    cycles only — the gate reason says exactly where a category stands."""
    if not cfg("forecasting.enabled") and not cfg("forecasting.auto_enable_earned"):
        return False, "forecasting disabled in config"
    row = db.query_one("SELECT * FROM backtest_results WHERE category=? AND model=? "
                       "ORDER BY as_of DESC LIMIT 1", (category, model))
    if row is None:
        return False, "no graded backtest history yet — visibility is earned, not asserted"
    if not row["passed"]:
        return False, (f"backtest not passed: Brier {row['brier']} vs ceiling "
                       f"{cfg('forecasting.brier_ceiling')} over {row['n_graded']} graded "
                       f"(need ≥{cfg('forecasting.min_graded_predictions')})")
    return True, f"earned: Brier {row['brier']} over {row['n_graded']} graded predictions"
