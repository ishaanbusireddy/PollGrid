#!/usr/bin/env python3
"""Real state-level presidential history straight from OpenElections raw CSVs —
no aggregator in the middle. For each state and cycle the county file (falling
back to the precinct file when the county rollup is missing or partial) is
parsed with the tier-2 importer's own CSV parser, landed for every office in the file (president, senate, governor, house) as county_equivalent rows plus a state-tier rollup summed
from the same vote totals (full-coverage files only — a statewide number summed
from a partial file would be flatly wrong, same rule as ingestion tier 2).

The heavy lifting is reused from ingestion.results_tiers (being extended in
parallel); helpers are imported lazily by name with a local fallback so this
script keeps working whichever shape that module lands in.

Usage:
  python scripts/backfill_president_states.py                     # GA PA MI WI AZ NV NC ME
  python scripts/backfill_president_states.py --states PA MI --cycles 2020
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_STATES = ["GA", "PA", "MI", "WI", "AZ", "NV", "NC", "ME"]
DEFAULT_CYCLES = [2016, 2020, 2024]
RAW_BASE = "https://raw.githubusercontent.com/openelections"


def _tiers():
    import ingestion.results_tiers as rt
    return rt


def _parse_csv(raw: str) -> dict[tuple[str, str], dict[str, int]]:
    """The tier-2 parser: CSV text -> {(county, office): {party3: votes}}.
    Prefers the importer's current name, accepts the older one."""
    rt = _tiers()
    fn = getattr(rt, "_import_openelections_csv", None) or getattr(rt, "_parse_openelections_csv")
    return fn(raw)


def _land(parsed: dict[tuple[str, str], dict[str, int]], state_fips: str, cycle: int,
          expected_counties: int) -> tuple[int, int]:
    """County rows + (full-coverage only) the state rollup, via the tier-2
    helper when its signature still fits; local sum-by-office otherwise."""
    rt = _tiers()
    land = getattr(rt, "_land_openelections", None)
    if land is not None:
        try:
            return land(parsed, state_fips, cycle, expected_counties)
        except TypeError:
            pass  # helper signature moved under us — fall through to local landing
    return _land_locally(parsed, state_fips, cycle, expected_counties)


def _land_locally(parsed: dict[tuple[str, str], dict[str, int]], state_fips: str,
                  cycle: int, expected_counties: int) -> tuple[int, int]:
    """Fallback landing: county rows into political_history, then a state-tier
    rollup per office summed from the same vote totals when the file covers
    (nearly) every county in the state."""
    from core import db
    rt = _tiers()
    min_coverage = getattr(rt, "_STATE_ROLLUP_MIN_COVERAGE", 0.9)
    insert_row = rt._insert_history_row
    n_county = 0
    per_office: dict[str, dict[str, int]] = {}
    geoid_cache: dict[str, str | None] = {}
    for (county, office_key), parties in parsed.items():
        bucket = per_office.setdefault(office_key, {})
        for party, votes in parties.items():
            bucket[party] = bucket.get(party, 0) + votes
        if county not in geoid_cache:
            crow = db.query_one("SELECT geoid FROM county_equivalents WHERE state_fips=? AND name LIKE ?",
                                (state_fips, county + "%"))
            geoid_cache[county] = crow["geoid"] if crow else None
        if geoid_cache[county]:
            insert_row("county_equivalent", geoid_cache[county], office_key, cycle, parties)
            n_county += 1
    n_state = 0
    if expected_counties and len({c for c, _ in parsed}) >= min_coverage * expected_counties:
        for office_key, parties in per_office.items():
            insert_row("state", state_fips, office_key, cycle, parties)
            n_state += 1
    return n_county, n_state


def backfill_state_cycle(usps: str, cycle: int, base: str = RAW_BASE) -> tuple[int, int]:
    """One (state, cycle): fetch, parse, land EVERY office the file carries.
    Returns (n_county_rows, n_state_rows) — (0, 0) when nothing usable exists."""
    from core import db
    from domain.geography import USPS_TO_FIPS
    from ingestion.http import get
    rt = _tiers()
    state_fips = USPS_TO_FIPS[usps]
    expected = db.query_one(
        "SELECT COUNT(*) c FROM county_equivalents WHERE state_fips=? AND effective_to IS NULL",
        (state_fips,))["c"]
    min_coverage = getattr(rt, "_STATE_ROLLUP_MIN_COVERAGE", 0.9)
    stem = (f"{base}/openelections-data-{usps.lower()}/master/"
            f"{cycle}/{rt.general_election_date(cycle)}__{usps.lower()}__general__")
    best: dict[tuple[str, str], dict[str, int]] | None = None
    for suffix in ("county.csv", "precinct.csv"):
        try:
            raw = get(stem + suffix, timeout=300).decode("utf-8", "replace")
        except Exception as e:
            print(f"  {usps} {cycle}: no {suffix} ({e})")
            continue
        parsed = _parse_csv(raw)  # ALL federal/statewide offices in the file land — president, senate, governor, house
        if best is None or len(parsed) > len(best):
            best = parsed
        if best and len(best) >= min_coverage * expected:
            break  # full coverage already — skip the (huge) precinct file
    if not best:
        return 0, 0
    return _land(best, state_fips, cycle, expected)


def run(states: list[str], cycles: list[int], base: str = RAW_BASE) -> dict:
    totals = {"county_rows": 0, "state_rows": 0}
    for usps in states:
        for cycle in cycles:
            n_county, n_state = backfill_state_cycle(usps.upper(), cycle, base)
            totals["county_rows"] += n_county
            totals["state_rows"] += n_state
            print(f"  {usps} {cycle}: {n_county} county president rows, {n_state} state rollup row(s)")
    return totals


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--states", nargs="+", default=DEFAULT_STATES,
                    help=f"USPS codes (default: {' '.join(DEFAULT_STATES)})")
    ap.add_argument("--cycles", nargs="+", type=int, default=DEFAULT_CYCLES,
                    help="presidential cycles (default: 2016 2020 2024)")
    args = ap.parse_args()
    from core import db
    db.migrate()
    if not db.query_one("SELECT 1 FROM county_equivalents LIMIT 1"):
        from api.server import bootstrap
        bootstrap(start_ingestion=False)  # geography must exist before county joins
    totals = run(args.states, args.cycles)
    print(f"total: {totals['county_rows']} county president rows, "
          f"{totals['state_rows']} state president rows")


if __name__ == "__main__":
    main()
