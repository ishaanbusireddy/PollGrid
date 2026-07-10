"""Parties, candidates, officeholders — the three-tier acquisition pattern:
1. curated floor (hand-seeded, cited, never blank on day one)
2. free structured-API sync (FEC/Congress.gov; additive, COALESCE-style,
   never overwrites a curated value, leaves synced rows alone thereafter)
3. LLM gap-fill (background, cached, merged over the floor, never blocking)
"""
from __future__ import annotations

from core import db
from core.util import now_iso
from domain.geography import USPS_TO_FIPS

PARTIES = [
    # code, name, color, platform_summary, citation
    ("DEM", "Democratic Party", "#3b6fb5",
     "Center-left major party; platform per the Democratic National Committee.",
     "hand-seeded 2026-07; democrats.org"),
    ("REP", "Republican Party", "#c04f4f",
     "Center-right major party; platform per the Republican National Committee.",
     "hand-seeded 2026-07; gop.com"),
    ("LIB", "Libertarian Party", "#d9b23c", "Libertarian minor party.", "hand-seeded 2026-07; lp.org"),
    ("GRN", "Green Party", "#4f9c5c", "Green minor party.", "hand-seeded 2026-07; gp.org"),
    ("IND", "Independent", "#8a8f98", "No party affiliation.", "hand-seeded 2026-07"),
    ("OTH", "Other", "#6d7278", "All other parties and write-ins.", "hand-seeded 2026-07"),
]

# Curated floor: a small, stable set of sitting officeholders whose terms run
# past this seed's vintage. Every row cited; sync (FEC/Congress.gov) thickens
# coverage to every filer — see the manual §04 and ingestion/fec.py.
CURATED_CANDIDATES = [
    # name, party, usps, office, district, bio, citation
    ("Gavin Newsom", "DEM", "CA", "governor", None,
     "Governor of California, first elected 2018; term ends January 2027.",
     "hand-seeded 2026-07; gov.ca.gov"),
    ("Greg Abbott", "REP", "TX", "governor", None,
     "Governor of Texas, first elected 2014.", "hand-seeded 2026-07; gov.texas.gov"),
    ("Kathy Hochul", "DEM", "NY", "governor", None,
     "Governor of New York, took office 2021, elected 2022.", "hand-seeded 2026-07; governor.ny.gov"),
    ("JB Pritzker", "DEM", "IL", "governor", None,
     "Governor of Illinois, first elected 2018.", "hand-seeded 2026-07; illinois.gov"),
    ("Ron DeSantis", "REP", "FL", "governor", None,
     "Governor of Florida, first elected 2018; term-limited in 2026.", "hand-seeded 2026-07; flgov.com"),
    ("Gretchen Whitmer", "DEM", "MI", "governor", None,
     "Governor of Michigan, first elected 2018; term-limited in 2026.", "hand-seeded 2026-07; michigan.gov"),
    ("Josh Shapiro", "DEM", "PA", "governor", None,
     "Governor of Pennsylvania, elected 2022.", "hand-seeded 2026-07; pa.gov"),
    ("Brian Kemp", "REP", "GA", "governor", None,
     "Governor of Georgia, first elected 2018; term-limited in 2026.", "hand-seeded 2026-07; gov.georgia.gov"),
    ("Susan Collins", "REP", "ME", "senate", None,
     "Senator from Maine (Class 2), first elected 1996; seat up in 2026.", "hand-seeded 2026-07; collins.senate.gov"),
    ("Jon Ossoff", "DEM", "GA", "senate", None,
     "Senator from Georgia (Class 2), elected in the January 2021 runoff; seat up in 2026.",
     "hand-seeded 2026-07; ossoff.senate.gov"),
    ("Bernie Sanders", "IND", "VT", "senate", None,
     "Senator from Vermont (Class 1), first elected 2006.", "hand-seeded 2026-07; sanders.senate.gov"),
]


def seed() -> None:
    with db.write() as conn:
        for code, name, color, summary, citation in PARTIES:
            conn.execute("INSERT OR IGNORE INTO parties(code,name,color,platform_summary,citation) "
                         "VALUES(?,?,?,?,?)", (code, name, color, summary, citation))
        for name, party, usps, office, dist, bio, citation in CURATED_CANDIDATES:
            conn.execute(
                "INSERT OR IGNORE INTO candidates(name,party_code,state_fips,office,district_number,"
                "bio,curated,citation) SELECT ?,?,?,?,?,?,1,? WHERE NOT EXISTS "
                "(SELECT 1 FROM candidates WHERE name=? AND state_fips=?)",
                (name, party, USPS_TO_FIPS[usps], office, dist, bio, citation, name, USPS_TO_FIPS[usps]))


def sync_merge_candidate(fec_id: str, name: str, party_code: str | None, state_fips: str | None,
                         office: str | None, district: int | None, first_cycle: int | None,
                         last_cycle: int | None) -> int:
    """Tier-2 sync: additive. Creates a stub the instant a filing record exists;
    COALESCE-merge onto existing rows; never overwrites curated values."""
    row = db.query_one("SELECT * FROM candidates WHERE fec_candidate_id=?", (fec_id,))
    if row is None and state_fips and office:
        row = db.query_one(
            "SELECT * FROM candidates WHERE name=? AND state_fips=? AND fec_candidate_id IS NULL",
            (name, state_fips))
    if row is None:
        return db.execute(
            "INSERT INTO candidates(fec_candidate_id,name,party_code,state_fips,office,district_number,"
            "first_cycle,last_cycle,synced_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (fec_id, name, party_code, state_fips, office, district, first_cycle, last_cycle, now_iso()))
    if row["curated"]:
        db.execute("UPDATE candidates SET fec_candidate_id=COALESCE(fec_candidate_id,?), "
                   "first_cycle=COALESCE(first_cycle,?), last_cycle=?, synced_at=? WHERE id=?",
                   (fec_id, first_cycle, last_cycle or row["last_cycle"], now_iso(), row["id"]))
    else:
        db.execute("UPDATE candidates SET fec_candidate_id=COALESCE(fec_candidate_id,?), "
                   "party_code=COALESCE(party_code,?), office=COALESCE(office,?), "
                   "district_number=COALESCE(district_number,?), first_cycle=COALESCE(first_cycle,?), "
                   "last_cycle=?, synced_at=? WHERE id=?",
                   (fec_id, party_code, office, district, first_cycle,
                    last_cycle or row["last_cycle"], now_iso(), row["id"]))
    return row["id"]


def ai_fill_candidate(candidate_id: int) -> None:
    """Tier-3 gap-fill: background LLM prose over the floor. Cached (ai_filled_at),
    merged with COALESCE so it never overwrites curated or synced values, and a
    page is never blocked on it — callers kick this on a daemon thread."""
    row = db.query_one("SELECT * FROM candidates WHERE id=?", (candidate_id,))
    if row is None or row["ai_filled_at"] or (row["bio"] and row["positions_summary"]):
        return
    from analyst.llm import complete_json  # local import: analyst layer is optional at runtime
    prompt = (
        "Write a neutral, factual 2-3 sentence bio and a 1-2 sentence plain-language "
        "policy positions summary for this US political candidate, using ONLY the fields "
        f"given (do not invent facts): {dict(row)!r}. "
        'Return JSON {"bio": "...", "positions_summary": "..."}.')
    out = complete_json(prompt, purpose="dossier")
    if not out:
        return  # no provider — page keeps showing the floor; never an error
    db.execute("UPDATE candidates SET bio=COALESCE(bio,?), positions_summary=COALESCE(positions_summary,?), "
               "ai_filled_at=? WHERE id=?",
               (out.get("bio"), out.get("positions_summary"), now_iso(), candidate_id))
