"""Economic indicators — the fundamentals model's economic_index input, real.

BLS public API v2 (keyless: 25 req/day; BLS_API_KEY raises the ceiling).
Series LNS14000000 = seasonally-adjusted national unemployment rate. The
index is the 6-month unemployment trend expressed as retrospective-voting
direction: an improving economy favors the sitting president's party, so the
stored value is DEM-oriented via app_meta['president_party'] and clamped to
[-1, 1]. The full computation is recorded alongside it for the audit trail.
"""
from __future__ import annotations

import json as _json
import os
import urllib.request

from core import db
from core.util import now_iso
from ingestion.scheduler import register

BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
SERIES = "LNS14000000"  # unemployment rate, seasonally adjusted
TREND_SCALE = 1.0       # 1 pt of 6-month unemployment change == full-scale signal


@register("economics")
def run(source: dict) -> None:
    payload: dict = {"seriesid": [SERIES]}
    key = os.environ.get(source["api_key_env"] or "")
    if key:
        payload["registrationkey"] = key
    req = urllib.request.Request(
        BLS_URL, data=_json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "PollGrid/1.0"})
    from ingestion.http import FetchError
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read().decode())
    except Exception as e:
        raise FetchError(f"{type(e).__name__}: {e}") from e
    series = (data.get("Results") or {}).get("series") or []
    points = series[0].get("data") if series else None
    if not points:
        raise FetchError(f"BLS returned no data: {str(data)[:200]}")

    monthly = [p for p in points if p.get("period", "").startswith("M")][:7]
    if len(monthly) < 2:
        raise FetchError("fewer than two monthly unemployment observations")
    latest = float(monthly[0]["value"])
    oldest = float(monthly[-1]["value"])
    trend = latest - oldest                         # + = unemployment rising
    pres_favor = max(-1.0, min(1.0, -trend / TREND_SCALE))  # improving economy favors the incumbent party
    pres_party = db.meta_get("president_party", "")
    dem_oriented = pres_favor if pres_party == "DEM" else (-pres_favor if pres_party == "REP" else 0.0)

    db.meta_set("economic_index", str(round(dem_oriented, 4)))
    db.meta_set("economic_index_detail", _json.dumps({
        "series": SERIES, "latest": latest, "oldest_in_window": oldest,
        "six_month_trend_pts": round(trend, 2), "president_party": pres_party,
        "formula": "dem_oriented = clamp(-(trend)/1.0) oriented by president_party",
        "as_of": now_iso(),
    }))
