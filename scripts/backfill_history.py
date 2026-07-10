#!/usr/bin/env python3
"""Real national presidential results, 1952-2024, hand-seeded with citation.
The deep per-state/county archive comes from OpenElections sync (tier 2) and
per-office importers as digitized crosswalks allow; this file gives the
fundamentals model its national baseline on day one. Confidence tagged once,
at import, never upgraded retroactively.

Usage: python scripts/backfill_history.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db  # noqa: E402

# cycle: (dem_pct, rep_pct, winner) — national popular vote, certified returns.
NATIONAL_PRESIDENT = {
    2024: (48.3, 49.8, "REP"), 2020: (51.3, 46.8, "DEM"), 2016: (48.2, 46.1, "REP"),
    2012: (51.1, 47.2, "DEM"), 2008: (52.9, 45.7, "DEM"), 2004: (48.3, 50.7, "REP"),
    2000: (48.4, 47.9, "REP"), 1996: (49.2, 40.7, "DEM"), 1992: (43.0, 37.4, "DEM"),
    1988: (45.6, 53.4, "REP"), 1984: (40.6, 58.8, "REP"), 1980: (41.0, 50.7, "REP"),
    1976: (50.1, 48.0, "DEM"), 1972: (37.5, 60.7, "REP"), 1968: (42.7, 43.4, "REP"),
    1964: (61.1, 38.5, "DEM"), 1960: (49.7, 49.6, "DEM"), 1956: (42.0, 57.4, "REP"),
    1952: (44.3, 55.2, "REP"),
}

SOURCE = "hand-seeded 2026-07 from certified national popular-vote returns (FEC/NARA archives)"


def run() -> int:
    db.migrate()
    n = 0
    with db.write() as conn:
        for cycle, (dem, rep, winner) in NATIONAL_PRESIDENT.items():
            other = round(100.0 - dem - rep, 1)
            cur = conn.execute(
                "INSERT OR IGNORE INTO political_history(tier,entity_id,office,seat,cycle_year,winner_party,"
                "dem_pct,rep_pct,other_pct,margin_pct,confidence,source) "
                "VALUES('nation','US','president','regular',?,?,?,?,?,?,'measured',?)",
                (cycle, winner, dem, rep, other, round(abs(dem - rep), 1), SOURCE))
            n += cur.rowcount
        # the sitting president's party (2024 winner) drives the midterm-penalty factor
        conn.execute("INSERT INTO app_meta(key,value) VALUES('president_party','REP') "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    return n


if __name__ == "__main__":
    print(f"imported {run()} national presidential rows")
