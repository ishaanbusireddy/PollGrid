"""Structural tests for the influence-ledger ingestion additions (addendum
§6.3/§8/§9/§10.1): LDA filing mapping, EAVS synthetic parse, FEC
committee_type → org_type map, schedule_e → pac_candidate_spend row shape, and
the X payload mapping. Network-facing adapters are verified on synthetic
payloads matching each API's documented shape — the sandbox can't reach the
live hosts; the scheduler's degrade paths own runtime failures."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# isolate the DB before core.db / adapter imports (same pattern as test_core)
os.environ.setdefault("POLLGRID_TEST", "1")
import core.config as config_mod  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="pollgrid-test-")
config_mod.CONFIG["database"]["path"] = os.path.join(_tmp, "test.db")

import core.db as db  # noqa: E402
db.DB_PATH = os.path.join(_tmp, "test.db")

from ingestion import fec, lobbying, social_x, voter_registration  # noqa: E402

FEC_CAND = "S8ZZ99999"


def _setup_race_and_candidate() -> tuple[int, int]:
    """Idempotent synthetic senate race (cycle 2098 — collides with nothing any
    other test module seeds) + one linked candidate with an FEC id."""
    db.migrate()
    with db.write() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO races(race_type,phase,cycle_year,state_fips,seat,name,"
            "competitiveness,is_synthetic) VALUES('senate','general',2098,'01','regular',"
            "'Test Senate 2098','tossup',1)")
        conn.execute(
            "INSERT OR IGNORE INTO candidates(fec_candidate_id,name,party_code,state_fips,"
            "office,is_synthetic) VALUES(?, 'Testerson, Testy', 'DEM', '01', 'senate', 1)",
            (FEC_CAND,))
    race = db.query_one("SELECT id FROM races WHERE cycle_year=2098 AND race_type='senate'")
    cand = db.query_one("SELECT id FROM candidates WHERE fec_candidate_id=?", (FEC_CAND,))
    db.execute("INSERT OR IGNORE INTO race_candidates(race_id,candidate_id,party_code,"
               "is_incumbent) VALUES(?,?,'DEM',0)", (race["id"], cand["id"]))
    return race["id"], cand["id"]


# ---------------------------------------------------------------------------
# §10.1 Senate LDA
# ---------------------------------------------------------------------------

LDA_FILING = {  # documented /api/v1/filings/ result shape
    "url": "https://lda.senate.gov/api/v1/filings/abc-123/",
    "filing_uuid": "abc-123",
    "filing_year": 2026,
    "filing_period": "second_quarter",
    "filing_document_url": "https://lda.senate.gov/filings/public/filing/abc-123/print/",
    "income": "120000.00",
    "expenses": None,
    "registrant": {"id": 401104, "name": "Grid Advocacy Partners LLC"},
    "client": {"id": 55, "name": "American Widget Council"},
    "lobbying_activities": [
        {"general_issue_code": "ENG", "description": "pipeline permitting"},
        {"general_issue_code": "TAX", "description": "credits"},
        {"general_issue_code": None},
    ],
}


class TestLdaFilingMapping(unittest.TestCase):
    def test_filing_maps_to_disclosure_fields(self):
        row = lobbying.map_filing(LDA_FILING)
        self.assertEqual(row["registrant"], "Grid Advocacy Partners LLC")
        self.assertEqual(row["period"], "2026-second_quarter")
        self.assertEqual(row["client"], "American Widget Council")
        self.assertEqual(json.loads(row["issue_codes"]), ["ENG", "TAX"])  # None dropped
        self.assertEqual(row["amount"], 120000.0)
        self.assertEqual(row["source_url"], LDA_FILING["filing_document_url"])

    def test_expenses_fallback_and_registrantless_skip(self):
        f = dict(LDA_FILING, income=None, expenses="55000.00")
        self.assertEqual(lobbying.map_filing(f)["amount"], 55000.0)
        f = dict(LDA_FILING, income="not disclosed", expenses=None)
        self.assertIsNone(lobbying.map_filing(f)["amount"])
        self.assertIsNone(lobbying.map_filing(dict(LDA_FILING, registrant={})))

    def test_land_filing_creates_org_and_is_idempotent(self):
        db.migrate()
        lobbying.land_filing(LDA_FILING)
        lobbying.land_filing(LDA_FILING)  # UNIQUE(org_id,period,client,amount) dedups
        org = db.query_one("SELECT * FROM lobbying_orgs WHERE name=?",
                           ("Grid Advocacy Partners LLC",))
        self.assertIsNotNone(org)
        self.assertEqual(org["org_type"], "lobbying_firm")
        self.assertEqual(org["sector"], "uncategorized")
        n = db.query_one("SELECT COUNT(*) c FROM lobbying_disclosures WHERE org_id=?",
                         (org["id"],))["c"]
        self.assertEqual(n, 1)
        row = db.query_one("SELECT * FROM lobbying_disclosures WHERE org_id=?", (org["id"],))
        self.assertEqual(row["source"], "senate_lda")
        self.assertEqual(row["period"], "2026-second_quarter")


# ---------------------------------------------------------------------------
# §6.3 EAVS voter registration
# ---------------------------------------------------------------------------

SYNTHETIC_EAVS_CSV = """FIPSCode,Jurisdiction_Name,State_Abbr,A1a,A1b,A1c
0100100000,AUTAUGA COUNTY,AL,40000,35000,5000
0100300000,BALDWIN COUNTY,AL,"150,000",120000,30000
0200000000,ALASKA,AK,600000,550000,-99
5100100000,ACCOMACK COUNTY,VA,not_reported,1200,300
0400100000,APACHE COUNTY,AZ,50000,45000,5000
"""


class TestEavsParse(unittest.TestCase):
    def test_jurisdictions_sum_to_state_rows(self):
        rows = voter_registration.parse_eavs_csv(SYNTHETIC_EAVS_CSV)
        got = {(r[1], r[4]): r[5] for r in rows}
        # two AL jurisdictions summed; thousands separator normalized
        self.assertEqual(got[("01", "registered_total")], 190000.0)
        self.assertEqual(got[("01", "registered_active")], 155000.0)
        self.assertEqual(got[("01", "registered_inactive")], 35000.0)
        # -99 sentinel skipped, not summed as negative
        self.assertEqual(got[("02", "registered_active")], 550000.0)
        self.assertNotIn(("02", "registered_inactive"), got)
        # non-numeric total skipped; the numeric columns of the same row survive
        self.assertNotIn(("51", "registered_total"), got)
        self.assertEqual(got[("51", "registered_active")], 1200.0)
        self.assertEqual(got[("04", "registered_total")], 50000.0)
        for r in rows:  # row shape is the demographics insert tuple
            self.assertEqual((r[0], r[2], r[3], r[6], r[7]),
                             ("state", "eavs_2024", "political_registration",
                              "measured", "eavs_2024"))

    def test_unrecognized_shape_yields_nothing(self):
        self.assertEqual(voter_registration.parse_eavs_csv("a,b\n1,2\n"), [])

    def test_state_feed_party_columns(self):
        text = ("County,Democratic,Republican,Minor,Total\n"
                "Ada,100000,150000,50000,300000\n"
                "Boise,\"1,000\",2000,500,3500\n")
        got = voter_registration.parse_state_feed(
            text, {"dem": "Democratic", "rep": "Republican", "other": "Minor", "total": "Total"})
        self.assertEqual(got, {"registered_dem": 101000.0, "registered_rep": 152000.0,
                               "registered_other": 50500.0, "registered_total": 303500.0})


# ---------------------------------------------------------------------------
# §9 FEC expansion
# ---------------------------------------------------------------------------

class TestFecCommitteeTypeMap(unittest.TestCase):
    def test_committee_type_to_org_type(self):
        self.assertEqual(fec._org_type_for_committee("O"), "super_pac")  # IE-only
        self.assertEqual(fec._org_type_for_committee("o"), "super_pac")
        self.assertEqual(fec._org_type_for_committee("N"), "pac")
        self.assertEqual(fec._org_type_for_committee("Q"), "pac")
        self.assertEqual(fec._org_type_for_committee("W"), "pac")  # best-effort default
        self.assertEqual(fec._org_type_for_committee(None), "pac")


class TestScheduleE(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.race_id, cls.cand_id = _setup_race_and_candidate()

    def _results(self):
        base = {"candidate_id": FEC_CAND, "expenditure_date": "2098-06-20T00:00:00",
                "committee": {"name": "GRID FUTURE SUPER PAC", "committee_id": "C00999991"}}
        return [
            dict(base, expenditure_amount=12500.0, support_oppose_indicator="S",
                 expenditure_description="TELEVISION AD BUY"),
            dict(base, expenditure_amount=8000.0, support_oppose_indicator="O",
                 expenditure_description="DIGITAL ADVERTISING"),
            # no S/O indicator (contribution-type record) → ad_spend only, never guessed
            dict(base, expenditure_amount=300.0, support_oppose_indicator=None,
                 expenditure_description="POSTAGE"),
        ]

    def test_ie_rows_land_in_ad_spend_and_pac_candidate_spend(self):
        landed = fec._land_schedule_e(self._results(), 2098)
        self.assertEqual(landed, 3)  # every attributable row becomes ad_spend
        org = db.query_one("SELECT * FROM lobbying_orgs WHERE fec_committee_id='C00999991'")
        self.assertIsNotNone(org)
        spends = db.query("SELECT * FROM pac_candidate_spend WHERE org_id=? ORDER BY amount",
                          (org["id"],))
        self.assertEqual(len(spends), 2)  # indicator-less row skipped, not guessed
        oppose, support = spends
        self.assertEqual(support["spend_type"], "ie_support")
        self.assertEqual(support["amount"], 12500.0)
        self.assertEqual(oppose["spend_type"], "ie_oppose")
        self.assertEqual(oppose["amount"], 8000.0)
        for s in spends:  # row shape: joined to race + candidate, dated, sourced
            self.assertEqual(s["candidate_id"], self.cand_id)
            self.assertEqual(s["race_id"], self.race_id)
            self.assertEqual(s["cycle_year"], 2098)
            self.assertEqual(s["as_of"], "2098-06-20")
            self.assertEqual(s["source"], "openfec:schedule_e")

    def test_relanding_is_idempotent(self):
        fec._land_schedule_e(self._results(), 2098)
        n_ads = db.query_one("SELECT COUNT(*) c FROM ad_spend WHERE race_id=? AND "
                             "source='openfec:schedule_e'", (self.race_id,))["c"]
        n_spend = db.query_one(
            "SELECT COUNT(*) c FROM pac_candidate_spend WHERE race_id=?", (self.race_id,))["c"]
        fec._land_schedule_e(self._results(), 2098)
        self.assertEqual(db.query_one("SELECT COUNT(*) c FROM ad_spend WHERE race_id=? AND "
                                      "source='openfec:schedule_e'", (self.race_id,))["c"], n_ads)
        self.assertEqual(db.query_one("SELECT COUNT(*) c FROM pac_candidate_spend WHERE "
                                      "race_id=?", (self.race_id,))["c"], n_spend)

    def test_unattributable_candidate_skipped(self):
        rows = [{"candidate_id": "H0NOPE0000", "expenditure_amount": 1.0,
                 "expenditure_date": "2098-01-01", "support_oppose_indicator": "S",
                 "committee": {"name": "X PAC", "committee_id": "C00999992"}}]
        self.assertEqual(fec._land_schedule_e(rows, 2098), 0)


class TestFecScheduleAB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.race_id, cls.cand_id = _setup_race_and_candidate()

    def test_donor_aggregates_predelete_then_insert(self):
        results = [{"contributor_name": "ACME CORP EMPLOYEES", "total": "5000.5", "count": 12},
                   {"contributor_name": "", "total": 1.0},          # nameless → skipped
                   {"contributor_name": "BAD", "total": "n/a"}]     # non-numeric → skipped
        self.assertEqual(fec._land_donor_aggregates(self.cand_id, 2098, results), 1)
        fec._land_donor_aggregates(self.cand_id, 2098, results)  # idempotent via pre-delete
        rows = db.query("SELECT * FROM donors_aggregated WHERE candidate_id=? AND cycle_year=2098 "
                        "AND source=?", (self.cand_id, fec.SCHEDULE_A_SOURCE))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["contributor_name"], "Acme Corp Employees")
        self.assertEqual(rows[0]["total_amount"], 5000.5)
        self.assertEqual(rows[0]["n_contributions"], 12)

    def test_schedule_b_media_rows_and_disbursement_total(self):
        results = [
            {"disbursement_amount": 2000.0, "disbursement_date": "2098-05-01",
             "disbursement_description": "DIGITAL ADVERTISING",
             "committee": {"name": "TESTERSON FOR SENATE"}},
            {"disbursement_amount": 900.0, "disbursement_date": "2098-05-02",
             "disbursement_description": "STAFF SALARY",  # non-media → total only
             "committee": {"name": "TESTERSON FOR SENATE"}},
        ]
        fec._land_schedule_b(self.cand_id, FEC_CAND, 2098, results)
        fec._land_schedule_b(self.cand_id, FEC_CAND, 2098, results)  # idempotent
        ads = db.query("SELECT * FROM ad_spend WHERE race_id=? AND source='openfec:schedule_b'",
                       (self.race_id,))
        self.assertEqual(len(ads), 1)
        self.assertEqual(ads[0]["medium"], "digital")
        self.assertEqual(ads[0]["amount"], 2000.0)
        totals = db.query("SELECT * FROM donors_aggregated WHERE candidate_id=? AND "
                          "cycle_year=2098 AND contributor_name='__disbursements__'",
                          (self.cand_id,))
        self.assertEqual(len(totals), 1)  # pre-delete+insert, like __totals__
        self.assertEqual(totals[0]["total_amount"], 2900.0)
        self.assertEqual(totals[0]["n_contributions"], 2)


# ---------------------------------------------------------------------------
# §8 X (gated) payload mapping + adapter/source registration
# ---------------------------------------------------------------------------

class TestSocialXMapping(unittest.TestCase):
    def test_tweets_payload_to_raw_items(self):
        payload = {"data": [
            {"id": "1901", "text": "Polls look great in the 4th.",
             "created_at": "2026-07-01T12:00:00.000Z"},
            {"id": "", "text": "dropped"},
            {"id": "1902", "text": ""},
        ]}
        items = social_x.posts_to_items("SenTesterson", payload)
        self.assertEqual(len(items), 1)
        external_id, title, url, body, published_at = items[0]
        self.assertEqual(external_id, "x:1901")
        self.assertEqual(url, "https://x.com/SenTesterson/status/1901")
        self.assertTrue(title.startswith("@SenTesterson: Polls look great"))
        self.assertEqual(body, "Polls look great in the 4th.")
        self.assertEqual(published_at, "2026-07-01T12:00:00.000Z")


class TestRegistration(unittest.TestCase):
    def test_new_adapters_registered(self):
        import ingestion.adapters  # noqa: F401
        from ingestion.scheduler import ADAPTERS
        for t in ("lda_lobbying", "voter_registration", "social_x"):
            self.assertIn(t, ADAPTERS)

    def test_ensure_source_rows_idempotent(self):
        db.migrate()
        for mod in (lobbying, voter_registration, social_x):
            mod.ensure_source()
            mod.ensure_source()
        for t in ("lda_lobbying", "voter_registration", "social_x"):
            rows = db.query("SELECT * FROM sources WHERE source_type=?", (t,))
            self.assertEqual(len(rows), 1, t)
        lda = db.query_one("SELECT * FROM sources WHERE source_type='lda_lobbying'")
        self.assertEqual(lda["interval_key"], "census")
        vr = db.query_one("SELECT * FROM sources WHERE source_type='voter_registration'")
        self.assertEqual(vr["interval_key"], "census")
        self.assertIn("state_feeds", json.loads(vr["config_json"]))
        sx = db.query_one("SELECT * FROM sources WHERE source_type='social_x'")
        self.assertEqual(sx["interval_key"], "social")

    def test_social_x_gated_when_disabled(self):
        from core.config import CONFIG
        from ingestion.http import SourceNotConfigured
        self.assertFalse(CONFIG["ingestion"]["social_x"]["enabled"])  # ships disabled
        db.migrate()
        social_x.ensure_source()
        src = db.query_one("SELECT * FROM sources WHERE source_type='social_x'")
        with self.assertRaises(SourceNotConfigured):
            social_x.run(src)


if __name__ == "__main__":
    unittest.main()
