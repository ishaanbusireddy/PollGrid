"""Official results, the honest tiered strategy (§05).

tier 1 — native state feeds, live on election night (plus a file-drop directory
         so any state feed you can script lands the same way);
tier 2 — OpenElections bulk CSVs (lagged; the historical backbone);
tier 3 — the real AP Elections API, gated behind config, default OFF;
manual — an authenticated internal entry path, every row tagged source_tier='manual'.

Every ingested row carries source_tier so the UI can honestly label which tier
is live for a given race — never implying AP-grade calling speed from a tier-1
feed that can't deliver it.
"""
from __future__ import annotations

import csv
import io
import json
import os

from core import db
from core.config import ROOT, cfg
from core.util import now_iso
from ingestion.http import SourceNotConfigured, get
from ingestion.scheduler import register

DROP_DIR = os.path.join(ROOT, "data", "results_native")


def upsert_result(race_id: int, county_geoid: str | None, party_code: str, votes: int,
                  pct_reporting: float | None, source_tier: str, is_synthetic: bool = False) -> None:
    db.execute(
        "INSERT INTO results_live(race_id,county_geoid,party_code,votes,pct_reporting,source_tier,"
        "updated_at,is_synthetic) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(race_id,county_geoid,party_code) DO UPDATE SET votes=excluded.votes, "
        "pct_reporting=excluded.pct_reporting, source_tier=excluded.source_tier, updated_at=excluded.updated_at",
        (race_id, county_geoid, party_code, votes, pct_reporting, source_tier, now_iso(), int(is_synthetic)))
    try:
        from api.websocket import broadcast
        from analyst.context_packs import invalidate_for_race
        invalidate_for_race(race_id)
        broadcast({"type": "results", "payload": {"race_id": race_id}})
    except Exception:
        pass


@register("results_native")
def run_tier1(source: dict) -> None:
    """File-drop tier-1: any JSON file in data/results_native/ shaped
    {race_id, results:[{county_geoid,party,votes,pct_reporting}]} is ingested
    then archived. Per-state HTTP feeds plug in via config_json.feeds the same
    way (a minority of states publish clean machine-readable results)."""
    ingested = 0
    if os.path.isdir(DROP_DIR):
        for fname in sorted(os.listdir(DROP_DIR)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(DROP_DIR, fname)
            payload = json.load(open(path, encoding="utf-8"))
            for r in payload.get("results", []):
                upsert_result(payload["race_id"], r.get("county_geoid"), r["party"],
                              int(r["votes"]), r.get("pct_reporting"), "native")
                ingested += 1
            os.rename(path, path + ".done")
    feeds = (json.loads(source["config_json"] or "{}")).get("feeds") or []
    for spec in feeds:  # {url, race_id} → same JSON shape over HTTP
        payload = json.loads(get(spec["url"]).decode("utf-8", "replace"))
        for r in payload.get("results", []):
            upsert_result(spec.get("race_id", payload.get("race_id")), r.get("county_geoid"),
                          r["party"], int(r["votes"]), r.get("pct_reporting"), "native")
    from modeling.race_calling import evaluate_callable
    evaluate_callable()


def general_election_date(cycle: int) -> str:
    """Real federal election day (first Tuesday after the first Monday in
    November) as YYYYMMDD — the OpenElections filename prefix. 2018→1106,
    2020→1103, 2022→1108, 2024→1105; never a hardcoded 1105."""
    import datetime
    for day in range(2, 9):  # election day always falls Nov 2–8
        if datetime.date(cycle, 11, day).weekday() == 1:  # Tuesday
            return f"{cycle}11{day:02d}"
    raise ValueError(f"no election day computed for {cycle}")  # unreachable


# office spellings vary across state repos; normalized (dots stripped, lowered)
_OFFICE_MAP = {
    "president": "president",
    "president of the united states": "president",
    "us president": "president",
    "us senate": "senate",
    "us senator": "senate",
    "united states senator": "senate",
    "senate": "senate",
    "us house": "house",
    "us house of representatives": "house",
    "house of representatives": "house",
    "united states representative": "house",
    "us representative": "house",
    "governor": "governor",
    "governor and lieutenant governor": "governor",
    "governor/lieutenant governor": "governor",
}


# Some repos have no 'votes' total column (e.g. GA precinct files split the
# count across per-mode columns); recognized mode columns are summed instead.
_MODE_COLUMNS = ("election_day", "advance_voting", "advanced", "early_voting", "absentee",
                 "absentee_by_mail", "mail", "provisional", "one_stop")

# A county (or fallback precinct) file must cover ≥90% of the state's counties
# before a state-tier rollup is written: several repos hold PARTIAL county
# files (PA 2020 has 13 of 67 counties) and a 'measured' statewide row summed
# from a partial file would be flatly wrong. Counties always land; the state
# rollup is what demands full coverage.
_STATE_ROLLUP_MIN_COVERAGE = 0.9


def _row_votes(row: dict) -> int | None:
    """A row's vote count. The 'votes' total column is used when it exists and
    agrees with (or lacks) the per-mode columns; when the two disagree the
    mode-sum wins — the disaggregated modes are the primary data and the total
    is derived (PA 2020 Philadelphia publishes votes = exactly 2× the modes;
    the mode-sum matches the certified count)."""
    total_col: int | None = None
    raw = (row.get("votes") or "").strip()
    if raw:
        try:
            total_col = int(float(raw.replace(",", "")))
        except ValueError:
            total_col = None
    mode_sum, found = 0, False
    for col, val in row.items():
        c = (col or "").strip().lower()
        if not (c.endswith("_votes") or c in _MODE_COLUMNS):
            continue
        v = (val or "").strip()
        if not v:
            continue
        try:
            mode_sum += int(float(v.replace(",", "")))
            found = True
        except ValueError:
            continue
    if total_col is not None and (not found or total_col == mode_sum):
        return total_col
    if found:
        return mode_sum
    return total_col


def _parse_openelections_csv(raw: str) -> dict[tuple[str, str], dict[str, int]]:
    """CSV text → {(county_name, office_key): {party3: votes}}. Works for both
    county files and precinct files (precinct rows aggregate through the same
    'county' column)."""
    parsed: dict[tuple[str, str], dict[str, int]] = {}
    for row in csv.DictReader(io.StringIO(raw)):
        office_key = _OFFICE_MAP.get((row.get("office") or "").replace(".", "").strip().lower())
        if not office_key:
            continue
        precinct = row.get("precinct")
        if precinct is not None:  # precinct-shaped file
            p = precinct.strip().lower()
            if not p or "total" in p:
                continue  # embedded summary rows (PA 2020 Philadelphia has a
                          # 'TOTAL' pseudo-precinct) would double-count everything
        county = (row.get("county") or "").strip()
        if not county or "total" in county.lower():
            continue
        party = (row.get("party") or "OTH").strip().upper()[:3] or "OTH"
        votes = _row_votes(row)
        if votes is None:
            continue
        bucket = parsed.setdefault((county, office_key), {})
        bucket[party] = bucket.get(party, 0) + votes
    return parsed


@register("results_openelections")
def run_tier2(source: dict) -> None:
    """Bulk-sync OpenElections general results for configured state/cycle pairs
    into political_history (the deep archive path). County file first; when a
    state publishes no county rollup (e.g. GA 2022) — or only a partial one —
    the precinct file is pulled and aggregated by its 'county' column. After
    county rows land, a state-tier row per office is rolled up from the same
    vote totals (full-coverage files only, see _STATE_ROLLUP_MIN_COVERAGE)."""
    conf = json.loads(source["config_json"] or "{}")
    pairs = [(s, c) for s in conf.get("states", []) for c in conf.get("cycles", [])]
    if not pairs:
        raise SourceNotConfigured("configure sources.config_json.states + .cycles for OpenElections sync")
    from domain.geography import USPS_TO_FIPS
    for usps, cycle in pairs:
        key = f"openelections_done:{usps}:{cycle}"
        if db.meta_get(key):
            continue
        state_fips = USPS_TO_FIPS[usps]
        expected = db.query_one(
            "SELECT COUNT(*) c FROM county_equivalents WHERE state_fips=? AND effective_to IS NULL",
            (state_fips,))["c"]
        stem = (f"{source['url']}/openelections-data-{usps.lower()}/master/"
                f"{cycle}/{general_election_date(cycle)}__{usps.lower()}__general__")
        best: dict[tuple[str, str], dict[str, int]] | None = None
        for suffix in ("county.csv", "precinct.csv"):
            try:
                raw = get(stem + suffix, timeout=180).decode("utf-8", "replace")
            except Exception:
                continue
            parsed = _parse_openelections_csv(raw)
            if best is None or len({c for c, _ in parsed}) > len({c for c, _ in best}):
                best = parsed
            if best and len({c for c, _ in best}) >= _STATE_ROLLUP_MIN_COVERAGE * expected:
                break  # full coverage already — no need for the (huge) precinct file
        if not best:
            db.meta_set(key, "unavailable")
            continue
        _land_openelections(best, state_fips, cycle, expected)
        db.meta_set(key, now_iso())


def _insert_history_row(tier: str, entity_id: str, office: str, cycle: int,
                        parties: dict[str, int]) -> None:
    """Real certified data ALWAYS beats a synthetic placeholder occupying the
    same UNIQUE slot: on conflict, replace only when the existing row is
    synthetic — never touch an existing real row."""
    total = sum(parties.values()) or 1
    dem, rep = parties.get("DEM", 0), parties.get("REP", 0)
    winner = max(parties, key=parties.get)
    db.execute(
        "INSERT INTO political_history(tier,entity_id,office,seat,cycle_year,winner_party,"
        "dem_pct,rep_pct,other_pct,margin_pct,confidence,source,is_synthetic) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0) "
        "ON CONFLICT(tier,entity_id,office,seat,cycle_year) DO UPDATE SET "
        "winner_party=excluded.winner_party, dem_pct=excluded.dem_pct, rep_pct=excluded.rep_pct, "
        "other_pct=excluded.other_pct, margin_pct=excluded.margin_pct, confidence=excluded.confidence, "
        "source=excluded.source, is_synthetic=0 "
        "WHERE political_history.is_synthetic=1",
        (tier, entity_id, office, "regular", cycle, winner,
         100 * dem / total, 100 * rep / total, 100 * (total - dem - rep) / total,
         100 * abs(dem - rep) / total, "measured", f"openelections:{cycle}"))


def _land_openelections(parsed: dict[tuple[str, str], dict[str, int]],
                        state_fips: str, cycle: int, expected_counties: int) -> tuple[int, int]:
    """Land county-tier rows, then — only when the file covers (nearly) every
    county in the state — a state-tier rollup per office summed from the same
    vote totals. Returns (n_county_rows, n_state_rows)."""
    geoid_cache: dict[str, str | None] = {}
    per_office: dict[str, dict[str, int]] = {}
    n_county = 0
    for (county, office_key), parties in parsed.items():
        st_bucket = per_office.setdefault(office_key, {})
        for party, votes in parties.items():
            st_bucket[party] = st_bucket.get(party, 0) + votes
        if county not in geoid_cache:
            crow = db.query_one("SELECT geoid FROM county_equivalents WHERE state_fips=? AND name LIKE ?",
                                (state_fips, county + "%"))
            geoid_cache[county] = crow["geoid"] if crow else None
        geoid = geoid_cache[county]
        if not geoid:
            continue
        _insert_history_row("county_equivalent", geoid, office_key, cycle, parties)
        n_county += 1
    n_state = 0
    coverage = len({c for c, _ in parsed})
    if expected_counties and coverage >= _STATE_ROLLUP_MIN_COVERAGE * expected_counties:
        for office_key, parties in per_office.items():
            _insert_history_row("state", state_fips, office_key, cycle, parties)
            n_state += 1
    return n_county, n_state


@register("results_ap")
def run_tier3(source: dict) -> None:
    if not cfg("ingestion.ap_elections.enabled"):
        raise SourceNotConfigured("AP Elections API disabled (ingestion.ap_elections.enabled=false); "
                                  "flip only once an AP account exists")
    if not os.environ.get(cfg("ingestion.ap_elections.api_key_env"), ""):
        raise SourceNotConfigured("AP enabled but AP_ELECTIONS_API_KEY not set")
    raise SourceNotConfigured("AP adapter scaffolded; wire the licensed endpoints when credentials exist")


def manual_entry(race_id: int, county_geoid: str | None, party_code: str, votes: int,
                 pct_reporting: float | None, entered_by: str) -> None:
    """The manual-entry tool: provenance stays visible, never laundered to look automated."""
    if not entered_by or entered_by.lower() in ("system", "model", "auto", "ai"):
        raise ValueError("manual entry requires a real human identifier")
    upsert_result(race_id, county_geoid, party_code, votes, pct_reporting, "manual")
    db.execute("INSERT INTO annotations(entity_type,entity_id,body,created_at) VALUES(?,?,?,?)",
               ("race", str(race_id), f"manual result entry by {entered_by}", now_iso()))
