"""Structural unit tests for the ingestion additions: CVAP CSV mapping,
OpenElections election-date filenames, FEC schedule-E medium heuristic, and
adapter registration coverage. Pure functions — no DB writes, no network."""
import csv
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingestion.cvap import rows_to_demographics  # noqa: E402
from ingestion.fec import _medium_from_purpose  # noqa: E402
from ingestion.results_tiers import general_election_date  # noqa: E402

SYNTHETIC_COUNTY_CSV = """GEONAME,LNTITLE,GEOID,LNNUMBER,TOT_EST,TOT_MOE,ADU_EST,ADU_MOE,CIT_EST,CIT_MOE,CVAP_EST,CVAP_MOE
"Autauga County, Alabama",Total,05000US01001,1,58805,0,44438,250,58005,300,"43,350",255
"Autauga County, Alabama",Hispanic or Latino,05000US01001,13,1757,140,1128,120,1576,130,1016,110
"Baldwin County, Alabama",Total,05000US01003,1,231767,0,180659,400,228400,500,177650,450
"""


class TestCvapMapping(unittest.TestCase):
    def test_county_csv_rows_map_to_demographics(self):
        rows = rows_to_demographics(csv.DictReader(io.StringIO(SYNTHETIC_COUNTY_CSV)))
        # only the two Total lines survive; the Hispanic-or-Latino line is not cvap_total
        self.assertEqual(len(rows), 2)
        by_geoid = {r[1]: r for r in rows}
        self.assertEqual(set(by_geoid), {"01001", "01003"})
        tier, entity_id, as_of, category, variable, value, confidence, source = by_geoid["01001"]
        self.assertEqual(tier, "county_equivalent")
        self.assertEqual(category, "population_age")
        self.assertEqual(variable, "cvap_total")
        self.assertEqual(value, 43350.0)  # thousands separator normalized
        self.assertEqual(confidence, "measured")
        self.assertEqual(source, "cvap_2018_2022")
        self.assertEqual(by_geoid["01003"][5], 177650.0)

    def test_malformed_rows_skipped(self):
        bad = ("geoname,lntitle,geoid,lnnumber,cvap_est\n"
               "Nowhere,Total,05000US1,1,42\n"          # geoid too short
               "Elsewhere,Total,05000US01005,1,n/a\n")  # non-numeric estimate
        self.assertEqual(rows_to_demographics(csv.DictReader(io.StringIO(bad))), [])


class TestOpenElectionsDates(unittest.TestCase):
    def test_real_election_days(self):
        self.assertEqual(general_election_date(2018), "20181106")
        self.assertEqual(general_election_date(2020), "20201103")
        self.assertEqual(general_election_date(2022), "20221108")
        self.assertEqual(general_election_date(2024), "20241105")


class TestFecMediumHeuristic(unittest.TestCase):
    def test_media_buckets(self):
        self.assertEqual(_medium_from_purpose("TELEVISION AD BUY"), "tv")
        self.assertEqual(_medium_from_purpose("radio production"), "radio")
        self.assertEqual(_medium_from_purpose("DIGITAL ADVERTISING"), "digital")
        self.assertEqual(_medium_from_purpose("EMAIL LIST RENTAL"), "digital")  # not postal mail
        self.assertEqual(_medium_from_purpose("DIRECT MAIL & POSTAGE"), "mail")
        self.assertEqual(_medium_from_purpose("canvassing literature drop"), "other")
        self.assertEqual(_medium_from_purpose(None), "other")


class TestAdapterCoverage(unittest.TestCase):
    def test_every_seeded_source_type_has_an_adapter(self):
        import ingestion.adapters  # noqa: F401 — registers everything
        from ingestion.scheduler import ADAPTERS
        from ingestion.sources_seed import SOURCES
        missing = {s[1] for s in SOURCES} - set(ADAPTERS)
        self.assertEqual(missing, set())

    def test_pollster_release_sources_are_wired(self):
        from ingestion.pollsters import POLLSTER_FEEDS
        from ingestion.sources_seed import SOURCES
        release_rows = [s for s in SOURCES if s[1] == "pollster_release"]
        self.assertEqual(len(release_rows), len(POLLSTER_FEEDS))
        self.assertGreaterEqual(len(release_rows), 12)
        for name, stype, ikey, url, key_env, tier, active, config in release_rows:
            self.assertEqual(ikey, "polls")
            self.assertEqual(tier, 1)
            self.assertEqual(active, 1)
            self.assertTrue(url.startswith("http"))
            self.assertTrue(config.get("outlet"))


if __name__ == "__main__":
    unittest.main()
