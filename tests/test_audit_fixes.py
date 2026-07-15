"""Regression tests for the v3.2 audit fixes — each locks in a specific bug the
subsystem bug-hunt confirmed, so it can't silently come back."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("POLLGRID_TEST", "1")
import core.config as config_mod  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="pollgrid-audit-")
config_mod.CONFIG["database"]["path"] = os.path.join(_tmp, "test.db")

import core.db as db  # noqa: E402
db.DB_PATH = os.path.join(_tmp, "test.db")


class TestPollingClassifier(unittest.TestCase):
    """A word-boundary keyword list must still catch the plural/gerund poll forms
    that dominate real release text, or genuine poll releases never land."""

    def test_plural_and_gerund_forms_classify_as_polling(self):
        from processing.extraction import classify
        for text in ("Latest polling shows Ossoff leads",
                     "New survey: Kemp ahead by 5 points",
                     "Recent polls show a dead heat",
                     "Pollster releases new numbers"):
            self.assertEqual(classify(text), "polling", text)

    def test_press_release_framing_does_not_beat_polling(self):
        from processing.extraction import classify
        self.assertEqual(classify("New poll: statement from the campaign on the survey"), "polling")


class TestHouseEffectPrecedence(unittest.TestCase):
    """A nightly grade='provisional' zero-row must never shadow a declared prior."""

    @classmethod
    def setUpClass(cls):
        db.migrate()

    def tearDown(self):
        # the suite shares one DB file across modules; don't leave stray 'prior'
        # rows that other modules' exact pollster counts would pick up
        row = db.query_one("SELECT id FROM pollsters WHERE name='TestShop Reg'")
        if row:
            db.execute("DELETE FROM pollster_ratings WHERE pollster_id=?", (row["id"],))
            db.execute("DELETE FROM pollsters WHERE id=?", (row["id"],))

    def test_declared_prior_survives_provisional_refresh(self):
        from modeling.averaging import _house_effects
        pid = db.execute("INSERT INTO pollsters(name) VALUES('TestShop Reg')")
        # declared prior (old as_of) with a real house effect
        db.execute("INSERT INTO pollster_ratings(pollster_id,as_of,n_graded,grade,house_effect_dem,"
                   "weight_multiplier,region) VALUES(?,?,0,'prior',-1.5,1.0,'national')",
                   (pid, "2000-01-01"))
        self.assertEqual(_house_effects(pid)[0], -1.5)
        # a newer provisional zero-row must NOT win
        db.execute("INSERT INTO pollster_ratings(pollster_id,as_of,n_graded,grade,house_effect_dem,"
                   "weight_multiplier,region) VALUES(?,?,0,'provisional',0,1.0,'national')",
                   (pid, "2026-07-13"))
        self.assertEqual(_house_effects(pid)[0], -1.5)
        # a genuinely graded row DOES supersede the prior
        db.execute("INSERT INTO pollster_ratings(pollster_id,as_of,avg_abs_error,n_graded,grade,"
                   "house_effect_dem,weight_multiplier,region) VALUES(?,?,2.0,8,'B',0.7,1.0,'national')",
                   (pid, "2026-07-14"))
        self.assertEqual(_house_effects(pid)[0], 0.7)


class TestFactorVectorAsOf(unittest.TestCase):
    """latest_vector(as_of) must read the snapshot as of that date, not today's —
    the ensemble refit's no-hindsight guarantee depends on it."""

    @classmethod
    def setUpClass(cls):
        db.migrate()

    def test_as_of_filters_future_scores(self):
        from modeling.factors_taxonomy import latest_vector, FACTORS
        key = next(iter(FACTORS))
        rid = db.execute("INSERT INTO races(race_type,phase,cycle_year,seat,name) "
                         "VALUES('senate','general',2026,'regular','Test Race AsOf')")
        db.execute("INSERT INTO qualitative_factor_scores(race_id,factor_key,as_of,score,method) "
                   "VALUES(?,?,?,?,'deterministic')", (rid, key, "2026-01-01", 0.3))
        db.execute("INSERT INTO qualitative_factor_scores(race_id,factor_key,as_of,score,method) "
                   "VALUES(?,?,?,?,'deterministic')", (rid, key, "2026-06-01", 0.9))
        self.assertEqual(latest_vector(rid, "2026-03-01")[key], 0.3)   # future 0.9 excluded
        self.assertEqual(latest_vector(rid)[key], 0.9)                 # newest by default


class TestAnalystStaleSession(unittest.TestCase):
    """A client-supplied session_id that no longer exists (e.g. after a DB
    reset) must never crash the Analyst with a FOREIGN KEY IntegrityError."""

    @classmethod
    def setUpClass(cls):
        db.migrate()
        from domain import geography, entities, races
        geography.seed(); entities.seed(); races.seed()

    def test_nonexistent_session_id_falls_back_to_a_new_session(self):
        from analyst.engine import query
        race = db.query_one("SELECT id FROM races LIMIT 1")
        result = query("race", str(race["id"]), "hello", session_id=999999999)
        self.assertNotEqual(result["session_id"], 999999999)
        row = db.query_one("SELECT 1 FROM analyst_sessions WHERE id=?", (result["session_id"],))
        self.assertIsNotNone(row)


class TestRssTolerantFallback(unittest.TestCase):
    """Real-world feeds embed raw, non-CDATA HTML in description/content that
    breaks strict XML parsing in ways the BOM/entity repair doesn't reach —
    the tolerant per-item regex fallback must still extract clean items."""

    def test_unclosed_tags_and_bare_entities_in_description(self):
        from ingestion.rss import parse_feed
        feed = (b'<?xml version="1.0"?><rss><channel>'
                b'<item><title>T</title><link>http://x</link><guid>g1</guid>'
                b'<description>Leads by 3.<br>Up next.&nbsp;More.</description>'
                b'<pubDate>Mon</pubDate></item>'
                b'</channel></rss>')
        items = parse_feed(feed)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "T")
        self.assertNotIn("<br>", items[0]["body"])
        self.assertNotIn("&nbsp;", items[0]["body"])
        self.assertIn("Leads by 3.", items[0]["body"])

    def test_mismatched_tag_in_description_does_not_lose_the_item(self):
        from ingestion.rss import parse_feed
        feed = (b'<?xml version="1.0"?><rss><channel>'
                b'<item><title>Recap</title><link>http://x</link><guid>g2</guid>'
                b'<description>He said <b>this</b></div> and more.</description>'
                b'<pubDate>Tue</pubDate></item>'
                b'</channel></rss>')
        items = parse_feed(feed)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Recap")

    def test_well_formed_feed_still_uses_the_fast_path(self):
        from ingestion.rss import parse_feed
        feed = (b'<?xml version="1.0"?><rss><channel><item>'
                b'<title>Fine</title><link>http://x</link><guid>3</guid>'
                b'<description><![CDATA[<p>ok</p>]]></description><pubDate>Wed</pubDate>'
                b'</item></channel></rss>')
        items = parse_feed(feed)
        self.assertEqual(items[0]["title"], "Fine")


class TestNeutralFallbackRationale(unittest.TestCase):
    """The stored rationale must say which of the two distinct reasons
    actually applies — 'no LLM provider reachable' is false when the
    provider is fine and the race simply has no facts to score yet."""

    @classmethod
    def setUpClass(cls):
        db.migrate()

    def test_no_facts_reports_the_honest_reason_not_provider_down(self):
        from unittest.mock import patch
        from modeling.factors_taxonomy import score_race
        rid = db.execute("INSERT INTO races(race_type,phase,cycle_year,seat,name) "
                         "VALUES('senate','general',2026,'regular','No Facts Test')")
        with patch("analyst.llm.provider_available", return_value=True):
            rows = score_race(rid)
        neutral = [r for r in rows if r["method"] == "neutral_fallback"]
        self.assertTrue(neutral)
        self.assertEqual(neutral[0]["rationale"], "no recent cited facts to score against")


if __name__ == "__main__":
    unittest.main()
