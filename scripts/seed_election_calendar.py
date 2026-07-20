#!/usr/bin/env python3
"""Seed the 2026 election calendar: every state's statewide/congressional
primary date plus the national general election (Tue Nov 3, 2026).

Dates cross-checked July 2026 against the public 2026 primary calendars
(FVAP / NCSL / 270toWin / FEC 2026pdates). INSERT OR IGNORE — a hand-corrected
row is never clobbered by a reseed. Zero network; runs at every boot.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GENERAL_2026 = "2026-11-03"

# USPS -> 2026 statewide/congressional primary date
PRIMARY_2026: dict[str, str] = {
    "TX": "2026-03-03", "NC": "2026-03-03", "AR": "2026-03-03",
    "MS": "2026-03-10",
    "IL": "2026-03-17",
    "IN": "2026-05-05", "OH": "2026-05-05",
    "NE": "2026-05-12", "WV": "2026-05-12",
    "LA": "2026-05-16",
    "AL": "2026-05-19", "GA": "2026-05-19", "ID": "2026-05-19",
    "KY": "2026-05-19", "OR": "2026-05-19", "PA": "2026-05-19",
    "CA": "2026-06-02", "IA": "2026-06-02", "MT": "2026-06-02",
    "NJ": "2026-06-02", "NM": "2026-06-02", "SD": "2026-06-02",
    "ME": "2026-06-09", "NV": "2026-06-09", "ND": "2026-06-09", "SC": "2026-06-09",
    "OK": "2026-06-16", "DC": "2026-06-16",
    "MD": "2026-06-23", "NY": "2026-06-23", "UT": "2026-06-23",
    "CO": "2026-06-30",
    "AZ": "2026-07-21",
    "KS": "2026-08-04", "MI": "2026-08-04", "MO": "2026-08-04",
    "VA": "2026-08-04", "WA": "2026-08-04",
    "TN": "2026-08-06",
    "HI": "2026-08-08",
    "CT": "2026-08-11", "MN": "2026-08-11", "VT": "2026-08-11", "WI": "2026-08-11",
    "AK": "2026-08-18", "FL": "2026-08-18", "WY": "2026-08-18",
    "MA": "2026-09-01",
    "DE": "2026-09-08", "NH": "2026-09-08", "RI": "2026-09-08",
}

_SOURCE = "2026 primary calendar (FVAP/NCSL/270toWin, checked 2026-07)"


def run() -> int:
    from core import db
    from domain.geography import USPS_TO_FIPS
    db.migrate()
    n = 0
    with db.write() as conn:
        for usps, date in PRIMARY_2026.items():
            fips = USPS_TO_FIPS.get(usps)
            if not fips:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO election_calendar(state_fips,cycle_year,kind,election_date,source) "
                "VALUES(?,?,?,?,?)", (fips, 2026, "primary", date, _SOURCE))
            n += cur.rowcount
        for usps in PRIMARY_2026:
            fips = USPS_TO_FIPS.get(usps)
            if not fips:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO election_calendar(state_fips,cycle_year,kind,election_date,source) "
                "VALUES(?,?,?,?,?)", (fips, 2026, "general", GENERAL_2026, "federal general election day"))
            n += cur.rowcount
    return n


if __name__ == "__main__":
    print(f"seeded {run()} election_calendar rows")
