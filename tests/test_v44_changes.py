"""v4.4 tests: the FEC roster catch-up cadence, the deterministic (LLM-free)
live-recompute pipeline, and the poll article-fetch fallback that lets a topline
land when it's in the linked article rather than the RSS excerpt."""
import inspect
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("POLLGRID_TEST", "1")
import core.config as config_mod  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="pollgrid-v44-")
config_mod.CONFIG["database"]["path"] = os.path.join(_tmp, "test.db")

import core.db as db  # noqa: E402
db.DB_PATH = os.path.join(_tmp, "test.db")


def _bootstrap():
    from api.server import bootstrap
    bootstrap(start_ingestion=False)


class TestFecCatchupCadence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.migrate()

    def test_fast_until_synced_then_hourly(self):
        from ingestion.scheduler import _interval_for
        from core.config import cfg
        src = {"interval_key": "fec"}
        db.execute("DELETE FROM app_meta WHERE key='fec_roster_synced_at'")
        self.assertEqual(_interval_for(src), cfg("ingestion.intervals_seconds.fec_catchup"))
        db.meta_set("fec_roster_synced_at", "2026-07-20T00:00:00")
        self.assertEqual(_interval_for(src), cfg("ingestion.intervals_seconds.fec"))
        db.execute("DELETE FROM app_meta WHERE key='fec_roster_synced_at'")  # reset for other tests

    def test_non_fec_source_unaffected(self):
        from ingestion.scheduler import _interval_for
        from core.config import cfg
        self.assertEqual(_interval_for({"interval_key": "news_rss"}),
                         cfg("ingestion.intervals_seconds.news_rss"))


class TestLiveRecompute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _bootstrap()

    def test_fast_pipeline_stamps_and_computes(self):
        from modeling.live_recompute import fast_pipeline
        db.execute("DELETE FROM app_meta WHERE key='live_recompute_at'")
        report = fast_pipeline()
        self.assertIsNotNone(db.meta_get("live_recompute_at"))
        self.assertGreater(db.query_one("SELECT COUNT(*) c FROM forecasts")["c"], 0)
        for step in ("competitiveness", "poll_averages", "forecasts", "chamber_senate"):
            self.assertIn(step, report)

    def test_pipeline_is_strictly_deterministic_no_llm(self):
        import modeling.live_recompute as lr
        src = inspect.getsource(lr)
        self.assertNotIn("analyst.llm", src)
        self.assertNotIn("from analyst import llm", src)


class TestPollArticleFetch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _bootstrap()

    def test_article_body_lands_a_poll_when_excerpt_is_bare(self):
        import ingestion.pollsters as pol
        from core import provenance
        race = db.query_one("SELECT id FROM races WHERE race_type='senate' AND phase='general' LIMIT 1")
        c1 = db.execute("INSERT INTO candidates(name,party_code,office,is_synthetic) "
                        "VALUES('Jane Smith','DEM','senate',0)")
        c2 = db.execute("INSERT INTO candidates(name,party_code,office,is_synthetic) "
                        "VALUES('Bob Jones','REP','senate',0)")
        db.execute("INSERT OR IGNORE INTO race_candidates(race_id,candidate_id,party_code,is_incumbent) "
                   "VALUES(?,?,'DEM',0)", (race["id"], c1))
        db.execute("INSERT OR IGNORE INTO race_candidates(race_id,candidate_id,party_code,is_incumbent) "
                   "VALUES(?,?,'REP',0)", (race["id"], c2))
        rid = db.execute("INSERT INTO raw_items(source_id,external_id,fetched_at,title,url,body,published_at) "
                         "VALUES(1,'ext-pollart','2026-07-19T12:00:00','Poll out','http://x/article',"
                         "'teaser with no numbers','2026-07-19')")
        provenance.chained_insert("extracted_facts", {
            "raw_item_id": rid, "category": "polling", "summary": "poll release",
            "entities_json": "{}", "state_fips": None, "county_geoid": None,
            "race_id": race["id"], "occurred_at": "2026-07-19T12:00:00",
            "created_at": "2026-07-19T12:00:00", "is_synthetic": 0})
        before = db.query_one("SELECT COUNT(*) c FROM polls")["c"]
        item = {"title": "Poll out", "body": "teaser with no numbers",
                "link": "http://x/article", "published": "2026-07-19"}
        with patch.object(pol, "get", lambda url, timeout=None: b"Smith 48%, Jones 45% among likely voters"):
            pol._maybe_ingest_toplines({"id": 1, "name": "TestPollster"}, "TestPollster", rid, item)
        self.assertEqual(db.query_one("SELECT COUNT(*) c FROM polls")["c"], before + 1)

    def test_fetch_article_strips_html_and_dedups(self):
        import ingestion.pollsters as pol
        with patch.object(pol, "get", lambda url, timeout=None: b"<p>Smith 48%</p>"):
            t1 = pol._fetch_article_text("http://y/dedup-1")
            self.assertIn("48%", t1)
            self.assertNotIn("<p>", t1)
            self.assertIsNone(pol._fetch_article_text("http://y/dedup-1"))  # fetched once only

    def test_fetch_article_none_on_empty_url(self):
        import ingestion.pollsters as pol
        self.assertIsNone(pol._fetch_article_text(None))
        self.assertIsNone(pol._fetch_article_text(""))


if __name__ == "__main__":
    unittest.main()
