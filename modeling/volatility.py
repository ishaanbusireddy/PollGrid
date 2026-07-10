"""Volatility index: 0-100 composite of poll-movement magnitude, cross-pollster
spread, and news volume. A rolling z-score plus a CUSUM chart run on top of the
composite to separate a real swing from noise (anomaly_flags)."""
from __future__ import annotations

import json
import statistics

from core import db
from core.config import cfg
from core.util import today


def _poll_movement(race_id: int | None, days: int = 14) -> float:
    where, params = ("", []) if race_id is None else ("AND race_id=?", [race_id])
    rows = db.query(
        f"SELECT as_of, avg_pct FROM poll_averages WHERE party_code='DEM' {where} "
        "AND as_of >= date('now', ?) ORDER BY as_of", params + [f"-{days} days"])
    if len(rows) < 2:
        return 0.0
    diffs = [abs(rows[i]["avg_pct"] - rows[i - 1]["avg_pct"]) for i in range(1, len(rows))]
    return sum(diffs) / len(diffs)


def _pollster_spread(race_id: int | None, days: int = 21) -> float:
    where, params = ("", []) if race_id is None else ("AND p.race_id=?", [race_id])
    rows = db.query(
        f"SELECT pr.pct FROM polls p JOIN poll_results pr ON pr.poll_id=p.id "
        f"WHERE pr.party_code='DEM' {where} AND p.field_end >= date('now', ?)",
        params + [f"-{days} days"])
    vals = [r["pct"] for r in rows]
    return statistics.pstdev(vals) if len(vals) >= 2 else 0.0


def _news_volume(race_id: int | None, days: int = 7) -> int:
    where, params = ("", []) if race_id is None else ("AND race_id=?", [race_id])
    row = db.query_one(
        f"SELECT COUNT(*) c FROM extracted_facts WHERE created_at >= datetime('now', ?) {where}",
        [f"-{days} days"] + params)
    return row["c"]


def compute(scope: str = "national", as_of: str | None = None) -> dict:
    as_of = as_of or today()
    race_id = int(scope.split(":")[1]) if scope.startswith("race:") else None
    movement = _poll_movement(race_id)
    spread = _pollster_spread(race_id)
    volume = _news_volume(race_id)
    # bounded contributions: movement 0-40, spread 0-30, volume 0-30
    score = min(40.0, movement * 10) + min(30.0, spread * 6) + min(30.0, volume / (2 if race_id else 20))
    components = {"poll_movement": round(movement, 3), "pollster_spread": round(spread, 3),
                  "news_volume": volume}
    db.execute("INSERT INTO volatility_scores(scope,as_of,score,components_json) VALUES(?,?,?,?) "
               "ON CONFLICT(scope,as_of) DO UPDATE SET score=excluded.score, components_json=excluded.components_json",
               (scope, as_of, round(score, 1), json.dumps(components)))
    _detect_anomalies(scope)
    try:
        from api.websocket import broadcast
        broadcast({"type": "volatility", "payload": {"scope": scope, "score": round(score, 1)}})
    except Exception:
        pass
    return {"scope": scope, "as_of": as_of, "score": round(score, 1), "components": components}


def _detect_anomalies(scope: str) -> None:
    """z-score + CUSUM changepoint detection over the volatility series."""
    window = cfg("volatility.z_window")
    k, h = cfg("volatility.cusum_k"), cfg("volatility.cusum_h")
    rows = db.query("SELECT as_of, score FROM volatility_scores WHERE scope=? ORDER BY as_of DESC LIMIT ?",
                    (scope, window * 3))
    series = [r["score"] for r in reversed(rows)]
    if len(series) < window:
        return
    base = series[-window:]
    mu = statistics.mean(base)
    sd = statistics.pstdev(base) or 1.0
    z = (series[-1] - mu) / sd
    if abs(z) >= 3.0:
        db.execute("INSERT INTO anomaly_flags(scope,as_of,kind,detail) VALUES(?,?,?,?)",
                   (scope, today(), "zscore", f"z={z:.2f} vs {window}-day window"))
    s_pos = s_neg = 0.0
    for x in series[-window:]:
        zi = (x - mu) / sd
        s_pos = max(0.0, s_pos + zi - k)
        s_neg = max(0.0, s_neg - zi - k)
    if s_pos > h or s_neg > h:
        db.execute("INSERT INTO anomaly_flags(scope,as_of,kind,detail) VALUES(?,?,?,?)",
                   (scope, today(), "cusum", f"S+={s_pos:.2f} S-={s_neg:.2f} h={h}"))


def latest(scope: str = "national", as_of: str | None = None) -> dict | None:
    as_of = as_of or today()
    row = db.query_one("SELECT * FROM volatility_scores WHERE scope=? AND as_of<=? ORDER BY as_of DESC LIMIT 1",
                       (scope, as_of))
    if row is None:
        return None
    return {"scope": scope, "as_of": row["as_of"], "score": row["score"],
            "components": json.loads(row["components_json"])}
