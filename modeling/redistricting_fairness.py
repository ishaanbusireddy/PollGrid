"""Redistricting fairness: efficiency gap and mean-median difference, computed
deterministically per district plan from the boundary-versioned archive."""
from __future__ import annotations

from core import db
from core.util import today


def compute_state(state_fips: str, congress_number: int, as_of: str | None = None) -> dict | None:
    as_of = as_of or today()
    districts = db.query(
        "SELECT district_version_id FROM congressional_districts WHERE state_fips=? AND congress_number=? "
        "AND is_voting=1", (state_fips, congress_number))
    shares, wasted_dem, wasted_rep, total_votes = [], 0.0, 0.0, 0.0
    for d in districts:
        h = db.query_one(
            "SELECT dem_pct, rep_pct FROM political_history WHERE tier='congressional_district' "
            "AND entity_id=? AND office='house' AND dem_pct IS NOT NULL ORDER BY cycle_year DESC LIMIT 1",
            (str(d["district_version_id"]),))
        if h is None:
            continue
        dem, rep = h["dem_pct"], h["rep_pct"]
        two_party = dem + rep
        if two_party <= 0:
            continue
        share = dem / two_party
        shares.append(share)
        # efficiency gap on normalized two-party votes (each district weight 1)
        if share > 0.5:
            wasted_dem += share - 0.5
            wasted_rep += 1 - share
        else:
            wasted_rep += (1 - share) - 0.5
            wasted_dem += share
        total_votes += 1.0
    if len(shares) < 2:
        return None
    eg = (wasted_rep - wasted_dem) / total_votes  # >0 favors DEM
    shares.sort()
    n = len(shares)
    median = shares[n // 2] if n % 2 else (shares[n // 2 - 1] + shares[n // 2]) / 2
    mean = sum(shares) / n
    mm = median - mean  # >0: Dem vote distributed efficiently
    db.execute(
        "INSERT INTO redistricting_fairness_scores(state_fips,congress_number,as_of,efficiency_gap,"
        "mean_median,n_districts) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(state_fips,congress_number,as_of) DO UPDATE SET efficiency_gap=excluded.efficiency_gap, "
        "mean_median=excluded.mean_median, n_districts=excluded.n_districts",
        (state_fips, congress_number, as_of, round(eg, 4), round(mm, 4), n))
    return {"state_fips": state_fips, "congress_number": congress_number,
            "efficiency_gap": round(eg, 4), "mean_median": round(mm, 4), "n_districts": n}


def for_district(district_version_id: int) -> dict | None:
    d = db.query_one("SELECT * FROM congressional_districts WHERE district_version_id=?", (district_version_id,))
    if d is None:
        return None
    row = db.query_one(
        "SELECT * FROM redistricting_fairness_scores WHERE state_fips=? AND congress_number=? "
        "ORDER BY as_of DESC LIMIT 1", (d["state_fips"], d["congress_number"]))
    if row is None:
        computed = compute_state(d["state_fips"], d["congress_number"])
        if computed is None:
            return {"state_fips": d["state_fips"], "congress_number": d["congress_number"],
                    "efficiency_gap": None, "mean_median": None, "n_districts": None,
                    "note": "insufficient district-level history for this plan"}
        return computed
    return dict(row)
