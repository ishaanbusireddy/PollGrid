"""News landing gates (ingestion/store.py): recency → raw_items.archival,
the deterministic US-relevance gate for us_domestic=0 firehoses, the
per-source rejection counter, and the debate-window check (ingestion/rss.py).
Runs against an isolated temp DB — no network anywhere."""
import email.utils
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("POLLGRID_TEST", "1")
import core.config as config_mod  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="pollgrid-test-")
config_mod.CONFIG["database"]["path"] = os.path.join(_tmp, "test.db")

import core.db as db  # noqa: E402
db.DB_PATH = os.path.join(_tmp, "test.db")
_conn = getattr(db._local, "conn", None)
if _conn is not None:  # standalone runs: drop any connection to a prior path
    _conn.close()
    db._local.conn = None

from ingestion.store import (  # noqa: E402
    foreign_hits, is_archival, land_raw_item, parse_published, passes_us_gate)


def _rfc822(dt: datetime) -> str:
    return email.utils.format_datetime(dt)


def _mk_source(name: str, us_domestic: int) -> int:
    sid = db.execute(
        "INSERT INTO sources(name,source_type,interval_key,url,api_key_env,reliability_tier,"
        "is_active,config_json,us_domestic) VALUES(?,?,?,?,?,?,?,?,?)",
        (name, "rss", "news_rss", "https://example.invalid/feed", "", 3, 1, "{}", us_domestic))
    return sid


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.migrate()
        from domain import geography
        geography.seed()


class TestRecencyGate(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.sid = _mk_source("recency-test-src", 1)

    def test_parse_published_formats(self):
        now = datetime.now(timezone.utc)
        self.assertIsNotNone(parse_published(_rfc822(now)))
        self.assertIsNotNone(parse_published("2026-07-01T12:00:00Z"))
        naive = parse_published("2026-07-01T12:00:00")     # naive → UTC
        self.assertEqual(naive.tzinfo, timezone.utc)
        self.assertIsNone(parse_published("not a date"))
        self.assertIsNone(parse_published(None))
        self.assertIsNone(parse_published("   "))

    def test_is_archival_boundaries(self):
        now = datetime.now(timezone.utc)
        self.assertFalse(is_archival(_rfc822(now - timedelta(hours=1))))
        self.assertTrue(is_archival(_rfc822(now - timedelta(hours=72))))   # window is 48h
        self.assertTrue(is_archival("2020-01-01T00:00:00Z"))               # ISO fallback
        # unparseable / missing published_at is NEVER archival — fresh, not guessed
        self.assertFalse(is_archival("garbage date"))
        self.assertFalse(is_archival(None))

    def test_fresh_item_lands_live_with_story(self):
        pub = _rfc822(datetime.now(timezone.utc) - timedelta(hours=2))
        rid = land_raw_item(self.sid, "fresh-1", "Senate poll shows a tied race in Arizona",
                            "https://example.invalid/a", "A new survey of the Arizona Senate race.", pub)
        self.assertIsNotNone(rid)
        row = db.query_one("SELECT archival FROM raw_items WHERE id=?", (rid,))
        self.assertEqual(row["archival"], 0)
        fact = db.query_one("SELECT id FROM extracted_facts WHERE raw_item_id=?", (rid,))
        self.assertIsNotNone(fact)
        story_link = db.query_one("SELECT 1 FROM story_facts WHERE fact_id=?", (fact["id"],))
        self.assertIsNotNone(story_link)  # fresh facts join/create a live story

    def test_stale_item_lands_archival_and_silent(self):
        pub = _rfc822(datetime.now(timezone.utc) - timedelta(days=5))
        stories_before = db.query_one("SELECT COUNT(*) c FROM stories")["c"]
        rid = land_raw_item(self.sid, "stale-1", "Governor kickoff rally recap from last week",
                            "https://example.invalid/b", "An old campaign stop writeup.", pub)
        self.assertIsNotNone(rid)
        row = db.query_one("SELECT archival FROM raw_items WHERE id=?", (rid,))
        self.assertEqual(row["archival"], 1)
        # still extracted + chained (searchable, citable) …
        fact = db.query_one("SELECT id FROM extracted_facts WHERE raw_item_id=?", (rid,))
        self.assertIsNotNone(fact)
        # … but lands silently: no story membership, no new story
        self.assertIsNone(db.query_one("SELECT 1 FROM story_facts WHERE fact_id=?", (fact["id"],)))
        self.assertEqual(db.query_one("SELECT COUNT(*) c FROM stories")["c"], stories_before)

    def test_unparseable_date_counts_as_fresh(self):
        rid = land_raw_item(self.sid, "nodate-1", "Congress schedules a vote on the bill",
                            "https://example.invalid/c", "Floor action expected.", "sometime soon")
        self.assertEqual(db.query_one("SELECT archival FROM raw_items WHERE id=?", (rid,))["archival"], 0)


class TestUsRelevanceGate(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        db.execute("INSERT OR IGNORE INTO candidates(name,party_code) VALUES('Jane Testwell','DEM')")

    def test_pass_cases(self):
        for text in [
            "Georgia Senate race tightens as early voting begins",       # state + federal word
            "Wisconsin voters weigh new ad blitz in the fall campaign",  # state name alone
            "White House unveils infrastructure push ahead of midterms",  # federal institution
            "Rematch in PA-07 draws national money",                     # XX-# district code
            "Testwell announces statewide bus tour",                     # tracked candidate surname
        ]:
            self.assertTrue(passes_us_gate(text), text)

    def test_reject_cases(self):
        for text in [
            "Knesset approves budget as coalition wobbles in Israel",
            "Georgia's parliament faces protests in Tbilisi as the ruling party clings to power",
            "Bundestag election looms as the Chancellor's coalition collapses in Germany",
            "Prime Minister calls a snap election in France",
            "EU election results reshape the European Parliament in Brussels",
        ]:
            self.assertFalse(passes_us_gate(text), text)

    def test_georgia_disambiguation(self):
        # the 'Georgia' trap both ways: US state race PASSES, Tbilisi politics REJECTS
        self.assertTrue(passes_us_gate("Georgia Senate race: runoff looms in the fall"))
        self.assertFalse(passes_us_gate(
            "Georgia's parliament in Tbilisi passes a foreign-agents law"))
        # one incidental foreign mention does not sink a genuinely US story
        self.assertTrue(passes_us_gate("President touts Ukraine aid deal at White House briefing"))
        self.assertEqual(
            foreign_hits("Georgia's parliament in Tbilisi passes a foreign-agents law"), 2)

    def test_new_mexico_is_not_mexico(self):
        self.assertEqual(foreign_hits("New Mexico Senate primary heats up"), 0)
        self.assertTrue(passes_us_gate("New Mexico Senate primary heats up"))

    def test_gated_landing_and_rejection_counter(self):
        sid = _mk_source("gated-firehose-src", 0)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        key = f"rejects:{sid}:{day}"
        self.assertIsNone(db.meta_get(key))
        # two rejects: never inserted, counter increments each time
        self.assertIsNone(land_raw_item(sid, "r1", "Knesset approves budget in Israel",
                                        None, "Coalition politics abroad.", None))
        self.assertIsNone(land_raw_item(sid, "r2", "Prime Minister calls snap election in France",
                                        None, None, None))
        self.assertEqual(db.meta_get(key), "2")
        self.assertEqual(db.query_one("SELECT COUNT(*) c FROM raw_items WHERE source_id=?", (sid,))["c"], 0)
        # a passing item on the same gated source still lands
        rid = land_raw_item(sid, "p1", "Georgia Senate race tightens", None,
                            "Early voting begins across the state.", None)
        self.assertIsNotNone(rid)
        self.assertEqual(db.meta_get(key), "2")  # counter untouched by passes

    def test_us_domestic_sources_never_gated(self):
        sid = _mk_source("trusted-desk-src", 1)
        rid = land_raw_item(sid, "k1", "Knesset approves budget in Israel", None,
                            "A trusted US desk's world coverage is not gated.", None)
        self.assertIsNotNone(rid)  # us_domestic=1 → gate never runs

    def test_seed_flags_google_news_sources(self):
        from ingestion import sources_seed
        sources_seed.seed()
        for name in sources_seed.NON_US_DOMESTIC_SOURCES:
            row = db.query_one("SELECT us_domestic FROM sources WHERE name=?", (name,))
            self.assertIsNotNone(row, name)
            self.assertEqual(row["us_domestic"], 0, name)
        row = db.query_one("SELECT us_domestic FROM sources WHERE name='NPR Politics'")
        self.assertEqual(row["us_domestic"], 1)


class TestPollsterPriors(Base):
    def test_declared_priors_seeded(self):
        from ingestion import sources_seed
        from ingestion.pollsters import POLLSTER_FEEDS, PRIOR_AS_OF
        sources_seed.seed()
        self.assertGreaterEqual(len(POLLSTER_FEEDS), 27)
        for name, expect in (("Trafalgar Group", -1.5), ("Rasmussen Reports", -1.5),
                             ("Public Policy Polling", 1.0), ("Marist", 0.0)):
            row = db.query_one(
                "SELECT r.* FROM pollster_ratings r JOIN pollsters p ON p.id=r.pollster_id "
                "WHERE p.name=? AND r.as_of=?", (name, PRIOR_AS_OF))
            self.assertIsNotNone(row, name)
            self.assertEqual(row["grade"], "prior")
            self.assertEqual(row["n_graded"], 0)
            self.assertEqual(row["house_effect_dem"], expect)
            self.assertEqual(row["weight_multiplier"], 1.0)
            self.assertEqual(row["region"], "national")
        sources_seed.seed()  # idempotent: INSERT OR IGNORE, no duplicate rows
        n = db.query_one("SELECT COUNT(*) c FROM pollster_ratings WHERE as_of=?", (PRIOR_AS_OF,))["c"]
        self.assertEqual(n, len(POLLSTER_FEEDS))


class TestDebateWindow(Base):
    def test_window_detection(self):
        from ingestion.rss import _in_debate_window
        db.execute("DELETE FROM debate_schedule")
        self.assertFalse(_in_debate_window())
        now = datetime.now(timezone.utc)
        db.execute("INSERT INTO debate_schedule(title,scheduled_at,window_hours) VALUES(?,?,6)",
                   ("Test debate", (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")))
        self.assertTrue(_in_debate_window())
        db.execute("DELETE FROM debate_schedule")
        db.execute("INSERT INTO debate_schedule(title,scheduled_at,window_hours) VALUES(?,?,6)",
                   ("Far future debate", (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")))
        self.assertFalse(_in_debate_window())
        db.execute("DELETE FROM debate_schedule")


if __name__ == "__main__":
    unittest.main()
