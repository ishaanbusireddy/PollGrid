#!/usr/bin/env python3
"""Seed current officeholders: all 50 governors and all 100 U.S. senators,
hand-transcribed July 2026 (119th Congress; NJ/VA governors from the Nov 2025
elections, FL/OH appointed senators included). House members are NOT seeded
here — the Congress.gov member sync (ingestion/congress_gov.py, needs
CONGRESS_GOV_API_KEY) fills all 435 and refreshes/corrects this roster too.

Idempotent and correction-friendly: for each seat, if the open officeholders
row already names the same person it is left alone; if it names someone else
(seat changed hands / a correction landed) the old row is closed with an
end_date and the new one inserted. Zero network; runs at every boot.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SOURCE = "hand-seeded 2026-07 current-officeholder roster"

# USPS -> (governor name, party)
GOVERNORS: dict[str, tuple[str, str]] = {
    "AL": ("Kay Ivey", "REP"), "AK": ("Mike Dunleavy", "REP"), "AZ": ("Katie Hobbs", "DEM"),
    "AR": ("Sarah Huckabee Sanders", "REP"), "CA": ("Gavin Newsom", "DEM"), "CO": ("Jared Polis", "DEM"),
    "CT": ("Ned Lamont", "DEM"), "DE": ("Matt Meyer", "DEM"), "FL": ("Ron DeSantis", "REP"),
    "GA": ("Brian Kemp", "REP"), "HI": ("Josh Green", "DEM"), "ID": ("Brad Little", "REP"),
    "IL": ("JB Pritzker", "DEM"), "IN": ("Mike Braun", "REP"), "IA": ("Kim Reynolds", "REP"),
    "KS": ("Laura Kelly", "DEM"), "KY": ("Andy Beshear", "DEM"), "LA": ("Jeff Landry", "REP"),
    "ME": ("Janet Mills", "DEM"), "MD": ("Wes Moore", "DEM"), "MA": ("Maura Healey", "DEM"),
    "MI": ("Gretchen Whitmer", "DEM"), "MN": ("Tim Walz", "DEM"), "MS": ("Tate Reeves", "REP"),
    "MO": ("Mike Kehoe", "REP"), "MT": ("Greg Gianforte", "REP"), "NE": ("Jim Pillen", "REP"),
    "NV": ("Joe Lombardo", "REP"), "NH": ("Kelly Ayotte", "REP"), "NJ": ("Mikie Sherrill", "DEM"),
    "NM": ("Michelle Lujan Grisham", "DEM"), "NY": ("Kathy Hochul", "DEM"), "NC": ("Josh Stein", "DEM"),
    "ND": ("Kelly Armstrong", "REP"), "OH": ("Mike DeWine", "REP"), "OK": ("Kevin Stitt", "REP"),
    "OR": ("Tina Kotek", "DEM"), "PA": ("Josh Shapiro", "DEM"), "RI": ("Dan McKee", "DEM"),
    "SC": ("Henry McMaster", "REP"), "SD": ("Larry Rhoden", "REP"), "TN": ("Bill Lee", "REP"),
    "TX": ("Greg Abbott", "REP"), "UT": ("Spencer Cox", "REP"), "VT": ("Phil Scott", "REP"),
    "VA": ("Abigail Spanberger", "DEM"), "WA": ("Bob Ferguson", "DEM"), "WV": ("Patrick Morrisey", "REP"),
    "WI": ("Tony Evers", "DEM"), "WY": ("Mark Gordon", "REP"),
}

# USPS -> [(senator name, party), (senator name, party)]
SENATORS: dict[str, list[tuple[str, str]]] = {
    "AL": [("Tommy Tuberville", "REP"), ("Katie Britt", "REP")],
    "AK": [("Lisa Murkowski", "REP"), ("Dan Sullivan", "REP")],
    "AZ": [("Mark Kelly", "DEM"), ("Ruben Gallego", "DEM")],
    "AR": [("John Boozman", "REP"), ("Tom Cotton", "REP")],
    "CA": [("Alex Padilla", "DEM"), ("Adam Schiff", "DEM")],
    "CO": [("Michael Bennet", "DEM"), ("John Hickenlooper", "DEM")],
    "CT": [("Richard Blumenthal", "DEM"), ("Chris Murphy", "DEM")],
    "DE": [("Chris Coons", "DEM"), ("Lisa Blunt Rochester", "DEM")],
    "FL": [("Rick Scott", "REP"), ("Ashley Moody", "REP")],
    "GA": [("Jon Ossoff", "DEM"), ("Raphael Warnock", "DEM")],
    "HI": [("Brian Schatz", "DEM"), ("Mazie Hirono", "DEM")],
    "ID": [("Mike Crapo", "REP"), ("Jim Risch", "REP")],
    "IL": [("Dick Durbin", "DEM"), ("Tammy Duckworth", "DEM")],
    "IN": [("Todd Young", "REP"), ("Jim Banks", "REP")],
    "IA": [("Chuck Grassley", "REP"), ("Joni Ernst", "REP")],
    "KS": [("Jerry Moran", "REP"), ("Roger Marshall", "REP")],
    "KY": [("Mitch McConnell", "REP"), ("Rand Paul", "REP")],
    "LA": [("Bill Cassidy", "REP"), ("John Kennedy", "REP")],
    "ME": [("Susan Collins", "REP"), ("Angus King", "IND")],
    "MD": [("Chris Van Hollen", "DEM"), ("Angela Alsobrooks", "DEM")],
    "MA": [("Elizabeth Warren", "DEM"), ("Ed Markey", "DEM")],
    "MI": [("Gary Peters", "DEM"), ("Elissa Slotkin", "DEM")],
    "MN": [("Amy Klobuchar", "DEM"), ("Tina Smith", "DEM")],
    "MS": [("Roger Wicker", "REP"), ("Cindy Hyde-Smith", "REP")],
    "MO": [("Josh Hawley", "REP"), ("Eric Schmitt", "REP")],
    "MT": [("Steve Daines", "REP"), ("Tim Sheehy", "REP")],
    "NE": [("Deb Fischer", "REP"), ("Pete Ricketts", "REP")],
    "NV": [("Catherine Cortez Masto", "DEM"), ("Jacky Rosen", "DEM")],
    "NH": [("Jeanne Shaheen", "DEM"), ("Maggie Hassan", "DEM")],
    "NJ": [("Cory Booker", "DEM"), ("Andy Kim", "DEM")],
    "NM": [("Martin Heinrich", "DEM"), ("Ben Ray Lujan", "DEM")],
    "NY": [("Chuck Schumer", "DEM"), ("Kirsten Gillibrand", "DEM")],
    "NC": [("Thom Tillis", "REP"), ("Ted Budd", "REP")],
    "ND": [("John Hoeven", "REP"), ("Kevin Cramer", "REP")],
    "OH": [("Bernie Moreno", "REP"), ("Jon Husted", "REP")],
    "OK": [("James Lankford", "REP"), ("Markwayne Mullin", "REP")],
    "OR": [("Ron Wyden", "DEM"), ("Jeff Merkley", "DEM")],
    "PA": [("John Fetterman", "DEM"), ("Dave McCormick", "REP")],
    "RI": [("Jack Reed", "DEM"), ("Sheldon Whitehouse", "DEM")],
    "SC": [("Lindsey Graham", "REP"), ("Tim Scott", "REP")],
    "SD": [("John Thune", "REP"), ("Mike Rounds", "REP")],
    "TN": [("Marsha Blackburn", "REP"), ("Bill Hagerty", "REP")],
    "TX": [("John Cornyn", "REP"), ("Ted Cruz", "REP")],
    "UT": [("Mike Lee", "REP"), ("John Curtis", "REP")],
    "VT": [("Bernie Sanders", "IND"), ("Peter Welch", "DEM")],
    "VA": [("Mark Warner", "DEM"), ("Tim Kaine", "DEM")],
    "WA": [("Patty Murray", "DEM"), ("Maria Cantwell", "DEM")],
    "WV": [("Shelley Moore Capito", "REP"), ("Jim Justice", "REP")],
    "WI": [("Ron Johnson", "REP"), ("Tammy Baldwin", "DEM")],
    "WY": [("John Barrasso", "REP"), ("Cynthia Lummis", "REP")],
}


def _candidate_id(conn, name: str, office: str, state_fips: str, party: str) -> int:
    row = conn.execute("SELECT id FROM candidates WHERE name=? AND office=? AND state_fips=?",
                       (name, office, state_fips)).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO candidates(name,party_code,state_fips,office,citation,is_synthetic) "
        "VALUES(?,?,?,?,?,0)", (name, party, state_fips, office, _SOURCE))
    return cur.lastrowid


def upsert_officeholder(conn, name: str, party: str, office: str, state_fips: str,
                        district_number: int | None, start_date: str) -> bool:
    """Close-and-replace semantics for SINGLE-seat offices (governor, a House
    district). Senate (two seats sharing one state key) goes through
    sync_multi_seat instead. Returns True if a row changed."""
    cid = _candidate_id(conn, name, office, state_fips, party)
    dn_clause = "district_number IS NULL" if district_number is None else "district_number=?"
    params = [office, state_fips] + ([] if district_number is None else [district_number])
    cur = conn.execute(f"SELECT id, candidate_id FROM officeholders WHERE office=? AND state_fips=? "
                       f"AND {dn_clause} AND end_date IS NULL", params).fetchone()
    if cur and cur[1] == cid:
        return False
    if cur:
        conn.execute("UPDATE officeholders SET end_date=date('now') WHERE id=?", (cur[0],))
    conn.execute("INSERT INTO officeholders(candidate_id,office,state_fips,district_number,start_date) "
                 "VALUES(?,?,?,?,?)", (cid, office, state_fips, district_number, start_date))
    return True


def sync_multi_seat(conn, office: str, state_fips: str,
                    people: list[tuple[str, str]], start_date: str) -> int:
    """Set-reconciliation for offices with multiple holders per state (senate):
    close open rows whose person is no longer in the list, insert the missing.
    Never touches rows whose person still serves."""
    want = {(_candidate_id(conn, name, office, state_fips, party)) for name, party in people}
    have = {r[1]: r[0] for r in conn.execute(
        "SELECT id, candidate_id FROM officeholders WHERE office=? AND state_fips=? "
        "AND end_date IS NULL", (office, state_fips)).fetchall()}
    changed = 0
    for cid, oid in have.items():
        if cid not in want:
            conn.execute("UPDATE officeholders SET end_date=date('now') WHERE id=?", (oid,))
            changed += 1
    for cid in want - set(have):
        conn.execute("INSERT INTO officeholders(candidate_id,office,state_fips,start_date) "
                     "VALUES(?,?,?,?)", (cid, office, state_fips, start_date))
        changed += 1
    return changed


def run() -> int:
    from core import db
    from domain.geography import USPS_TO_FIPS
    db.migrate()
    n = 0
    with db.write() as conn:
        for usps, (name, party) in GOVERNORS.items():
            n += upsert_officeholder(conn, name, party, "governor", USPS_TO_FIPS[usps], None,
                                     "2026-07-01")
        for usps, pair in SENATORS.items():
            n += sync_multi_seat(conn, "senate", USPS_TO_FIPS[usps], pair, "2026-07-01")
    return n


if __name__ == "__main__":
    print(f"officeholders changed: {run()}")
