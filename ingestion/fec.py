"""OpenFEC ingestion (api.open.fec.gov). DEMO_KEY works throttled; a registered
key raises the ceiling to 1,000/hour. Seeds/refreshes the candidate roster (a
stub the instant a filing record exists), links candidates to races, and pulls
finance totals — the deterministic input to the fundraising-ratio term and the
corroboration engine's finance signal.

Addendum §9 expansion: after each completed roster pass this adapter also
budget-checks a few pages of /committees/ (→ pacs + the influence ledger),
schedule_a by-contributor aggregates and schedule_b disbursements for
competitive-race candidates (→ donors_aggregated / ad_spend), and schedule_e
independent expenditures (→ ad_spend + pac_candidate_spend). Every branch
degrades independently — a hiccup in one never fails the roster path."""
from __future__ import annotations

import os

from core import db
from core.util import now_iso
from domain import influence
from domain.entities import sync_merge_candidate
from ingestion import budget
from ingestion.http import BudgetExhausted, get_json
from ingestion.scheduler import register

OFFICE_MAP = {"P": "president", "S": "senate", "H": "house"}
CYCLES = [2026, 2028]
AD_SPEND_PAGES_PER_RUN = 3   # schedule_e pages pulled after each completed roster pass
COMMITTEE_PAGES_PER_RUN = 2  # /committees/ pages pulled after each completed roster pass
SCHEDULE_CANDIDATES_PER_RUN = 5  # competitive-race candidates per schedule_a/b batch


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
        _pull_committees(base, key, cycle)
        _pull_top_donors(base, key, cycle)
        _pull_disbursements(base, key, cycle)
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
# Committees (§9a): /committees/ pages → pacs upsert + influence ledger.
# ---------------------------------------------------------------------------

def _org_type_for_committee(committee_type: str | None) -> str:
    """FEC committee_type → lobbying_orgs.org_type. O (independent-expenditure
    only) is a super PAC; N/Q (non-/qualified party-connected PACs) and every
    other designation map to plain 'pac' best-effort."""
    return "super_pac" if (committee_type or "").upper() == "O" else "pac"


def _pull_committees(base: str, key: str, cycle: int) -> None:
    """A few budget-checked /committees/ pages per completed roster pass; page
    cursor persists in app_meta so the whole roster is walked over many runs."""
    page = int(db.meta_get("fec_committees_page", "1"))
    for _ in range(COMMITTEE_PAGES_PER_RUN):
        try:
            budget.spend("fec")
        except BudgetExhausted:
            break
        try:
            data = get_json(f"{base}/committees/", {
                "api_key": key, "cycle": cycle, "per_page": 100, "page": page, "sort": "name"})
        except Exception:
            break  # committees degrade independently of the roster path
        for c in data.get("results") or []:
            fec_committee_id = c.get("committee_id")
            name = (c.get("name") or "").strip().title()
            if not fec_committee_id or not name:
                continue
            with db.write() as conn:
                conn.execute(
                    "INSERT INTO pacs(fec_committee_id,name,type,total_receipts,total_disbursements,"
                    "synced_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(fec_committee_id) DO UPDATE SET name=excluded.name, type=excluded.type, "
                    "total_receipts=COALESCE(excluded.total_receipts,pacs.total_receipts), "
                    "total_disbursements=COALESCE(excluded.total_disbursements,pacs.total_disbursements), "
                    "synced_at=excluded.synced_at",
                    (fec_committee_id, name, c.get("committee_type"),
                     c.get("receipts"), c.get("disbursements"), now_iso()))
            influence.ensure_org(name, "uncategorized",
                                 _org_type_for_committee(c.get("committee_type")), fec_committee_id)
        pages = (data.get("pagination") or {}).get("pages", 1)
        page = 1 if page >= pages else page + 1
    db.meta_set("fec_committees_page", str(page))


# ---------------------------------------------------------------------------
# Schedule A/B (§9b/§9c) for candidates on competitive races: real aggregated
# donor rows and media-purpose disbursements, a few candidates per run.
# ---------------------------------------------------------------------------

SCHEDULE_A_SOURCE = "openfec:schedule_a_by_contributor"


def _competitive_fec_candidates(limit: int) -> list[dict]:
    rows = db.query(
        "SELECT DISTINCT c.id, c.fec_candidate_id FROM candidates c "
        "JOIN race_candidates rc ON rc.candidate_id=c.id "
        "JOIN races r ON r.id=rc.race_id "
        "WHERE c.fec_candidate_id IS NOT NULL AND r.competitiveness IN ('tossup','lean') "
        "ORDER BY c.id")
    if not rows:
        return []
    offset = int(db.meta_get("fec_schedule_cursor", "0")) % len(rows)  # rotate across runs
    db.meta_set("fec_schedule_cursor", str((offset + limit) % len(rows)))
    return (rows + rows)[offset:offset + min(limit, len(rows))]


def _land_donor_aggregates(candidate_id: int, cycle: int, results: list[dict]) -> int:
    """by_contributor aggregates → donors_aggregated rows, pre-delete+insert per
    candidate+cycle (the same idempotency pattern as the __totals__ rows)."""
    rows = []
    for a in results:
        name = (a.get("contributor_name") or "").strip()
        try:
            total = float(a.get("total"))
        except (TypeError, ValueError):
            continue
        if not name:
            continue
        rows.append((candidate_id, name.title(), total, int(a.get("count") or 0),
                     cycle, SCHEDULE_A_SOURCE))
    if rows:
        with db.write() as conn:
            conn.execute("DELETE FROM donors_aggregated WHERE candidate_id=? AND cycle_year=? "
                         "AND source=?", (candidate_id, cycle, SCHEDULE_A_SOURCE))
            conn.executemany(
                "INSERT INTO donors_aggregated(candidate_id,contributor_name,total_amount,"
                "n_contributions,cycle_year,source) VALUES(?,?,?,?,?,?)", rows)
    return len(rows)


def _pull_top_donors(base: str, key: str, cycle: int) -> None:
    for r in _competitive_fec_candidates(SCHEDULE_CANDIDATES_PER_RUN):
        try:
            budget.spend("fec")
        except BudgetExhausted:
            return
        try:
            data = get_json(f"{base}/schedules/schedule_a/by_contributor/", {
                "api_key": key, "candidate_id": r["fec_candidate_id"],
                "cycle": cycle, "per_page": 50})
        except Exception:
            return  # schedule_a degrades independently
        _land_donor_aggregates(r["id"], cycle, data.get("results") or [])


def _insert_ad_spend(race_id: int, sponsor: str, medium: str, amount: float,
                     as_of: str, source: str) -> bool:
    """ad_spend has no unique constraint, so a pre-check guards the insert."""
    if db.query_one("SELECT 1 FROM ad_spend WHERE race_id=? AND sponsor=? AND medium=? "
                    "AND as_of=? AND amount=?", (race_id, sponsor, medium, as_of, amount)):
        return False
    db.execute("INSERT INTO ad_spend(race_id,sponsor,medium,amount,as_of,source) "
               "VALUES(?,?,?,?,?,?)", (race_id, sponsor, medium, amount, as_of, source))
    return True


def _land_schedule_b(candidate_id: int, fec_candidate_id: str, cycle: int,
                     results: list[dict]) -> None:
    """Media-purpose schedule-B disbursements → ad_spend (via the shared medium
    heuristic); the batch total → one '__disbursements__' donors_aggregated row
    per candidate+cycle, pre-delete+insert exactly like __totals__."""
    total, n = 0.0, 0
    race_id = _race_for_fec_candidate(fec_candidate_id)
    for d in results:
        try:
            amount = float(d.get("disbursement_amount"))
        except (TypeError, ValueError):
            continue
        total += amount
        n += 1
        as_of = (d.get("disbursement_date") or "")[:10]
        medium = _medium_from_purpose(d.get("disbursement_description")
                                      or d.get("disbursement_purpose_category"))
        if race_id is None or not as_of or medium == "other":
            continue  # only media-purpose disbursements become ad_spend rows
        sponsor = ((d.get("committee") or {}).get("name") or d.get("committee_id")
                   or "unknown committee")
        _insert_ad_spend(race_id, sponsor, medium, amount, as_of, "openfec:schedule_b")
    with db.write() as conn:
        conn.execute("DELETE FROM donors_aggregated WHERE candidate_id=? AND cycle_year=? "
                     "AND contributor_name='__disbursements__'", (candidate_id, cycle))
        conn.execute(
            "INSERT INTO donors_aggregated(candidate_id,contributor_name,total_amount,"
            "n_contributions,cycle_year,source) VALUES(?,?,?,?,?,?)",
            (candidate_id, "__disbursements__", total, n, cycle, f"openfec:schedule_b:{cycle}"))


def _pull_disbursements(base: str, key: str, cycle: int) -> None:
    for r in _competitive_fec_candidates(SCHEDULE_CANDIDATES_PER_RUN):
        try:
            budget.spend("fec")
        except BudgetExhausted:
            return
        try:
            data = get_json(f"{base}/schedules/schedule_b/", {
                "api_key": key, "candidate_id": r["fec_candidate_id"],
                "two_year_transaction_period": cycle, "per_page": 100,
                "sort": "-disbursement_date"})
        except Exception:
            return  # schedule_b degrades independently
        _land_schedule_b(r["id"], r["fec_candidate_id"], cycle, data.get("results") or [])


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


def _land_schedule_e(results: list[dict], cycle: int) -> int:
    """One ad_spend row per schedule-E expenditure that joins to a race via
    candidate_id → race_candidates (dedup via _insert_ad_spend's pre-check).
    Each IE row ALSO lands in pac_candidate_spend under the spending committee's
    influence-ledger org: spend_type ie_support when support_oppose_indicator
    is 'S', ie_oppose when 'O'. Rows without a clear S/O indicator (e.g.
    contribution-type records that occasionally surface in schedule feeds) are
    skipped for the ledger rather than guessed; a missing committee name is
    likewise skipped rather than minting a junk org."""
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
        committee_name = ((e.get("committee") or {}).get("name") or e.get("committee_name")
                          or "").strip()
        sponsor = committee_name or e.get("committee_id") or "unknown committee"
        medium = _medium_from_purpose(e.get("expenditure_description") or e.get("purpose"))
        if _insert_ad_spend(race_id, sponsor, medium, amount, as_of, "openfec:schedule_e"):
            landed += 1
        soi = (e.get("support_oppose_indicator") or "").strip().upper()
        if soi in ("S", "O") and committee_name:
            org_id = influence.ensure_org(committee_name.title(), "uncategorized", "pac",
                                          (e.get("committee") or {}).get("committee_id")
                                          or e.get("committee_id"))
            cand = db.query_one("SELECT id FROM candidates WHERE fec_candidate_id=?", (fec_id,))
            db.execute(
                "INSERT OR IGNORE INTO pac_candidate_spend(org_id,candidate_id,race_id,amount,"
                "spend_type,cycle_year,as_of,source) VALUES(?,?,?,?,?,?,?,?)",
                (org_id, cand["id"] if cand else None, race_id, amount,
                 "ie_support" if soi == "S" else "ie_oppose", cycle, as_of,
                 "openfec:schedule_e"))
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
        _land_schedule_e(data.get("results") or [], cycle)
        last_indexes = (data.get("pagination") or {}).get("last_indexes") or {}
        if not last_indexes:
            return
        params = {**params, **last_indexes}
