"""Five-tier geography: constants + Phase-A seed.

Seeds are real: 56 state/territory rows, electoral-vote allocations per the 2020
apportionment (versioned — the allocations table is the sole source of truth for
EV counts and elector method, per review §2.4), 435 118th-Congress districts +
6 non-voting delegate seats (flagged is_voting=0), and 3,143 county-equivalents
from the vendored Census FIPS reference. Precincts are deliberately NOT seeded:
coverage is genuinely thin and is flagged, never silently backfilled.
"""
from __future__ import annotations

import csv
import os

from core import db
from core.util import today

# fips: (usps, name, is_territory)
STATES: dict[str, tuple[str, str, int]] = {
    "01": ("AL", "Alabama", 0), "02": ("AK", "Alaska", 0), "04": ("AZ", "Arizona", 0),
    "05": ("AR", "Arkansas", 0), "06": ("CA", "California", 0), "08": ("CO", "Colorado", 0),
    "09": ("CT", "Connecticut", 0), "10": ("DE", "Delaware", 0), "11": ("DC", "District of Columbia", 0),
    "12": ("FL", "Florida", 0), "13": ("GA", "Georgia", 0), "15": ("HI", "Hawaii", 0),
    "16": ("ID", "Idaho", 0), "17": ("IL", "Illinois", 0), "18": ("IN", "Indiana", 0),
    "19": ("IA", "Iowa", 0), "20": ("KS", "Kansas", 0), "21": ("KY", "Kentucky", 0),
    "22": ("LA", "Louisiana", 0), "23": ("ME", "Maine", 0), "24": ("MD", "Maryland", 0),
    "25": ("MA", "Massachusetts", 0), "26": ("MI", "Michigan", 0), "27": ("MN", "Minnesota", 0),
    "28": ("MS", "Mississippi", 0), "29": ("MO", "Missouri", 0), "30": ("MT", "Montana", 0),
    "31": ("NE", "Nebraska", 0), "32": ("NV", "Nevada", 0), "33": ("NH", "New Hampshire", 0),
    "34": ("NJ", "New Jersey", 0), "35": ("NM", "New Mexico", 0), "36": ("NY", "New York", 0),
    "37": ("NC", "North Carolina", 0), "38": ("ND", "North Dakota", 0), "39": ("OH", "Ohio", 0),
    "40": ("OK", "Oklahoma", 0), "41": ("OR", "Oregon", 0), "42": ("PA", "Pennsylvania", 0),
    "44": ("RI", "Rhode Island", 0), "45": ("SC", "South Carolina", 0), "46": ("SD", "South Dakota", 0),
    "47": ("TN", "Tennessee", 0), "48": ("TX", "Texas", 0), "49": ("UT", "Utah", 0),
    "50": ("VT", "Vermont", 0), "51": ("VA", "Virginia", 0), "53": ("WA", "Washington", 0),
    "54": ("WV", "West Virginia", 0), "55": ("WI", "Wisconsin", 0), "56": ("WY", "Wyoming", 0),
    "60": ("AS", "American Samoa", 1), "66": ("GU", "Guam", 1),
    "69": ("MP", "Northern Mariana Islands", 1), "72": ("PR", "Puerto Rico", 1),
    "78": ("VI", "U.S. Virgin Islands", 1),
}

USPS_TO_FIPS = {v[0]: k for k, v in STATES.items()}

# 2020-apportionment House seats (118th Congress forward). Sum asserted = 435.
HOUSE_SEATS: dict[str, int] = {
    "AL": 7, "AK": 1, "AZ": 9, "AR": 4, "CA": 52, "CO": 8, "CT": 5, "DE": 1, "FL": 28,
    "GA": 14, "HI": 2, "ID": 2, "IL": 17, "IN": 9, "IA": 4, "KS": 4, "KY": 6, "LA": 6,
    "ME": 2, "MD": 8, "MA": 9, "MI": 13, "MN": 8, "MS": 4, "MO": 8, "MT": 2, "NE": 3,
    "NV": 4, "NH": 2, "NJ": 12, "NM": 3, "NY": 26, "NC": 14, "ND": 1, "OH": 15, "OK": 5,
    "OR": 6, "PA": 17, "RI": 2, "SC": 7, "SD": 1, "TN": 9, "TX": 38, "UT": 4, "VT": 1,
    "VA": 11, "WA": 10, "WV": 2, "WI": 8, "WY": 1,
}
assert sum(HOUSE_SEATS.values()) == 435, "House apportionment must sum to 435"

# Non-voting delegate seats (review §2.6: in the table, flagged is_voting=0,
# and the Phase-A check counts voting and non-voting separately).
DELEGATE_SEATS = ["DC", "AS", "GU", "MP", "PR", "VI"]

# Senate classes. Each state appears in exactly two; totals 33/33/34 (asserted).
SENATE_CLASS_1 = {"AZ", "CA", "CT", "DE", "FL", "HI", "IN", "ME", "MD", "MA", "MI", "MN",
                  "MS", "MO", "MT", "NE", "NV", "NJ", "NM", "NY", "ND", "OH", "PA", "RI",
                  "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY"}
SENATE_CLASS_2 = {"AL", "AK", "AR", "CO", "DE", "GA", "ID", "IL", "IA", "KS", "KY", "LA",
                  "ME", "MA", "MI", "MN", "MS", "MT", "NE", "NH", "NJ", "NM", "NC", "OK",
                  "OR", "RI", "SC", "SD", "TN", "TX", "VA", "WV", "WY"}
SENATE_CLASS_3 = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "FL", "GA", "HI", "ID", "IL",
                  "IN", "IA", "KS", "KY", "LA", "MD", "MO", "NV", "NH", "NY", "NC", "ND",
                  "OH", "OK", "OR", "PA", "SC", "SD", "UT", "VT", "WA", "WI"}
assert (len(SENATE_CLASS_1), len(SENATE_CLASS_2), len(SENATE_CLASS_3)) == (33, 33, 34)
for _u in HOUSE_SEATS:
    assert sum(_u in c for c in (SENATE_CLASS_1, SENATE_CLASS_2, SENATE_CLASS_3)) == 2, _u

# Election years per class: class 1 → 2024, 2030…; class 2 → 2026…; class 3 → 2028…
CLASS_CYCLE_ANCHOR = {"class_1": 2024, "class_2": 2026, "class_3": 2028}

# States with a regular gubernatorial election in 2026 (36).
GOV_2026 = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "FL", "GA", "HI", "ID", "IL", "IA",
            "KS", "ME", "MD", "MA", "MI", "MN", "NE", "NV", "NH", "NM", "NY", "OH", "OK",
            "OR", "PA", "RI", "SC", "SD", "TN", "TX", "VT", "WI", "WY"}
assert len(GOV_2026) == 36

CURRENT_CONGRESS = 119  # seated 2025-01-03 on 2022-cycle lines


def senate_classes(usps: str) -> list[str]:
    out = []
    if usps in SENATE_CLASS_1:
        out.append("class_1")
    if usps in SENATE_CLASS_2:
        out.append("class_2")
    if usps in SENATE_CLASS_3:
        out.append("class_3")
    return out


_COUNTY_TYPES = {"22": "parish", "02": "borough", "51": "county", "09": "county"}


def _county_type(geoid: str, name: str) -> str:
    low = name.lower()
    if "planning region" in low:
        return "planning_region"
    if geoid.startswith("22"):
        return "parish"
    if geoid.startswith("02"):
        if "census area" in low:
            return "census_area"
        if "city and borough" in low:
            return "city_and_borough"
        if "municipality" in low:
            return "municipality"
        return "borough"
    if geoid.startswith("51") and ("city" in low and "county" not in low):
        return "independent_city"
    if geoid.startswith("29") and low == "st. louis city":
        return "independent_city"
    if geoid.startswith("24") and low == "baltimore city":
        return "independent_city"
    if geoid.startswith("32") and low == "carson city":
        return "independent_city"
    return "county"


def seed() -> None:
    if db.query_one("SELECT 1 FROM states LIMIT 1"):
        return  # idempotent: seed only an empty geography

    with db.write() as conn:
        for fips, (usps, name, terr) in STATES.items():
            conn.execute(
                "INSERT OR IGNORE INTO states(fips_code,usps_code,name,is_territory,is_state) VALUES(?,?,?,?,?)",
                (fips, usps, name, terr, 0 if terr else 1))

        # Electoral-vote allocations, 2024-cycle vintage (2020 apportionment), plus the
        # real historical method transitions for ME (1972) and NE (1992).
        for fips, (usps, _, terr) in STATES.items():
            if terr:
                continue
            ev = 3 if usps == "DC" else HOUSE_SEATS[usps] + 2
            method = "congressional_district" if usps in ("ME", "NE") else "winner_take_all"
            conn.execute(
                "INSERT OR IGNORE INTO electoral_vote_allocations"
                "(state_fips,cycle_from,cycle_to,electoral_votes,elector_method) VALUES(?,?,?,?,?)",
                (fips, 2024, None, ev, method))
        conn.execute("INSERT OR IGNORE INTO electoral_vote_allocations"
                     "(state_fips,cycle_from,cycle_to,electoral_votes,elector_method) "
                     "VALUES(?,?,?,?,?)", (USPS_TO_FIPS["ME"], 1972, 2020, 4, "congressional_district"))
        conn.execute("INSERT OR IGNORE INTO electoral_vote_allocations"
                     "(state_fips,cycle_from,cycle_to,electoral_votes,elector_method) "
                     "VALUES(?,?,?,?,?)", (USPS_TO_FIPS["NE"], 1992, 2020, 5, "congressional_district"))

        # Congressional districts, current vintage (2022-cycle lines).
        for usps, seats in HOUSE_SEATS.items():
            fips = USPS_TO_FIPS[usps]
            if seats == 1:
                nums = [0]  # at-large
            else:
                nums = list(range(1, seats + 1))
            for n in nums:
                geoid = f"{fips}{n:02d}"
                conn.execute(
                    "INSERT OR IGNORE INTO congressional_districts"
                    "(geoid,congress_number,state_fips,district_number,is_voting,effective_from,effective_to) "
                    "VALUES(?,?,?,?,1,'2023-01-03',NULL)",
                    (geoid, CURRENT_CONGRESS, fips, n))
        for usps in DELEGATE_SEATS:
            fips = USPS_TO_FIPS[usps]
            conn.execute(
                "INSERT OR IGNORE INTO congressional_districts"
                "(geoid,congress_number,state_fips,district_number,is_voting,effective_from,effective_to) "
                "VALUES(?,?,?,?,0,'2023-01-03',NULL)",
                (f"{fips}98", CURRENT_CONGRESS, fips, 98))

        # County-equivalents from the vendored Census FIPS reference (50 states + DC,
        # pre-2022 vintage: Connecticut appears as its 8 legacy counties, effective_to
        # set at the planning-region switchover; the 9 planning regions are seeded as
        # the current CT vintage so post-2022 Census pulls join cleanly. Review §2.2.)
        path = os.path.join(os.path.dirname(__file__), "data", "county_fips_ref.csv")
        n_counties = 0
        with open(path, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                raw = row["fips"].strip()
                if not raw.isdigit():
                    continue
                code = int(raw)
                if code == 0 or code % 1000 == 0:
                    continue  # national/state rows
                geoid = f"{code:05d}"
                st = geoid[:2]
                if st not in STATES:
                    continue
                name = row["name"].strip()
                eff_to = "2022-06-01" if st == "09" else None
                conn.execute(
                    "INSERT OR IGNORE INTO county_equivalents"
                    "(geoid,state_fips,name,type,effective_from,effective_to) VALUES(?,?,?,?, '1950-01-01', ?)",
                    (geoid, st, name, _county_type(geoid, name), eff_to))
                n_counties += 1
        assert 3140 <= n_counties <= 3150, f"county seed count {n_counties} outside Phase-A tolerance"

        for geoid, name in [
            ("09110", "Capitol Planning Region"), ("09120", "Greater Bridgeport Planning Region"),
            ("09130", "Lower Connecticut River Valley Planning Region"),
            ("09140", "Naugatuck Valley Planning Region"), ("09150", "Northeastern Connecticut Planning Region"),
            ("09160", "Northwest Hills Planning Region"), ("09170", "South Central Connecticut Planning Region"),
            ("09180", "Southeastern Connecticut Planning Region"), ("09190", "Western Connecticut Planning Region"),
        ]:
            conn.execute(
                "INSERT OR IGNORE INTO county_equivalents"
                "(geoid,state_fips,name,type,effective_from,effective_to) "
                "VALUES(?,?,?, 'planning_region', '2022-06-01', NULL)", (geoid, "09", name))

        conn.execute("INSERT OR IGNORE INTO redistricting_events(state_fips,congress_number,effective_from,note) "
                     "VALUES('09', ?, '2022-06-01', 'CT county-equivalents replaced by planning regions (Census)')",
                     (CURRENT_CONGRESS,))


def phase_a_checks() -> dict:
    """The testable 'Phase A is done' definition (§19, amended per review)."""
    out: dict = {}
    out["states_total"] = db.query_one("SELECT COUNT(*) c FROM states")["c"]
    out["states_expected"] = 56
    out["ev_total"] = db.query_one(
        "SELECT SUM(electoral_votes) c FROM electoral_vote_allocations WHERE cycle_to IS NULL")["c"]
    out["ev_expected"] = 538
    out["voting_districts_current"] = db.query_one(
        "SELECT COUNT(*) c FROM congressional_districts WHERE effective_to IS NULL AND is_voting=1")["c"]
    out["voting_districts_expected"] = 435
    out["delegate_districts_current"] = db.query_one(
        "SELECT COUNT(*) c FROM congressional_districts WHERE effective_to IS NULL AND is_voting=0")["c"]
    out["delegate_districts_expected"] = 6
    cur = db.query_one(
        "SELECT COUNT(*) c FROM county_equivalents WHERE effective_to IS NULL AND state_fips NOT IN ('60','66','69','72','78')")["c"]
    out["current_county_equivalents"] = cur
    out["county_range_ok"] = 3140 <= cur <= 3150
    out["integrity"] = db.run_integrity_checks()
    out["ok"] = (out["states_total"] == 56 and out["ev_total"] == 538
                 and out["voting_districts_current"] == 435 and out["county_range_ok"]
                 and all(v == 0 for v in out["integrity"].values()))
    return out


def current_districts(state_fips: str | None = None) -> list[dict]:
    sql = ("SELECT * FROM congressional_districts WHERE effective_to IS NULL")
    if state_fips:
        return db.query(sql + " AND state_fips=? ORDER BY district_number", (state_fips,))
    return db.query(sql + " ORDER BY state_fips, district_number")


def ev_allocation(state_fips: str, cycle_year: int) -> dict | None:
    return db.query_one(
        "SELECT * FROM electoral_vote_allocations WHERE state_fips=? AND cycle_from<=? "
        "AND (cycle_to IS NULL OR cycle_to>=?) ORDER BY cycle_from DESC LIMIT 1",
        (state_fips, cycle_year, cycle_year))
