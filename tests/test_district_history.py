"""Regression tests for derived congressional-district partisan history
(modeling/district_history.py) — population-weighted areal interpolation of real
county results onto current district lines. Guards correctness + the honesty
contract (derived, never overwrites measured, honest no-op when data is absent)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("POLLGRID_TEST", "1")
import core.config as config_mod  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="pollgrid-district-")
config_mod.CONFIG["database"]["path"] = os.path.join(_tmp, "test.db")

import core.db as db  # noqa: E402
db.DB_PATH = os.path.join(_tmp, "test.db")


class TestDistrictInterpolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.migrate()
        from domain import geography, entities, races
        geography.seed(); entities.seed(); races.seed()
        # a real multi-district state present in the vendored geometry: Arizona (04)
        from core.gazetteer import _load_features
        cls.az_counties = [f["id"] for f in _load_features("us_counties.json")
                           if (f.get("id") or "").startswith("04")][:6]

    def _seed_county(self, geoid, dem, pop, cycle=2024, confidence="measured"):
        rep = 100 - dem - 3
        db.execute("INSERT OR REPLACE INTO political_history(tier,entity_id,office,seat,cycle_year,"
                   "winner_party,dem_pct,rep_pct,other_pct,margin_pct,confidence,source,is_synthetic) "
                   "VALUES('county_equivalent',?,'president','regular',?,?,?,?,3,?,?,'test',0)",
                   (geoid, cycle, "DEM" if dem > rep else "REP", dem, rep, abs(dem - rep), confidence))
        db.execute("INSERT OR REPLACE INTO demographics(tier,entity_id,category,variable,value,confidence,"
                   "source,as_of,is_synthetic) VALUES('county_equivalent',?,'population_age',"
                   "'total_population',?,'measured','test','2024-01-01',0)", (geoid, pop))

    def test_derives_in_range_labeled_rows_from_real_counties(self):
        from modeling.district_history import derive_all
        for i, g in enumerate(self.az_counties):
            self._seed_county(g, dem=60 if i % 2 == 0 else 35, pop=100000 + i * 40000)
        n = derive_all()
        self.assertGreater(n, 0)
        rows = db.query("SELECT entity_id, dem_pct, rep_pct, other_pct, confidence, source, is_synthetic "
                        "FROM political_history WHERE tier='congressional_district' AND office='president'")
        self.assertTrue(rows)
        for r in rows:
            self.assertTrue(0 <= r["dem_pct"] <= 100 and 0 <= r["rep_pct"] <= 100)
            self.assertAlmostEqual(r["dem_pct"] + r["rep_pct"] + r["other_pct"], 100, delta=0.2)
            self.assertEqual(r["confidence"], "derived")
            self.assertTrue(r["source"].startswith("derived:areal_pop_interpolation"))
            self.assertEqual(r["is_synthetic"], 0)
            # entity_id must be a real district_version_id (the key fundamentals reads)
            dv = db.query_one("SELECT 1 FROM congressional_districts WHERE district_version_id=?",
                              (int(r["entity_id"]),))
            self.assertIsNotNone(dv)

    def test_never_overwrites_a_measured_district_row(self):
        from modeling.district_history import derive_all
        for i, g in enumerate(self.az_counties):
            self._seed_county(g, dem=55, pop=120000)
        # plant a MEASURED district-president row; derivation must not touch it
        dv = db.query_one("SELECT district_version_id FROM congressional_districts "
                          "WHERE geoid LIKE '04%' AND effective_to IS NULL LIMIT 1")
        db.execute("INSERT OR REPLACE INTO political_history(tier,entity_id,office,seat,cycle_year,"
                   "winner_party,dem_pct,rep_pct,other_pct,margin_pct,confidence,source,is_synthetic) "
                   "VALUES('congressional_district',?,'president','regular',2024,'DEM',"
                   "99,1,0,98,'measured','certified',0)", (str(dv["district_version_id"]),))
        derive_all()
        kept = db.query_one("SELECT dem_pct, confidence FROM political_history "
                            "WHERE tier='congressional_district' AND entity_id=? AND cycle_year=2024",
                            (str(dv["district_version_id"]),))
        self.assertEqual(kept["confidence"], "measured")
        self.assertEqual(kept["dem_pct"], 99)  # untouched

    def test_no_county_history_writes_nothing(self):
        from modeling.district_history import derive_all
        # with no REAL county president history on hand, derivation is an honest
        # no-op — it never emits a wall of zero-lean district rows
        db.execute("DELETE FROM political_history WHERE tier='county_equivalent' AND office='president'")
        self.assertEqual(derive_all(), 0)


class TestDistrictDemographics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.migrate()
        from domain import geography, entities, races
        geography.seed(); entities.seed(); races.seed()
        from core.gazetteer import _load_features
        cls.az = [f["id"] for f in _load_features("us_counties.json")
                  if (f.get("id") or "").startswith("04")][:6]

    def test_apportions_counts_conserves_total_and_skips_rates(self):
        from modeling.district_demographics import derive_all
        pop_total = 0
        for i, g in enumerate(self.az):
            pop = 200000 + i * 100000
            pop_total += pop
            for cat, var, val in (("population_age", "total_population", pop),
                                  ("education", "pop_25plus", pop * 0.7),
                                  ("education", "bachelors", pop * 0.2),
                                  ("population_age", "median_age", 38)):
                db.execute("INSERT OR REPLACE INTO demographics(tier,entity_id,as_of,category,variable,"
                           "value,confidence,source,is_synthetic) VALUES('county_equivalent',?,'2024-01-01',"
                           "?,?,?,'measured','census',0)", (g, cat, var, val))
        n = derive_all()
        self.assertGreater(n, 0)
        # rate variable must be skipped entirely
        self.assertEqual(db.query_one("SELECT COUNT(*) c FROM demographics WHERE "
                         "tier='congressional_district' AND variable='median_age'")["c"], 0)
        # extensive count is conserved by areal apportionment (shares sum to 1)
        dist_total = db.query_one("SELECT SUM(value) s FROM demographics WHERE "
                     "tier='congressional_district' AND variable='total_population'")["s"]
        self.assertAlmostEqual(dist_total, pop_total, delta=pop_total * 0.001)
        for r in db.query("SELECT confidence, source FROM demographics WHERE tier='congressional_district'"):
            self.assertEqual(r["confidence"], "derived")
            self.assertTrue(r["source"].startswith("derived:areal_apportionment"))


if __name__ == "__main__":
    unittest.main()
