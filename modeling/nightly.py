"""Nightly job: the deterministic pipeline end to end + integrity checks.
Runs on a daemon thread from api/server.py; callable directly for tests."""
from __future__ import annotations

import threading
import traceback

from core import db
from core.util import now_iso, today


def _active_races() -> list[int]:
    """Races the nightly deepens: anything polled, competitive, or live."""
    return [r["id"] for r in db.query(
        "SELECT DISTINCT id FROM races WHERE id IN (SELECT race_id FROM poll_averages) "
        "OR competitiveness IN ('tossup','lean') OR status IN ('live','callable')")]


def run() -> dict:
    from modeling import averaging, chamber_simulation, coalition, correlation, factors_taxonomy, \
        forecasting, fundamentals, genius_ensemble, narrative, pollster_ratings, rhetoric, volatility
    report: dict = {"started": now_iso()}
    # classify competitiveness FIRST — everything below (active-race selection,
    # PR-wire poll search, FEC's competitive rotation) keys off it, and it's the
    # only signal available before a single real poll has landed
    try:
        report["competitiveness_classified"] = fundamentals.classify_all_competitiveness()
    except Exception as e:
        report["competitiveness_classified"] = f"ERROR {type(e).__name__}: {e}"
        traceback.print_exc()
    active = _active_races()
    steps = [
        ("pollster_ratings", lambda: pollster_ratings.refresh()),
        ("poll_averages", lambda: averaging.run_all()),
        ("forecasts", lambda: sum(1 for r in db.query(
            "SELECT DISTINCT race_id id FROM poll_averages UNION "
            "SELECT id FROM races WHERE race_type != 'generic_ballot' LIMIT 600")
            if forecasting.compute(r["id"]))),
        # the genius layer's production loop (audit #14/#15): re-score the factor
        # vector for active races, then produce ensemble forecasts wherever
        # fitted weights exist — the gate decides what is shown, never this step
        ("factor_scorecards", lambda: sum(1 for rid in active if factors_taxonomy.score_race(rid))),
        ("ensemble_forecasts", lambda: sum(1 for rid in active if genius_ensemble.predict(rid))),
        ("candidate_stances", lambda: sum(rhetoric.score_stances(c["id"]) for c in db.query(
            "SELECT DISTINCT rc.candidate_id id FROM race_candidates rc "
            "WHERE rc.race_id IN (%s)" % (",".join(map(str, active)) or "NULL")))),
        ("race_narratives", lambda: sum(1 for rid in active if narrative.refresh_cache(rid))),
        ("grade_predictions", forecasting.grade_predictions),
        ("backtest", lambda: len(forecasting.backtest())),
        ("volatility_national", lambda: volatility.compute("national")["score"]),
        ("volatility_races", lambda: sum(1 for rid in active if volatility.compute(f"race:{rid}"))),
        ("second_order_links", correlation.find_second_order_links),
        ("coalitions", lambda: sum(1 for r in db.query(
            "SELECT DISTINCT race_id id FROM poll_averages") if coalition.compute(r["id"]))),
        ("chamber_senate", lambda: (chamber_simulation.run("senate") or {}).get("dem_control_prob")),
        ("chamber_house", lambda: (chamber_simulation.run("house") or {}).get("dem_control_prob")),
        ("chamber_ec", lambda: (chamber_simulation.run("ec") or {}).get("dem_control_prob")),
        ("ensemble_refit", lambda: genius_ensemble.refit()),
        ("ensemble_gates", lambda: [genius_ensemble.gate(c["race_type"]) for c in
                                    db.query("SELECT DISTINCT race_type FROM races")]),
        ("daily_briefing", lambda: (__import__("modeling.briefing", fromlist=["generate"]).generate() or {}).get("model")),
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
