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


if __name__ == "__main__":
    unittest.main()
