"""Regression tests for v4.3: primary/special race seeding (idempotent),
the election calendar, the officeholder roster + API, the primary forecast
guard, and the US-relevance story gate."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("POLLGRID_TEST", "1")
import core.config as config_mod  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="pollgrid-v43-")
config_mod.CONFIG["database"]["path"] = os.path.join(_tmp, "test.db")

import core.db as db  # noqa: E402
db.DB_PATH = os.path.join(_tmp, "test.db")


def _seed():
    db.migrate()
    from domain import geography, entities, races
    geography.seed(); entities.seed(); races.seed()
    from scripts.seed_officeholders import run as soh
    soh()


class Req:
    def __init__(self, q):
        self.query = q


class TestRaceUniverse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _seed()

    def test_primaries_and_specials_seeded_idempotently(self):
        from domain.races import upgrade_2026
        counts = lambda: (  # noqa: E731
            db.query_one("SELECT COUNT(*) c FROM races WHERE phase='primary'")["c"],
            db.query_one("SELECT COUNT(*) c FROM races WHERE seat='special'")["c"],
            db.query_one("SELECT COUNT(*) c FROM races")["c"])
        first = counts()
        upgrade_2026()
        upgrade_2026()
        self.assertEqual(first, counts())          # no growth on re-boot
        self.assertGreaterEqual(first[0], 500)     # ~506 primary elections
        self.assertEqual(first[1], 4)              # FL/OH specials, general + primary

    def test_generals_are_dated(self):
        n = db.query_one("SELECT COUNT(*) c FROM races WHERE cycle_year=2026 "
                         "AND phase='general' AND election_date IS NULL")["c"]
        self.assertEqual(n, 0)

    def test_primaries_carry_state_calendar_dates(self):
        r = db.query_one("SELECT election_date FROM races WHERE phase='primary' "
                         "AND state_fips='48' LIMIT 1")   # Texas
        self.assertEqual(r["election_date"], "2026-03-03")

    def test_races_api_defaults_to_general(self):
        from api.routes import races as races_route
        rows = races_route(Req({"type": "house", "state": "48"}))
        self.assertTrue(rows)
        self.assertTrue(all(r["phase"] == "general" for r in rows))
        primaries = races_route(Req({"type": "house", "state": "48", "phase": "primary"}))
        self.assertTrue(all(r["phase"] == "primary" for r in primaries))


class TestOfficeholders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _seed()

    def test_roster_complete_and_idempotent(self):
        from scripts.seed_officeholders import run as soh
        n_open = db.query_one("SELECT COUNT(*) c FROM officeholders WHERE end_date IS NULL")["c"]
        self.assertEqual(n_open, 150)  # 50 governors + 100 senators
        self.assertEqual(soh(), 0)     # reseed changes nothing

    def test_officeholders_api_shape(self):
        from api.routes import officeholders as oh_route
        ca = oh_route(Req({}), "06")
        self.assertEqual(ca["governor"]["name"], "Gavin Newsom")
        self.assertEqual(len(ca["senators"]), 2)
        vt = oh_route(Req({}), "50")
        parties = {s["name"]: s["party_code"] for s in vt["senators"]}
        self.assertEqual(parties.get("Bernie Sanders"), "IND")

    def test_congress_gov_name_and_party_normalization(self):
        from ingestion.congress_gov import _party3
        self.assertEqual(_party3("Democratic"), "DEM")
        self.assertEqual(_party3("Republican"), "REP")
        self.assertEqual(_party3("Independent"), "IND")
        self.assertEqual(_party3("Something Else"), "OTH")


class TestElectionsCalendar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _seed()

    def test_calendar_covers_every_state_plus_dc(self):
        n = db.query_one("SELECT COUNT(DISTINCT state_fips) c FROM election_calendar "
                         "WHERE kind='primary' AND cycle_year=2026")["c"]
        self.assertEqual(n, 51)

    def test_elections_api(self):
        from api.routes import elections as elections_route
        out = elections_route(Req({}))
        self.assertTrue(out["entries"])
        dates = [e["date"] for e in out["entries"]]
        self.assertEqual(dates, sorted(dates))
        tx = elections_route(Req({"state": "48"}))
        self.assertTrue(tx.get("races"))
        self.assertIn("2026-03-03", [e["date"] for e in tx["entries"]])


class TestPrimaryForecastGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _seed()

    def test_no_two_party_forecast_for_a_primary(self):
        from modeling.forecasting import compute
        r = db.query_one("SELECT id FROM races WHERE phase='primary' LIMIT 1")
        self.assertIsNone(compute(r["id"]))


class TestUsRelevanceStoryGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _seed()

    def _fact(self, summary, state_fips=None):
        from core import provenance
        return provenance.chained_insert("extracted_facts", {
            "raw_item_id": None, "category": "other", "summary": summary,
            "entities_json": "{}", "state_fips": state_fips, "county_geoid": None,
            "race_id": None, "occurred_at": "2026-07-19T12:00:00",
            "created_at": "2026-07-19T12:00:00", "is_synthetic": 0})

    def test_foreign_no_entity_item_makes_no_story(self):
        from processing.extraction import cluster_fact
        before = db.query_one("SELECT COUNT(*) c FROM stories")["c"]
        fid = self._fact("Parliament in Ottawa debates a new Canada trade pact with the EU")
        cluster_fact(fid)
        self.assertEqual(db.query_one("SELECT COUNT(*) c FROM stories")["c"], before)

    def test_us_item_still_makes_a_story(self):
        from processing.extraction import cluster_fact
        before = db.query_one("SELECT COUNT(*) c FROM stories")["c"]
        fid = self._fact("Georgia Senate contest tightens after new filing", state_fips="13")
        cluster_fact(fid)
        self.assertEqual(db.query_one("SELECT COUNT(*) c FROM stories")["c"], before + 1)


if __name__ == "__main__":
    unittest.main()
