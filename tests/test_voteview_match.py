"""Unit tests for the VoteView -> candidates name-matching used by
scripts/backfill_voteview.py. Pure functions — no DB, no network."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domain.geography import USPS_TO_FIPS  # noqa: E402
from scripts.backfill_voteview import match_member, name_key  # noqa: E402


def _index(*cands):
    """(candidate_id, 'Name', 'GA') tuples -> the index shape match_member expects."""
    index = {}
    for cid, name, usps in cands:
        key = name_key(name)
        index.setdefault((key[0], key[1], USPS_TO_FIPS[usps]), []).append(cid)
    return index


class TestVoteviewNameMatch(unittest.TestCase):
    def test_fec_style_comma_name_matches_bioname(self):
        # FEC roster stores 'Ossoff, T. Jonathan'-style names; VoteView bionames
        # are 'OSSOFF, Jon' — same surname + initial + state must match.
        idx = _index((7, "Ossoff, Jon", "GA"))
        self.assertEqual(match_member("OSSOFF, Jon", "GA", idx), 7)

    def test_plain_order_hyphen_accent_and_suffix(self):
        # curated 'First Last' order, folded accents, hyphenated surnames, and
        # generational suffixes all normalize to the same key
        idx = _index((3, "Nydia Velazquez", "NY"), (4, "Alexandria Ocasio-Cortez", "NY"))
        self.assertEqual(match_member("VELÁZQUEZ, Nydia M.", "NY", idx), 3)
        self.assertEqual(match_member("OCASIO-CORTEZ, Alexandria", "NY", idx), 4)
        self.assertEqual(name_key("Angus King"), name_key("KING, Angus S., Jr."))
        self.assertEqual(name_key("Chris Van Hollen"), name_key("VAN HOLLEN, Chris"))

    def test_state_is_load_bearing(self):
        # same surname + initial in a different state must NOT match
        idx = _index((11, "Smith, Adam", "WA"))
        self.assertIsNone(match_member("SMITH, Adam", "NJ", idx))
        self.assertEqual(match_member("SMITH, Adam", "WA", idx), 11)

    def test_first_initial_is_load_bearing(self):
        idx = _index((21, "Smith, Christopher H.", "NJ"))
        self.assertIsNone(match_member("SMITH, Adam", "NJ", idx))
        self.assertEqual(match_member("SMITH, Chris", "NJ", idx), 21)

    def test_ambiguous_and_unmappable_rows_never_match(self):
        # two candidates sharing surname+initial+state -> skipped, not guessed
        idx = _index((31, "Johnson, Mike", "LA"), (32, "Johnson, Marcus", "LA"))
        self.assertIsNone(match_member("JOHNSON, Mike", "LA", idx))
        # presidents carry state 'USA' in HSall_members.csv -> never matched
        self.assertIsNone(match_member("OBAMA, Barack", "USA", idx))
        # surname-only strings have no usable key
        self.assertIsNone(name_key("Cher"))
        self.assertIsNone(name_key(""))


if __name__ == "__main__":
    unittest.main()
