#!/usr/bin/env python3
"""One command removes every trace of synthetic data (non-negotiable #8).

Chained tables (polls, extracted_facts, predictions) are append-only for real
data; purging synthetic rows is the ONE sanctioned chain rewrite. The chain is
rebuilt atomically over the surviving rows and the rewrite is recorded in
app_meta (purge_synthetic_at) so verification history stays honest.

Derived snapshots (averages, forecasts, simulations, factor scores, coalition
models, volatility) for races that carried synthetic inputs are deleted too —
recompute from real data afterwards via the nightly job.

Usage: python scripts/purge_synthetic.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db, provenance  # noqa: E402
from core.util import now_iso  # noqa: E402


def _rebuild_chain(conn, table: str) -> None:
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    prev = provenance.GENESIS
    for r in rows:
        r = dict(r)
        h = provenance.row_hash(table, r, prev)
        conn.execute(f"UPDATE {table} SET row_hash=?, prev_hash=? WHERE id=?", (h, prev, r["id"]))
        prev = h
    conn.execute("INSERT INTO app_meta(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (f"chain_head:{table}", prev if rows else provenance.GENESIS))


def main() -> None:
    db.migrate()
    with db.write() as conn:
        synth_races = [r["id"] for r in conn.execute(
            "SELECT DISTINCT race_id id FROM polls WHERE is_synthetic=1").fetchall()]
        placeholders = ",".join("?" * len(synth_races)) or "NULL"

        counts = {}
        conn.execute("DELETE FROM poll_results WHERE poll_id IN (SELECT id FROM polls WHERE is_synthetic=1)")
        conn.execute("DELETE FROM story_facts WHERE fact_id IN "
                     "(SELECT id FROM extracted_facts WHERE is_synthetic=1)")
        conn.execute("DELETE FROM article_entity_links WHERE raw_item_id IN "
                     "(SELECT id FROM raw_items WHERE is_synthetic=1)")
        for table in ("polls", "extracted_facts", "raw_items", "stories", "results_live",
                      "demographics", "political_history"):
            cur = conn.execute(f"DELETE FROM {table} WHERE is_synthetic=1")
            counts[table] = cur.rowcount
        for dep in ("race_candidates", "donors_aggregated", "topic_stance_scores", "ideology_scores",
                    "officeholders"):
            col = "candidate_id"
            conn.execute(f"DELETE FROM {dep} WHERE {col} IN (SELECT id FROM candidates WHERE is_synthetic=1)")
        cur = conn.execute("DELETE FROM candidates WHERE is_synthetic=1")
        counts["candidates"] = cur.rowcount

        if synth_races:
            for table in ("poll_averages", "fundamentals_snapshots", "forecasts", "coalition_models",
                          "qualitative_factor_scores"):
                conn.execute(f"DELETE FROM {table} WHERE race_id IN ({placeholders})", synth_races)
            conn.execute(f"DELETE FROM predictions WHERE race_id IN ({placeholders})", synth_races)
            conn.execute("DELETE FROM volatility_scores WHERE scope IN (%s)"
                         % ",".join(f"'race:{r}'" for r in synth_races))
        conn.execute("DELETE FROM chamber_simulations")  # derived over the whole field; recompute

        for table in provenance.CHAINS:
            _rebuild_chain(conn, table)
        conn.execute("INSERT INTO app_meta(key,value) VALUES('purge_synthetic_at',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now_iso(),))

    for table, n in counts.items():
        print(f"purged {n:6d} rows from {table}")
    for table, ok, detail in provenance.verify_all():
        print(("OK " if ok else "FAIL ") + detail)


if __name__ == "__main__":
    main()
