#!/usr/bin/env python3
"""Real state-level presidential toplines (2020, 2024), hand-seeded — the same
day-one pattern as the national baseline (backfill_history.py), extended to the
state tier so the STATE map colors immediately with zero keys and zero network.

HONESTY CONTRACT: these values are transcriptions of certified public-record
returns. The WINNERS are certain; the one-decimal percentages are transcribed
and may be off by a couple tenths — so every row is written
confidence='uncertain' (not 'measured'), the source string says exactly that,
and results_tiers._insert_history_row supersedes these rows the moment a real
certified import (OpenElections state rollup / official file) lands. They make
the map real on day one; they never masquerade as certified-exact, and they
never block the genuine article.

Usage: python scripts/seed_state_presidentials.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db  # noqa: E402
from domain.geography import USPS_TO_FIPS  # noqa: E402

# usps: (dem_pct, rep_pct) — certified statewide presidential returns, one decimal.
STATE_PRESIDENT: dict[int, dict[str, tuple[float, float]]] = {
    2020: {  # Biden (DEM) v. Trump (REP)
        "AL": (36.6, 62.0), "AK": (42.8, 52.8), "AZ": (49.4, 49.1), "AR": (34.8, 62.4),
        "CA": (63.5, 34.3), "CO": (55.4, 41.9), "CT": (59.3, 39.2), "DE": (58.7, 39.8),
        "DC": (92.1, 5.4), "FL": (47.9, 51.2), "GA": (49.5, 49.2), "HI": (63.7, 34.3),
        "ID": (33.1, 63.8), "IL": (57.5, 40.6), "IN": (41.0, 57.0), "IA": (44.9, 53.1),
        "KS": (41.6, 56.2), "KY": (36.2, 62.1), "LA": (39.9, 58.5), "ME": (53.1, 44.0),
        "MD": (65.4, 32.2), "MA": (65.6, 32.1), "MI": (50.6, 47.8), "MN": (52.4, 45.3),
        "MS": (41.1, 57.6), "MO": (41.4, 56.8), "MT": (40.5, 56.9), "NE": (39.2, 58.2),
        "NV": (50.1, 47.7), "NH": (52.7, 45.4), "NJ": (57.3, 41.4), "NM": (54.3, 43.5),
        "NY": (60.9, 37.7), "NC": (48.6, 49.9), "ND": (31.8, 65.1), "OH": (45.2, 53.3),
        "OK": (32.3, 65.4), "OR": (56.5, 40.4), "PA": (49.9, 48.7), "RI": (59.4, 38.6),
        "SC": (43.4, 55.1), "SD": (35.6, 61.8), "TN": (37.5, 60.7), "TX": (46.5, 52.1),
        "UT": (37.7, 58.1), "VT": (66.1, 30.7), "VA": (54.1, 44.0), "WA": (58.0, 38.8),
        "WV": (29.7, 68.6), "WI": (49.4, 48.8), "WY": (26.6, 69.9),
    },
    2024: {  # Harris (DEM) v. Trump (REP)
        "AL": (34.1, 64.8), "AK": (41.4, 54.5), "AZ": (46.7, 52.2), "AR": (33.6, 64.2),
        "CA": (58.5, 38.3), "CO": (54.2, 43.2), "CT": (56.4, 41.9), "DE": (56.6, 41.9),
        "DC": (90.3, 6.5), "FL": (43.0, 56.1), "GA": (48.5, 50.7), "HI": (60.6, 37.5),
        "ID": (30.4, 66.9), "IL": (54.1, 43.8), "IN": (39.6, 58.6), "IA": (42.7, 56.0),
        "KS": (41.0, 57.2), "KY": (33.9, 64.6), "LA": (38.2, 60.2), "ME": (52.1, 45.5),
        "MD": (63.0, 34.4), "MA": (61.2, 36.5), "MI": (48.3, 49.7), "MN": (50.9, 46.7),
        "MS": (37.5, 61.2), "MO": (40.1, 58.5), "MT": (38.5, 58.4), "NE": (39.0, 59.6),
        "NV": (47.5, 50.6), "NH": (50.9, 48.1), "NJ": (52.0, 46.1), "NM": (51.9, 45.9),
        "NY": (56.0, 43.4), "NC": (47.8, 51.0), "ND": (30.5, 67.5), "OH": (43.9, 55.2),
        "OK": (31.9, 66.2), "OR": (55.3, 41.0), "PA": (48.7, 50.4), "RI": (55.5, 42.4),
        "SC": (40.4, 58.2), "SD": (34.2, 63.4), "TN": (34.5, 64.2), "TX": (42.4, 56.2),
        "UT": (37.8, 59.4), "VT": (64.4, 32.3), "VA": (51.8, 46.6), "WA": (57.0, 39.2),
        "WV": (28.1, 70.0), "WI": (48.7, 49.6), "WY": (26.1, 71.6),
    },
}

SOURCE = ("hand-seeded 2026-07: transcription of certified statewide presidential returns "
          "(winners certain; percentages transcribed to one decimal — superseded automatically "
          "by any official/OpenElections import)")


def run() -> int:
    """INSERT OR IGNORE: never touches a row that already exists — a genuinely
    measured OpenElections/official rollup that landed first always wins, and
    results_tiers supersedes THESE rows if it lands later."""
    db.migrate()
    n = 0
    with db.write() as conn:
        for cycle, states in STATE_PRESIDENT.items():
            for usps, (dem, rep) in states.items():
                fips = USPS_TO_FIPS.get(usps)
                if not fips:
                    continue
                other = round(max(0.0, 100.0 - dem - rep), 1)
                winner = "DEM" if dem > rep else "REP"
                cur = conn.execute(
                    "INSERT OR IGNORE INTO political_history(tier,entity_id,office,seat,cycle_year,"
                    "winner_party,dem_pct,rep_pct,other_pct,margin_pct,confidence,source) "
                    "VALUES('state',?,'president','regular',?,?,?,?,?,?,'uncertain',?)",
                    (fips, cycle, winner, dem, rep, other, round(abs(dem - rep), 1), SOURCE))
                n += cur.rowcount
    return n


if __name__ == "__main__":
    print(f"seeded {run()} state presidential rows (confidence='uncertain', transcribed toplines)")
