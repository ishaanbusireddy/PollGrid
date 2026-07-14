"""Chamber-control Monte Carlo: every race's own deterministic probability,
correlated through a shared national-environment shock — similar seats move
together, not independently. Yields a real 'probability the chamber flips',
not a pile of separate seat odds. stdlib random, seeded for reproducibility."""
from __future__ import annotations

import json
import math
import random

from core import db
from core.config import cfg
from core.util import today
from modeling.audit import record
from modeling.forecasting import MARGIN_SCALE, latest as latest_forecast  # MARGIN_SCALE is a fn

# Seats not up in 2026 need a current-holder assumption; unmodeled seats count
# by their latest political_history winner, else split evenly (stated in audit).
def SENATE_NOT_UP_DEM():
    return cfg("chamber_simulation.senate_not_up_dem")


def SENATE_NOT_UP_REP():
    return cfg("chamber_simulation.senate_not_up_rep")


def _race_probs(chamber: str) -> list[tuple[float, float]]:
    """→ [(seat_weight, dem_prob)]. Senate/House seats weigh 1; EC races weigh
    their electoral votes — statewide races carry the state's EVs except ME/NE,
    whose district races carry 1 EV each and statewide races carry the 2
    at-large electors (the elector_method modeled explicitly, never assumed)."""
    if chamber != "ec":
        rt = {"senate": "senate", "house": "house"}[chamber]
        out = []
        for r in db.query("SELECT id FROM races WHERE race_type=? AND phase='general'", (rt,)):
            f = latest_forecast(r["id"])
            if f:
                out.append((1.0, f["dem_prob"]))
        return out
    from domain.geography import ev_allocation
    out = []
    races = db.query("SELECT * FROM races WHERE race_type='president' AND phase='general' "
                     "AND state_fips IS NOT NULL ORDER BY cycle_year DESC")
    if not races:
        return []
    cycle = races[0]["cycle_year"]
    for r in races:
        if r["cycle_year"] != cycle:
            continue
        f = latest_forecast(r["id"])
        if f is None:
            continue
        alloc = ev_allocation(r["state_fips"], cycle)
        if alloc is None:
            continue
        if alloc["elector_method"] == "congressional_district":
            weight = 1.0 if r["district_version_id"] else 2.0  # district elector vs 2 statewide
        else:
            weight = float(alloc["electoral_votes"])
        out.append((weight, f["dem_prob"]))
    return out


def run(chamber: str, as_of: str | None = None) -> dict | None:
    as_of = as_of or today()
    if chamber not in ("senate", "house", "ec"):
        return None
    n_sims = cfg("chamber_simulation.n_sims")
    shock_sd = cfg("chamber_simulation.national_shock_sd")
    probs = _race_probs(chamber)
    if not probs:
        return None
    rng = random.Random(f"{chamber}:{as_of}")  # deterministic per (chamber, day)
    need = {"senate": 50, "house": 218, "ec": 270}[chamber]
    base_dem = SENATE_NOT_UP_DEM() if chamber == "senate" else 0
    dist: dict[int, int] = {}
    control = 0
    for _ in range(n_sims):
        shock = rng.gauss(0.0, shock_sd)  # national swing, shared across every seat
        seats = float(base_dem)
        for weight, p in probs:
            # shift each seat's probability through logit space by the shared shock
            logit = math.log(max(p, 1e-6) / max(1 - p, 1e-6)) + shock * (10 / MARGIN_SCALE())
            p_i = 1 / (1 + math.exp(-logit))
            if rng.random() < p_i:
                seats += weight
        seats = int(seats)
        dist[seats] = dist.get(seats, 0) + 1
        if seats >= need:
            control += 1
    prob = control / n_sims
    metric_id = record(
        "chamber_simulation", chamber,
        f"{n_sims} Monte Carlo draws; shared N(0,{shock_sd}) national shock applied in logit space; "
        f"dem control at >= {need} seats (senate assumes {SENATE_NOT_UP_DEM()}D/{SENATE_NOT_UP_REP()}R "
        "not-up baseline and VP tiebreak to DEM)",
        {"n_races_modeled": len(probs)}, {"dem_control_prob": prob})
    seat_distribution = {str(k): v / n_sims for k, v in sorted(dist.items())}
    db.execute("INSERT INTO chamber_simulations(chamber,as_of,n_sims,dem_control_prob,"
               "seat_distribution_json,metric_id) VALUES(?,?,?,?,?,?) "
               "ON CONFLICT(chamber,as_of) DO UPDATE SET n_sims=excluded.n_sims, "
               "dem_control_prob=excluded.dem_control_prob, "
               "seat_distribution_json=excluded.seat_distribution_json, metric_id=excluded.metric_id",
               (chamber, as_of, n_sims, round(prob, 4), json.dumps(seat_distribution), metric_id))
    # seat_distribution + metric_id are contract-required (API_CONTRACT.md); latest()
    # returns them, so on-demand run() must too or the histogram renders blank
    return {"chamber": chamber, "as_of": as_of, "n_sims": n_sims, "dem_control_prob": round(prob, 4),
            "seat_distribution": seat_distribution, "metric_id": metric_id}


def latest(chamber: str, as_of: str | None = None) -> dict | None:
    as_of = as_of or today()
    row = db.query_one("SELECT * FROM chamber_simulations WHERE chamber=? AND as_of<=? "
                       "ORDER BY as_of DESC LIMIT 1", (chamber, as_of))
    if row is None:
        return None
    return {"chamber": chamber, "as_of": row["as_of"], "n_sims": row["n_sims"],
            "dem_control_prob": row["dem_control_prob"],
            "seat_distribution": json.loads(row["seat_distribution_json"]), "metric_id": row["metric_id"]}
