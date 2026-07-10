"""The race-search-profile hunter (§15). Deliberately not GDELT. Per-query
Google News RSS, budget-capped with competitiveness-weighted rotation (review
§4.2): competitive races get searched far more often than safe seats; a 429 or
captcha response degrades the source rather than retrying tighter."""
from __future__ import annotations

import json
import urllib.parse

from core import db
from core.util import now_iso
from ingestion import budget
from ingestion.http import FetchError, get
from ingestion.rss import parse_feed
from ingestion.scheduler import register
from ingestion.store import land_raw_item

_PER_RUN = 6  # profiles searched per tick; rotation cursor lives in app_meta


def _next_profiles(n: int) -> list[dict]:
    # competitive races first, then least-recently-hunted
    return db.query(
        "SELECT p.*, r.competitiveness, r.name AS race_name FROM race_search_profiles p "
        "JOIN races r ON r.id=p.race_id "
        "ORDER BY CASE r.competitiveness WHEN 'tossup' THEN 0 WHEN 'lean' THEN 1 "
        "WHEN 'likely' THEN 2 ELSE 3 END, COALESCE(p.last_hit_at,'') ASC LIMIT ?", (n,))


@register("targeted_search")
def run(source: dict) -> None:
    for profile in _next_profiles(_PER_RUN):
        terms = json.loads(profile["terms_json"])
        if not terms:
            continue
        query = terms[0] if len(terms) == 1 else f'"{terms[0]}"'
        budget.spend("targeted_search")
        url = f"{source['url']}?q={urllib.parse.quote(query)}&hl=en-US&gl=US&ceid=US:en"
        try:
            items = parse_feed(get(url))
        except FetchError as e:
            if "429" in str(e) or "403" in str(e):
                raise  # scheduler marks degraded; never retried in a tight loop
            items = []
        for item in items[:20]:
            if item["id"]:
                rid = land_raw_item(source["id"], item["id"], item["title"], item["link"],
                                    item["body"], item["published"], matched_profile_id=profile["id"])
                if rid:
                    db.execute("INSERT OR IGNORE INTO article_entity_links(raw_item_id,entity_type,entity_id) "
                               "VALUES(?,?,?)", (rid, "race", profile["race_id"]))
        db.execute("UPDATE race_search_profiles SET last_hit_at=? WHERE id=?", (now_iso(), profile["id"]))
