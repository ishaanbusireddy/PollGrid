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
from ingestion.http import BudgetExhausted, get_json
from ingestion.scheduler import register

OFFICE_MAP = {"P": "president", "S": "senate", "H": "house"}
CYCLES = [2026, 2028]
AD_SPEND_PAGES_PER_RUN = 3  # schedule_e pages pulled after each completed roster pass


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
        _pull_ad_spend(base, key, cycle)
        try:  # new filings/finance invalidate the packs of every touched race
            from analyst.context_packs import invalidate_for_race
            for r in db.query("SELECT DISTINCT race_id FROM race_candidates"):
                invalidate_for_race(r["race_id"])
        except Exception:
            pass
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


# ---------------------------------------------------------------------------
# Independent expenditures (schedule E) → ad_spend. Runs after the roster pass
# completes; a few budget-checked pages per run, degrading independently of
# the roster/finance paths (a schedule_e hiccup never fails the whole source).
# ---------------------------------------------------------------------------

def _medium_from_purpose(text: str | None) -> str:
    """Deterministic medium heuristic over the expenditure purpose text.
    Digital is checked before mail so 'email' never reads as postal mail."""
    t = (text or "").lower()
    if any(w in t for w in ("tv", "television", "broadcast", "cable")):
        return "tv"
    if "radio" in t:
        return "radio"
    if any(w in t for w in ("digital", "online", "internet", "email", "e-mail", "social media",
                            "facebook", "google", "youtube", "streaming", "web", "sms",
                            "text message", "texting")):
        return "digital"
    if any(w in t for w in ("mail", "postage", "postcard", "printing", "print")):
        return "mail"
    return "other"


def _race_for_fec_candidate(fec_candidate_id: str) -> int | None:
    row = db.query_one(
        "SELECT rc.race_id FROM candidates c JOIN race_candidates rc ON rc.candidate_id=c.id "
        "JOIN races r ON r.id=rc.race_id WHERE c.fec_candidate_id=? "
        "ORDER BY r.cycle_year DESC LIMIT 1", (fec_candidate_id,))
    return row["race_id"] if row else None


def _land_schedule_e(results: list[dict]) -> int:
    """One ad_spend row per schedule-E expenditure that joins to a race via
    candidate_id → race_candidates. Dedup per (race,sponsor,medium,as_of,amount):
    the table has no unique constraint, so a pre-check guards the insert."""
    landed = 0
    for e in results:
        fec_id = e.get("candidate_id")
        amount = e.get("expenditure_amount")
        as_of = (e.get("expenditure_date") or "")[:10]
        if not fec_id or amount in (None, "") or not as_of:
            continue
        race_id = _race_for_fec_candidate(fec_id)
        if race_id is None:
            continue  # can't attribute — skip rather than land an orphan row
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            continue
        sponsor = ((e.get("committee") or {}).get("name") or e.get("committee_name")
                   or e.get("committee_id") or "unknown committee")
        medium = _medium_from_purpose(e.get("expenditure_description") or e.get("purpose"))
        if db.query_one("SELECT 1 FROM ad_spend WHERE race_id=? AND sponsor=? AND medium=? "
                        "AND as_of=? AND amount=?", (race_id, sponsor, medium, as_of, amount)):
            continue
        db.execute("INSERT OR IGNORE INTO ad_spend(race_id,sponsor,medium,amount,as_of,source) "
                   "VALUES(?,?,?,?,?,?)",
                   (race_id, sponsor, medium, amount, as_of, "openfec:schedule_e"))
        landed += 1
    return landed


def _pull_ad_spend(base: str, key: str, cycle: int) -> None:
    """/schedules/schedule_e/ uses seek pagination (pagination.last_indexes),
    cycle-scoped, newest first. Budget-checked per page; exhaustion or a fetch
    error just ends this batch — the next completed roster pass resumes."""
    params: dict = {"api_key": key, "cycle": cycle, "per_page": 100,
                    "sort": "-expenditure_date"}
    for _ in range(AD_SPEND_PAGES_PER_RUN):
        try:
            budget.spend("fec")
        except BudgetExhausted:
            return
        try:
            data = get_json(f"{base}/schedules/schedule_e/", params)
        except Exception:
            return  # schedule_e degrades independently of the roster path
        _land_schedule_e(data.get("results") or [])
        last_indexes = (data.get("pagination") or {}).get("last_indexes") or {}
        if not last_indexes:
            return
        params = {**params, **last_indexes}
