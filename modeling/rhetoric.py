"""Rhetoric & framing: a FIXED policy-topic taxonomy, stance per topic
(rubric-constrained LLM with a deterministic keyword fallback the pipeline
actually exercises), and a media-framing matrix comparing how differently-
leaning outlets cover the identical story."""
from __future__ import annotations

import re

from core import db
from core.util import today

TOPICS: dict[str, dict[str, list[str]]] = {
    # topic -> direction keyword sets; stance = (pro - anti) / total, deterministic fallback
    "economy": {"pro": ["tax cut", "deregulat", "growth"], "anti": ["tax the rich", "minimum wage", "union"]},
    "healthcare": {"pro": ["repeal", "private insurance"], "anti": ["medicare for all", "public option", "expand coverage"]},
    "immigration": {"pro": ["border security", "deport", "wall"], "anti": ["pathway to citizenship", "dreamers", "asylum"]},
    "abortion": {"pro": ["pro-life", "heartbeat"], "anti": ["pro-choice", "reproductive rights", "roe"]},
    "guns": {"pro": ["second amendment", "gun rights"], "anti": ["gun control", "background check", "assault weapons ban"]},
    "climate": {"pro": ["drill", "fossil", "energy independence"], "anti": ["clean energy", "climate crisis", "emissions"]},
    "democracy": {"pro": ["election integrity", "voter id"], "anti": ["voting rights", "gerrymander", "suppression"]},
}


def score_stances(candidate_id: int) -> int:
    """Deterministic keyword stance per topic from the candidate's ingested
    rhetoric facts (stance in [-1,1], >0 = conventionally-right direction).
    An LLM rubric pass may refine these rows later; method column says which."""
    facts = db.query(
        "SELECT f.summary FROM extracted_facts f JOIN article_entity_links l "
        "ON l.raw_item_id=f.raw_item_id AND l.entity_type='candidate' AND l.entity_id=? "
        "WHERE f.category IN ('rhetoric','debate','campaign_event') LIMIT 300", (candidate_id,))
    text = " ".join(f["summary"].lower() for f in facts)
    n = 0
    for topic, kws in TOPICS.items():
        pro = sum(len(re.findall(re.escape(k), text)) for k in kws["pro"])
        anti = sum(len(re.findall(re.escape(k), text)) for k in kws["anti"])
        if pro + anti == 0:
            continue
        stance = (pro - anti) / (pro + anti)
        db.execute("INSERT INTO topic_stance_scores(candidate_id,topic,stance,method,as_of) VALUES(?,?,?,?,?)",
                   (candidate_id, topic, round(stance, 3), "deterministic", today()))
        n += 1
    return n


def framing_matrix(race_id: int) -> list[dict]:
    """How differently-leaning outlets frame the same race's stories: share of
    each outlet's coverage per category, deterministic."""
    rows = db.query(
        """SELECT mo.name outlet, mo.leaning, f.category, COUNT(*) n
           FROM extracted_facts f
           JOIN raw_items ri ON ri.id=f.raw_item_id
           JOIN sources s ON s.id=ri.source_id
           JOIN media_outlets mo ON mo.name = COALESCE(json_extract(s.config_json,'$.outlet'), s.name)
           WHERE f.race_id=? GROUP BY mo.name, mo.leaning, f.category""", (race_id,))
    totals: dict[str, int] = {}
    for r in rows:
        totals[r["outlet"]] = totals.get(r["outlet"], 0) + r["n"]
    return [{"outlet": r["outlet"], "leaning": r["leaning"], "topic": r["category"],
             "framing": round(r["n"] / totals[r["outlet"]], 3)} for r in rows]
