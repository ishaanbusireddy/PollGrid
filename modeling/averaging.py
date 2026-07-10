"""Poll averaging: recency-weighted, house-effect-adjusted, sample-size-weighted.
Every weight config-driven, zero LLM anywhere near it. Snapshots append to
poll_averages on the as_of axis — never overwritten in place."""
from __future__ import annotations

import math
from datetime import date, datetime

from core import db
from core.config import cfg
from core.util import today
from modeling.audit import record


def _house_effects(pollster_id: int) -> tuple[float, float]:
    """→ (dem_lean_pts, weight_multiplier) from the latest transparent rating.
    Provisional pollsters get zero lean and full weight — the grade sets the
    house-effect weight, not a hidden internal number."""
    row = db.query_one(
        "SELECT house_effect_dem, weight_multiplier FROM pollster_ratings "
        "WHERE pollster_id=? ORDER BY as_of DESC LIMIT 1", (pollster_id,))
    if row is None:
        return 0.0, 1.0
    return row["house_effect_dem"], row["weight_multiplier"]


def compute_average(race_id: int, as_of: str | None = None) -> dict | None:
    as_of = as_of or today()
    half_life = cfg("polling.averaging.recency_half_life_days")
    min_n = cfg("polling.averaging.min_sample_size")
    use_house = cfg("polling.averaging.house_effect_adjustment")
    use_size = cfg("polling.averaging.weight_by_sample_size")

    polls = db.query(
        "SELECT p.id, p.pollster_id, p.field_end, p.sample_size FROM polls p "
        "WHERE p.race_id=? AND p.field_end<=? AND COALESCE(p.sample_size, 100000)>=? "
        "ORDER BY p.field_end DESC LIMIT 100", (race_id, as_of, min_n))
    if not polls:
        return None

    ref = datetime.fromisoformat(as_of)
    sums: dict[str, float] = {}
    weight_sum = 0.0
    audit_rows = []
    for p in polls:
        try:
            age_days = max(0.0, (ref - datetime.fromisoformat(p["field_end"][:10])).days)
        except ValueError:
            continue
        w = 0.5 ** (age_days / half_life)
        if use_size and p["sample_size"]:
            w *= math.sqrt(min(p["sample_size"], 5000) / 600.0)
        lean, mult = _house_effects(p["pollster_id"]) if use_house else (0.0, 1.0)
        w *= mult
        results = db.query("SELECT party_code, pct FROM poll_results WHERE poll_id=?", (p["id"],))
        adj = {}
        for r in results:
            pct = r["pct"]
            if r["party_code"] == "DEM":
                pct -= lean
            elif r["party_code"] == "REP":
                pct += lean
            adj[r["party_code"]] = pct
            sums[r["party_code"]] = sums.get(r["party_code"], 0.0) + w * pct
        weight_sum += w
        audit_rows.append({"poll_id": p["id"], "weight": round(w, 4), "house_lean_dem": lean,
                           "adjusted": adj})
    if weight_sum <= 0:
        return None
    parties = {pc: round(total / weight_sum, 2) for pc, total in sums.items()}
    metric_id = record(
        "poll_average", f"race:{race_id}",
        "sum(w_i * adjusted_pct_i) / sum(w_i); w_i = 0.5^(age/half_life) * sqrt(min(n,5000)/600) * rating_multiplier; "
        "adjusted = topline -/+ house_effect_dem",
        {"as_of": as_of, "half_life_days": half_life, "polls": audit_rows}, parties)
    with db.write() as conn:
        for pc, avg in parties.items():
            conn.execute(
                "INSERT OR IGNORE INTO poll_averages(race_id,as_of,party_code,avg_pct,n_polls,weight_sum,metric_id) "
                "VALUES(?,?,?,?,?,?,?)", (race_id, as_of, pc, avg, len(audit_rows), weight_sum, metric_id))
    return {"race_id": race_id, "as_of": as_of, "parties": parties,
            "n_polls": len(audit_rows), "metric_id": metric_id}


def latest_average(race_id: int, as_of: str | None = None) -> dict | None:
    as_of = as_of or today()
    rows = db.query(
        "SELECT * FROM poll_averages WHERE race_id=? AND as_of=(SELECT MAX(as_of) FROM poll_averages "
        "WHERE race_id=? AND as_of<=?)", (race_id, race_id, as_of))
    if not rows:
        return None
    return {"race_id": race_id, "as_of": rows[0]["as_of"], "metric_id": rows[0]["metric_id"],
            "n_polls": rows[0]["n_polls"], "parties": {r["party_code"]: r["avg_pct"] for r in rows}}


def run_all(as_of: str | None = None) -> int:
    n = 0
    for race in db.query("SELECT DISTINCT race_id AS id FROM polls"):
        if compute_average(race["id"], as_of):
            n += 1
    return n
