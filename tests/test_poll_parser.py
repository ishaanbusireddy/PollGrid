"""Deterministic pollster-release topline parser (ingestion/pollsters.py).
Pure-function tests — no DB, no network."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingestion.pollsters import parse_release_toplines  # noqa: E402


def flat(entries):
    """[{name: pct}, ...] → {name: pct} for easy assertions."""
    out = {}
    for e in entries:
        out.update(e)
    return out


class TestToplineParser(unittest.TestCase):
    def test_name_pct_pairs(self):
        got = flat(parse_release_toplines("Smith 48%, Jones 45%", ""))
        self.assertEqual(got, {"Smith": 48.0, "Jones": 45.0})

    def test_name_at_pct(self):
        got = flat(parse_release_toplines("Biden at 44%", ""))
        self.assertEqual(got, {"Biden": 44.0})

    def test_leads_pct_to_pct(self):
        got = flat(parse_release_toplines(
            "New poll: Whitmer leads James 48% to 45% among likely voters", ""))
        self.assertEqual(got, {"Whitmer": 48.0, "James": 45.0})

    def test_bare_pair_with_lead_verb(self):
        got = flat(parse_release_toplines("Marquette Law School Poll finds Trump leads Harris 48-45", ""))
        self.assertEqual(got, {"Trump": 48.0, "Harris": 45.0})

    def test_party_words(self):
        got = flat(parse_release_toplines("Democrats lead Republicans 47-43 on the generic ballot", ""))
        self.assertEqual(got, {"Democrats": 47.0, "Republicans": 43.0})

    def test_pair_with_percent_signs_and_names(self):
        got = flat(parse_release_toplines("Smith vs. Jones: 48% - 45%", ""))
        self.assertEqual(got, {"Smith": 48.0, "Jones": 45.0})

    def test_html_and_title_prefixes_stripped(self):
        got = flat(parse_release_toplines(
            "", "<p>Sen. Smith stands at 52% while Gov. Jones sits on 41%.</p>"))
        self.assertEqual(got.get("Smith"), 52.0)

    def test_negative_margin_of_error(self):
        self.assertEqual(parse_release_toplines(
            "The poll surveyed 1,200 adults with a margin of error of 3.5%", ""), [])

    def test_negative_no_name_cooccurrence(self):
        self.assertEqual(parse_release_toplines("Turnout was 62% overall", ""), [])
        self.assertEqual(parse_release_toplines("The result was 48% - 45%.", ""), [])

    def test_negative_more_than_five_numbers(self):
        self.assertEqual(parse_release_toplines(
            "Adams 21%, Baker 19%, Clark 18%, Davis 17%, Evans 13%, Ford 9%", ""), [])

    def test_negative_out_of_range_and_year_ranges(self):
        self.assertEqual(parse_release_toplines("Smith at 100%", ""), [])
        self.assertEqual(parse_release_toplines(
            "Election recap 2020-2024", "Smith leads coverage of the 2018-2022 cycles"), [])

    def test_leader_only_pair_yields_single_entry(self):
        # 'leads 48-45' with no named opponent: only the subject's number is
        # attributable — downstream (<2 resolved) skips it, nothing is invented.
        got = flat(parse_release_toplines("Trump leads 48-45", ""))
        self.assertEqual(got, {"Trump": 48.0})


if __name__ == "__main__":
    unittest.main()
