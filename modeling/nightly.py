"""Nightly job: the deterministic pipeline end to end + integrity checks.
Runs on a daemon thread from api/server.py; callable directly for tests."""
from __future__ import annotations

import threading
import traceback

from core import db
from core.util import now_iso, today


def run() -> dict:
    from modeling import averaging, chamber_simulation, coalition, correlation, forecasting, \
        genius_ensemble, pollster_ratings, volatility
    report: dict = {"started": now_iso()}
    steps = [
        ("pollster_ratings", lambda: pollster_ratings.refresh()),
        ("poll_averages", lambda: averaging.run_all()),
        ("forecasts", lambda: sum(1 for r in db.query(
            "SELECT DISTINCT race_id id FROM poll_averages UNION "
            "SELECT id FROM races WHERE race_type != 'generic_ballot' LIMIT 600")
            if forecasting.compute(r["id"]))),
        ("grade_predictions", forecasting.grade_predictions),
        ("backtest", lambda: len(forecasting.backtest())),
        ("volatility_national", lambda: volatility.compute("national")["score"]),
        ("second_order_links", correlation.find_second_order_links),
        ("coalitions", lambda: sum(1 for r in db.query(
            "SELECT DISTINCT race_id id FROM poll_averages") if coalition.compute(r["id"]))),
        ("chamber_senate", lambda: (chamber_simulation.run("senate") or {}).get("dem_control_prob")),
        ("chamber_house", lambda: (chamber_simulation.run("house") or {}).get("dem_control_prob")),
        ("ensemble_refit", lambda: genius_ensemble.refit()),
        ("ensemble_gates", lambda: [genius_ensemble.gate(c["race_type"]) for c in
                                    db.query("SELECT DISTINCT race_type FROM races")]),
        ("integrity", db.run_integrity_checks),
    ]
    for name, fn in steps:
        try:
            report[name] = fn()
        except Exception as e:
            report[name] = f"ERROR {type(e).__name__}: {e}"
            traceback.print_exc()
    report["finished"] = now_iso()
    db.meta_set(f"nightly:{today()}", now_iso())
    return report


def start_thread(stop_event: threading.Event, interval_hours: float = 24.0) -> threading.Thread:
    def loop():
        stop_event.wait(60)  # let ingestion warm up after boot
        while not stop_event.is_set():
            if db.meta_get(f"nightly:{today()}") is None:
                run()
            stop_event.wait(interval_hours * 3600 / 24)  # check hourly, run once per day
    t = threading.Thread(target=loop, daemon=True, name="nightly")
    t.start()
    return t
