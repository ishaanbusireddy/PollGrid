"""Hash-chained provenance. Every row in a chained table links to the previous
row's hash; verify_chain() proves our copy wasn't altered after being written.
It does not — and never claims to — prove the underlying poll or statement was true.

Appends are serialized by core.db's single writer lock; the chain head is cached
in app_meta for O(1) appends.
"""
from __future__ import annotations

import hashlib
import json

from core import db

GENESIS = "0" * 64

# chained table -> ordered content columns hashed into row_hash
CHAINS: dict[str, list[str]] = {
    "polls": ["pollster_id", "race_id", "field_start", "field_end", "sample_size",
              "population", "moe", "release_url", "created_at"],
    "extracted_facts": ["raw_item_id", "category", "summary", "entities_json",
                        "race_id", "state_fips", "occurred_at", "created_at"],
    "predictions": ["race_id", "as_of", "model", "probs_json"],
}


def _canonical(table: str, values: dict) -> str:
    return json.dumps({c: values.get(c) for c in CHAINS[table]}, sort_keys=True,
                      separators=(",", ":"), default=str)


def row_hash(table: str, values: dict, prev_hash: str) -> str:
    return hashlib.sha256((_canonical(table, values) + "|" + prev_hash).encode()).hexdigest()


def _head_key(table: str) -> str:
    return f"chain_head:{table}"


def chained_insert(table: str, values: dict) -> int:
    """Insert one row into a chained table. Must be the ONLY way rows enter these
    tables. Runs inside the global writer lock, so the chain stays linear."""
    with db.write() as conn:
        row = conn.execute("SELECT value FROM app_meta WHERE key=?", (_head_key(table),)).fetchone()
        prev = row["value"] if row else GENESIS
        h = row_hash(table, values, prev)
        cols = list(values.keys()) + ["row_hash", "prev_hash"]
        sql = f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
        cur = conn.execute(sql, list(values.values()) + [h, prev])
        conn.execute("INSERT INTO app_meta(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_head_key(table), h))
        return cur.lastrowid


def verify_chain(table: str) -> tuple[bool, str]:
    rows = db.query(f"SELECT * FROM {table} ORDER BY id")
    prev = GENESIS
    for r in rows:
        if r["prev_hash"] != prev:
            return False, f"{table} id={r['id']}: prev_hash mismatch (chain forked or row removed)"
        if row_hash(table, r, prev) != r["row_hash"]:
            return False, f"{table} id={r['id']}: row_hash mismatch (content altered after write)"
        prev = r["row_hash"]
    head = db.meta_get(_head_key(table))
    if rows and head != prev:
        return False, f"{table}: cached head does not match last row"
    return True, f"{table}: {len(rows)} rows verified"


def verify_all() -> list[tuple[str, bool, str]]:
    return [(t, *verify_chain(t)) for t in CHAINS]
