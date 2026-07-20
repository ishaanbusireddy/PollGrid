"""Nightly job: the deterministic pipeline end to end + integrity checks.
Runs on a daemon thread from api/server.py; callable directly for tests."""
from __future__ import annotations

import threading
import time
import traceback

from core import db
from core.util import now_iso, today


def _active_races() -> list[int]:
    """Races the nightly deepens: anything polled, competitive, or live."""
    return [r["id"] for r in db.query(
        "SELECT DISTINCT id FROM races WHERE id IN (SELECT race_id FROM poll_averages) "
        "OR competitiveness IN ('tossup','lean') OR status IN ('live','callable')")]


def run(progress=None, item_progress=None) -> dict:
    """progress(step_name, value, elapsed_seconds), called after each step —
    optional live feedback for a caller (e.g. bootstrap_real.py). Several steps
    (factor_scorecards, candidate_stances) can each run for a long time doing
    one local-LLM call per race/factor/candidate — item_progress(step_name,
    done, total) fires after EACH race/candidate within those specific steps,
    so a long cold-start pass isn't just silence until the whole step ends."""
    from modeling import averaging, chamber_simulation, coalition, correlation, district_demographics, \
        district_history, factors_taxonomy, forecasting, fundamentals, genius_ensemble, narrative, \
        pollster_ratings, rhetoric, volatility
    report: dict = {"started": now_iso()}

    def _run_step(name, fn):
        t0 = time.monotonic()
        try:
            report[name] = fn()
        except Exception as e:
            report[name] = f"ERROR {type(e).__name__}: {e}"
            traceback.print_exc()
        if progress:
            progress(name, report[name], time.monotonic() - t0)

    # classify competitiveness FIRST — everything below (active-race selection,
    # PR-wire poll search, FEC's competitive rotation) keys off it, and it's the
    # only signal available before a single real poll has landed
    # derive current-line district presidential leans from real county history
    # FIRST, so House races have a real (derived) partisan_lean when competitiveness
    # classification and fundamentals read it this same run
    _run_step("district_history_derived", district_history.derive_all)
    _run_step("district_demographics_derived", district_demographics.derive_all)
    _run_step("competitiveness_classified", fundamentals.classify_all_competitiveness)
    active = _active_races()

    def _score_factor_scorecards():
        n, done = len(active), 0
        for rid in active:
            if factors_taxonomy.score_race(rid):
                done += 1
            if item_progress:
                item_progress("factor_scorecards", done, n)
        return done

    def _score_candidate_stances():
        cand_ids = [c["id"] for c in db.query(
            "SELECT DISTINCT rc.candidate_id id FROM race_candidates rc "
            "WHERE rc.race_id IN (%s)" % (",".join(map(str, active)) or "NULL"))]
        n, total = len(cand_ids), 0
        for i, cid in enumerate(cand_ids, 1):
            total += rhetoric.score_stances(cid)
            if item_progress:
                item_progress("candidate_stances", i, n)
        return total

    def _refresh_race_narratives():
        # narrative.generate() calls the LLM per race when the cache is stale
        # (every race, on a first real pass) — same shape as factor_scorecards
        n, done = len(active), 0
        for i, rid in enumerate(active, 1):
            if narrative.refresh_cache(rid):
                done += 1
            if item_progress:
                item_progress("race_narratives", i, n)
        return done

    steps = [
        ("pollster_ratings", lambda: pollster_ratings.refresh()),
        ("poll_averages", lambda: averaging.run_all()),
        ("forecasts", lambda: sum(1 for r in db.query(
            "SELECT DISTINCT race_id id FROM poll_averages UNION "
            "SELECT id FROM races WHERE race_type != 'generic_ballot' AND phase='general' LIMIT 600")
            if forecasting.compute(r["id"]))),
        # the genius layer's production loop (audit #14/#15): re-score the factor
        # vector for active races, then produce ensemble forecasts wherever
        # fitted weights exist — the gate decides what is shown, never this step
        ("factor_scorecards", _score_factor_scorecards),
        # coalitions is a pure county-demographics-on-history regression (no polls) — drive it off
        # the competitiveness `active` set like every other deterministic genius step, and run it
        # BEFORE the ensemble so genius_ensemble's coalition_r2 feature is fresh the same night
        ("coalitions", lambda: sum(1 for rid in active if coalition.compute(rid))),
        ("ensemble_forecasts", lambda: sum(1 for rid in active if genius_ensemble.predict(rid))),
        ("candidate_stances", _score_candidate_stances),
        ("race_narratives", _refresh_race_narratives),
        ("grade_predictions", forecasting.grade_predictions),
        ("backtest", lambda: len(forecasting.backtest())),
        ("volatility_national", lambda: volatility.compute("national")["score"]),
        ("volatility_races", lambda: sum(1 for rid in active if volatility.compute(f"race:{rid}"))),
        ("second_order_links", correlation.find_second_order_links),
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
        _run_step(name, fn)
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
