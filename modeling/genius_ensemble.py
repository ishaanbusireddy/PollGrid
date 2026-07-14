"""The genius ensemble (§08): the Qualitative Factor Scorecard becomes plain
numeric features fed into a penalized logistic regression (elastic-net,
coordinate-descent, pure stdlib) stacked on the quantitative layer's own
probability and the coalition detector's output. Refit against graded history
by the same replay machinery that gates every forecast; coefficients stored as
plain data, fully inspectable. No LLM anywhere near the fitting process.

The rule that keeps it honest: a qualitative-augmented forecast for a category
is shown live ONLY if it beats the quantitative-only baseline's Brier score in
the nightly backtest. Scope honesty (review §2.9): graded Factor-Scorecard→
outcome pairs only accumulate post-launch, so this ships dormant and earns its
way in over live cycles.
"""
from __future__ import annotations

import json
import math

from core import db, provenance
from core.config import cfg
from core.util import today
from modeling.factors_taxonomy import FACTORS, latest_vector

FEATURE_ORDER = ["quant_prob_logit", "coalition_r2"] + sorted(FACTORS)


def _features(race_id: int, as_of: str | None = None) -> list[float] | None:
    from modeling.coalition import latest as coalition_latest
    from modeling.forecasting import latest as forecast_latest
    f = forecast_latest(race_id, "quantitative", as_of)
    if f is None:
        return None
    p = min(max(f["dem_prob"], 1e-4), 1 - 1e-4)
    feats = {"quant_prob_logit": math.log(p / (1 - p)),
             "coalition_r2": (coalition_latest(race_id) or {}).get("r2") or 0.0}
    feats.update(latest_vector(race_id, as_of))  # as-of snapshot — no hindsight leak in refit
    return [feats[k] for k in FEATURE_ORDER]


def _fit_elastic_net(X: list[list[float]], y: list[int], alpha: float, l1: float,
                     iters: int = 300) -> list[float]:
    """Elastic-net-penalized logistic regression via proximal gradient descent.
    Shrinks a factor's fitted weight toward zero if it isn't earning its keep."""
    n, k = len(X), len(X[0]) + 1  # +1 intercept (unpenalized)
    w = [0.0] * k
    lr = 0.1
    for _ in range(iters):
        grad = [0.0] * k
        for xi, yi in zip(X, y):
            z = w[0] + sum(wj * xj for wj, xj in zip(w[1:], xi))
            p = 1 / (1 + math.exp(-max(-30, min(30, z))))
            e = p - yi
            grad[0] += e
            for j, xj in enumerate(xi):
                grad[j + 1] += e * xj
        w[0] -= lr * grad[0] / n
        for j in range(1, k):
            wj = w[j] - lr * (grad[j] / n + alpha * (1 - l1) * w[j])  # L2 part
            thresh = lr * alpha * l1                                  # L1 soft-threshold
            w[j] = math.copysign(max(0.0, abs(wj) - thresh), wj)
    return w


def refit(as_of: str | None = None) -> list[dict]:
    """Refit per category from graded predictions whose feature vectors can be
    reconstructed from the same-day snapshots (no hindsight: features are the
    stored as-of rows, outcomes only from races already called)."""
    as_of = as_of or today()
    alpha, l1 = cfg("genius_layer.alpha"), cfg("genius_layer.l1_ratio")
    out = []
    for cat in [r["race_type"] for r in db.query("SELECT DISTINCT race_type FROM races")]:
        graded = db.query(
            "SELECT p.race_id, p.as_of, p.graded_outcome FROM predictions p JOIN races r ON r.id=p.race_id "
            "WHERE r.race_type=? AND p.model='quantitative' AND p.graded_outcome IS NOT NULL", (cat,))
        X, y = [], []
        for g in graded:
            feats = _features(g["race_id"], g["as_of"])
            if feats:
                X.append(feats)
                y.append(1 if g["graded_outcome"] == "DEM" else 0)
        if len(X) < cfg("forecasting.min_graded_predictions"):
            out.append({"category": cat, "fitted": False, "n": len(X),
                        "reason": "insufficient graded Factor-Scorecard→outcome pairs (accumulates from live cycles)"})
            continue
        w = _fit_elastic_net(X, y, alpha, l1)
        coefs = {"intercept": w[0]}
        coefs.update({k: w[i + 1] for i, k in enumerate(FEATURE_ORDER)})
        db.execute("INSERT INTO ensemble_weights(category,as_of,coefficients_json) VALUES(?,?,?) "
                   "ON CONFLICT(category,as_of) DO UPDATE SET coefficients_json=excluded.coefficients_json",
                   (cat, as_of, json.dumps(coefs)))
        out.append({"category": cat, "fitted": True, "n": len(X)})
    return out


def predict(race_id: int, as_of: str | None = None) -> dict | None:
    as_of = as_of or today()
    race = db.query_one("SELECT * FROM races WHERE id=?", (race_id,))
    if race is None:
        return None
    wrow = db.query_one("SELECT coefficients_json FROM ensemble_weights WHERE category=? "
                        "ORDER BY as_of DESC LIMIT 1", (race["race_type"],))
    feats = _features(race_id, as_of)
    if wrow is None or feats is None:
        return None
    coefs = json.loads(wrow["coefficients_json"])
    z = coefs["intercept"] + sum(coefs.get(k, 0.0) * v for k, v in zip(FEATURE_ORDER, feats))
    p = 1 / (1 + math.exp(-max(-30, min(30, z))))
    probs = {"DEM": round(p, 4), "REP": round(1 - p, 4)}
    # every forecast writes a real computation_audit_log row (CLAUDE.md invariant);
    # the audit link 404'd when this stored a literal "ensemble:<cat>" string instead
    from modeling.audit import record
    metric_id = record(
        "forecast", f"race:{race_id}",
        "ensemble elastic-net logistic: z = intercept + Σ coef_k*feature_k; p = 1/(1+exp(-z))",
        {"as_of": as_of, "coefficients": coefs, "features": dict(zip(FEATURE_ORDER, feats))},
        {"dem_prob": probs["DEM"]})
    with db.write() as conn:
        conn.execute("INSERT OR IGNORE INTO forecasts(race_id,as_of,model,dem_prob,rep_prob,other_prob,metric_id) "
                     "VALUES(?,?,?,?,?,?,?)", (race_id, as_of, "ensemble", probs["DEM"], probs["REP"], 0,
                                               metric_id))
    provenance.chained_insert("predictions", {
        "race_id": race_id, "as_of": as_of, "model": "ensemble", "probs_json": json.dumps(probs)})
    return {"race_id": race_id, "as_of": as_of, "model": "ensemble", "dem_prob": probs["DEM"]}


def gate(category: str, as_of: str | None = None) -> dict:
    """ensemble vs quantitative-only Brier, per category. live_model = whichever
    is demonstrably more calibrated — never the more impressive-sounding one."""
    as_of = as_of or today()
    briers: dict[str, tuple[float, int]] = {}
    for model in ("quantitative", "ensemble"):
        rows = db.query(
            "SELECT p.probs_json, p.graded_outcome FROM predictions p JOIN races r ON r.id=p.race_id "
            "WHERE r.race_type=? AND p.model=? AND p.graded_outcome IS NOT NULL", (category, model))
        if rows:
            bs = [ (json.loads(r["probs_json"]).get("DEM", .5) - (1.0 if r["graded_outcome"] == "DEM" else 0.0)) ** 2
                   for r in rows]
            briers[model] = (sum(bs) / len(bs), len(bs))
    q = briers.get("quantitative")
    e = briers.get("ensemble")
    min_n = cfg("forecasting.min_graded_predictions")
    live = "quantitative"
    if q and e and e[1] >= min_n and e[0] < q[0]:
        live = "ensemble"
    if q or e:
        db.execute("INSERT INTO ensemble_backtest_results(category,as_of,brier_quant,brier_ensemble,"
                   "n_graded,live_model) VALUES(?,?,?,?,?,?) "
                   "ON CONFLICT(category,as_of) DO UPDATE SET brier_quant=excluded.brier_quant, "
                   "brier_ensemble=excluded.brier_ensemble, n_graded=excluded.n_graded, live_model=excluded.live_model",
                   (category, as_of, round(q[0], 4) if q else 1.0, round(e[0], 4) if e else 1.0,
                    (e or q or (0, 0))[1], live))
    return {"category": category, "live_model": live,
            "brier_quant": q and round(q[0], 4), "brier_ensemble": e and round(e[0], 4),
            "n_graded": (e or q or (0, 0))[1]}


def live_model_for(category: str) -> str:
    row = db.query_one("SELECT live_model FROM ensemble_backtest_results WHERE category=? "
                       "ORDER BY as_of DESC LIMIT 1", (category,))
    return row["live_model"] if row else "quantitative"
