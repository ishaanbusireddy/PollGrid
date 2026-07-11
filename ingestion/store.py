"""Shared landing path: raw_items dedup → recency + US-relevance gates →
extraction pipeline → fact chain.

Two deterministic gates run at landing time (addendum §3/§4):

  Recency — an item whose real published_at is older than
  cfg('ingestion.news_recency_hours') lands with raw_items.archival=1: it is
  still extracted, chained, searchable and citable, but never creates or
  updates a live story and never broadcasts (processing/extraction.py honors
  the flag). Unparseable or missing published_at counts as FRESH — staleness
  is never guessed.

  US relevance — sources flagged us_domestic=0 (mixed firehoses like the
  Google News topic feed) must show at least one US political signal (state
  name, 'XX-#' district code, tracked candidate name, or a federal-institution
  word) and fewer than two hits from a curated foreign-politics exclusion
  list. Rejected items are never inserted; a per-source daily counter in
  app_meta ('rejects:{source_id}:{YYYYMMDD}') feeds diagnostics.
"""
from __future__ import annotations

import email.utils
import re
from datetime import datetime, timedelta, timezone

from core import db
from core.util import now_iso
from domain.geography import STATES

# ---------------------------------------------------------------------------
# US-relevance gate (deterministic, regex + roster only — no LLM, no network)
# ---------------------------------------------------------------------------

_STATE_NAMES = sorted((v[1] for v in STATES.values()), key=len, reverse=True)
_STATE_NAME_RE = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in _STATE_NAMES) + r")\b")
_DISTRICT_CODE_RE = re.compile(
    r"\b(?:" + "|".join(v[0] for v in STATES.values()) + r")-\d{1,2}\b")
_FEDERAL_RE = re.compile(r"\b(Congress|Senate|House of Representatives|Governor|White House|President)\b")

# Curated foreign-politics exclusion terms. Matching is case-insensitive except
# for the flagged ambiguous proper nouns ('Diet' the Japanese legislature vs.
# diet the noun). 'Mexico' carries a lookbehind so 'New Mexico' never counts.
_FOREIGN_TERMS: list[tuple[str, bool]] = [  # (pattern, case_sensitive)
    (r"\bParliament\b", False), (r"\bBundestag\b", False), (r"\bBundesrat\b", False),
    (r"\bKnesset\b", False), (r"\bDuma\b", False), (r"\bDiet\b", True),
    (r"\bPrime Minister\b", False), (r"\bChancellor\b", True),
    (r"\bEU election\b", False), (r"\bEuropean Union\b", False),
    (r"\bEuropean Parliament\b", False), (r"\bEuropean Commission\b", False),
    (r"\bBrussels\b", False), (r"\bWestminster\b", False), (r"\bDowning Street\b", False),
    (r"\bHolyrood\b", False), (r"\bKremlin\b", False), (r"\bTbilisi\b", False),
    (r"\bUnited Kingdom\b", False), (r"\bBritain\b", False), (r"\bEngland\b", False),
    (r"\bScotland\b", False), (r"\bIreland\b", False),
    (r"\bFrance\b", False), (r"\bGermany\b", False), (r"\bItaly\b", False),
    (r"\bSpain\b", False), (r"\bPortugal\b", False), (r"\bNetherlands\b", False),
    (r"\bBelgium\b", False), (r"\bAustria\b", False), (r"\bPoland\b", False),
    (r"\bHungary\b", False), (r"\bUkraine\b", False), (r"\bRussia\b", False),
    (r"\bIsrael\b", False), (r"\bIran\b", False), (r"\bTurkey\b", True),
    (r"\bIndia\b", False), (r"\bPakistan\b", False), (r"\bChina\b", True),
    (r"\bJapan\b", False), (r"\bSouth Korea\b", False), (r"\bNorth Korea\b", False),
    (r"\bAustralia\b", False), (r"\bCanada\b", False), (r"(?<!New )\bMexico\b", False),
    (r"\bBrazil\b", False), (r"\bArgentina\b", False), (r"\bNigeria\b", False),
]
_FOREIGN_RES = [re.compile(p, 0 if cs else re.IGNORECASE) for p, cs in _FOREIGN_TERMS]

_WORD_RE = re.compile(r"[a-z'’\-]+")


def _candidate_mentioned(text: str) -> bool:
    """True when a tracked candidate's full name or unambiguous surname appears.
    Reuses the extraction alias cache (rebuilt only when the roster grows)."""
    try:
        from processing.extraction import _aliases  # deferred: avoids import cycle
        aliases = _aliases()
    except Exception:
        return False
    low = text.lower()
    tokens = set(_WORD_RE.findall(low))
    for alias in aliases:
        if alias == "__n__":
            continue
        if " " in alias:
            if alias in low:
                return True
        elif alias in tokens:
            return True
    return False


def foreign_hits(text: str) -> int:
    """Distinct foreign-politics exclusion terms present in the text."""
    return sum(1 for rx in _FOREIGN_RES if rx.search(text))


def passes_us_gate(text: str) -> bool:
    """Deterministic PASS/REJECT for items from non-US-domestic firehoses:
    at least one US signal AND fewer than two foreign-politics hits."""
    us_signal = bool(_STATE_NAME_RE.search(text) or _DISTRICT_CODE_RE.search(text)
                     or _FEDERAL_RE.search(text) or _candidate_mentioned(text))
    return us_signal and foreign_hits(text) < 2


def _count_reject(source_id: int) -> None:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    db.execute(
        "INSERT INTO app_meta(key,value) VALUES(?, '1') "
        "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)",
        (f"rejects:{source_id}:{day}",))


# ---------------------------------------------------------------------------
# Recency gate
# ---------------------------------------------------------------------------

def parse_published(published: str | None) -> datetime | None:
    """RFC822 (RSS pubDate) via email.utils, ISO-8601 fallback; naive → UTC.
    None on anything unparseable — the caller must then treat the item as fresh."""
    if not published or not str(published).strip():
        return None
    raw = str(published).strip()
    dt: datetime | None = None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except Exception:
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_archival(published_at: str | None, now: datetime | None = None) -> bool:
    """True when the item's real published_at is older than the recency window.
    Missing/unparseable dates are NEVER archival (staleness is never guessed)."""
    from core.config import cfg
    dt = parse_published(published_at)
    if dt is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - dt) > timedelta(hours=cfg("ingestion.news_recency_hours"))


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------

def land_raw_item(source_id: int, external_id: str, title: str | None, url: str | None,
                  body: str | None, published_at: str | None,
                  matched_profile_id: int | None = None,
                  source_row: dict | None = None) -> int | None:
    """Insert one raw item (UNIQUE(source_id, external_id) dedup — an article both
    the firehose and a targeted search find is never double-counted). Returns the
    raw_item id if new, None if already seen or rejected by the US-relevance gate.

    source_row: the caller's already-loaded sources row (saves a query per item);
    fetched once here when omitted."""
    existing = db.query_one("SELECT id, matched_profile_id FROM raw_items WHERE source_id=? AND external_id=?",
                            (source_id, external_id))
    if existing:
        if matched_profile_id and not existing["matched_profile_id"]:
            db.execute("UPDATE raw_items SET matched_profile_id=? WHERE id=?",
                       (matched_profile_id, existing["id"]))
        return None
    if source_row is None:
        source_row = db.query_one("SELECT * FROM sources WHERE id=?", (source_id,))
    us_domestic = 1
    if source_row is not None:
        try:
            us_domestic = 1 if source_row["us_domestic"] is None else int(source_row["us_domestic"])
        except (KeyError, IndexError):
            us_domestic = 1  # pre-migration row shape — gate off, never guess
    if not us_domestic:
        if not passes_us_gate(" ".join(filter(None, [title, body]))):
            _count_reject(source_id)
            return None
    archival = 1 if is_archival(published_at) else 0
    rid = db.execute(
        "INSERT INTO raw_items(source_id,external_id,fetched_at,title,url,body,published_at,"
        "matched_profile_id,archival) VALUES(?,?,?,?,?,?,?,?,?)",
        (source_id, external_id, now_iso(), title, url, body, published_at, matched_profile_id, archival))
    from processing.extraction import process_raw_item  # deferred: avoids import cycle
    try:
        process_raw_item(rid)
    except Exception:
        import traceback
        traceback.print_exc()  # extraction failure never kills ingestion of the next item
    return rid
