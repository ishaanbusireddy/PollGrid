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


def _house_effects(pollster_id: int, region: str | None = None) -> tuple[float, float]:
    """→ (dem_lean_pts, weight_multiplier) from the latest transparent rating.
    A regional rating row (addendum §1.3 — written only past the graded-count
    threshold) wins over the national one for races in that region; declared
    priors (grade='prior') and provisional rows behave like any other rating."""
    if region:
        row = db.query_one(
            "SELECT house_effect_dem, weight_multiplier FROM pollster_ratings "
            "WHERE pollster_id=? AND region=? ORDER BY as_of DESC LIMIT 1", (pollster_id, region))
        if row is not None:
            return row["house_effect_dem"], row["weight_multiplier"]
    row = db.query_one(
        "SELECT house_effect_dem, weight_multiplier FROM pollster_ratings "
        "WHERE pollster_id=? AND region='national' ORDER BY as_of DESC LIMIT 1", (pollster_id,))
    if row is None:
        return 0.0, 1.0
    return row["house_effect_dem"], row["weight_multiplier"]


def _race_region(race_id: int) -> str | None:
    from domain.geography import CENSUS_REGION, STATES
    race = db.query_one("SELECT state_fips FROM races WHERE id=?", (race_id,))
    if not race or not race["state_fips"] or race["state_fips"] not in STATES:
        return None
    return CENSUS_REGION.get(STATES[race["state_fips"]][0])


def _herding_buckets(entries: list[dict], window_days: int) -> None:
    """Same-window multi-pollster bursts share information (herding): polls from
    DIFFERENT pollsters whose field_end dates fall within one window are a
    bucket, and each member's weight is multiplied by 1/sqrt(bucket_size) so a
    six-poll flurry can't move the average as if it were six independent reads.
    Mutates entries in place; annotates each with its discount for the audit."""
    entries.sort(key=lambda e: e["field_end"])
    i = 0
    while i < len(entries):
        j = i
        pollsters = {entries[i]["pollster_id"]}
        while j + 1 < len(entries):
            try:
                gap = (datetime.fromisoformat(entries[j + 1]["field_end"][:10])
                       - datetime.fromisoformat(entries[i]["field_end"][:10])).days
            except ValueError:
                break
            if gap > window_days:
                break
            j += 1
            pollsters.add(entries[j]["pollster_id"])
        k = len(pollsters)
        discount = 1.0 / math.sqrt(k) if k > 1 else 1.0
        for e in entries[i:j + 1]:
            e["w"] *= discount
            e["herding_discount"] = round(discount, 4)
        i = j + 1


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
    region = _race_region(race_id) if use_house else None

    # pass 1: base weights + house adjustment per poll
    entries: list[dict] = []
    for p in polls:
        try:
            age_days = max(0.0, (ref - datetime.fromisoformat(p["field_end"][:10])).days)
        except ValueError:
            continue
        w = 0.5 ** (age_days / half_life)
        if use_size and p["sample_size"]:
            w *= math.sqrt(min(p["sample_size"], 5000) / 600.0)
        lean, mult = _house_effects(p["pollster_id"], region) if use_house else (0.0, 1.0)
        w *= mult
        entries.append({"poll_id": p["id"], "pollster_id": p["pollster_id"],
                        "field_end": p["field_end"], "w": w, "lean": lean, "herding_discount": 1.0})

    # pass 2: herding discount over same-window multi-pollster buckets (§1.3)
    if cfg("polling.averaging.herding_discount") and len(entries) > 1:
        _herding_buckets(entries, cfg("polling.averaging.herding_window_days"))

    # pass 3: aggregate
    sums: dict[str, float] = {}
    weight_sum = 0.0
    audit_rows = []
    for e in entries:
        results = db.query("SELECT party_code, pct FROM poll_results WHERE poll_id=?", (e["poll_id"],))
        adj = {}
        for r in results:
            pct = r["pct"]
            if r["party_code"] == "DEM":
                pct -= e["lean"]
            elif r["party_code"] == "REP":
                pct += e["lean"]
            adj[r["party_code"]] = pct
            sums[r["party_code"]] = sums.get(r["party_code"], 0.0) + e["w"] * pct
        weight_sum += e["w"]
        audit_rows.append({"poll_id": e["poll_id"], "weight": round(e["w"], 4),
                           "house_lean_dem": e["lean"], "herding_discount": e["herding_discount"],
                           "adjusted": adj})
    if weight_sum <= 0:
        return None
    parties = {pc: round(total / weight_sum, 2) for pc, total in sums.items()}
    metric_id = record(
        "poll_average", f"race:{race_id}",
        "sum(w_i * adjusted_pct_i) / sum(w_i); w_i = 0.5^(age/half_life) * sqrt(min(n,5000)/600) "
        "* rating_multiplier * herding_discount (1/sqrt(k) over same-window multi-pollster buckets); "
        "adjusted = topline -/+ house_effect_dem (regional rating preferred when graded)",
        {"as_of": as_of, "half_life_days": half_life, "region": region, "polls": audit_rows}, parties)
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
