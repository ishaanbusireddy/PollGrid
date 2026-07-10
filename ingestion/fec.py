"""OpenFEC ingestion (api.open.fec.gov). DEMO_KEY works throttled; a registered
key raises the ceiling to 1,000/hour. Seeds/refreshes the candidate roster (a
stub the instant a filing record exists), links candidates to races, and pulls
finance totals — the deterministic input to the fundraising-ratio term and the
corroboration engine's finance signal."""
from __future__ import annotations

import os

from core import db
from core.util import now_iso
from domain.entities import sync_merge_candidate
from ingestion import budget
from ingestion.http import get_json
from ingestion.scheduler import register

OFFICE_MAP = {"P": "president", "S": "senate", "H": "house"}
CYCLES = [2026, 2028]


def _api_key(source: dict) -> str:
    return os.environ.get(source["api_key_env"] or "", "") or "DEMO_KEY"


def _link_race(candidate_id: int, office: str, state_fips: str | None,
               district: int | None, cycle: int, party: str | None, incumbent: bool) -> None:
    if office == "house" and state_fips is not None and district is not None:
        race = db.query_one(
            "SELECT r.id FROM races r JOIN congressional_districts d "
            "ON d.district_version_id=r.district_version_id "
            "WHERE r.race_type='house' AND r.cycle_year=? AND d.state_fips=? AND d.district_number=?",
            (cycle, state_fips, district))
    elif office == "president":
        race = db.query_one("SELECT id FROM races WHERE race_type='president' AND cycle_year=? "
                            "AND state_fips IS NULL", (cycle,))
    else:
        race = db.query_one("SELECT id FROM races WHERE race_type=? AND cycle_year=? AND state_fips=?",
                            (office, cycle, state_fips))
    if race:
        db.execute("INSERT OR IGNORE INTO race_candidates(race_id,candidate_id,party_code,is_incumbent) "
                   "VALUES(?,?,?,?)", (race["id"], candidate_id, party, int(incumbent)))


@register("fec")
def run(source: dict) -> None:
    from domain.geography import USPS_TO_FIPS
    key = _api_key(source)
    base = source["url"]
    page = int(db.meta_get("fec_page", "1"))
    cycle = CYCLES[int(db.meta_get("fec_cycle_idx", "0")) % len(CYCLES)]

    budget.spend("fec")
    data = get_json(f"{base}/candidates/", {
        "api_key": key, "election_year": cycle, "per_page": 100, "page": page,
        "sort": "name", "is_active_candidate": "true",
    })
    for c in data.get("results", []):
        office = OFFICE_MAP.get(c.get("office"))
        if not office:
            continue
        usps = c.get("state") or ""
        state_fips = USPS_TO_FIPS.get(usps) if usps and usps != "US" else None
        district = int(c["district"]) if c.get("district") and c["district"].isdigit() else None
        party3 = (c.get("party") or "")[:3].upper()
        party = party3 if party3 in ("DEM", "REP", "LIB", "GRN", "IND") else ("OTH" if party3 else None)
        cid = sync_merge_candidate(
            c["candidate_id"], c.get("name", "?").title(), party, state_fips, office, district,
            min(c.get("cycles") or [cycle]), max(c.get("cycles") or [cycle]))
        _link_race(cid, office, state_fips, district, cycle, party,
                   c.get("incumbent_challenge") == "I")

    pages = (data.get("pagination") or {}).get("pages", 1)
    if page >= pages:
        db.meta_set("fec_page", "1")
        db.meta_set("fec_cycle_idx", str((int(db.meta_get("fec_cycle_idx", "0")) + 1) % len(CYCLES)))
        db.meta_set("fec_roster_synced_at", now_iso())
        from domain.races import rebuild_search_profiles
        rebuild_search_profiles()  # new filers get hunted for from the instant they file
        _pull_finance_batch(base, key, cycle)
    else:
        db.meta_set("fec_page", str(page + 1))


def _pull_finance_batch(base: str, key: str, cycle: int) -> None:
    """Totals for candidates linked to competitive races — fundraising trend input."""
    rows = db.query(
        "SELECT DISTINCT c.id, c.fec_candidate_id FROM candidates c "
        "JOIN race_candidates rc ON rc.candidate_id=c.id "
        "WHERE c.fec_candidate_id IS NOT NULL AND c.synced_at IS NOT NULL "
        "ORDER BY COALESCE(c.last_cycle,0) DESC LIMIT 20")
    for r in rows:
        try:
            budget.spend("fec")
        except Exception:
            return
        data = get_json(f"{base}/candidate/{r['fec_candidate_id']}/totals/",
                        {"api_key": key, "cycle": cycle, "per_page": 1})
        for t in data.get("results", [])[:1]:
            with db.write() as conn:
                conn.execute("DELETE FROM donors_aggregated WHERE candidate_id=? AND cycle_year=? "
                             "AND contributor_name='__totals__'", (r["id"], cycle))
                conn.execute(
                    "INSERT INTO donors_aggregated(candidate_id,contributor_name,total_amount,"
                    "n_contributions,cycle_year,source) VALUES(?,?,?,?,?,?)",
                    (r["id"], "__totals__", t.get("receipts") or 0, 0, cycle,
                     f"openfec:totals:{cycle}"))
