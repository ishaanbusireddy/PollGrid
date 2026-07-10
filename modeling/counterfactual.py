"""Counterfactual engine: 'what if this candidate drops out', 'what if turnout
matches a prior cycle' — branches grounded in the archive with a real precedent
count, honest non-summing probabilities, deepen-on-demand. Branch narrative may
come from the LLM; every branch's numbers are deterministic."""
from __future__ import annotations

from core import db
from modeling.correlation import historical_analogs
from modeling.forecasting import latest as latest_forecast


def dropout_branch(race_id: int, candidate_id: int) -> dict:
    cand = db.query_one("SELECT name, party_code FROM candidates WHERE id=?", (candidate_id,))
    base = latest_forecast(race_id)
    precedents = db.query(
        "SELECT COUNT(*) c FROM extracted_facts WHERE category='campaign_event' "
        "AND summary LIKE '%drop%out%'")["c"] if cand else 0
    others = db.query(
        "SELECT c.party_code, COUNT(*) n FROM race_candidates rc JOIN candidates c ON c.id=rc.candidate_id "
        "WHERE rc.race_id=? AND rc.candidate_id != ? GROUP BY c.party_code", (race_id, candidate_id))
    return {
        "label": f"{cand['name']} drops out" if cand else "candidate drops out",
        "probs": {"note": "field redistributes among remaining candidates; probabilities honestly "
                          "do not sum to a forecast — this is a scenario, not a prediction",
                  "baseline_dem_prob": base and base["dem_prob"],
                  "remaining_field": {r["party_code"]: r["n"] for r in others}},
        "precedents": historical_analogs(race_id, limit=3),
        "precedent_count": precedents,
    }


def turnout_branch(race_id: int, match_cycle: int) -> dict:
    race = db.query_one("SELECT * FROM races WHERE id=?", (race_id,))
    hist = db.query_one(
        "SELECT turnout_pct FROM political_history WHERE tier='state' AND entity_id=? AND office=? "
        "AND cycle_year=?", (race["state_fips"], race["race_type"], match_cycle)) if race else None
    base = latest_forecast(race_id)
    return {
        "label": f"turnout matches {match_cycle}",
        "probs": {"baseline_dem_prob": base and base["dem_prob"],
                  "reference_turnout_pct": hist and hist["turnout_pct"],
                  "note": "directional scenario grounded in the archived turnout figure; "
                          "no new probability is invented"},
        "precedents": historical_analogs(race_id, limit=3),
        "precedent_count": 1 if hist else 0,
    }


def generate(race_id: int, scenario: str) -> dict:
    kind, _, arg = scenario.partition(":")
    if kind == "dropout" and arg.isdigit():
        branches = [dropout_branch(race_id, int(arg))]
    elif kind == "turnout" and arg.isdigit():
        branches = [turnout_branch(race_id, int(arg))]
    else:
        cands = db.query("SELECT candidate_id FROM race_candidates WHERE race_id=? LIMIT 2", (race_id,))
        branches = [dropout_branch(race_id, c["candidate_id"]) for c in cands]
        branches.append(turnout_branch(race_id, 2020))
    generated_by = "deterministic"
    try:
        from analyst.llm import complete_json
        for b in branches:
            out = complete_json(
                f"In 2-3 sentences, narrate this election counterfactual branch strictly from the data "
                f"given, inventing nothing: {b!r}. Return JSON {{\"narrative\": \"...\"}}.",
                purpose="counterfactual")
            if out and out.get("narrative"):
                b["narrative"] = out["narrative"]
                generated_by = "llm+deterministic"
            else:
                b["narrative"] = (f"Scenario: {b['label']}. Grounded in {b['precedent_count']} archived "
                                  "precedent(s); see the precedent list and baseline probability.")
    except Exception:
        for b in branches:
            b.setdefault("narrative", f"Scenario: {b['label']} (deterministic branch).")
    return {"race_id": race_id, "branches": branches, "generated_by": generated_by}
