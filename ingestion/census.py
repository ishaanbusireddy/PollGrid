"""Census ACS 5-year ingestion — the concrete endpoints and table IDs from §05.
Keyless works at low volume (the API allows ~500/day/IP without a key); a key
raises the ceiling. Budget-capped either way. Geography rotates through a
cursor so one run never burns the whole budget: state tier first, then counties
and districts state by state.

CVAP is a separate special-tabulation product (not a B-table) and gets its own
ingestion path when enabled; B16001 (language detail) is limited-geo in recent
vintages — C16001 is used instead, per the architecture review §2.7.
"""
from __future__ import annotations

import os
import time

from core import db
from core.util import today
from domain.geography import STATES
from ingestion import budget
from ingestion.http import FetchError, get_json
from ingestion.scheduler import register

ACS_YEAR = 2023  # latest 5-year vintage assumed available; adjust in config_json when it rolls

# Keyless etiquette: the Census API tolerates keyless use at a gentle rate but
# rejects rapid bursts with a "Missing Key" HTML page — which is a RATE response,
# not a hard key requirement. A short pause between keyless calls plus a
# backoff-retry on that rejection makes the ~104-call bootstrap land without a
# key. With a key there is no pause and no need to retry.
KEYLESS_DELAY_SECONDS = 1.2
RETRY_BACKOFFS = (5, 20, 60)  # seconds; only for rate-style rejections

# category -> {variable_name: acs_variable}
VARIABLES: dict[str, dict[str, str]] = {
    "population_age": {"total_population": "B01003_001E", "median_age": "B01002_001E"},
    "race_ethnicity": {"white_nh": "B03002_003E", "black_nh": "B03002_004E",
                       "hispanic": "B03002_012E", "asian_nh": "B03002_006E"},
    "education": {"pop_25plus": "B15003_001E", "hs_diploma": "B15003_017E",
                  "bachelors": "B15003_022E", "graduate": "B15003_023E"},
    "economic": {"median_household_income": "B19013_001E",
                 "labor_force": "B23025_002E", "unemployed": "B23025_005E"},
    "housing_urbanicity": {"occupied_units": "B25003_001E", "owner_occupied": "B25003_002E"},
    "social_nativity": {"foreign_born": "B05002_013E", "veterans": "B21001_002E"},
}

_ALL_VARS = [(cat, name, var) for cat, group in VARIABLES.items() for name, var in group.items()]


def _store(tier: str, entity_id: str, values: dict[str, str | None]) -> None:
    as_of = f"acs5_{ACS_YEAR}"
    rows = []
    for cat, name, var in _ALL_VARS:
        raw = values.get(var)
        if raw in (None, "", "null"):
            continue
        try:
            val = float(raw)
        except ValueError:
            continue
        if val < 0:  # ACS sentinel for suppressed estimates
            continue
        rows.append((tier, entity_id, as_of, cat, name, val, "measured", f"acs5_{ACS_YEAR}:{var}"))
    if rows:
        db.executemany(
            "INSERT OR IGNORE INTO demographics(tier,entity_id,as_of,category,variable,value,confidence,source) "
            "VALUES(?,?,?,?,?,?,?,?)", rows)


def _rate_rejected(exc: Exception) -> bool:
    """Census signals keyless over-rate with a 'Missing Key' HTML page (and
    occasionally a plain 429) — retryable, unlike a genuine 4xx data error."""
    s = str(exc).lower()
    return "missing key" in s or "429" in s or "over_rate" in s


def _fetch(url_base: str, geo_for: str, geo_in: str | None, key: str | None) -> list[list[str]]:
    budget.spend("census")
    params = {"get": "NAME," + ",".join(v for _, _, v in _ALL_VARS), "for": geo_for}
    if geo_in:
        params["in"] = geo_in
    if key:
        params["key"] = key
    else:
        time.sleep(KEYLESS_DELAY_SECONDS)   # keyless etiquette — don't look like a burst
    url = f"{url_base}/{ACS_YEAR}/acs/acs5"
    try:
        return get_json(url, params)
    except FetchError as e:
        if key or not _rate_rejected(e):
            raise
        for backoff in RETRY_BACKOFFS:      # keyless rate rejection: back off and retry
            time.sleep(backoff)
            try:
                return get_json(url, params)
            except FetchError as e2:
                if not _rate_rejected(e2):
                    raise
                e = e2
        raise e


def _district_entity_id(state_fips: str, cd_code: str) -> str | None:
    """Join a Census CD row to the district-shape version it actually describes
    (review §3.1: entity_id = district_version_id, never a bare geoid)."""
    if cd_code in ("ZZ", "98"):
        return None
    num = 0 if cd_code == "00" else int(cd_code)
    row = db.query_one(
        "SELECT district_version_id FROM congressional_districts "
        "WHERE state_fips=? AND district_number=? AND effective_to IS NULL", (state_fips, num))
    return str(row["district_version_id"]) if row else None


def _sync_national(base: str, key: str | None) -> None:
    """Nation row + the full state tier (two calls)."""
    data = _fetch(base, "us:*", None, key)
    header, row = data[0], data[1]
    _store("nation", "US", dict(zip(header, row)))
    data = _fetch(base, "state:*", None, key)
    header = data[0]
    for row in data[1:]:
        rec = dict(zip(header, row))
        if rec["state"] in STATES:
            _store("state", rec["state"], rec)


def _sync_state(base: str, st: str, key: str | None) -> None:
    """One state's counties + congressional districts (two calls)."""
    data = _fetch(base, "county:*", f"state:{st}", key)
    header = data[0]
    for row in data[1:]:
        rec = dict(zip(header, row))
        _store("county_equivalent", rec["state"] + rec["county"], rec)
    data = _fetch(base, "congressional district:*", f"state:{st}", key)
    header = data[0]
    for row in data[1:]:
        rec = dict(zip(header, row))
        eid = _district_entity_id(rec["state"], rec["congressional district"])
        if eid:
            _store("congressional_district", eid, rec)


def _bootstrap(base: str, key: str | None, state_fipses: list[str]) -> None:
    """First run only: nation + state tier + EVERY state's counties and districts
    back-to-back — ~104 budget-checked calls, well inside the 400/day census
    budget — so full coverage lands on day one instead of ~50 days of rotation.

    RESUMABLE: progress persists per state in census_bootstrap_cursor, so a
    mid-run failure (keyless rate rejection, network blip, Ctrl+C) re-runs from
    where it stopped, not from nation. All inserts are INSERT OR IGNORE, so any
    overlap is harmless. Pack invalidation runs once at the end."""
    done_through = db.meta_get("census_bootstrap_cursor")
    if done_through != "national_done" and done_through not in state_fipses:
        _sync_national(base, key)
        db.meta_set("census_bootstrap_cursor", "national_done")
        done_through = "national_done"
    start_idx = 0 if done_through == "national_done" else state_fipses.index(done_through) + 1
    for st in state_fipses[start_idx:]:
        _sync_state(base, st, key)
        db.meta_set("census_bootstrap_cursor", st)  # persisted per state — resumable
    db.meta_set("census_bootstrap_done", today())
    db.meta_set("census_last_full_sync", today())
    db.meta_set("census_cursor", state_fipses[0])  # daily refresh rotation starts here
    for st in state_fipses:
        _invalidate_touched(st)


@register("census")
def run(source: dict) -> None:
    key = os.environ.get(source["api_key_env"] or "") or None
    base = source["url"]
    state_fipses = [f for f, (_, _, terr) in sorted(STATES.items()) if not terr]

    if not db.meta_get("census_bootstrap_done"):
        _bootstrap(base, key, state_fipses)
        return

    cursor = db.meta_get("census_cursor", "nation")
    if cursor == "nation":
        _sync_national(base, key)
        db.meta_set("census_cursor", state_fipses[0])
        return

    # per-state pass: counties + districts for one state per run, then advance
    st = cursor if cursor in state_fipses else state_fipses[0]
    _sync_state(base, st, key)
    nxt = state_fipses[(state_fipses.index(st) + 1) % len(state_fipses)]
    db.meta_set("census_cursor", nxt)
    if nxt == state_fipses[0]:
        db.meta_set("census_last_full_sync", today())
    _invalidate_touched(st)


def _invalidate_touched(state_fips: str) -> None:
    """A sync that lands new data invalidates (does not rebuild) context packs."""
    from analyst.context_packs import invalidate_for_state
    invalidate_for_state(state_fips)
