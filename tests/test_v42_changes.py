"""Regression tests for the v4.2 changes: OpenElections CSV lone-\\r tolerance,
the RSS content:encoded body read, the district->state partisan-lean fallback,
forecasts always being visible, and the House county-tier fill."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("POLLGRID_TEST", "1")
import core.config as config_mod  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="pollgrid-v42-")
config_mod.CONFIG["database"]["path"] = os.path.join(_tmp, "test.db")

import core.db as db  # noqa: E402
db.DB_PATH = os.path.join(_tmp, "test.db")


def _seed_full():
    db.migrate()
    from domain import geography, entities, races
    geography.seed(); entities.seed(); races.seed()
    from scripts.backfill_history import run as seed_nat
    from scripts.seed_state_presidentials import run as seed_states
    seed_nat(); seed_states()


class TestOpenElectionsCsvNewline(unittest.TestCase):
    def test_lone_cr_does_not_raise(self):
        # the exact failure mode that took the OpenElections source DOWN:
        # a file with lone \r characters. Must parse, not raise.
        from ingestion.results_tiers import _parse_openelections_csv
        raw = "county,office,party,votes\rAda,President,DEM,100\rAda,President,REP,80\r"
        parsed = _parse_openelections_csv(raw)
        self.assertTrue(parsed)
        self.assertIn(("Ada", "president"), parsed)


class TestRssContentEncoded(unittest.TestCase):
    def test_reads_content_encoded_over_teaser(self):
        from ingestion.rss import parse_feed
        feed = (b'<?xml version="1.0"?>'
                b'<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>'
                b'<item><title>Poll</title><guid>x1</guid>'
                b'<description>teaser only, no numbers</description>'
                b'<content:encoded><![CDATA[<p>Smith leads 48% to 45%.</p>]]></content:encoded>'
                b'</item></channel></rss>')
        items = parse_feed(feed)
        self.assertEqual(len(items), 1)
        self.assertIn("48%", items[0]["body"])
        self.assertIn("45%", items[0]["body"])
        self.assertNotIn("<p>", items[0]["body"])  # tags stripped


class TestForecastAlwaysVisible(unittest.TestCase):
    def test_visible_regardless_of_backtest(self):
        from modeling.forecasting import category_visible
        for cat in ("senate", "house", "governor", "president"):
            vis, _ = category_visible(cat)
            self.assertTrue(vis, cat)


class TestDistrictStateLeanFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _seed_full()

    def test_district_inherits_state_lean_without_district_history(self):
        from modeling.fundamentals import partisan_lean
        state_lean = partisan_lean({"state_fips": "06", "district_version_id": None})
        self.assertNotEqual(state_lean, 0.0)  # CA has real seeded state history
        race = db.query_one("SELECT * FROM races WHERE race_type='house' AND state_fips='06' LIMIT 1")
        self.assertIsNotNone(race)
        # no district-level presidential history was derived (no county import),
        # so the house race must fall back to its state's lean, not paint 0.0
        self.assertEqual(partisan_lean(race), state_lean)


class TestHouseCountyNotBlank(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _seed_full()

    def test_house_county_tier_fills(self):
        from api.routes import map_values

        class Req:
            def __init__(self, q):
                self.query = q

        r = map_values(Req({"mode": "partisan_lean", "tier": "county", "race_type": "house"}))
        five = sum(1 for k in r["values"] if len(k) == 5)
        self.assertGreater(five, 1000)  # counties fan from state lean on a fresh DB

    def test_all_districts_fill(self):
        from api.routes import map_values

        class Req:
            def __init__(self, q):
                self.query = q

        r = map_values(Req({"mode": "partisan_lean", "tier": "district", "race_type": "house"}))
        nonzero = sum(1 for v in r["values"].values() if v)
        self.assertGreaterEqual(nonzero, 400)  # ~all 435 colored via fallback


if __name__ == "__main__":
    unittest.main()
