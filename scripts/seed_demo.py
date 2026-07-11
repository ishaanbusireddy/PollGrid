#!/usr/bin/env python3
"""Synthetic demo data — EVERY row tagged is_synthetic=1, removable in one
operation (scripts/purge_synthetic.py). Deterministically seeded RNG so two
demo databases look identical.

Generates: competitive-race ratings, synthetic candidates, ~15 polls per
competitive race across 8 synthetic pollsters (with distinct house leans),
synthetic state/county history for those states, synthetic news facts, one
live election-night race with partial county results — then runs the real
deterministic pipeline over it so every page has numbers with audit trails.

Usage: python scripts/seed_demo.py
"""
import json
import os
import random
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db  # noqa: E402
from core.config import cfg  # noqa: E402
from core.util import now_iso  # noqa: E402

RNG = random.Random(2026)

POLLSTERS = [  # name, house_lean_dem_pts, quality_sigma
    ("Meridian Research (synthetic)", 0.8, 1.5), ("Bluegrass Polling (synthetic)", 1.5, 2.5),
    ("Hawthorne Analytics (synthetic)", -0.5, 1.2), ("Redline Surveys (synthetic)", -1.8, 2.8),
    ("Statehouse Poll (synthetic)", 0.2, 1.8), ("Civic Pulse (synthetic)", -0.2, 2.0),
    ("Landmark Opinion (synthetic)", 1.0, 3.0), ("Foursquare Data (synthetic)", -0.9, 1.6),
]

FIRST = ["Avery", "Jordan", "Morgan", "Riley", "Casey", "Quinn", "Rowan", "Hayden", "Skyler",
         "Emerson", "Marlow", "Sutton", "Ellis", "Lennox", "Arden", "Blair"]
LAST = ["Calloway", "Whitfield", "Ostrander", "Vance", "Merritt", "Halloran", "Kessler",
        "Ashford", "Delacroix", "Pemberton", "Rooker", "Santoro", "Winslow", "Yardley"]

TOSSUP_STATES = ["13", "04", "55", "42", "26", "32", "37", "23"]  # GA AZ WI PA MI NV NC ME


def _mk_candidate(conn, race, party) -> int:
    name = f"{RNG.choice(FIRST)} {RNG.choice(LAST)}"
    cur = conn.execute(
        "INSERT INTO candidates(name,party_code,state_fips,office,bio,is_synthetic) VALUES(?,?,?,?,?,1)",
        (name, party, race["state_fips"], race["race_type"],
         f"Synthetic demo candidate for {race['name']}."))
    conn.execute("INSERT OR IGNORE INTO race_candidates(race_id,candidate_id,party_code,is_incumbent) "
                 "VALUES(?,?,?,?)", (race["id"], cur.lastrowid, party, int(party == "REP" and RNG.random() < .5)))
    return cur.lastrowid


def seed_polls(race: dict) -> None:
    from ingestion.pollsters import ingest_poll
    base_margin = RNG.uniform(-4, 4)
    margin = base_margin
    for days_ago in sorted(RNG.sample(range(1, 120), 15), reverse=True):
        margin += RNG.gauss(0, 0.6)  # slow random walk
        pollster, lean, sigma = RNG.choice(POLLSTERS)
        noise = RNG.gauss(0, sigma)
        dem = 47 + (margin + lean + noise) / 2
        rep = 47 - (margin + lean + noise) / 2
        other = round(RNG.uniform(1, 5), 1)
        end = date.today() - timedelta(days=days_ago)
        start = end - timedelta(days=3)
        ingest_poll(pollster=pollster, race_id=race["id"], field_start=start.isoformat(),
                    field_end=end.isoformat(),
                    results={"DEM": round(dem, 1), "REP": round(rep, 1), "OTH": other},
                    sample_size=RNG.choice([420, 600, 800, 1000, 1200]),
                    population=RNG.choice(["lv", "lv", "rv"]),
                    moe=round(RNG.uniform(2.5, 4.5), 1),
                    methodology=RNG.choice(["live phone", "online panel", "IVR/text mix"]),
                    release_url="https://example.invalid/synthetic-poll",
                    is_synthetic=True,
                    created_at=end.isoformat() + "T12:00:00Z")


def seed_history(conn, state_fips: str) -> None:
    """Synthetic state + county history so partisan lean, coalition regression
    and the history panels have rows. Tagged synthetic, confidence 'derived'."""
    lean = RNG.uniform(-6, 6)
    for office, cycles in (("president", [2016, 2020, 2024]), ("senate", [2018, 2022]),
                           ("governor", [2018, 2022]), ("house", [2022, 2024])):
        for cy in cycles:
            m = lean + RNG.gauss(0, 3)
            dem = 49 + m / 2
            rep = 49 - m / 2
            conn.execute(
                "INSERT OR IGNORE INTO political_history(tier,entity_id,office,seat,cycle_year,winner_party,"
                "dem_pct,rep_pct,other_pct,margin_pct,turnout_pct,confidence,source,is_synthetic) "
                "VALUES('state',?,?,'regular',?,?,?,?,?,?,?, 'derived','synthetic demo seed',1)",
                (state_fips, office, cy, "DEM" if m > 0 else "REP", round(dem, 1), round(rep, 1),
                 round(100 - dem - rep, 1), round(abs(m), 1), round(RNG.uniform(48, 68), 1)))
    counties = [r["geoid"] for r in db.query(
        "SELECT geoid FROM county_equivalents WHERE state_fips=? AND effective_to IS NULL", (state_fips,))]
    for geoid in counties:
        clean = lean + RNG.gauss(0, 8)
        for cy in (2020, 2024):
            m = clean + RNG.gauss(0, 2)
            dem = 49 + m / 2
            conn.execute(
                "INSERT OR IGNORE INTO political_history(tier,entity_id,office,seat,cycle_year,winner_party,"
                "dem_pct,rep_pct,other_pct,margin_pct,confidence,source,is_synthetic) "
                "VALUES('county_equivalent',?,?,'regular',?,?,?,?,?,?, 'derived','synthetic demo seed',1)",
                (geoid, "president", cy, "DEM" if m > 0 else "REP", round(dem, 1),
                 round(98 - dem, 1), 2.0, round(abs(m), 1)))
        # a couple of synthetic county demographic rows for the coalition regression
        for cat, var, lo, hi in (("education", "bachelors", 800, 90000), ("education", "pop_25plus", 4000, 300000),
                                 ("population_age", "median_age", 30, 52), ("population_age", "total_population", 5000, 400000),
                                 ("economic", "median_household_income", 38000, 110000),
                                 ("race_ethnicity", "white_nh", 2000, 250000),
                                 ("social_nativity", "foreign_born", 100, 60000)):
            conn.execute(
                "INSERT OR IGNORE INTO demographics(tier,entity_id,as_of,category,variable,value,confidence,"
                "source,is_synthetic) VALUES('county_equivalent',?,?,?,?,?,'derived','synthetic demo seed',1)",
                (geoid, "demo_2026", cat, var, round(RNG.uniform(lo, hi), 1)))


def seed_news(competitive: list[dict]) -> None:
    from processing.extraction import process_raw_item
    src = db.query_one("SELECT id FROM sources WHERE source_type='rss' LIMIT 1")
    headlines = [
        ("{cand} holds rally in {state} as early-vote requests surge", "campaign_event"),
        ("New poll shows tightening {office} race in {state}", "polling"),
        ("{cand} posts record fundraising quarter, FEC filing shows", "finance"),
        ("Debate night: {cand} and rival clash over economy in {state}", "debate"),
        ("Editorial board endorses {cand} in {state} {office} race", "endorsement"),
    ]
    for race in competitive[:10]:
        state = db.query_one("SELECT name FROM states WHERE fips_code=?", (race["state_fips"],))
        cand = db.query_one(
            "SELECT c.name FROM race_candidates rc JOIN candidates c ON c.id=rc.candidate_id "
            "WHERE rc.race_id=? LIMIT 1", (race["id"],))
        if not (state and cand):
            continue
        for i, (tpl, _cat) in enumerate(RNG.sample(headlines, 3)):
            title = tpl.format(cand=cand["name"], state=state["name"], office=race["race_type"].title())
            rid = db.execute(
                "INSERT OR IGNORE INTO raw_items(source_id,external_id,fetched_at,title,url,body,"
                "published_at,is_synthetic) VALUES(?,?,?,?,?,?,?,1)",
                (src["id"], f"synthetic:{race['id']}:{i}", now_iso(), title,
                 "https://example.invalid/synthetic", title,
                 (date.today() - timedelta(days=RNG.randint(0, 12))).isoformat()))
            if rid:
                process_raw_item(rid)


def seed_election_night(race: dict) -> None:
    from ingestion.results_tiers import upsert_result
    counties = db.query("SELECT geoid FROM county_equivalents WHERE state_fips=? AND effective_to IS NULL "
                        "LIMIT 40", (race["state_fips"],))
    db.execute("UPDATE races SET status='live' WHERE id=?", (race["id"],))
    for c in counties:
        reporting = RNG.uniform(20, 95)
        total = RNG.randint(2000, 90000)
        dem_share = RNG.uniform(0.35, 0.65)
        for party, share in (("DEM", dem_share), ("REP", 1 - dem_share - 0.02), ("OTH", 0.02)):
            upsert_result(race["id"], c["geoid"], party, int(total * share * reporting / 100),
                          round(reporting, 1), "native", is_synthetic=True)
    from modeling.race_calling import evaluate_callable
    evaluate_callable()


def main() -> None:
    if not cfg("synthetic.allow_seed_demo"):
        sys.exit("synthetic.allow_seed_demo is false — refusing to seed demo data")
    db.migrate()
    if db.query_one("SELECT 1 FROM polls WHERE is_synthetic=1 LIMIT 1"):
        sys.exit("demo data already present — run scripts/purge_synthetic.py first.")
    from api.server import bootstrap  # boot path seeds geography/races/sources
    bootstrap(start_ingestion=False)
    import scripts.backfill_history as bh
    bh.run()

    competitive = []
    with db.write() as conn:
        for st in TOSSUP_STATES:
            for rt in ("senate", "governor"):
                race = db.query_one("SELECT * FROM races WHERE race_type=? AND state_fips=? AND phase='general'",
                                    (rt, st))
                if race:
                    conn.execute("UPDATE races SET competitiveness='tossup', is_synthetic=0 WHERE id=?",
                                 (race["id"],))
                    competitive.append(dict(race))
            house = db.query("SELECT * FROM races WHERE race_type='house' AND state_fips=? LIMIT 3", (st,))
            for race in house:
                conn.execute("UPDATE races SET competitiveness='lean' WHERE id=?", (race["id"],))
                competitive.append(dict(race))
        gb = db.query_one("SELECT * FROM races WHERE race_type='generic_ballot'")
        if gb:
            competitive.append(dict(gb))
        for race in competitive:
            if race["race_type"] != "generic_ballot" and not db.query_one(
                    "SELECT 1 FROM race_candidates WHERE race_id=?", (race["id"],)):
                _mk_candidate(conn, race, "DEM")
                _mk_candidate(conn, race, "REP")
        for st in TOSSUP_STATES:
            seed_history(conn, st)

    print(f"seeding polls for {len(competitive)} competitive races…")
    for race in competitive:
        seed_polls(race)
    seed_news(competitive)
    seed_election_night(competitive[0])

    print("running the deterministic pipeline…")
    from modeling import nightly
    report = nightly.run()
    for k, v in report.items():
        print(f"  {k}: {v if not isinstance(v, list) else len(v)}")
    from modeling.factors_taxonomy import score_race
    for race in competitive[:8]:
        score_race(race["id"])
    from domain.races import rebuild_search_profiles
    rebuild_search_profiles()
    print("demo seed complete — every synthetic row is tagged; "
          "python scripts/purge_synthetic.py removes every trace.")


if __name__ == "__main__":
    main()
