"""Fundamentals: incumbency, generic-ballot lean, economic indicators, partisan
lean (computed PVI-style), fundraising ratio — a weighted composite, not a
model's opinion. Weights from config, validated to sum to 1.0."""
from __future__ import annotations

import json

from core import db
from core.config import cfg
from core.util import today
from modeling.audit import record


def partisan_lean(race: dict) -> float:
    """PVI-style: mean Dem-minus-Rep margin over the entity's last two
    presidential results, relative to the national margin. Deterministic,
    from political_history — never LLM-guessed.

    A congressional district falls back to its STATE's lean when it has no
    district-level presidential history yet (real district leans, derived by
    areal interpolation from county results, supersede the fallback wherever
    county data has landed). This keeps EVERY district colored instead of
    painting the ~2/3 without derived data a dead neutral."""
    nat = db.query(
        "SELECT dem_pct, rep_pct, cycle_year FROM political_history "
        "WHERE tier='nation' AND entity_id='US' AND office='president' AND dem_pct IS NOT NULL "
        "ORDER BY cycle_year DESC LIMIT 2")
    if not nat:
        return 0.0
    natm = sum(r["dem_pct"] - r["rep_pct"] for r in nat) / len(nat)

    def _lean(tier: str, entity: str) -> float | None:
        rows = db.query(
            "SELECT dem_pct, rep_pct FROM political_history "
            "WHERE tier=? AND entity_id=? AND office='president' AND dem_pct IS NOT NULL "
            "ORDER BY cycle_year DESC LIMIT 2", (tier, entity))
        if not rows:
            return None
        ent = sum(r["dem_pct"] - r["rep_pct"] for r in rows) / len(rows)
        return round(ent - natm, 2)

    if race["district_version_id"]:
        lean = _lean("congressional_district", str(race["district_version_id"]))
        if lean is None and race["state_fips"]:
            lean = _lean("state", race["state_fips"])  # fallback: inherit the state lean
        return lean if lean is not None else 0.0
    if race["state_fips"]:
        lean = _lean("state", race["state_fips"])
        return lean if lean is not None else 0.0
    lean = _lean("nation", "US")
    return lean if lean is not None else 0.0


def _generic_ballot(as_of: str) -> float:
    row = db.query_one(
        "SELECT r.id FROM races r WHERE r.race_type='generic_ballot' ORDER BY cycle_year DESC LIMIT 1")
    if not row:
        return 0.0
    from modeling.averaging import latest_average
    avg = latest_average(row["id"], as_of)
    if not avg:
        return 0.0
    return (avg["parties"].get("DEM", 0) - avg["parties"].get("REP", 0))


def _incumbency(race_id: int) -> float:
    row = db.query_one(
        "SELECT rc.party_code FROM race_candidates rc WHERE rc.race_id=? AND rc.is_incumbent=1", (race_id,))
    if row is None:
        return 0.0  # open seat
    return 1.0 if row["party_code"] == "DEM" else -1.0


def _economic_index() -> float:
    """From ingested economic facts when present; 0 (neutral) otherwise —
    an honest 'no data' rather than an invented number."""
    row = db.query_one("SELECT value FROM app_meta WHERE key='economic_index'")
    return float(row["value"]) if row else 0.0


def _fundraising_ratio(race_id: int) -> float:
    rows = db.query(
        "SELECT rc.party_code, SUM(d.total_amount) amt FROM race_candidates rc "
        "JOIN donors_aggregated d ON d.candidate_id=rc.candidate_id "
        "WHERE rc.race_id=? AND rc.party_code IN ('DEM','REP') GROUP BY rc.party_code", (race_id,))
    amts = {r["party_code"]: r["amt"] or 0 for r in rows}
    dem, rep = amts.get("DEM", 0), amts.get("REP", 0)
    if dem + rep <= 0:
        return 0.0
    return round((dem - rep) / (dem + rep), 3)


def compute(race_id: int, as_of: str | None = None) -> dict | None:
    as_of = as_of or today()
    race = db.query_one("SELECT * FROM races WHERE id=?", (race_id,))
    if race is None:
        return None
    w = cfg("fundamentals.weights")
    components = {
        "incumbency": _incumbency(race_id),                       # -1..1
        "generic_ballot": max(-1, min(1, _generic_ballot(as_of) / 10.0)),
        "economic_index": max(-1, min(1, _economic_index())),
        "partisan_lean": max(-1, min(1, partisan_lean(race) / 15.0)),
        "fundraising_ratio": _fundraising_ratio(race_id),         # -1..1
    }
    score = sum(w[k] * v for k, v in components.items())          # >0 favors DEM
    metric_id = record("fundamentals", f"race:{race_id}",
                       "dem_score = Σ weight_k * component_k (components normalized to [-1,1])",
                       {"weights": w, "components": components, "as_of": as_of}, {"dem_score": score})
    db.execute("INSERT OR IGNORE INTO fundamentals_snapshots(race_id,as_of,dem_score,components_json,metric_id) "
               "VALUES(?,?,?,?,?)", (race_id, as_of, round(score, 4), json.dumps(components), metric_id))
    return {"race_id": race_id, "as_of": as_of, "dem_score": round(score, 4),
            "components": components, "metric_id": metric_id}


def classify_competitiveness(race_id: int) -> str | None:
    """Bootstraps race.competitiveness from fundamentals alone — the only
    signal that exists before any poll has landed. Without this, a real
    (non-demo) install deadlocks: the PR-wire poll search, FEC's competitive-
    candidate rotation, and the nightly genius-layer pass (factor scorecards,
    ensemble, coalitions, narratives) all key off competitiveness IN
    ('tossup','lean') OR real poll_averages — and poll_averages can't exist
    until a poll lands. Honest about zero signal: generic_ballot and
    economic_index are national constants (the same nonzero value for every
    race in the country) and prove nothing about THIS race, so only the
    race-specific components (incumbency, partisan_lean, fundraising_ratio)
    count as real signal — if all three are exactly 0 (no history, no
    incumbent, no fundraising), the race stays 'unrated' rather than every
    uncontested seat in the country being fabricated as a 'tossup' off the
    national mood alone. Safe to re-run — recomputed every nightly pass so
    it improves as real data (fundraising, redistricting) arrives.
    'incumbency' reads 0.0 for BOTH a genuine open seat and a race with no
    candidate roster at all, so it can't disambiguate signal from absence by
    itself — a roster existing at all (any race_candidates row) is checked
    directly instead."""
    f = compute(race_id)
    if f is None:
        return None
    has_roster = db.query_one("SELECT 1 FROM race_candidates WHERE race_id=?", (race_id,)) is not None
    if not (has_roster or f["components"]["partisan_lean"] or f["components"]["fundraising_ratio"]):
        return None
    bands = cfg("fundamentals.competitiveness_bands")
    mag = abs(f["dem_score"])
    band = ("tossup" if mag < bands["tossup_max"] else
            "lean" if mag < bands["lean_max"] else
            "likely" if mag < bands["likely_max"] else "safe")
    db.execute("UPDATE races SET competitiveness=? WHERE id=?", (band, race_id))
    return band


def classify_all_competitiveness() -> int:
    n = 0
    for r in db.query(
            "SELECT id FROM races WHERE status IN ('upcoming','live') AND race_type != 'generic_ballot'"):
        if classify_competitiveness(r["id"]):
            n += 1
    return n


def latest(race_id: int, as_of: str | None = None) -> dict | None:
    as_of = as_of or today()
    row = db.query_one("SELECT * FROM fundamentals_snapshots WHERE race_id=? AND as_of<=? "
                       "ORDER BY as_of DESC LIMIT 1", (race_id, as_of))
    if row is None:
        return None
    return {"race_id": race_id, "as_of": row["as_of"], "dem_score": row["dem_score"],
            "components": json.loads(row["components_json"]), "metric_id": row["metric_id"]}
