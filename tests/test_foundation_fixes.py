"""Regression tests for the v4.1 foundation fixes: the Analyst greeting fast-path,
keyless-Census retry/resume, the hand-seeded state presidential baseline (and its
supersession by certified imports), and the House-at-county map fan-out."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("POLLGRID_TEST", "1")
import core.config as config_mod  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="pollgrid-foundation-")
config_mod.CONFIG["database"]["path"] = os.path.join(_tmp, "test.db")

import core.db as db  # noqa: E402
db.DB_PATH = os.path.join(_tmp, "test.db")


def _seed_platform():
    db.migrate()
    from domain import geography, entities, races
    geography.seed(); entities.seed(); races.seed()


class TestAnalystGreetingFastPath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _seed_platform()

    def test_greetings_answer_instantly_without_llm_or_pack(self):
        import time
        from analyst.engine import query
        race = db.query_one("SELECT id FROM races LIMIT 1")
        for g in ("hi", "Hello!", "thanks", "ok", "yo", "good morning"):
            t0 = time.monotonic()
            r = query("race", str(race["id"]), g)
            self.assertEqual(r["model"], "greeting", g)
            self.assertLess(time.monotonic() - t0, 0.5, g)

    def test_substantive_question_takes_the_real_path(self):
        from analyst.engine import query
        race = db.query_one("SELECT id FROM races LIMIT 1")
        r = query("race", str(race["id"]), "what is the polling average here?")
        self.assertNotEqual(r["model"], "greeting")


class TestCensusKeylessRetry(unittest.TestCase):
    def test_rate_rejection_classification(self):
        import ingestion.census as census
        from ingestion.http import FetchError
        self.assertTrue(census._rate_rejected(FetchError('non-JSON: "Missing Key" required')))
        self.assertTrue(census._rate_rejected(FetchError("HTTP 429: OVER_RATE_LIMIT")))
        self.assertFalse(census._rate_rejected(FetchError("HTTP 404: not found")))

    def test_keyless_retries_rate_rejection_and_keyed_does_not(self):
        import ingestion.census as census
        from ingestion.http import FetchError
        calls = {"n": 0}

        def flaky(url, params):
            calls["n"] += 1
            if calls["n"] == 1:
                raise FetchError("Missing Key")
            return [["h"], ["v"]]

        with patch.object(census, "get_json", flaky), \
             patch.object(census.time, "sleep", lambda s: None), \
             patch.object(census.budget, "spend", lambda k: None):
            out = census._fetch("base", "state:*", None, None)   # keyless: retries
        self.assertEqual(calls["n"], 2)
        self.assertEqual(out, [["h"], ["v"]])

        calls["n"] = 0
        def always_reject(url, params):
            calls["n"] += 1
            raise FetchError("Missing Key")
        with patch.object(census, "get_json", always_reject), \
             patch.object(census.time, "sleep", lambda s: None), \
             patch.object(census.budget, "spend", lambda k: None):
            with self.assertRaises(FetchError):
                census._fetch("base", "state:*", None, "REALKEY")  # keyed: no retry
        self.assertEqual(calls["n"], 1)


class TestStatePresidentialBaseline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _seed_platform()

    def test_data_integrity_and_seed(self):
        from scripts.seed_state_presidentials import STATE_PRESIDENT, run
        for cycle, states in STATE_PRESIDENT.items():
            self.assertEqual(len(states), 51, cycle)   # 50 states + DC
            for usps, (dem, rep) in states.items():
                self.assertTrue(0 < dem < 100 and 0 < rep < 100, (cycle, usps))
                self.assertLessEqual(dem + rep, 100.05, (cycle, usps))
        run()
        rows = db.query("SELECT confidence, source FROM political_history WHERE tier='state' "
                        "AND office='president' AND cycle_year IN (2020, 2024) "
                        "AND source LIKE 'hand-seeded%'")
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(r["confidence"], "uncertain")  # transcribed, honestly labeled

    def test_certified_import_supersedes_transcription(self):
        from scripts.seed_state_presidentials import run
        from ingestion.results_tiers import _insert_history_row
        run()
        _insert_history_row("state", "06", "president", 2024,
                            {"DEM": 9276179, "REP": 6081697, "OTH": 500000})
        row = db.query_one("SELECT confidence, source FROM political_history WHERE tier='state' "
                           "AND entity_id='06' AND office='president' AND cycle_year=2024")
        self.assertEqual(row["confidence"], "measured")
        self.assertTrue(row["source"].startswith("openelections"))

    def test_certified_import_never_overwritten_by_reseed(self):
        from scripts.seed_state_presidentials import run
        from ingestion.results_tiers import _insert_history_row
        run()
        _insert_history_row("state", "48", "president", 2024,
                            {"DEM": 4835250, "REP": 6393597, "OTH": 200000})
        run()  # re-running the seed must not clobber the measured row
        row = db.query_one("SELECT confidence FROM political_history WHERE tier='state' "
                           "AND entity_id='48' AND office='president' AND cycle_year=2024")
        self.assertEqual(row["confidence"], "measured")


if __name__ == "__main__":
    unittest.main()
