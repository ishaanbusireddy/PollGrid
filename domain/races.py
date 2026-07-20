"""Race seeding for the covered scope (President, Senate w/ classes, Governor,
House incl. primaries-as-phase) plus per-race search profiles (§15: coverage
that hunts, not waits). Ballot measures / state legislatures deliberately absent."""
from __future__ import annotations

import json

from core import db
from core.util import now_iso
from domain import geography as geo

CYCLE_SENATE = 2026
CYCLE_GOV = 2026
CYCLE_HOUSE = 2026
CYCLE_PRESIDENT = 2028

OFFICE_LABEL = {"president": "President", "senate": "Senate", "governor": "Governor", "house": "House"}


def _insert_race(conn, race_type: str, cycle: int, state_fips: str | None,
                 district_version_id: int | None, seat: str, name: str,
                 phase: str = "general", election_date: str | None = None) -> None:
    # SQLite UNIQUE treats NULLs as distinct, so INSERT OR IGNORE alone would
    # re-insert every state-level (district NULL) race on each boot — dedupe
    # with an explicit NULL-safe (IS) existence check first.
    exists = conn.execute(
        "SELECT 1 FROM races WHERE race_type=? AND phase=? AND cycle_year=? "
        "AND state_fips IS ? AND district_version_id IS ? AND seat=?",
        (race_type, phase, cycle, state_fips, district_version_id, seat)).fetchone()
    if exists:
        return
    conn.execute(
        "INSERT INTO races(race_type,phase,cycle_year,state_fips,district_version_id,seat,name,"
        "election_date) VALUES(?,?,?,?,?,?,?,?)",
        (race_type, phase, cycle, state_fips, district_version_id, seat, name, election_date))


# 2026 Senate SPECIAL elections (unexpired terms being filled this cycle):
# FL (Rubio's Class-3 seat, Moody appointed) and OH (Vance's Class-3 seat,
# Husted appointed). Keyed by USPS.
SENATE_SPECIALS_2026 = {"FL": "Florida", "OH": "Ohio"}


def seed() -> None:
    if db.query_one("SELECT 1 FROM races LIMIT 1"):
        upgrade_2026()
        return
    districts = {(d["state_fips"], d["district_number"]): d for d in geo.current_districts()}
    with db.write() as conn:
        # National umbrella races: presidential national + generic ballot.
        _insert_race(conn, "president", CYCLE_PRESIDENT, None, None, "regular",
                     f"{CYCLE_PRESIDENT} President — National")
        _insert_race(conn, "generic_ballot", CYCLE_HOUSE, None, None, "regular",
                     f"{CYCLE_HOUSE} Generic Congressional Ballot")

        for fips, (usps, sname, terr) in geo.STATES.items():
            if terr:
                continue
            # Presidential state races; ME/NE additionally get district races —
            # the model reads its electoral math off the district tier for exactly
            # those two states (elector_method), the state tier for the rest.
            _insert_race(conn, "president", CYCLE_PRESIDENT, fips, None, "regular",
                         f"{CYCLE_PRESIDENT} President — {sname}")
            alloc = geo.ev_allocation(fips, CYCLE_PRESIDENT)
            if alloc and alloc["elector_method"] == "congressional_district":
                for (sf, dn), d in districts.items():
                    if sf == fips and d["is_voting"]:
                        _insert_race(conn, "president", CYCLE_PRESIDENT, fips,
                                     d["district_version_id"], "regular",
                                     f"{CYCLE_PRESIDENT} President — {usps}-{dn:02d}")
            if usps == "DC":
                continue  # DC votes for President (3 EV) but has no Senate/Governor race
            if usps in geo.SENATE_CLASS_2:
                _insert_race(conn, "senate", CYCLE_SENATE, fips, None, "class_2",
                             f"{CYCLE_SENATE} Senate — {sname}")
            if usps in geo.GOV_2026:
                _insert_race(conn, "governor", CYCLE_GOV, fips, None, "regular",
                             f"{CYCLE_GOV} Governor — {sname}")

        for (sf, dn), d in districts.items():
            if not d["is_voting"]:
                continue
            usps = geo.STATES[sf][0]
            label = "AL" if dn == 0 else f"{dn:02d}"
            _insert_race(conn, "house", CYCLE_HOUSE, sf, d["district_version_id"], "regular",
                         f"{CYCLE_HOUSE} House — {usps}-{label}")

    upgrade_2026()
    rebuild_search_profiles()


def upgrade_2026() -> None:
    """Additive completeness pass, safe on every boot (INSERT OR IGNORE keyed by
    the races UNIQUE constraint): 2026 senate specials, a primary-phase race for
    every 2026 general, and election_date stamped from the calendar. This is how
    an existing database (seed() early-returns on non-empty) gains the full
    race/primary universe without a wipe."""
    from scripts.seed_election_calendar import GENERAL_2026, PRIMARY_2026, run as seed_calendar
    seed_calendar()
    fips_by_usps = {v[0]: k for k, v in geo.STATES.items()}
    with db.write() as conn:
        # senate specials (FL/OH Class-3 unexpired terms)
        for usps, sname in SENATE_SPECIALS_2026.items():
            _insert_race(conn, "senate", 2026, fips_by_usps[usps], None, "special",
                         f"2026 Senate — {sname} (special)", "general", GENERAL_2026)
        conn.execute("UPDATE races SET election_date=? WHERE cycle_year=2026 AND phase='general' "
                     "AND election_date IS NULL", (GENERAL_2026,))
    # a primary-phase row mirroring every 2026 general (the primary ELECTION, both
    # parties' contests within it — the schema keys phase, not party), dated from
    # the hand-checked state calendar. Queried AFTER the specials commit so the
    # specials' primaries land in the same pass.
    generals = db.query("SELECT * FROM races WHERE cycle_year=2026 AND phase='general' "
                        "AND race_type != 'generic_ballot'")
    with db.write() as conn:
        for r in generals:
            usps = geo.STATES.get(r["state_fips"], (None,))[0] if r["state_fips"] else None
            pdate = PRIMARY_2026.get(usps) if usps else None
            _insert_race(conn, r["race_type"], 2026, r["state_fips"], r["district_version_id"],
                         r["seat"], r["name"].replace("2026 ", "2026 Primary: ", 1),
                         "primary", pdate)


def rebuild_search_profiles() -> None:
    """Every tracked race carries a search profile: race-name variants, cycle tags,
    candidate name variants (auto-updated the moment a filing lands — fec sync and
    manual candidate linkage both call back into here)."""
    races = db.query("SELECT r.*, s.name AS state_name, s.usps_code FROM races r "
                     "LEFT JOIN states s ON s.fips_code=r.state_fips WHERE r.phase='general'")
    # primaries share the general's news-search terms — no separate profiles
    for r in races:
        terms: list[str] = []
        office = OFFICE_LABEL.get(r["race_type"], r["race_type"])
        if r["state_name"]:
            terms += [f"{r['state_name']} {office} race", f"{r['state_name']} {office} election {r['cycle_year']}"]
            if r["district_version_id"]:
                d = db.query_one("SELECT district_number FROM congressional_districts WHERE district_version_id=?",
                                 (r["district_version_id"],))
                if d:
                    terms.append(f"{r['usps_code']}-{d['district_number']:02d}")
        else:
            terms += [f"{r['cycle_year']} {office} election"]
        for c in db.query(
                "SELECT c.name FROM race_candidates rc JOIN candidates c ON c.id=rc.candidate_id "
                "WHERE rc.race_id=?", (r["id"],)):
            terms.append(c["name"])
            surname = c["name"].split()[-1]
            if len(surname) > 3:
                terms.append(surname)
        db.execute("INSERT INTO race_search_profiles(race_id,terms_json,updated_at) VALUES(?,?,?) "
                   "ON CONFLICT(race_id) DO UPDATE SET terms_json=excluded.terms_json, updated_at=excluded.updated_at",
                   (r["id"], json.dumps(sorted(set(terms))), now_iso()))


def race_category(race: dict) -> str:
    """Backtest gate category: race-type is the granularity forecasts earn visibility at."""
    return race["race_type"]
