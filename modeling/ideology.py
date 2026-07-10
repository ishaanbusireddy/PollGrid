"""Deterministic ideology proxy: roll-call-derived score where VoteView data is
imported, party-baseline + donor-pattern adjustments otherwise. Scored against
a fixed rubric; never an LLM's guess at where someone sits on a spectrum."""
from __future__ import annotations

import json

from core import db
from core.util import today

PARTY_BASELINE = {"DEM": -0.35, "REP": 0.35, "LIB": 0.25, "GRN": -0.45, "IND": 0.0, "OTH": 0.0}


def compute(candidate_id: int, as_of: str | None = None) -> dict | None:
    as_of = as_of or today()
    c = db.query_one("SELECT * FROM candidates WHERE id=?", (candidate_id,))
    if c is None:
        return None
    components: dict[str, float] = {}
    vv = db.query_one("SELECT value FROM app_meta WHERE key=?", (f"voteview_dim1:{candidate_id}",))
    if vv:  # DW-NOMINATE first dimension, imported by scripts/backfill_voteview.py
        components["dw_nominate_dim1"] = float(vv["value"])
        score = components["dw_nominate_dim1"]
        source = "voteview:dw_nominate"
    else:
        base = PARTY_BASELINE.get(c["party_code"] or "OTH", 0.0)
        components["party_baseline"] = base
        stances = db.query("SELECT stance FROM topic_stance_scores WHERE candidate_id=? AND method='deterministic'",
                           (candidate_id,))
        if stances:
            adj = sum(s["stance"] for s in stances) / len(stances) * 0.2
            components["stance_adjustment"] = round(adj, 3)
            base += adj
        score = max(-1.0, min(1.0, base))
        source = "proxy:party_baseline+stances"
    db.execute("INSERT OR IGNORE INTO ideology_scores(candidate_id,as_of,score,components_json,source) "
               "VALUES(?,?,?,?,?)", (candidate_id, as_of, round(score, 4), json.dumps(components), source))
    return {"candidate_id": candidate_id, "score": round(score, 4), "components": components, "source": source}


def latest(candidate_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM ideology_scores WHERE candidate_id=? ORDER BY as_of DESC LIMIT 1",
                       (candidate_id,))
    if row is None:
        return None
    return {"candidate_id": candidate_id, "score": row["score"],
            "components": json.loads(row["components_json"]), "source": row["source"]}
