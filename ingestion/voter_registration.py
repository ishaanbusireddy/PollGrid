"""Voter registration (addendum §6.3) — two tiers.

Tier-2 backbone: the EAC's EAVS state-level public-release CSV (2024 vintage by
default; config_json.url is user-configurable so a moved/renewed release is a
config edit, not a code change — a 404 at runtime just degrades the source via
the scheduler). The file is jurisdiction-level, so rows are summed to state
tier using the EAVS codebook column conventions: A1a total registered, A1b
active, A1c inactive. Parsing is deliberately tolerant — the state column and
the A1* columns are found by convention, never by fixed position — and EAVS
negative sentinels (-88 does-not-apply / -99 data-not-available) are skipped.

Tier-1 framework: per-state party-registration feeds via config_json.state_feeds
[{usps, url, format:'csv', columns:{dem,rep,other,total}}] → variables
registered_dem / registered_rep / registered_other (+ registered_total when a
total column is mapped). Each feed degrades independently.
"""
from __future__ import annotations

import csv
import io
import json
import re

from core import db
from core.util import now_iso, today
from domain.geography import STATES, USPS_TO_FIPS
from ingestion.http import FetchError, get
from ingestion.scheduler import register

DEFAULT_EAVS_URL = ("https://www.eac.gov/sites/default/files/2025-06/"
                    "2024_EAVS_for_Public_Release_V1.csv")
AS_OF = "eavs_2024"
SOURCE_TAG = "eavs_2024"

# EAVS codebook item -> demographics variable
EAVS_VARS = {"a1a": "registered_total", "a1b": "registered_active", "a1c": "registered_inactive"}

_NAME_TO_FIPS = {name.lower(): fips for fips, (_usps, name, _t) in STATES.items()}


def ensure_source() -> None:
    """Idempotent sources row (new adapters own their row; sources_seed.py is
    owned elsewhere). interval_key reuses 'census': slow-moving reference data."""
    db.execute(
        "INSERT OR IGNORE INTO sources(name,source_type,interval_key,url,api_key_env,"
        "reliability_tier,is_active,config_json) VALUES(?,?,?,?,?,?,?,?)",
        ("Voter registration (EAVS + state feeds)", "voter_registration", "census", "", "",
         1, 1, json.dumps({"url": DEFAULT_EAVS_URL, "state_feeds": []})))


def _norm(header: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (header or "").lower())


def _state_column(headers: list[str]) -> tuple[str, str] | None:
    """('usps'|'name_or_usps'|'fips', header). Preference order: an explicit
    abbreviation column, a bare 'state' column, then any FIPS column."""
    norm = {h: _norm(h) for h in headers}
    for h in headers:
        if "state" in norm[h] and ("abbr" in norm[h] or "abv" in norm[h] or "postal" in norm[h]):
            return ("usps", h)
    for h in headers:
        if norm[h] in ("state", "statefull", "statename"):
            return ("name_or_usps", h)
    for h in headers:
        if "fips" in norm[h]:
            return ("fips", h)
    return None


def _var_columns(headers: list[str]) -> dict[str, str]:
    """EAVS item codes → actual header names. Matches 'A1a', 'A1a_Total',
    'a1a. registered total' … but never 'A1ax' (the next letter must not be
    alphabetic, so distinct items cannot collide)."""
    out: dict[str, str] = {}
    for h in headers:
        n = _norm(h)
        for code, var in EAVS_VARS.items():
            if n == code or (n.startswith(code) and not n[len(code):len(code) + 1].isalpha()):
                out.setdefault(var, h)
    return out


def _fips_for(kind: str, raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    if kind == "fips":
        digits = re.sub(r"\D", "", raw)
        fips = digits.zfill(2)[:2] if digits else None
        return fips if fips in STATES else None
    if len(raw) == 2:
        return USPS_TO_FIPS.get(raw.upper())
    return _NAME_TO_FIPS.get(raw.lower())  # some vintages carry full state names


def parse_eavs_csv(text: str) -> list[tuple]:
    """EAVS public-release CSV → demographics tuples, jurisdiction rows summed
    to state tier. Pure function (unit-tested on a synthetic EAVS-shaped CSV)."""
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    state_col = _state_column(headers)
    var_cols = _var_columns(headers)
    if state_col is None or "registered_total" not in var_cols:
        return []  # not an EAVS-shaped file — the caller raises, health degrades
    kind, col = state_col
    sums: dict[str, dict[str, float]] = {}
    for rec in reader:
        fips = _fips_for(kind, rec.get(col) or "")
        if not fips:
            continue
        for var, h in var_cols.items():
            try:
                v = float((rec.get(h) or "").replace(",", "").strip())
            except ValueError:
                continue
            if v < 0:
                continue  # EAVS sentinels: -88 does-not-apply, -99 not-available
            per_state = sums.setdefault(fips, {})
            per_state[var] = per_state.get(var, 0.0) + v
    rows: list[tuple] = []
    for fips in sorted(sums):
        for var in sorted(sums[fips]):
            rows.append(("state", fips, AS_OF, "political_registration", var,
                         sums[fips][var], "measured", SOURCE_TAG))
    return rows


def parse_state_feed(text: str, columns: dict) -> dict[str, float]:
    """Tier-1 per-state party-registration CSV → {variable: value}. columns maps
    dem/rep/other/total → the feed's column names; values are summed over every
    row, so a one-row statewide file and a per-county file both work."""
    keymap = {"dem": "registered_dem", "rep": "registered_rep",
              "other": "registered_other", "total": "registered_total"}
    reader = csv.DictReader(io.StringIO(text))
    out: dict[str, float] = {}
    for rec in reader:
        low = {(k or "").strip().lower(): (v or "") for k, v in rec.items() if k}
        for key, var in keymap.items():
            col = str(columns.get(key) or "").strip().lower()
            if not col or col not in low:
                continue
            try:
                v = float(low[col].replace(",", "").strip())
            except ValueError:
                continue
            if v >= 0:
                out[var] = out.get(var, 0.0) + v
    return out


def _land_demographics(rows: list[tuple]) -> None:
    db.executemany(
        "INSERT OR IGNORE INTO demographics(tier,entity_id,as_of,category,variable,value,"
        "confidence,source) VALUES(?,?,?,?,?,?,?,?)", rows)


@register("voter_registration")
def run(source: dict) -> None:
    conf = json.loads(source["config_json"] or "{}")

    # tier 2: EAVS backbone — a static release, imported once per configured url
    url = conf.get("url") or DEFAULT_EAVS_URL
    done_key = f"eavs_synced:{url}"
    if not db.meta_get(done_key):
        text = get(url, timeout=300).decode("utf-8", "replace")  # 404 → FetchError → degraded
        rows = parse_eavs_csv(text)
        if not rows:
            raise FetchError("EAVS csv parsed to zero state rows — check sources.config_json.url")
        _land_demographics(rows)
        db.meta_set(done_key, now_iso())

    # tier 1: per-state party-registration feeds, each degrading independently
    for feed in conf.get("state_feeds") or []:
        usps = (feed.get("usps") or "").strip().upper()
        fips = USPS_TO_FIPS.get(usps)
        feed_url = feed.get("url")
        if not fips or not feed_url or (feed.get("format") or "csv") != "csv":
            continue
        try:
            text = get(feed_url).decode("utf-8", "replace")
        except Exception:
            continue  # one bad state feed never fails the rest
        values = parse_state_feed(text, feed.get("columns") or {})
        as_of = feed.get("as_of") or today()
        rows = [("state", fips, as_of, "political_registration", var, v, "measured",
                 f"state_feed:{usps}") for var, v in sorted(values.items())]
        if rows:
            _land_demographics(rows)


try:  # at boot migrate() runs before adapters are imported, so this lands the row;
    ensure_source()  # a bare unit-test import without a migrated DB skips quietly
except Exception:
    pass
