"""The influence ledger's curated floor (addendum §10): a sector taxonomy and a
seed of well-known PACs/lobbies/advocacy orgs so the directory is never blank
on day one. Every organization gets identical treatment — categorized by its
real, disclosed policy sector, never by anyone's religion, ethnicity, or
nationality. FEC /committees/ and Senate LDA sync (ingestion/) thicken and
COALESCE onto this floor; endorsements are ONLY ever recorded from an org's
own public announcement (schema-enforced source_url NOT NULL)."""
from __future__ import annotations

from core import db

SECTORS = [
    "energy_oil_gas", "energy_renewable", "defense", "pharma_health", "tech",
    "finance_banking", "labor", "agriculture", "gun_rights", "gun_control",
    "pro_israel_policy", "pro_palestinian_advocacy", "environment", "education",
    "trial_lawyers", "real_estate", "telecom", "transportation", "crypto",
    "senior_advocacy", "veterans", "civil_rights", "religious_advocacy",
    "single_issue_other", "party_committee", "uncategorized",
]

# name, sector, org_type, fec_committee_id (None = filled by sync), citation
CURATED_ORGS = [
    ("National Rifle Association PVF", "gun_rights", "pac", None,
     "hand-seeded 2026-07; nrapvf.org / FEC filings"),
    ("Everytown for Gun Safety Victory Fund", "gun_control", "super_pac", None,
     "hand-seeded 2026-07; everytown.org / FEC filings"),
    ("AIPAC PAC", "pro_israel_policy", "pac", None,
     "hand-seeded 2026-07; aipacpac.org / FEC filings"),
    ("United Democracy Project", "pro_israel_policy", "super_pac", None,
     "hand-seeded 2026-07; FEC filings (AIPAC-affiliated super PAC)"),
    ("J Street PAC", "pro_israel_policy", "pac", None,
     "hand-seeded 2026-07; jstreetpac.org / FEC filings"),
    ("American Petroleum Institute", "energy_oil_gas", "trade_assoc", None,
     "hand-seeded 2026-07; api.org / Senate LDA filings"),
    ("Chevron Corporation PAC", "energy_oil_gas", "pac", None,
     "hand-seeded 2026-07; FEC filings"),
    ("League of Conservation Voters Action Fund", "environment", "super_pac", None,
     "hand-seeded 2026-07; lcv.org / FEC filings"),
    ("PhRMA", "pharma_health", "trade_assoc", None,
     "hand-seeded 2026-07; phrma.org / Senate LDA filings"),
    ("American Medical Association PAC", "pharma_health", "pac", None,
     "hand-seeded 2026-07; FEC filings"),
    ("National Association of Realtors PAC", "real_estate", "pac", None,
     "hand-seeded 2026-07; FEC filings"),
    ("US Chamber of Commerce", "finance_banking", "trade_assoc", None,
     "hand-seeded 2026-07; uschamber.com / Senate LDA filings"),
    ("AFL-CIO COPE", "labor", "pac", None,
     "hand-seeded 2026-07; aflcio.org / FEC filings"),
    ("SEIU COPE", "labor", "pac", None,
     "hand-seeded 2026-07; seiu.org / FEC filings"),
    ("National Education Association PAC", "education", "pac", None,
     "hand-seeded 2026-07; FEC filings"),
    ("Lockheed Martin Employees' PAC", "defense", "pac", None,
     "hand-seeded 2026-07; FEC filings"),
    ("Boeing Company PAC", "defense", "pac", None,
     "hand-seeded 2026-07; FEC filings"),
    ("Fairshake", "crypto", "super_pac", None,
     "hand-seeded 2026-07; FEC filings"),
    ("AARP", "senior_advocacy", "advocacy", None,
     "hand-seeded 2026-07; aarp.org / Senate LDA filings"),
    ("Club for Growth Action", "single_issue_other", "super_pac", None,
     "hand-seeded 2026-07; FEC filings"),
    ("Emily's List", "single_issue_other", "pac", None,
     "hand-seeded 2026-07; emilyslist.org / FEC filings"),
    ("Senate Leadership Fund", "party_committee", "super_pac", None,
     "hand-seeded 2026-07; FEC filings"),
    ("Senate Majority PAC", "party_committee", "super_pac", None,
     "hand-seeded 2026-07; FEC filings"),
    ("Congressional Leadership Fund", "party_committee", "super_pac", None,
     "hand-seeded 2026-07; FEC filings"),
    ("House Majority PAC", "party_committee", "super_pac", None,
     "hand-seeded 2026-07; FEC filings"),
    ("CAIR Action", "civil_rights", "advocacy", None,
     "hand-seeded 2026-07; FEC/LDA filings"),
    ("NAACP National Voter Fund", "civil_rights", "advocacy", None,
     "hand-seeded 2026-07; FEC filings"),
    ("Google LLC (lobbying)", "tech", "lobbying_firm", None,
     "hand-seeded 2026-07; Senate LDA filings"),
    ("Meta Platforms (lobbying)", "tech", "lobbying_firm", None,
     "hand-seeded 2026-07; Senate LDA filings"),
    ("American Farm Bureau Federation", "agriculture", "trade_assoc", None,
     "hand-seeded 2026-07; Senate LDA filings"),
]


def seed() -> None:
    with db.write() as conn:
        for name, sector, org_type, fec_id, citation in CURATED_ORGS:
            conn.execute(
                "INSERT OR IGNORE INTO lobbying_orgs(name,sector,org_type,fec_committee_id,citation) "
                "VALUES(?,?,?,?,?)", (name, sector, org_type, fec_id, citation))


def ensure_org(name: str, sector: str = "uncategorized", org_type: str = "pac",
               fec_committee_id: str | None = None) -> int:
    """Sync path: find-or-create an org row; never overwrites a curated sector."""
    row = db.query_one("SELECT id FROM lobbying_orgs WHERE name=? OR "
                       "(fec_committee_id IS NOT NULL AND fec_committee_id=?)",
                       (name, fec_committee_id))
    if row:
        if fec_committee_id:
            db.execute("UPDATE lobbying_orgs SET fec_committee_id=COALESCE(fec_committee_id,?) WHERE id=?",
                       (fec_committee_id, row["id"]))
        return row["id"]
    return db.execute(
        "INSERT INTO lobbying_orgs(name,sector,org_type,fec_committee_id,citation) VALUES(?,?,?,?,?)",
        (name, sector, org_type, fec_committee_id, "auto-created from FEC/LDA sync"))
