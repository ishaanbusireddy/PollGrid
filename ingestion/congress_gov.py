"""Congress.gov API (key-gated; free). Bills + sponsorship feed the ideology
proxy and the legislation fact category, and the MEMBER roster feeds the
current-officeholder panels (all 435 House reps + senate refresh — the
hand-seeded scripts/seed_officeholders.py covers governors + senators at boot;
this sync corrects/refreshes and adds the House, which is too big and too
churn-prone to hand-seed). Honest caveat carried over from the manual: House
roll-call coverage starts with the 118th Congress; deep roll-call history
comes from VoteView (scripts/backfill_voteview.py), not from here."""
from __future__ import annotations

import os

from core import db
from core.util import today
from ingestion import budget
from ingestion.http import SourceNotConfigured, get_json
from ingestion.scheduler import register
from ingestion.store import land_raw_item

_PARTY = {"democratic": "DEM", "democrat": "DEM", "republican": "REP",
          "independent": "IND", "libertarian": "LIB", "green": "GRN"}


def _party3(party_name: str | None) -> str:
    return _PARTY.get((party_name or "").strip().lower(), "OTH")


def _sync_members(source: dict, key: str) -> int:
    """Pull the full current-member roster (House + Senate) and reconcile the
    officeholders table. Runs at most once a day (meta-gated) — the roster
    changes rarely and the full paginated pull is ~3 requests."""
    if db.meta_get("congress_members_synced_at", "")[:10] == today():
        return 0
    from domain.geography import STATES
    name_to_fips = {name.lower(): fips for fips, (_, name, _) in STATES.items()}
    from scripts.seed_officeholders import sync_multi_seat, upsert_officeholder
    house: dict[tuple[str, int], tuple[str, str]] = {}
    senate: dict[str, list[tuple[str, str]]] = {}
    offset = 0
    while True:
        budget.spend("congress_gov")
        data = get_json(f"{source['url']}/member",
                        {"api_key": key, "limit": 250, "offset": offset, "currentMember": "true"})
        members = data.get("members", [])
        if not members:
            break
        for m in members:
            fips = name_to_fips.get((m.get("state") or "").strip().lower())
            if not fips:
                continue
            # congress.gov gives "Last, First Middle" — normalize to "First Last"
            raw = m.get("name") or ""
            name = " ".join(reversed([p.strip() for p in raw.split(",", 1)])) if "," in raw else raw
            party = _party3(m.get("partyName"))
            terms = (m.get("terms") or {}).get("item") or []
            chamber = (terms[-1].get("chamber") or "") if terms else ""
            if "House" in chamber:
                house[(fips, int(m.get("district") or 0))] = (name, party)
            elif "Senate" in chamber:
                senate.setdefault(fips, []).append((name, party))
        offset += 250
        if offset >= int((data.get("pagination") or {}).get("count") or 0):
            break
    changed = 0
    with db.write() as conn:
        for (fips, dn), (name, party) in house.items():
            changed += upsert_officeholder(conn, name, party, "house", fips, dn, today())
        for fips, people in senate.items():
            changed += sync_multi_seat(conn, "senate", fips, people, today())
    db.meta_set("congress_members_synced_at", today())
    return changed


@register("congress_gov")
def run(source: dict) -> None:
    key = os.environ.get(source["api_key_env"] or "")
    if not key:
        raise SourceNotConfigured("CONGRESS_GOV_API_KEY not set")
    n = _sync_members(source, key)
    if n:
        print(f"congress.gov member roster: {n} officeholder change(s)")
    budget.spend("congress_gov")
    data = get_json(f"{source['url']}/bill", {"api_key": key, "limit": 50, "sort": "updateDate+desc"})
    for bill in data.get("bills", []):
        ext = f"{bill.get('congress')}-{bill.get('type')}-{bill.get('number')}"
        title = bill.get("title") or ext
        land_raw_item(source["id"], ext, f"[{bill.get('type', '?')}{bill.get('number', '')}] {title}",
                      bill.get("url"), bill.get("latestAction", {}).get("text"),
                      bill.get("updateDate"))
