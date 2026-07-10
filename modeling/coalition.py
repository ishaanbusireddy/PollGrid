"""Demographic coalition detector: deterministic least-squares regression across
the demographic panel at a race's tier — which variables (education mix, age,
urbanicity, income) explain the most movement across the last cycles. The
number comes from statistics; only the plain-English explanation of it ever
comes from the Analyst."""
from __future__ import annotations

import json

from core import db
from core.util import today

FEATURES = [
    ("education", "bachelors_share"),
    ("population_age", "median_age"),
    ("economic", "median_household_income"),
    ("race_ethnicity", "white_nh_share"),
    ("social_nativity", "foreign_born_share"),
]


def _feature_value(tier: str, entity_id: str, category: str, variable: str) -> float | None:
    base = variable.replace("_share", "")
    row = db.query_one(
        "SELECT value FROM demographics WHERE tier=? AND entity_id=? AND category=? AND variable=? "
        "ORDER BY as_of DESC LIMIT 1", (tier, entity_id, category, base))
    if row is None:
        return None
    if variable.endswith("_share"):
        denom_var = {"education": "pop_25plus", "race_ethnicity": "total_population",
                     "social_nativity": "total_population"}.get(category)
        denom_cat = "population_age" if denom_var == "total_population" else category
        d = db.query_one(
            "SELECT value FROM demographics WHERE tier=? AND entity_id=? AND category=? AND variable=? "
            "ORDER BY as_of DESC LIMIT 1", (tier, entity_id, denom_cat, denom_var))
        if not d or not d["value"]:
            return None
        return row["value"] / d["value"]
    return row["value"]


def _ols(X: list[list[float]], y: list[float]) -> list[float] | None:
    """Least squares via normal equations + Gaussian elimination. k is small."""
    n, k = len(X), len(X[0])
    A = [[sum(X[i][a] * X[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
    B = [sum(X[i][a] * y[i] for i in range(n)) for a in range(k)]
    for a in range(k):
        A[a][a] += 1e-6  # ridge jitter for singular panels
    for col in range(k):
        piv = max(range(col, k), key=lambda r: abs(A[r][col]))
        if abs(A[piv][col]) < 1e-12:
            return None
        A[col], A[piv] = A[piv], A[col]
        B[col], B[piv] = B[piv], B[col]
        for r in range(col + 1, k):
            f = A[r][col] / A[col][col]
            for c in range(col, k):
                A[r][c] -= f * A[col][c]
            B[r] -= f * B[col]
    beta = [0.0] * k
    for r in range(k - 1, -1, -1):
        beta[r] = (B[r] - sum(A[r][c] * beta[c] for c in range(r + 1, k))) / A[r][r]
    return beta


def compute(race_id: int, as_of: str | None = None) -> dict | None:
    """Regress county-level swing (dem margin change across the last two cycles
    of this race's office) on county demographics within the race's state."""
    as_of = as_of or today()
    race = db.query_one("SELECT * FROM races WHERE id=?", (race_id,))
    if race is None or not race["state_fips"]:
        return None
    counties = db.query("SELECT geoid FROM county_equivalents WHERE state_fips=? AND effective_to IS NULL",
                        (race["state_fips"],))
    X, y, used = [], [], []
    for c in counties:
        hist = db.query(
            "SELECT cycle_year, dem_pct, rep_pct FROM political_history WHERE tier='county_equivalent' "
            "AND entity_id=? AND office=? AND dem_pct IS NOT NULL ORDER BY cycle_year DESC LIMIT 2",
            (c["geoid"], race["race_type"]))
        if len(hist) < 2:  # presidential results are the densest county series; honest fallback
            hist = db.query(
                "SELECT cycle_year, dem_pct, rep_pct FROM political_history WHERE tier='county_equivalent' "
                "AND entity_id=? AND office='president' AND dem_pct IS NOT NULL "
                "ORDER BY cycle_year DESC LIMIT 2", (c["geoid"],))
        if len(hist) < 2:
            continue
        swing = (hist[0]["dem_pct"] - hist[0]["rep_pct"]) - (hist[1]["dem_pct"] - hist[1]["rep_pct"])
        feats = [_feature_value("county_equivalent", c["geoid"], cat, var) for cat, var in FEATURES]
        if any(f is None for f in feats):
            continue
        X.append([1.0] + [float(f) for f in feats])
        y.append(swing)
        used.append(c["geoid"])
    if len(X) < len(FEATURES) + 3:
        return None
    beta = _ols(X, y)
    if beta is None:
        return None
    yhat = [sum(b * x for b, x in zip(beta, row)) for row in X]
    ybar = sum(y) / len(y)
    ss_res = sum((yi - yh) ** 2 for yi, yh in zip(y, yhat))
    ss_tot = sum((yi - ybar) ** 2 for yi in y) or 1.0
    r2 = 1 - ss_res / ss_tot
    coefs = {"intercept": round(beta[0], 4)}
    coefs.update({f"{cat}:{var}": round(b, 6) for (cat, var), b in zip(FEATURES, beta[1:])})
    db.execute("INSERT OR IGNORE INTO coalition_models(race_id,as_of,coefficients_json,r2,n) VALUES(?,?,?,?,?)",
               (race_id, as_of, json.dumps(coefs), round(r2, 4), len(X)))
    return {"race_id": race_id, "as_of": as_of, "coefficients": coefs, "r2": round(r2, 4), "n": len(X)}


def latest(race_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM coalition_models WHERE race_id=? ORDER BY as_of DESC LIMIT 1", (race_id,))
    if row is None:
        return None
    return {"race_id": race_id, "as_of": row["as_of"], "coefficients": json.loads(row["coefficients_json"]),
            "r2": row["r2"], "n": row["n"]}
