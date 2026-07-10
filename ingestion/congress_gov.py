"""Congress.gov API (key-gated; free). Bills + sponsorship feed the ideology
proxy and the legislation fact category. Honest caveat carried over from the
manual: House roll-call coverage starts with the 118th Congress; deep roll-call
history comes from VoteView (scripts/backfill_voteview.py), not from here."""
from __future__ import annotations

import os

from ingestion import budget
from ingestion.http import SourceNotConfigured, get_json
from ingestion.scheduler import register
from ingestion.store import land_raw_item


@register("congress_gov")
def run(source: dict) -> None:
    key = os.environ.get(source["api_key_env"] or "")
    if not key:
        raise SourceNotConfigured("CONGRESS_GOV_API_KEY not set")
    budget.spend("congress_gov")
    data = get_json(f"{source['url']}/bill", {"api_key": key, "limit": 50, "sort": "updateDate+desc"})
    for bill in data.get("bills", []):
        ext = f"{bill.get('congress')}-{bill.get('type')}-{bill.get('number')}"
        title = bill.get("title") or ext
        land_raw_item(source["id"], ext, f"[{bill.get('type', '?')}{bill.get('number', '')}] {title}",
                      bill.get("url"), bill.get("latestAction", {}).get("text"),
                      bill.get("updateDate"))
