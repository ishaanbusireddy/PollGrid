"""Prediction markets (legal: Kalshi) — the forecast/market-divergence signal.
Public market data, keyless. Never a source of fact; a low-tier context signal."""
from __future__ import annotations

import json

from core import db
from core.util import now_iso
from ingestion.http import get_json
from ingestion.scheduler import register


@register("markets")
def run(source: dict) -> None:
    data = get_json(f"{source['url']}/markets", {"limit": 100, "status": "open"})
    rows = []
    for m in data.get("markets", []):
        title = (m.get("title") or "").lower()
        if not any(w in title for w in ("senate", "house", "president", "governor", "election")):
            continue
        rows.append((f"kalshi:{m.get('ticker')}", json.dumps({
            "ticker": m.get("ticker"), "title": m.get("title"),
            "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"),
            "volume": m.get("volume"),
        })))
    with db.write() as conn:
        for key, value in rows:
            conn.execute("INSERT INTO app_meta(key,value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        conn.execute("INSERT INTO app_meta(key,value) VALUES('markets_updated_at',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now_iso(),))


def market_snapshot() -> list[dict]:
    return [json.loads(r["value"]) for r in
            db.query("SELECT value FROM app_meta WHERE key LIKE 'kalshi:%'")]
