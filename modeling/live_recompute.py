"""Continuous fast recompute — the deterministic (NO-LLM) analytics that keep the
app fresh between the once-a-day LLM nightly, WITHOUT making it laggy.

Two cadences so the UI never stalls:
  - LIGHT pass (frequent): competitiveness + poll averages + forecasts for the
    handful of ACTIVE races. Sub-second to a couple seconds.
  - HEAVY pass (rare): district derivations, a full forecast sweep, coalitions,
    and the 3 chamber Monte-Carlo simulations (20k draws each — the real CPU
    cost). Runs at most every ~30 min AND only when new data has landed, so it
    never hammers the machine on a 10-minute drumbeat the way v4.4 did.

Strictly deterministic: imports nothing from the analyst LLM layer; every number
it writes the nightly would write identically."""
from __future__ import annotations

import threading
import time
import traceback

from core import db
from core.config import cfg
from core.util import now_iso


def _safe(report: dict, name: str, fn):
    try:
        report[name] = fn()
    except Exception as e:
        report[name] = f"ERROR {type(e).__name__}: {e}"
        traceback.print_exc()


def _active_general_races() -> list[int]:
    return [r["id"] for r in db.query(
        "SELECT DISTINCT id FROM races WHERE phase='general' AND ("
        "id IN (SELECT race_id FROM poll_averages) OR competitiveness IN ('tossup','lean') "
        "OR status IN ('live','callable'))")]


def light_pass() -> dict:
    """Cheap, safe-to-run-often refresh: competitiveness, poll averages, and
    forecasts for ACTIVE races only. No chamber sims, no full sweep."""
    from modeling import averaging, forecasting, fundamentals
    report: dict = {"kind": "light"}
    _safe(report, "competitiveness", fundamentals.classify_all_competitiveness)
    _safe(report, "poll_averages", averaging.run_all)
    _safe(report, "forecasts_active",
          lambda: sum(1 for rid in _active_general_races() if forecasting.compute(rid)))
    db.meta_set("live_recompute_at", now_iso())
    return report


def heavy_pass() -> dict:
    """Expensive full refresh — district derivations, a full forecast sweep,
    coalitions, and the 3 chamber simulations. Rare cadence so it never lags."""
    from modeling import (averaging, chamber_simulation, coalition, district_demographics,
                          district_history, forecasting, fundamentals)
    report: dict = {"kind": "heavy"}
    _safe(report, "district_history", district_history.derive_all)
    _safe(report, "district_demographics", district_demographics.derive_all)
    _safe(report, "competitiveness", fundamentals.classify_all_competitiveness)
    _safe(report, "poll_averages", averaging.run_all)
    _safe(report, "forecasts", lambda: sum(1 for r in db.query(
        "SELECT DISTINCT race_id id FROM poll_averages UNION "
        "SELECT id FROM races WHERE race_type != 'generic_ballot' AND phase='general' LIMIT 600")
        if forecasting.compute(r["id"])))
    _safe(report, "coalitions", lambda: sum(1 for r in db.query(
        "SELECT DISTINCT id FROM races WHERE competitiveness IN ('tossup','lean')")
        if coalition.compute(r["id"])))
    _safe(report, "chamber_senate", lambda: (chamber_simulation.run("senate") or {}).get("dem_control_prob"))
    _safe(report, "chamber_house", lambda: (chamber_simulation.run("house") or {}).get("dem_control_prob"))
    _safe(report, "chamber_ec", lambda: (chamber_simulation.run("ec") or {}).get("dem_control_prob"))
    db.meta_set("live_recompute_at", now_iso())
    db.meta_set("live_heavy_at", now_iso())
    return report


def _data_watermark() -> str:
    """Cheap fingerprint of 'has new data landed?' — max raw_item id + poll count.
    Lets the heavy pass skip entirely when nothing has changed since last time."""
    r = db.query_one("SELECT COALESCE(MAX(id),0) m FROM raw_items")["m"]
    p = db.query_one("SELECT COUNT(*) c FROM polls")["c"]
    return f"{r}:{p}"


def _broadcast() -> None:
    try:  # nudge open pages to re-color the map with fresh numbers
        from api.websocket import broadcast
        broadcast({"type": "recompute", "payload": {"at": db.meta_get("live_recompute_at")}})
    except Exception:
        pass


def start_thread(stop_event: threading.Event) -> threading.Thread:
    """Light pass every `live_recompute_seconds`; heavy pass at boot and then at
    most every `live_recompute_heavy_seconds`, skipped when no new data landed.
    Waits on the shared stop-event so shutdown is immediate."""
    light_iv = cfg("ingestion.live_recompute_seconds")
    heavy_iv = cfg("ingestion.live_recompute_heavy_seconds")

    def loop():
        stop_event.wait(20)  # let boot + first ingestion settle
        last_heavy = 0.0
        last_watermark = None
        while not stop_event.is_set():
            try:
                now = time.monotonic()
                wm = _data_watermark()
                if now - last_heavy >= heavy_iv and (last_heavy == 0.0 or wm != last_watermark):
                    heavy_pass()
                    last_heavy = now
                    last_watermark = wm
                else:
                    light_pass()
                _broadcast()
            except Exception:
                traceback.print_exc()
            stop_event.wait(light_iv)

    t = threading.Thread(target=loop, daemon=True, name="live-recompute")
    t.start()
    return t


# Back-compat shim: bootstrap_real / tests may call fast_pipeline(); the heavy pass
# is the full deterministic sweep it expects.
def fast_pipeline() -> dict:
    return heavy_pass()


def run_once() -> dict:
    report = heavy_pass()
    _broadcast()
    return report
