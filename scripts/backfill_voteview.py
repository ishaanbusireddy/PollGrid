#!/usr/bin/env python3
"""VoteView DW-NOMINATE backfill — roll-call-derived ideology for candidates
who have served in Congress. Streams HSall_members.csv (~40 MB; congress >= 110
kept), matches each member to a candidates row by normalized surname +
first-initial + state, stores app_meta voteview_dim1:{candidate_id} = the
member's first-dimension NOMINATE score, and recomputes modeling.ideology so
the roll-call score replaces the party-baseline proxy.

Matching is deliberately conservative: an ambiguous key (two candidates sharing
surname + initial + state) is skipped rather than guessed. VoteView members are
public officials; NOMINATE scores are deterministic — no LLM anywhere near this
path (manual §11).

Usage:
  python scripts/backfill_voteview.py                 # download from voteview.com
  python scripts/backfill_voteview.py --file HSall_members.csv
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
import unicodedata
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VOTEVIEW_URL = "https://voteview.com/static/data/out/members/HSall_members.csv"
MIN_CONGRESS = 110  # 2007 onward — anything older predates plausible 2026+ candidacies

_PARENS = re.compile(r"\([^)]*\)")
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "md", "phd", "esq"}
# surname particles kept attached when parsing "First Van Hollen"-style names
_PARTICLES = {"van", "von", "de", "del", "della", "der", "di", "da", "la", "le",
              "los", "st", "ste", "mc", "mac"}


def _fold(text: str) -> str:
    """Lowercase, accents stripped (VELÁZQUEZ → velazquez), periods dropped."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.replace(".", " ").lower()


def _surname_norm(surname: str) -> str:
    """Alpha-only surname: 'Ocasio-Cortez' / 'OCASIO CORTEZ' → 'ocasiocortez'."""
    return re.sub(r"[^a-z]", "", surname)


def name_key(name: str) -> tuple[str, str] | None:
    """(normalized_surname, first_initial) from either name order:
    VoteView bionames ('SMITH, John A., Jr.') and FEC/curated candidate names
    ('Smith, John A.' or 'John A. Smith') hash to the same key. Parenthesized
    nicknames and generational suffixes are ignored. None when no given name
    can be found (initials are load-bearing — never wildcard them)."""
    if not name:
        return None
    folded = _fold(_PARENS.sub(" ", name))
    if "," in folded:
        surname_part, _, given_part = folded.partition(",")
        given_tokens = [t for t in re.split(r"[\s,]+", given_part)
                        if t and t not in _SUFFIXES]
        surname = _surname_norm(surname_part)
    else:
        tokens = [t for t in re.split(r"\s+", folded) if t]
        while tokens and tokens[-1] in _SUFFIXES:
            tokens.pop()
        if len(tokens) < 2:
            return None
        surname_tokens = [tokens.pop()]
        while len(tokens) > 1 and tokens[-1] in _PARTICLES:
            surname_tokens.insert(0, tokens.pop())
        surname = _surname_norm("".join(surname_tokens))
        given_tokens = tokens
    if not surname or not given_tokens or not given_tokens[0][:1].isalpha():
        return None
    return surname, given_tokens[0][0]


def match_member(bioname: str, state_abbrev: str,
                 index: dict[tuple[str, str, str], list[int]]) -> int | None:
    """Candidate id for a VoteView member row, or None. index maps
    (surname, first_initial, state_fips) -> [candidate_id, ...]; a key with
    more than one candidate is ambiguous and never matched."""
    from domain.geography import USPS_TO_FIPS
    state_fips = USPS_TO_FIPS.get((state_abbrev or "").strip().upper())
    if not state_fips:
        return None  # POTUS rows carry 'USA'; territories without candidates skip too
    key = name_key(bioname)
    if not key:
        return None
    hits = index.get((key[0], key[1], state_fips), [])
    return hits[0] if len(hits) == 1 else None


def build_candidate_index() -> dict[tuple[str, str, str], list[int]]:
    from core import db
    index: dict[tuple[str, str, str], list[int]] = {}
    for row in db.query("SELECT id, name, state_fips FROM candidates "
                        "WHERE is_synthetic=0 AND state_fips IS NOT NULL"):
        key = name_key(row["name"])
        if key:
            index.setdefault((key[0], key[1], row["state_fips"]), []).append(row["id"])
    return index


def _member_rows(path: str | None):
    """Stream csv.DictReader rows without holding the 40 MB file in memory."""
    if path:
        with open(path, encoding="utf-8", newline="") as fh:
            yield from csv.DictReader(fh)
    else:
        req = urllib.request.Request(VOTEVIEW_URL, headers={"User-Agent": "PollGrid/1.0"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            yield from csv.DictReader(io.TextIOWrapper(resp, encoding="utf-8", newline=""))


def run(path: str | None = None) -> dict:
    from core import db
    from core.util import today
    from modeling import ideology

    index = build_candidate_index()
    matched: dict[int, float] = {}  # candidate_id -> dim1 (later congress wins)
    n_members = n_unmatched = 0
    for row in _member_rows(path):
        try:
            if int(row.get("congress") or 0) < MIN_CONGRESS:
                continue
        except ValueError:
            continue
        if (row.get("chamber") or "") not in ("House", "Senate"):
            continue
        raw_dim1 = (row.get("nominate_dim1") or "").strip()
        if not raw_dim1:
            continue
        try:
            dim1 = float(raw_dim1)
        except ValueError:
            continue
        n_members += 1
        cid = match_member(row.get("bioname") or "", row.get("state_abbrev") or "", index)
        if cid is None:
            n_unmatched += 1
        else:
            matched[cid] = dim1  # rows arrive congress-ascending; latest score wins

    for cid, dim1 in sorted(matched.items()):
        db.meta_set(f"voteview_dim1:{cid}", str(dim1))
        # today's proxy snapshot (if any) yields to the roll-call score
        db.execute("DELETE FROM ideology_scores WHERE candidate_id=? AND as_of=?",
                   (cid, today()))
        ideology.compute(cid)

    return {"member_rows": n_members, "matched_candidates": len(matched),
            "unmatched_member_rows": n_unmatched}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None,
                    help="local HSall_members.csv (default: download from voteview.com)")
    args = ap.parse_args()
    from core import db
    db.migrate()
    stats = run(args.file)
    print(f"voteview member-rows considered (congress >= {MIN_CONGRESS}): {stats['member_rows']}")
    print(f"matched candidates (dim1 stored + ideology recomputed): {stats['matched_candidates']}")
    print(f"unmatched member rows: {stats['unmatched_member_rows']}")


if __name__ == "__main__":
    main()
