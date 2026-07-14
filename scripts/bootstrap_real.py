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


def bootstrap_real(fec_pages: int = 40) -> None:
    from core import db

    _step("(a) platform bootstrap (config, schema, geography, races, sources)")
    from api.server import bootstrap
    bootstrap(start_ingestion=False)

    _step("(b) national presidential history (hand-seeded, cited)")
    import scripts.backfill_history as backfill_history
    print(f"imported {backfill_history.run()} new national presidential rows")

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

    _step("(i) deterministic nightly pipeline")
    from modeling import nightly
    import time as _time

    _item_t0 = {}

    def _live(name, value, elapsed):
        v = len(value) if isinstance(value, list) else value
        print(f"  {name}: {v}  ({elapsed:.1f}s)", flush=True)

    def _live_item(step, done, total):
        # this is what makes a long cold-start pass (factor_scorecards,
        # candidate_stances) visibly alive instead of silent for hours —
        # one line per race/candidate scored, with a running rate/ETA
        t0 = _item_t0.setdefault(step, _time.monotonic())
        elapsed = _time.monotonic() - t0
        rate = elapsed / done if done else 0
        eta = rate * (total - done)
        print(f"    {step}: {done}/{total}  ({elapsed:.0f}s elapsed, ~{eta:.0f}s remaining)", flush=True)

    report = nightly.run(progress=_live, item_progress=_live_item)

    _step("(j) row counts")
    print_summary()


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
    args = ap.parse_args()
    bootstrap_real(fec_pages=args.fec_pages)
    print("\nbootstrap_real complete — safe to re-run any time.")


if __name__ == "__main__":
    main()
