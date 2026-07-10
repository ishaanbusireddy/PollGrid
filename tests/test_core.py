"""python -m unittest discover — core: config, schema seed, provenance, models."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isolate the DB before core.db import
os.environ.setdefault("POLLGRID_TEST", "1")
import core.config as config_mod  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="pollgrid-test-")
config_mod.CONFIG["database"]["path"] = os.path.join(_tmp, "test.db")

import core.db as db  # noqa: E402
db.DB_PATH = os.path.join(_tmp, "test.db")

from core import provenance  # noqa: E402
from core.config import parse_yaml  # noqa: E402
from core.config_schema import ConfigError, validate_config  # noqa: E402


class TestYamlAndConfig(unittest.TestCase):
    def test_parse_yaml_subset(self):
        doc = parse_yaml("a:\n  b: 3\n  c: [x, y]\n  d: true\n  e: 0.5   # comment\nf: hello\n")
        self.assertEqual(doc, {"a": {"b": 3, "c": ["x", "y"], "d": True, "e": 0.5}, "f": "hello"})

    def test_real_config_validates(self):
        validate_config(config_mod.CONFIG)

    def test_bad_weights_fail_loudly(self):
        bad = json.loads(json.dumps(config_mod.CONFIG))
        bad["fundamentals"]["weights"]["incumbency"] = 0.9
        with self.assertRaises(ConfigError):
            validate_config(bad)

    def test_unknown_key_fails(self):
        bad = json.loads(json.dumps(config_mod.CONFIG))
        bad["surprise"] = {"x": 1}
        with self.assertRaises(ConfigError):
            validate_config(bad)

    def test_auto_publish_must_be_false(self):
        bad = json.loads(json.dumps(config_mod.CONFIG))
        bad["election_night"]["auto_publish_calls"] = True
        with self.assertRaises(ConfigError):
            validate_config(bad)


class TestPhaseA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.migrate()
        from domain import geography
        geography.seed()

    def test_phase_a_definition(self):
        from domain.geography import phase_a_checks
        checks = phase_a_checks()
        self.assertTrue(checks["ok"], checks)
        self.assertEqual(checks["states_total"], 56)
        self.assertEqual(checks["ev_total"], 538)
        self.assertEqual(checks["voting_districts_current"], 435)
        self.assertEqual(checks["delegate_districts_current"], 6)

    def test_maine_nebraska_elector_method(self):
        from domain.geography import ev_allocation
        self.assertEqual(ev_allocation("23", 2028)["elector_method"], "congressional_district")
        self.assertEqual(ev_allocation("31", 2028)["elector_method"], "congressional_district")
        self.assertEqual(ev_allocation("06", 2028)["elector_method"], "winner_take_all")
        # historical versioning: allocations table, not a static column (review §2.4)
        self.assertEqual(ev_allocation("23", 1968), None)

    def test_connecticut_both_vintages(self):
        legacy = db.query("SELECT * FROM county_equivalents WHERE state_fips='09' AND effective_to IS NOT NULL")
        current = db.query("SELECT * FROM county_equivalents WHERE state_fips='09' AND effective_to IS NULL")
        self.assertEqual(len(legacy), 8)
        self.assertEqual(len(current), 9)
        self.assertTrue(all(c["type"] == "planning_region" for c in current))


class TestProvenance(unittest.TestCase):
    def test_chain_append_and_verify(self):
        db.migrate()
        from domain import geography, entities, races
        geography.seed(); entities.seed(); races.seed()
        race = db.query_one("SELECT id FROM races LIMIT 1")
        from ingestion.pollsters import ingest_poll
        pid = ingest_poll(pollster="Test Poll Co", race_id=race["id"], field_start="2026-06-01",
                          field_end="2026-06-03", results={"DEM": 48, "REP": 47},
                          sample_size=800, is_synthetic=True)
        self.assertIsNotNone(pid)
        ok, detail = provenance.verify_chain("polls")
        self.assertTrue(ok, detail)

    def test_tamper_detected(self):
        race = db.query_one("SELECT id FROM races LIMIT 1")
        from ingestion.pollsters import ingest_poll
        ingest_poll(pollster="Tamper Co", race_id=race["id"], field_start="2026-06-05",
                    field_end="2026-06-07", results={"DEM": 51, "REP": 44}, is_synthetic=True)
        with db.write() as conn:
            conn.execute("UPDATE polls SET sample_size=9999 WHERE pollster_id="
                         "(SELECT id FROM pollsters WHERE name='Tamper Co')")
        ok, detail = provenance.verify_chain("polls")
        self.assertFalse(ok)
        # repair for later tests: recompute the chain (the purge script's rewrite path)
        from scripts.purge_synthetic import _rebuild_chain
        with db.write() as conn:
            _rebuild_chain(conn, "polls")
        self.assertTrue(provenance.verify_chain("polls")[0])


class TestRaceCalling(unittest.TestCase):
    def test_human_only(self):
        from modeling.race_calling import AUTO_PUBLISH_CALLS, submit_call
        self.assertIs(AUTO_PUBLISH_CALLS, False)
        race = db.query_one("SELECT id FROM races LIMIT 1")
        for bad in ("system", "SYSTEM", "model", "auto", "ai", ""):
            with self.assertRaises(ValueError):
                submit_call(race["id"], "DEM", bad)
        cid = submit_call(race["id"], "DEM", "Test Editor")
        self.assertGreater(cid, 0)


class TestModels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.migrate()
        from domain import geography, entities, races
        geography.seed(); entities.seed(); races.seed()
        race = db.query_one("SELECT id FROM races WHERE race_type='senate' LIMIT 1")
        from ingestion.pollsters import ingest_poll
        for i, (d, r) in enumerate(((48, 46), (47, 47), (49, 45))):
            ingest_poll(pollster="Model Test Co", race_id=race["id"],
                        field_start=f"2026-05-{10+i:02d}", field_end=f"2026-05-{12+i:02d}",
                        results={"DEM": d, "REP": r}, sample_size=900, is_synthetic=True)

    def test_elastic_net_shrinks_useless_features(self):
        from modeling.genius_ensemble import _fit_elastic_net
        import random
        rng = random.Random(7)
        X, y = [], []
        for _ in range(400):
            signal = rng.uniform(-2, 2)
            noise = rng.uniform(-2, 2)
            X.append([signal, noise])
            y.append(1 if signal + rng.gauss(0, 0.5) > 0 else 0)
        w = _fit_elastic_net(X, y, alpha=0.1, l1=0.5)
        self.assertGreater(abs(w[1]), abs(w[2]))  # signal weight >> noise weight

    def test_fairness_metrics(self):
        from modeling.redistricting_fairness import compute_state
        with db.write() as conn:
            for i, dem in enumerate((62, 61, 45, 44, 43, 42, 41, 40)):  # packed-Dem plan
                d = db.query_one("SELECT district_version_id FROM congressional_districts "
                                 "WHERE state_fips='17' AND district_number=? AND effective_to IS NULL",
                                 (i + 1,))
                conn.execute(
                    "INSERT OR IGNORE INTO political_history(tier,entity_id,office,seat,cycle_year,"
                    "winner_party,dem_pct,rep_pct,margin_pct,confidence,source,is_synthetic) "
                    "VALUES('congressional_district',?,?,'regular',2024,?,?,?,?, 'derived','test',1)",
                    (str(d["district_version_id"]), "house", "DEM" if dem > 50 else "REP",
                     dem, 100 - dem, abs(2 * dem - 100)))
        out = compute_state("17", 119)
        self.assertIsNotNone(out)
        self.assertLess(out["efficiency_gap"], 0)  # plan wastes Dem votes
        self.assertEqual(out["n_districts"], 8)

    def test_averaging_deterministic_and_audited(self):
        from modeling.averaging import compute_average
        race = db.query_one("SELECT DISTINCT race_id id FROM polls LIMIT 1")
        a = compute_average(race["id"], "2026-06-10")
        b = compute_average(race["id"], "2026-06-10")  # idempotent snapshot (INSERT OR IGNORE)
        self.assertIsNotNone(a)
        audit = db.query_one("SELECT * FROM computation_audit_log WHERE metric_id=?", (a["metric_id"],))
        self.assertIsNotNone(audit)
        self.assertIn("half_life", audit["formula"] + audit["inputs_json"])


class TestRouter(unittest.TestCase):
    def test_dispatch_params(self):
        from api.router import dispatch, route

        @route("GET", "/api/test/{x}/deep/{y}")
        def handler(req, x, y):
            return {"x": x, "y": y}

        status, payload = dispatch("GET", "/api/test/a1/deep/b2", None)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"x": "a1", "y": "b2"})
        self.assertIsNone(dispatch("GET", "/api/test/a1/deep", None))


if __name__ == "__main__":
    unittest.main()
