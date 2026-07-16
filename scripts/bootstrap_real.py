#!/usr/bin/env python3
"""One-command real-data populator — run this on YOUR machine (full network);
the platform then has real geography, national + battleground history, Census
demographics, the FEC roster + finance/ad-spend, OpenElections county results,
VoteView ideology, and a first news pass, with the deterministic pipeline run
over all of it.

Every step is idempotent (INSERT OR IGNORE / meta-gated) and every step that
needs the network degrades to a WARNING instead of aborting the run — re-run
the script after fixing keys/connectivity and it picks up where it left off.

No synthetic rows are created here, ever. Demo data is scripts/seed_demo.py;
this is the real thing.

Usage: python scripts/bootstrap_real.py [--fec-pages 40]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _source(source_type: str) -> dict | None:
    from core import db
    return db.query_one("SELECT * FROM sources WHERE source_type=?", (source_type,))


def _step(label: str):
    print(f"\n=== {label} ===")


def _warn(label: str, exc: Exception) -> None:
    print(f"WARNING: {label} failed ({type(exc).__name__}: {exc}) — continuing")


def bootstrap_real(fec_pages: int = 40, with_nightly: bool = False) -> None:
    from core import db

    _step("(a) platform bootstrap (config, schema, geography, races, sources)")
    from api.server import bootstrap
    bootstrap(start_ingestion=False)

    _step("(b) national presidential history (hand-seeded, cited)")
    import scripts.backfill_history as backfill_history
    print(f"imported {backfill_history.run()} new national presidential rows")

    _step("(b2) state presidential toplines 2020+2024 (hand-seeded, transcribed — zero network)")
    import scripts.seed_state_presidentials as ssp
    print(f"seeded {ssp.run()} state presidential rows (confidence='uncertain'; any official "
          f"import supersedes them automatically)")

    _step("(c) Census ACS bootstrap (nation + every state's counties/districts)")
    from ingestion.http import BudgetExhausted, FetchError, SourceNotConfigured
    src = _source("census")
    if src is None:
        print("WARNING: no census source row — skipping")
    else:
        try:
            from ingestion import census
            census.run(src)
            print(f"census bootstrap done: {db.meta_get('census_bootstrap_done', '(pending)')}")
        except (FetchError, BudgetExhausted, SourceNotConfigured) as e:
            _warn("census", e)

    _step(f"(d) FEC candidate roster (up to {fec_pages} pages or full sync)")
    src = _source("fec")
    if src is None:
        print("WARNING: no fec source row — skipping")
    elif db.meta_get("fec_roster_synced_at"):
        print(f"roster already synced at {db.meta_get('fec_roster_synced_at')} — skipping")
    else:
        from ingestion import fec
        for i in range(fec_pages):
            try:
                fec.run(src)
            except (FetchError, BudgetExhausted, SourceNotConfigured) as e:
                _warn(f"fec page {i + 1}", e)
                break
            if db.meta_get("fec_roster_synced_at"):
                break
            time.sleep(1)  # stay polite on DEMO_KEY rate limits
        synced = db.meta_get("fec_roster_synced_at")
        print(f"fec roster synced: {synced or 'INCOMPLETE — re-run to resume (cursor kept)'}")

    _step("(e) OpenElections tier-2 sync (configured states/cycles)")
    src = _source("results_openelections")
    states: list[str] = []
    if src is None:
        print("WARNING: no results_openelections source row — skipping")
    else:
        states = (json.loads(src["config_json"] or "{}")).get("states") or []
        try:
            from ingestion import results_tiers
            results_tiers.run_tier2(src)
            print("tier-2 sync pass complete")
        except Exception as e:
            _warn("openelections tier 2", e)

    _step("(f) state-level presidential history from OpenElections raw CSVs")
    from scripts.backfill_president_states import DEFAULT_CYCLES, DEFAULT_STATES
    from scripts.backfill_president_states import run as president_run
    try:
        totals = president_run(states or DEFAULT_STATES, DEFAULT_CYCLES)
        print(f"president backfill: {totals['county_rows']} county rows, "
              f"{totals['state_rows']} state rows")
    except Exception as e:
        _warn("backfill_president_states", e)

    _step("(f2) derive current-line district partisan leans + demographics from county data")
    try:
        from modeling.district_history import derive_all as derive_hist
        n_rows = derive_hist()
        print(f"district history derived: {n_rows} district-cycle rows "
              f"(confidence='derived', population-weighted areal interpolation)")
    except Exception as e:
        _warn("district_history", e)
    try:
        from modeling.district_demographics import derive_all as derive_demo
        n_demo = derive_demo()
        print(f"district demographics derived: {n_demo} rows "
              f"(confidence='derived', areal apportionment of county Census counts)")
    except Exception as e:
        _warn("district_demographics", e)

    _step("(g) VoteView DW-NOMINATE ideology backfill")
    try:
        from scripts.backfill_voteview import run as voteview_run
        stats = voteview_run()
        print(f"voteview: {stats['matched_candidates']} candidates matched, "
              f"{stats['unmatched_member_rows']} member rows unmatched")
    except Exception as e:
        _warn("backfill_voteview", e)

    _step("(h) one targeted-search pass (race-profile news hunt)")
    src = _source("targeted_search")
    if src is None:
        print("WARNING: no targeted_search source row — skipping")
    else:
        try:
            from ingestion import targeted_search
            targeted_search.run(src)
            print("targeted search pass complete")
        except Exception as e:
            _warn("targeted_search", e)

    # ---- data-health readout BEFORE anything slow: what actually landed, loudly ----
    _step("(i) data health — what the fast steps actually landed")
    print_summary()
    _print_foundation_verdict()

    if with_nightly:
        _step("(j) nightly pipeline WITH LLM factor scoring (--with-nightly)")
        _run_nightly()
    else:
        _step("(j) fast deterministic pipeline (no LLM — re-run with --with-nightly for factor scoring)")
        _run_fast_pipeline()

    _step("(k) final row counts")
    print_summary()
    print("\nNOTE: polls are NOT ingested by this script — they arrive continuously while")
    print("the server runs (python run.py). The LLM factor scorecards run nightly in the")
    print("background on the server, or on demand via --with-nightly here.")


def _print_foundation_verdict() -> None:
    """A loud verdict on the data foundation, printed BEFORE any slow step —
    so a blank map is never a mystery hidden behind an hours-long LLM pass."""
    from core import db
    st = db.query_one("SELECT COUNT(*) c FROM political_history WHERE tier='state' AND is_synthetic=0")["c"]
    cty = db.query_one("SELECT COUNT(*) c FROM political_history WHERE tier='county_equivalent' AND is_synthetic=0")["c"]
    demo = db.query_one("SELECT COUNT(*) c FROM demographics WHERE tier='state' AND is_synthetic=0")["c"]
    problems = []
    if st == 0:
        problems.append("NO state-level election history -> state map paints neutral everywhere")
    if cty == 0:
        problems.append("NO county-level results -> county map blank, NO derived district leans (House map neutral)")
    if demo == 0:
        problems.append("NO demographics -> all demographic map layers blank, coalition + 4 scorecard factors dead "
                        "(usually a Census fetch failure — check the warnings above / CENSUS_API_KEY)")
    if problems:
        print("\n*** DATA FOUNDATION PROBLEMS — this is WHY things look empty: ***")
        for p in problems:
            print(f"  ! {p}")
        print("Fix the cause (keys/connectivity), re-run this script; it resumes where it left off.")
    else:
        print("\ndata foundation OK: state history, county results, and demographics all present.")


def _run_fast_pipeline() -> None:
    """The deterministic, no-LLM subset of the nightly: everything that makes the app
    usable in MINUTES (derived district data, competitiveness, averages, forecasts,
    chamber sims). The LLM-heavy factor/narrative scoring is deliberately excluded —
    it runs on the server's own nightly thread, or here via --with-nightly."""
    from modeling import (averaging, chamber_simulation, coalition, district_demographics,
                          district_history, forecasting, fundamentals)
    from core import db
    steps = [
        ("district_history_derived", district_history.derive_all),
        ("district_demographics_derived", district_demographics.derive_all),
        ("competitiveness_classified", fundamentals.classify_all_competitiveness),
        ("poll_averages", averaging.run_all),
        ("forecasts", lambda: sum(1 for r in db.query(
            "SELECT id FROM races WHERE race_type != 'generic_ballot' LIMIT 600")
            if forecasting.compute(r["id"]))),
        ("coalitions", lambda: sum(1 for r in db.query(
            "SELECT DISTINCT id FROM races WHERE competitiveness IN ('tossup','lean')")
            if coalition.compute(r["id"]))),
        ("chamber_senate", lambda: (chamber_simulation.run("senate") or {}).get("dem_control_prob")),
        ("chamber_house", lambda: (chamber_simulation.run("house") or {}).get("dem_control_prob")),
        ("chamber_ec", lambda: (chamber_simulation.run("ec") or {}).get("dem_control_prob")),
    ]
    import time as _time
    for name, fn in steps:
        t0 = _time.monotonic()
        try:
            v = fn()
            print(f"  {name}: {v if not isinstance(v, list) else len(v)}  ({_time.monotonic()-t0:.1f}s)", flush=True)
        except Exception as e:
            _warn(name, e)


def _run_nightly() -> None:
    import statistics
    from collections import deque
    from modeling import nightly
    import time as _time

    _item_windows: dict = {}

    def _live(name, value, elapsed):
        v = len(value) if isinstance(value, list) else value
        print(f"  {name}: {v}  ({elapsed:.1f}s)", flush=True)

    def _live_item(step, done, total):
        # one line per race/candidate scored — what makes a long cold-start pass
        # (factor_scorecards, candidate_stances) visibly alive instead of silent
        # for hours. ETA uses the MEDIAN of the last 20 items' pace, not a mean —
        # a single slow outlier barely moves a median.
        now = _time.monotonic()
        win = _item_windows.setdefault(step, {"start": now, "last": now, "gaps": deque(maxlen=20)})
        win["gaps"].append(now - win["last"])
        win["last"] = now
        elapsed = now - win["start"]
        typical = statistics.median(win["gaps"])
        eta = typical * (total - done)
        print(f"    {step}: {done}/{total}  ({elapsed:.0f}s elapsed, ~{eta:.0f}s remaining "
              f"@ typical pace)", flush=True)

    nightly.run(progress=_live, item_progress=_live_item)


def print_summary() -> None:
    from core import db
    rows: list[tuple[str, int]] = []
    for table in ("races", "candidates", "polls", "ad_spend", "raw_items"):
        rows.append((table, db.query_one(f"SELECT COUNT(*) c FROM {table}")["c"]))
    for r in db.query("SELECT tier, COUNT(*) c FROM political_history GROUP BY tier ORDER BY tier"):
        rows.append((f"political_history[{r['tier']}]", r["c"]))
    for r in db.query("SELECT tier, COUNT(*) c FROM demographics GROUP BY tier ORDER BY tier"):
        rows.append((f"demographics[{r['tier']}]", r["c"]))
    width = max(len(name) for name, _ in rows)
    print(f"{'table':<{width}}  rows")
    print(f"{'-' * width}  {'-' * 8}")
    for name, count in rows:
        print(f"{name:<{width}}  {count:>8}")
    synth = db.query_one("SELECT COUNT(*) c FROM polls WHERE is_synthetic=1")["c"]
    if synth:
        print(f"\nnote: {synth} synthetic poll(s) present — scripts/purge_synthetic.py removes them")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fec-pages", type=int, default=40,
                    help="max FEC roster pages per run (default 40)")
    ap.add_argument("--with-nightly", action="store_true",
                    help="also run the LLM-heavy factor/narrative scoring (can take hours on a "
                         "local model; by default that runs on the server's own nightly thread)")
    args = ap.parse_args()
    bootstrap_real(fec_pages=args.fec_pages, with_nightly=args.with_nightly)
    print("\nbootstrap_real complete — safe to re-run any time.")


if __name__ == "__main__":
    main()
