"""Extraction: text → categorized, canonicalized, geocoded, hash-chained facts.

Entity recognition: spaCy if installed, deterministic capitalized-run regex
fallback otherwise (the auto-detect-with-fallback idiom). Category: word-boundary
keyword matching with a FIXED tie-break order. Canonicalization: exact-alias
cache then fuzzy match. Geocoding: state/county mention via the gazetteer.
"""
from __future__ import annotations

import difflib
import json
import re

from core import db, provenance
from core.util import cosine, embed, now_iso
from domain.geography import STATES, USPS_TO_FIPS

try:  # optional dependency, auto-detected
    import spacy  # type: ignore
    _NLP = spacy.load("en_core_web_sm")
except Exception:
    _NLP = None

# fixed tie-break order — first match wins on equal counts ("war on drugs"
# doesn't trip anything here because matching is politics-tuned and bounded)
CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("election_result", ["certified", "election results", "wins race", "concedes", "recount"]),
    ("polling", ["poll", "survey", "approval rating", "favorability", "leads by"]),
    ("finance", ["fundraising", "fec filing", "donation", "donor", "super pac", "campaign finance", "ad buy"]),
    ("legislation", ["bill", "vote on", "roll call", "amendment", "committee", "senate passed", "house passed"]),
    ("endorsement", ["endorse", "endorsement", "backs", "throws support"]),
    ("scandal", ["scandal", "indictment", "investigation", "resign", "allegation", "subpoena"]),
    ("debate", ["debate", "town hall", "moderator"]),
    ("campaign_event", ["rally", "campaign stop", "launches campaign", "announces run", "kickoff"]),
    ("rhetoric", ["speech", "remarks", "statement", "press release", "interview"]),
]

_CAP_RUN = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b")

_alias_cache: dict[str, int] | None = None


def _aliases() -> dict[str, int]:
    """candidate name variants → canonical id ('Gov. Whitmer'/'Whitmer'/'Gretchen
    Whitmer' collapse to one row). Rebuilt when the roster grows."""
    global _alias_cache
    n = db.query_one("SELECT COUNT(*) c FROM candidates")["c"]
    if _alias_cache is not None and _alias_cache.get("__n__") == n:
        return _alias_cache
    cache: dict = {"__n__": n}
    for c in db.query("SELECT id, name FROM candidates"):
        full = c["name"].strip()
        cache[full.lower()] = c["id"]
        parts = full.split()
        if len(parts) > 1 and len(parts[-1]) > 3:
            cache.setdefault(parts[-1].lower(), c["id"])
    _alias_cache = cache
    return cache


def classify(text: str) -> str:
    low = text.lower()
    best, best_n = "other", 0
    for cat, words in CATEGORY_KEYWORDS:  # fixed order = fixed tie-break
        n = sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", low))
        if n > best_n:
            best, best_n = cat, n
    return best


def extract_entities(text: str) -> list[str]:
    if _NLP is not None:
        return list({e.text for e in _NLP(text[:5000]).ents if e.label_ in ("PERSON", "GPE", "ORG")})
    return list({m.group(1) for m in _CAP_RUN.finditer(text[:5000])})[:20]


def canonicalize(names: list[str]) -> list[int]:
    aliases = _aliases()
    ids: set[int] = set()
    keys = [k for k in aliases if k != "__n__"]
    for name in names:
        low = re.sub(r"^(gov|sen|rep|sec|pres)\.?\s+", "", name.lower())
        if low in aliases:
            ids.add(aliases[low])
            continue
        close = difflib.get_close_matches(low, keys, n=1, cutoff=0.92)
        if close:
            ids.add(aliases[close[0]])
    return sorted(ids)


def geocode(text: str) -> tuple[str | None, str | None]:
    """→ (state_fips, county_geoid). Dateline first, then state names/codes,
    then 'X County'-style county mentions scoped to the found state."""
    state_fips = None
    for fips, (usps, name, _) in STATES.items():
        if re.search(rf"\b{re.escape(name)}\b", text) or re.search(rf"\b{usps}-\d", text):
            state_fips = fips
            break
    county_geoid = None
    m = re.search(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s+(County|Parish|Borough)\b", text)
    if m and state_fips:
        row = db.query_one("SELECT geoid FROM county_equivalents WHERE state_fips=? AND name LIKE ?",
                           (state_fips, m.group(1) + "%"))
        county_geoid = row["geoid"] if row else None
    return state_fips, county_geoid


def _match_race(text: str, candidate_ids: list[int], state_fips: str | None) -> int | None:
    for cid in candidate_ids:
        row = db.query_one(
            "SELECT rc.race_id FROM race_candidates rc JOIN races r ON r.id=rc.race_id "
            "WHERE rc.candidate_id=? ORDER BY r.cycle_year DESC LIMIT 1", (cid,))
        if row:
            return row["race_id"]
    low = text.lower()
    if state_fips:
        for rt, words in (("senate", ["senate"]), ("governor", ["governor", "gubernatorial"]),
                          ("president", ["president", "presidential"]), ("house", ["house", "congressional"])):
            if any(w in low for w in words):
                row = db.query_one(
                    "SELECT id FROM races WHERE race_type=? AND state_fips=? ORDER BY cycle_year LIMIT 1",
                    (rt, state_fips))
                if row:
                    return row["id"]
    return None


def process_raw_item(raw_item_id: int) -> int | None:
    item = db.query_one("SELECT * FROM raw_items WHERE id=?", (raw_item_id,))
    if item is None:
        return None
    text = " ".join(filter(None, [item["title"], item["body"]]))[:8000]
    if not text.strip():
        return None
    category = classify(text)
    names = extract_entities(text)
    candidate_ids = canonicalize(names)
    state_fips, county_geoid = geocode(text)
    race_id = _match_race(text, candidate_ids, state_fips)
    fact_id = provenance.chained_insert("extracted_facts", {
        "raw_item_id": raw_item_id, "category": category,
        "summary": (item["title"] or text[:200]).strip(),
        "entities_json": json.dumps({"names": names[:10], "candidate_ids": candidate_ids}),
        "race_id": race_id, "state_fips": state_fips, "county_geoid": county_geoid,
        "occurred_at": item["published_at"], "created_at": now_iso(),
        "is_synthetic": item["is_synthetic"],
    })
    for cid in candidate_ids:
        db.execute("INSERT OR IGNORE INTO article_entity_links(raw_item_id,entity_type,entity_id) "
                   "VALUES(?,?,?)", (raw_item_id, "candidate", cid))
    if race_id:
        db.execute("INSERT OR IGNORE INTO article_entity_links(raw_item_id,entity_type,entity_id) "
                   "VALUES(?,?,?)", (raw_item_id, "race", race_id))
    cluster_fact(fact_id)
    return fact_id


def cluster_fact(fact_id: int) -> None:
    """Same-window story clustering: cosine against recent story centroids;
    above threshold joins the story, otherwise a new story is born. The feed
    pushes story clusters, never raw articles."""
    from core.config import cfg
    fact = db.query_one("SELECT * FROM extracted_facts WHERE id=?", (fact_id,))
    if fact is None:
        return
    vec = embed(fact["summary"])
    threshold = cfg("correlation.same_window_similarity_threshold")
    gap_hours = cfg("correlation.same_window_max_gap_hours")
    best, best_sim = None, 0.0
    for story in db.query(
            "SELECT s.id, s.headline FROM stories s WHERE s.updated_at >= datetime('now', ?) "
            "ORDER BY s.updated_at DESC LIMIT 200", (f"-{gap_hours} hours",)):
        sim = cosine(vec, embed(story["headline"]))
        if sim > best_sim:
            best, best_sim = story, sim
    if best and best_sim >= threshold:
        with db.write() as conn:
            conn.execute("INSERT OR IGNORE INTO story_facts(story_id,fact_id) VALUES(?,?)", (best["id"], fact_id))
            conn.execute("UPDATE stories SET updated_at=?, score=score+1 WHERE id=?", (now_iso(), best["id"]))
        story_id = best["id"]
    else:
        story_id = db.execute(
            "INSERT INTO stories(headline,category,race_id,state_fips,score,created_at,updated_at,is_synthetic) "
            "VALUES(?,?,?,?,1,?,?,?)",
            (fact["summary"][:200], fact["category"], fact["race_id"], fact["state_fips"],
             now_iso(), now_iso(), fact["is_synthetic"]))
        db.execute("INSERT OR IGNORE INTO story_facts(story_id,fact_id) VALUES(?,?)", (story_id, fact_id))
    try:
        from api.websocket import broadcast
        story = db.query_one("SELECT * FROM stories WHERE id=?", (story_id,))
        broadcast({"type": "story", "payload": story})
    except Exception:
        pass
