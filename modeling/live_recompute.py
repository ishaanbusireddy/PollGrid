"""Continuous fast recompute — the deterministic (NO-LLM) analytics loop that runs
every few minutes so poll averages, forecasts, competitiveness, derived district
leans, and chamber sims move as raw data lands, instead of sitting still until the
once-a-day LLM nightly. It broadcasts a 'recompute' websocket frame each pass so
open pages can refresh.

Strictly deterministic: it imports nothing from the analyst LLM layer and produces
only computed numbers, every one of which the nightly would produce identically.
The heavy language-model steps (factor scorecards, candidate stances, narratives)
stay in modeling/nightly.py and run once per day."""
from __future__ import annotations

import threading
import time
import traceback

from core import db
from core.config import cfg
from core.util import now_iso


def fast_pipeline() -> dict:
    """The deterministic subset of the nightly — seconds, not hours. Safe to run on
    a short interval (idempotent upserts under the global write lock). Returns a
    per-step report; a failing step is recorded and skipped, never fatal."""
    from modeling import (averaging, chamber_simulation, coalition, district_demographics,
                          district_history, forecasting, fundamentals)
    report: dict = {}

    def _step(name, fn):
        try:
            report[name] = fn()
        except Exception as e:
            report[name] = f"ERROR {type(e).__name__}: {e}"
            traceback.print_exc()

    # derive district leans first so House competitiveness/fundamentals read them
    _step("district_history", district_history.derive_all)
    _step("district_demographics", district_demographics.derive_all)
    _step("competitiveness", fundamentals.classify_all_competitiveness)
    _step("poll_averages", averaging.run_all)
    _step("forecasts", lambda: sum(1 for r in db.query(
        "SELECT DISTINCT race_id id FROM poll_averages UNION "
        "SELECT id FROM races WHERE race_type != 'generic_ballot' AND phase='general' LIMIT 600")
        if forecasting.compute(r["id"])))
    _step("coalitions", lambda: sum(1 for r in db.query(
        "SELECT DISTINCT id FROM races WHERE competitiveness IN ('tossup','lean')")
        if coalition.compute(r["id"])))
    _step("chamber_senate", lambda: (chamber_simulation.run("senate") or {}).get("dem_control_prob"))
    _step("chamber_house", lambda: (chamber_simulation.run("house") or {}).get("dem_control_prob"))
    _step("chamber_ec", lambda: (chamber_simulation.run("ec") or {}).get("dem_control_prob"))
    db.meta_set("live_recompute_at", now_iso())
    return report


def run_once() -> dict:
    report = fast_pipeline()
    try:  # tell open pages to refresh their (now possibly-changed) numbers
        from api.websocket import broadcast
        broadcast({"type": "recompute", "payload": {"at": db.meta_get("live_recompute_at")}})
    except Exception:
        pass
    return report


def start_thread(stop_event: threading.Event) -> threading.Thread:
    """Daemon loop on the config cadence; waits on the shared stop-event so shutdown
    is immediate, never a bare sleep."""
    interval = cfg("ingestion.live_recompute_seconds")

    def loop():
        stop_event.wait(20)  # let the boot seed + first ingestion settle
        while not stop_event.is_set():
            try:
                run_once()
            except Exception:
                traceback.print_exc()
            stop_event.wait(interval)

    t = threading.Thread(target=loop, daemon=True, name="live-recompute")
    t.start()
    return t
