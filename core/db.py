"""SQLite session + schema. WAL mode, one connection per thread, a single global
writer lock shared by every writer thread (ingestion, modeling, analyst) so the
hash chains stay linear and SQLITE_BUSY stays theoretical."""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

from core.config import ROOT, cfg

DB_PATH = os.path.join(ROOT, cfg("database.path"))

_local = threading.local()
_write_lock = threading.RLock()
_wal_done = False


class WriteLockTimeout(RuntimeError):
    pass


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=8.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=8000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        global _wal_done
        if not _wal_done:
            conn.execute("PRAGMA journal_mode=WAL")
            _wal_done = True
        _local.conn = conn
    return conn


def query(sql: str, params: tuple | list = ()) -> list[dict]:
    cur = connect().execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def query_one(sql: str, params: tuple | list = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


@contextmanager
def write():
    """All writes go through here: single global writer, one transaction."""
    if not _write_lock.acquire(timeout=20):
        raise WriteLockTimeout("writer lock not acquired within 20s")
    conn = connect()
    try:
        with conn:  # commits on success, rolls back on exception
            yield conn
    finally:
        _write_lock.release()


def execute(sql: str, params: tuple | list = ()) -> int:
    with write() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


def executemany(sql: str, rows: list[tuple]) -> None:
    with write() as conn:
        conn.executemany(sql, rows)


def meta_get(key: str, default: str | None = None) -> str | None:
    row = query_one("SELECT value FROM app_meta WHERE key=?", (key,))
    return row["value"] if row else default


def meta_set(key: str, value: str) -> None:
    execute("INSERT INTO app_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---------------------------------------------------------------------------
# Schema. Naming follows the clean-slate manual §19, amended per the
# architecture review: surrogate district_version_id (review §3.1), versioned
# county_equivalents with planning_region (§2.2), precinct cycle_year +
# precinct→district crosswalk (§3.2/3.3), Senate seat column (§2.5),
# is_voting districts (§2.6), electoral_vote_allocations as the sole EV/method
# source of truth (§2.4), citation columns on the curated floor (§6.5).
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT);

-- ============ geography & demographics ============
CREATE TABLE IF NOT EXISTS states (
  fips_code    TEXT PRIMARY KEY,
  usps_code    TEXT NOT NULL UNIQUE,
  name         TEXT NOT NULL,
  is_territory INTEGER NOT NULL DEFAULT 0,
  is_state     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS electoral_vote_allocations (
  id              INTEGER PRIMARY KEY,
  state_fips      TEXT NOT NULL REFERENCES states(fips_code),
  cycle_from      INTEGER NOT NULL,
  cycle_to        INTEGER,
  electoral_votes INTEGER NOT NULL,
  elector_method  TEXT NOT NULL CHECK (elector_method IN ('winner_take_all','congressional_district')),
  UNIQUE (state_fips, cycle_from)
);

CREATE TABLE IF NOT EXISTS congressional_districts (
  district_version_id INTEGER PRIMARY KEY,
  geoid           TEXT NOT NULL,
  congress_number INTEGER NOT NULL,
  state_fips      TEXT NOT NULL REFERENCES states(fips_code),
  district_number INTEGER NOT NULL,
  is_voting       INTEGER NOT NULL DEFAULT 1,
  effective_from  TEXT NOT NULL,
  effective_to    TEXT,
  UNIQUE (geoid, congress_number)
);

CREATE TABLE IF NOT EXISTS county_equivalents (
  county_version_id INTEGER PRIMARY KEY,
  geoid          TEXT NOT NULL,
  state_fips     TEXT NOT NULL REFERENCES states(fips_code),
  name           TEXT NOT NULL,
  type           TEXT NOT NULL CHECK (type IN
                   ('county','parish','borough','census_area','independent_city',
                    'planning_region','municipality','city_and_borough','municipio','district','island')),
  effective_from TEXT NOT NULL DEFAULT '1950-01-01',
  effective_to   TEXT,
  UNIQUE (geoid, effective_from)
);

CREATE TABLE IF NOT EXISTS precincts (
  precinct_id         INTEGER PRIMARY KEY,
  county_geoid        TEXT NOT NULL,
  state_precinct_code TEXT,
  name                TEXT,
  cycle_year          INTEGER NOT NULL,
  coverage_confidence TEXT NOT NULL CHECK (coverage_confidence IN ('measured','derived','thin_coverage')),
  source_tier         TEXT NOT NULL CHECK (source_tier IN ('native','vest','openelections','manual'))
);

CREATE TABLE IF NOT EXISTS precinct_district_assignments (
  precinct_id         INTEGER NOT NULL REFERENCES precincts(precinct_id),
  district_version_id INTEGER NOT NULL REFERENCES congressional_districts(district_version_id),
  fraction            REAL NOT NULL DEFAULT 1.0,
  PRIMARY KEY (precinct_id, district_version_id)
);

CREATE TABLE IF NOT EXISTS demographics (
  id           INTEGER PRIMARY KEY,
  tier         TEXT NOT NULL CHECK (tier IN ('nation','state','congressional_district','county_equivalent','precinct')),
  entity_id    TEXT NOT NULL,               -- states.fips_code / district_version_id / county geoid / precinct_id / 'US'
  as_of        TEXT NOT NULL,
  category     TEXT NOT NULL CHECK (category IN ('population_age','race_ethnicity','education','economic',
                                                 'housing_urbanicity','social_nativity','political_registration')),
  variable     TEXT NOT NULL,
  value        REAL,
  confidence   TEXT NOT NULL CHECK (confidence IN ('measured','derived')),
  source       TEXT NOT NULL,
  is_synthetic INTEGER NOT NULL DEFAULT 0,
  UNIQUE (tier, entity_id, as_of, category, variable)
);

CREATE TABLE IF NOT EXISTS political_history (
  id           INTEGER PRIMARY KEY,
  tier         TEXT NOT NULL CHECK (tier IN ('nation','state','congressional_district','county_equivalent','precinct')),
  entity_id    TEXT NOT NULL,
  office       TEXT NOT NULL CHECK (office IN ('president','senate','governor','house')),
  seat         TEXT NOT NULL DEFAULT 'regular',   -- 'regular' | 'class_1|2|3' | 'special' — review §2.5
  cycle_year   INTEGER NOT NULL,
  winner_party TEXT,
  dem_pct      REAL, rep_pct REAL, other_pct REAL,
  margin_pct   REAL,
  turnout_pct  REAL,
  confidence   TEXT NOT NULL CHECK (confidence IN ('measured','derived','uncertain')),
  source       TEXT NOT NULL,
  is_synthetic INTEGER NOT NULL DEFAULT 0,
  UNIQUE (tier, entity_id, office, seat, cycle_year)
);

-- ============ political entities ============
CREATE TABLE IF NOT EXISTS parties (
  id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  color TEXT, platform_summary TEXT, citation TEXT, ai_filled_at TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY,
  fec_candidate_id TEXT UNIQUE,
  name TEXT NOT NULL,
  party_code TEXT,
  state_fips TEXT,
  office TEXT CHECK (office IN ('president','senate','governor','house')),
  district_number INTEGER,
  bio TEXT,
  positions_summary TEXT,
  curated INTEGER NOT NULL DEFAULT 0,          -- curated floor rows are never overwritten by sync
  citation TEXT,                               -- per-row citation (review §6.5 — new vs GlobeGrid)
  synced_at TEXT, ai_filled_at TEXT,
  first_cycle INTEGER, last_cycle INTEGER,
  is_synthetic INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS officeholders (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  office TEXT NOT NULL, state_fips TEXT, district_number INTEGER,
  start_date TEXT NOT NULL, end_date TEXT
);

CREATE TABLE IF NOT EXISTS election_calendar (
  id INTEGER PRIMARY KEY,
  state_fips TEXT NOT NULL,
  cycle_year INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('primary','runoff','general')),
  election_date TEXT NOT NULL,
  source TEXT NOT NULL,
  UNIQUE (state_fips, cycle_year, kind)
);

CREATE TABLE IF NOT EXISTS pacs (
  id INTEGER PRIMARY KEY, fec_committee_id TEXT UNIQUE, name TEXT NOT NULL,
  type TEXT, total_receipts REAL, total_disbursements REAL, synced_at TEXT
);

CREATE TABLE IF NOT EXISTS donors_aggregated (
  id INTEGER PRIMARY KEY, candidate_id INTEGER REFERENCES candidates(id),
  contributor_name TEXT, total_amount REAL, n_contributions INTEGER, cycle_year INTEGER, source TEXT
);

CREATE TABLE IF NOT EXISTS media_outlets (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, url TEXT,
  reliability_tier INTEGER NOT NULL DEFAULT 3,   -- 1 wire-quality … 5 noise
  leaning TEXT
);

-- ============ races ============
CREATE TABLE IF NOT EXISTS races (
  id INTEGER PRIMARY KEY,
  race_type TEXT NOT NULL CHECK (race_type IN ('president','senate','governor','house','generic_ballot')),
  phase TEXT NOT NULL DEFAULT 'general' CHECK (phase IN ('primary','general','runoff')),
  cycle_year INTEGER NOT NULL,
  state_fips TEXT,
  district_version_id INTEGER REFERENCES congressional_districts(district_version_id),
  seat TEXT NOT NULL DEFAULT 'regular',
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'upcoming' CHECK (status IN ('upcoming','live','callable','called','certified')),
  competitiveness TEXT NOT NULL DEFAULT 'unrated',
  is_synthetic INTEGER NOT NULL DEFAULT 0,
  UNIQUE (race_type, phase, cycle_year, state_fips, district_version_id, seat)
);

CREATE TABLE IF NOT EXISTS race_candidates (
  race_id INTEGER NOT NULL REFERENCES races(id),
  candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  party_code TEXT, is_incumbent INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (race_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS redistricting_events (
  id INTEGER PRIMARY KEY, state_fips TEXT NOT NULL, congress_number INTEGER NOT NULL,
  effective_from TEXT NOT NULL, note TEXT
);

-- ============ core fact chain (append-only, hash-chained) ============
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, source_type TEXT NOT NULL,
  url TEXT, api_key_env TEXT, is_active INTEGER NOT NULL DEFAULT 1,
  interval_key TEXT NOT NULL,                  -- key into ingestion.intervals_seconds
  reliability_tier INTEGER NOT NULL DEFAULT 3,
  health TEXT NOT NULL DEFAULT 'ok' CHECK (health IN ('ok','degraded','down')),
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_run_at TEXT, last_error TEXT, config_json TEXT
);

CREATE TABLE IF NOT EXISTS raw_items (
  id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
  external_id TEXT NOT NULL, fetched_at TEXT NOT NULL,
  title TEXT, url TEXT, body TEXT, published_at TEXT,
  matched_profile_id INTEGER,
  is_synthetic INTEGER NOT NULL DEFAULT 0,
  UNIQUE (source_id, external_id)
);

CREATE TABLE IF NOT EXISTS pollsters (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, url TEXT,
  methodology TEXT, transparency_note TEXT
);

CREATE TABLE IF NOT EXISTS polls (
  id INTEGER PRIMARY KEY,
  source_id INTEGER REFERENCES sources(id),
  raw_item_id INTEGER REFERENCES raw_items(id),
  pollster_id INTEGER NOT NULL REFERENCES pollsters(id),
  race_id INTEGER NOT NULL REFERENCES races(id),
  field_start TEXT NOT NULL, field_end TEXT NOT NULL,
  sample_size INTEGER, population TEXT CHECK (population IN ('lv','rv','a')),
  moe REAL, methodology TEXT, release_url TEXT,
  created_at TEXT NOT NULL,
  is_synthetic INTEGER NOT NULL DEFAULT 0,
  row_hash TEXT NOT NULL, prev_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_results (
  id INTEGER PRIMARY KEY, poll_id INTEGER NOT NULL REFERENCES polls(id),
  candidate_id INTEGER REFERENCES candidates(id), party_code TEXT, pct REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS extracted_facts (
  id INTEGER PRIMARY KEY,
  raw_item_id INTEGER REFERENCES raw_items(id),
  category TEXT NOT NULL CHECK (category IN ('polling','finance','legislation','endorsement','scandal',
                                             'debate','election_result','rhetoric','campaign_event','other')),
  summary TEXT NOT NULL,
  entities_json TEXT, race_id INTEGER, state_fips TEXT, county_geoid TEXT,
  occurred_at TEXT, created_at TEXT NOT NULL,
  is_synthetic INTEGER NOT NULL DEFAULT 0,
  row_hash TEXT NOT NULL, prev_hash TEXT NOT NULL
);

-- ============ modeling (all dated snapshots on an as_of axis, never overwritten) ============
CREATE TABLE IF NOT EXISTS poll_averages (
  id INTEGER PRIMARY KEY, race_id INTEGER NOT NULL REFERENCES races(id),
  as_of TEXT NOT NULL, party_code TEXT NOT NULL, avg_pct REAL NOT NULL,
  n_polls INTEGER NOT NULL, weight_sum REAL NOT NULL, metric_id TEXT NOT NULL,
  is_synthetic INTEGER NOT NULL DEFAULT 0,
  UNIQUE (race_id, as_of, party_code)
);

CREATE TABLE IF NOT EXISTS fundamentals_snapshots (
  id INTEGER PRIMARY KEY, race_id INTEGER NOT NULL REFERENCES races(id),
  as_of TEXT NOT NULL, dem_score REAL NOT NULL, components_json TEXT NOT NULL,
  metric_id TEXT NOT NULL, is_synthetic INTEGER NOT NULL DEFAULT 0,
  UNIQUE (race_id, as_of)
);

CREATE TABLE IF NOT EXISTS ideology_scores (
  id INTEGER PRIMARY KEY, candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  as_of TEXT NOT NULL, score REAL NOT NULL, components_json TEXT NOT NULL, source TEXT NOT NULL,
  UNIQUE (candidate_id, as_of)
);

CREATE TABLE IF NOT EXISTS forecasts (
  id INTEGER PRIMARY KEY, race_id INTEGER NOT NULL REFERENCES races(id),
  as_of TEXT NOT NULL, model TEXT NOT NULL CHECK (model IN ('quantitative','ensemble')),
  dem_prob REAL NOT NULL, rep_prob REAL NOT NULL, other_prob REAL NOT NULL DEFAULT 0,
  metric_id TEXT NOT NULL, is_synthetic INTEGER NOT NULL DEFAULT 0,
  UNIQUE (race_id, as_of, model)
);

CREATE TABLE IF NOT EXISTS predictions (
  id INTEGER PRIMARY KEY, race_id INTEGER NOT NULL REFERENCES races(id),
  as_of TEXT NOT NULL, model TEXT NOT NULL, probs_json TEXT NOT NULL,
  graded_outcome TEXT, graded_at TEXT,
  row_hash TEXT NOT NULL, prev_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_results (
  id INTEGER PRIMARY KEY, category TEXT NOT NULL, as_of TEXT NOT NULL,
  model TEXT NOT NULL, brier REAL NOT NULL, n_graded INTEGER NOT NULL, passed INTEGER NOT NULL,
  UNIQUE (category, as_of, model)
);

CREATE TABLE IF NOT EXISTS chamber_simulations (
  id INTEGER PRIMARY KEY, chamber TEXT NOT NULL CHECK (chamber IN ('senate','house','ec')),
  as_of TEXT NOT NULL, n_sims INTEGER NOT NULL,
  dem_control_prob REAL NOT NULL, seat_distribution_json TEXT NOT NULL, metric_id TEXT NOT NULL,
  UNIQUE (chamber, as_of)
);

CREATE TABLE IF NOT EXISTS volatility_scores (
  id INTEGER PRIMARY KEY, scope TEXT NOT NULL,   -- 'national' or race:{id}
  as_of TEXT NOT NULL, score REAL NOT NULL, components_json TEXT NOT NULL,
  UNIQUE (scope, as_of)
);

CREATE TABLE IF NOT EXISTS anomaly_flags (
  id INTEGER PRIMARY KEY, scope TEXT NOT NULL, as_of TEXT NOT NULL,
  kind TEXT NOT NULL, detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS second_order_links (
  id INTEGER PRIMARY KEY, race_a INTEGER NOT NULL, race_b INTEGER NOT NULL,
  as_of TEXT NOT NULL, similarity REAL NOT NULL, status TEXT NOT NULL DEFAULT 'flagged',
  plausible_cause TEXT
);

CREATE TABLE IF NOT EXISTS coalition_models (
  id INTEGER PRIMARY KEY, race_id INTEGER NOT NULL REFERENCES races(id),
  as_of TEXT NOT NULL, coefficients_json TEXT NOT NULL, r2 REAL, n INTEGER,
  UNIQUE (race_id, as_of)
);

-- ============ genius layer ============
CREATE TABLE IF NOT EXISTS qualitative_factor_scores (
  id INTEGER PRIMARY KEY, race_id INTEGER NOT NULL REFERENCES races(id),
  factor_key TEXT NOT NULL, as_of TEXT NOT NULL, score REAL NOT NULL,
  method TEXT NOT NULL CHECK (method IN ('deterministic','llm_rubric','neutral_fallback')),
  citation_fact_ids TEXT, rationale TEXT,
  scored_against_fact_id INTEGER   -- newest extracted_fact id present when scored; caches on it
);

CREATE TABLE IF NOT EXISTS ensemble_weights (
  id INTEGER PRIMARY KEY, category TEXT NOT NULL, as_of TEXT NOT NULL,
  coefficients_json TEXT NOT NULL, UNIQUE (category, as_of)
);

CREATE TABLE IF NOT EXISTS ensemble_backtest_results (
  id INTEGER PRIMARY KEY, category TEXT NOT NULL, as_of TEXT NOT NULL,
  brier_quant REAL NOT NULL, brier_ensemble REAL NOT NULL, n_graded INTEGER NOT NULL,
  live_model TEXT NOT NULL CHECK (live_model IN ('quantitative','ensemble')),
  UNIQUE (category, as_of)
);

-- ============ articles & retrieval ============
CREATE TABLE IF NOT EXISTS race_search_profiles (
  id INTEGER PRIMARY KEY, race_id INTEGER NOT NULL UNIQUE REFERENCES races(id),
  terms_json TEXT NOT NULL, updated_at TEXT NOT NULL, last_hit_at TEXT
);

CREATE TABLE IF NOT EXISTS article_entity_links (
  id INTEGER PRIMARY KEY, raw_item_id INTEGER NOT NULL REFERENCES raw_items(id),
  entity_type TEXT NOT NULL CHECK (entity_type IN ('race','candidate','party')),
  entity_id INTEGER NOT NULL,
  UNIQUE (raw_item_id, entity_type, entity_id)
);

-- ============ rhetoric, framing & spend ============
CREATE TABLE IF NOT EXISTS media_framing_scores (
  id INTEGER PRIMARY KEY, outlet_id INTEGER NOT NULL REFERENCES media_outlets(id),
  story_key TEXT NOT NULL, topic TEXT NOT NULL, framing REAL NOT NULL,
  method TEXT NOT NULL, as_of TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_stance_scores (
  id INTEGER PRIMARY KEY, candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  topic TEXT NOT NULL, stance REAL NOT NULL, method TEXT NOT NULL,
  citation_fact_ids TEXT, as_of TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ad_spend (
  id INTEGER PRIMARY KEY, race_id INTEGER REFERENCES races(id),
  sponsor TEXT NOT NULL, medium TEXT, amount REAL NOT NULL, as_of TEXT NOT NULL, source TEXT NOT NULL
);

-- ============ trust & transparency ============
CREATE TABLE IF NOT EXISTS pollster_ratings (
  id INTEGER PRIMARY KEY, pollster_id INTEGER NOT NULL REFERENCES pollsters(id),
  as_of TEXT NOT NULL, avg_abs_error REAL, n_graded INTEGER NOT NULL DEFAULT 0,
  grade TEXT NOT NULL DEFAULT 'provisional', house_effect_dem REAL NOT NULL DEFAULT 0,
  weight_multiplier REAL NOT NULL DEFAULT 1.0,
  region TEXT NOT NULL DEFAULT 'national',   -- 'national' or a Census region; regional rows
  UNIQUE (pollster_id, as_of, region)        -- only written past a graded-count threshold
);

CREATE TABLE IF NOT EXISTS redistricting_fairness_scores (
  id INTEGER PRIMARY KEY, state_fips TEXT NOT NULL, congress_number INTEGER NOT NULL,
  as_of TEXT NOT NULL, efficiency_gap REAL, mean_median REAL, n_districts INTEGER,
  UNIQUE (state_fips, congress_number, as_of)
);

-- Persistent county->district area shares (modeling/areal.py). A pure function of the
-- vendored geometry (cycle-independent), so it is cached once and reused across every
-- nightly run; invalidated only when the geometry fingerprint (app_meta) changes.
CREATE TABLE IF NOT EXISTS county_district_area_shares (
  county_geoid TEXT NOT NULL, district_geoid TEXT NOT NULL, share REAL NOT NULL,
  UNIQUE (county_geoid, district_geoid)
);

CREATE TABLE IF NOT EXISTS computation_audit_log (
  metric_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, metric_type TEXT NOT NULL,
  scope TEXT NOT NULL, formula TEXT NOT NULL, inputs_json TEXT NOT NULL, output_json TEXT NOT NULL
);

-- ============ the analyst ============
CREATE TABLE IF NOT EXISTS context_packs (
  id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
  built_at TEXT, stale INTEGER NOT NULL DEFAULT 1, pack_json TEXT, token_estimate INTEGER,
  UNIQUE (entity_type, entity_id)
);

CREATE TABLE IF NOT EXISTS correlation_feedback (
  id INTEGER PRIMARY KEY, link_id INTEGER NOT NULL REFERENCES second_order_links(id),
  verdict TEXT NOT NULL CHECK (verdict IN ('confirmed','spurious','unclear')),
  note TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analyst_sessions (
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, entity_type TEXT, entity_id TEXT
);

CREATE TABLE IF NOT EXISTS analyst_messages (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES analyst_sessions(id),
  role TEXT NOT NULL, content TEXT NOT NULL, citations_json TEXT, model TEXT, created_at TEXT NOT NULL
);

-- ============ stories (feed clusters) ============
CREATE TABLE IF NOT EXISTS stories (
  id INTEGER PRIMARY KEY, headline TEXT NOT NULL, category TEXT NOT NULL,
  race_id INTEGER, state_fips TEXT, score REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, is_synthetic INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS story_facts (
  story_id INTEGER NOT NULL REFERENCES stories(id),
  fact_id INTEGER NOT NULL REFERENCES extracted_facts(id),
  PRIMARY KEY (story_id, fact_id)
);

-- ============ election night ============
CREATE TABLE IF NOT EXISTS results_live (
  id INTEGER PRIMARY KEY, race_id INTEGER NOT NULL REFERENCES races(id),
  county_geoid TEXT, party_code TEXT NOT NULL, votes INTEGER NOT NULL,
  pct_reporting REAL, source_tier TEXT NOT NULL CHECK (source_tier IN ('native','openelections','ap','manual')),
  updated_at TEXT NOT NULL, is_synthetic INTEGER NOT NULL DEFAULT 0,
  UNIQUE (race_id, county_geoid, party_code)
);

CREATE TABLE IF NOT EXISTS race_calls (
  id INTEGER PRIMARY KEY, race_id INTEGER NOT NULL REFERENCES races(id),
  called_at TEXT NOT NULL, winner_party TEXT NOT NULL,
  called_by TEXT NOT NULL CHECK (length(called_by) > 0 AND lower(called_by) NOT IN ('system','model','auto','ai')),
  notes TEXT
);

-- ============ engagement ============
-- ============ influence ledger (addendum §9/§10) ============
CREATE TABLE IF NOT EXISTS lobbying_orgs (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  sector TEXT NOT NULL DEFAULT 'uncategorized',
  org_type TEXT NOT NULL DEFAULT 'pac' CHECK (org_type IN ('pac','super_pac','527','lobbying_firm','trade_assoc','advocacy')),
  fec_committee_id TEXT, lda_registrant_id TEXT,
  total_spend_ytd REAL, citation TEXT, synced_at TEXT
);

CREATE TABLE IF NOT EXISTS lobbying_disclosures (
  id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL REFERENCES lobbying_orgs(id),
  period TEXT NOT NULL, client TEXT, issue_codes TEXT, amount REAL,
  source TEXT NOT NULL, source_url TEXT,
  UNIQUE (org_id, period, client, amount)
);

CREATE TABLE IF NOT EXISTS pac_candidate_spend (
  id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL REFERENCES lobbying_orgs(id),
  candidate_id INTEGER REFERENCES candidates(id), race_id INTEGER REFERENCES races(id),
  amount REAL NOT NULL, spend_type TEXT NOT NULL CHECK (spend_type IN
    ('contribution','ie_support','ie_oppose')),
  cycle_year INTEGER NOT NULL, as_of TEXT, source TEXT NOT NULL,
  UNIQUE (org_id, candidate_id, spend_type, cycle_year, amount, as_of)
);

CREATE TABLE IF NOT EXISTS endorsements (
  id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL REFERENCES lobbying_orgs(id),
  candidate_id INTEGER NOT NULL REFERENCES candidates(id), race_id INTEGER REFERENCES races(id),
  as_of TEXT NOT NULL, source_url TEXT NOT NULL,   -- ONLY ever the org's own announcement
  UNIQUE (org_id, candidate_id, race_id)
);

CREATE TABLE IF NOT EXISTS debate_schedule (
  id INTEGER PRIMARY KEY, race_id INTEGER REFERENCES races(id),
  title TEXT NOT NULL, scheduled_at TEXT NOT NULL, window_hours INTEGER NOT NULL DEFAULT 6,
  transcript_hint_url TEXT
);

CREATE TABLE IF NOT EXISTS watchlist_items (id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, added_at TEXT NOT NULL, UNIQUE(entity_type, entity_id));
CREATE TABLE IF NOT EXISTS bookmarks (id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS annotations (id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS daily_briefings (id INTEGER PRIMARY KEY, as_of TEXT NOT NULL UNIQUE, body TEXT NOT NULL, model TEXT);

CREATE INDEX IF NOT EXISTS idx_demographics_entity ON demographics(tier, entity_id, as_of);
CREATE INDEX IF NOT EXISTS idx_history_entity ON political_history(tier, entity_id);
CREATE INDEX IF NOT EXISTS idx_polls_race ON polls(race_id, field_end);
CREATE INDEX IF NOT EXISTS idx_facts_race ON extracted_facts(race_id, created_at);
CREATE INDEX IF NOT EXISTS idx_raw_items_pub ON raw_items(published_at);
CREATE INDEX IF NOT EXISTS idx_averages_race ON poll_averages(race_id, as_of);
CREATE INDEX IF NOT EXISTS idx_links_entity ON article_entity_links(entity_type, entity_id);
"""

# Standing referential-integrity checks for polymorphic tables (review §3.4) —
# SQLite can't FK a (tier, entity_id) pair, so the nightly job runs these.
INTEGRITY_CHECKS: dict[str, str] = {
    "counties_reference_states":
        "SELECT geoid FROM county_equivalents WHERE state_fips NOT IN (SELECT fips_code FROM states)",
    "districts_reference_states":
        "SELECT geoid FROM congressional_districts WHERE state_fips NOT IN (SELECT fips_code FROM states)",
    "demographics_state_refs":
        "SELECT DISTINCT entity_id FROM demographics WHERE tier='state' "
        "AND entity_id NOT IN (SELECT fips_code FROM states)",
    "demographics_county_refs":
        "SELECT DISTINCT entity_id FROM demographics WHERE tier='county_equivalent' "
        "AND entity_id NOT IN (SELECT geoid FROM county_equivalents)",
    "demographics_district_refs":
        "SELECT DISTINCT entity_id FROM demographics WHERE tier='congressional_district' "
        "AND CAST(entity_id AS INTEGER) NOT IN (SELECT district_version_id FROM congressional_districts)",
    "history_state_refs":
        "SELECT DISTINCT entity_id FROM political_history WHERE tier='state' "
        "AND entity_id NOT IN (SELECT fips_code FROM states)",
    "precincts_reference_counties":
        "SELECT precinct_id FROM precincts WHERE county_geoid NOT IN (SELECT geoid FROM county_equivalents)",
}


def run_integrity_checks() -> dict[str, int]:
    return {name: len(query(sql)) for name, sql in INTEGRITY_CHECKS.items()}


# Additive column migrations for databases created by earlier versions.
# table -> {column: full ALTER type/default clause}
_NEW_COLUMNS: dict[str, dict[str, str]] = {
    "raw_items": {"archival": "INTEGER NOT NULL DEFAULT 0"},
    "sources": {"us_domestic": "INTEGER NOT NULL DEFAULT 1"},
    "states": {"flag_url": "TEXT"},
    "candidates": {"portrait_url": "TEXT"},
    "pollster_ratings": {"region": "TEXT NOT NULL DEFAULT 'national'"},
    "qualitative_factor_scores": {"scored_against_fact_id": "INTEGER"},
    "races": {"election_date": "TEXT"},
}


def _ensure_columns(conn) -> None:
    for table, cols in _NEW_COLUMNS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, decl in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _rebuild_pollster_ratings(conn) -> None:
    """Databases created before regional ratings carry UNIQUE(pollster_id, as_of),
    which would reject a same-day regional row even after the region column is
    ALTERed in — so rebuild the table once with the three-column constraint."""
    for idx in conn.execute("PRAGMA index_list(pollster_ratings)").fetchall():
        if not idx[2]:  # not unique
            continue
        cols = [r[2] for r in conn.execute(f"PRAGMA index_info({idx[1]})").fetchall()]
        if cols == ["pollster_id", "as_of"]:
            break
    else:
        return
    conn.execute("ALTER TABLE pollster_ratings RENAME TO pollster_ratings_pre_regional")
    conn.execute("""CREATE TABLE pollster_ratings (
  id INTEGER PRIMARY KEY, pollster_id INTEGER NOT NULL REFERENCES pollsters(id),
  as_of TEXT NOT NULL, avg_abs_error REAL, n_graded INTEGER NOT NULL DEFAULT 0,
  grade TEXT NOT NULL DEFAULT 'provisional', house_effect_dem REAL NOT NULL DEFAULT 0,
  weight_multiplier REAL NOT NULL DEFAULT 1.0,
  region TEXT NOT NULL DEFAULT 'national',
  UNIQUE (pollster_id, as_of, region))""")
    conn.execute(
        "INSERT INTO pollster_ratings(id,pollster_id,as_of,avg_abs_error,n_graded,grade,"
        "house_effect_dem,weight_multiplier,region) "
        "SELECT id,pollster_id,as_of,avg_abs_error,n_graded,grade,house_effect_dem,"
        "weight_multiplier,COALESCE(region,'national') FROM pollster_ratings_pre_regional")
    conn.execute("DROP TABLE pollster_ratings_pre_regional")


def migrate() -> None:
    with write() as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        _rebuild_pollster_ratings(conn)
