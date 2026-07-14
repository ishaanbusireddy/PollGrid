"""The factors taxonomy (§09): every qualitative factor the Scorecard tracks,
its grounding, and exactly how it's measured — deterministic formulas wired to
real columns, or LLM-rubric scoring against a fixed scale. Nothing ambiguous
about which. LLM-scored factors default to a neutral 0 when no provider is
reachable: the scorecard degrades to quantitative-only, never blocks, never
guesses."""
from __future__ import annotations

import json
import re

from core import db
from core.util import today

RUBRIC_SCALE = "score in [-1.0, +1.0]; -1 strongly favors REP, +1 strongly favors DEM, 0 neutral/no signal"


def _facts_text(race_id: int, categories: tuple[str, ...], days: int = 30) -> list[dict]:
    cats = ",".join(f"'{c}'" for c in categories)
    return db.query(
        f"SELECT id, summary FROM extracted_facts WHERE race_id=? AND category IN ({cats}) "
        "AND created_at >= datetime('now', ?) ORDER BY created_at DESC LIMIT 40",
        (race_id, f"-{days} days"))


# ---------------- deterministic factor implementations ----------------

def _f_anti_incumbency(race: dict) -> float:
    """Abramowitz-style: president's-party midterm penalty. Midterm cycle +
    incumbent of president's party → negative for that party."""
    midterm = race["cycle_year"] % 4 == 2
    if not midterm:
        return 0.0
    pres_party = db.meta_get("president_party", "")  # set by seed/sync; empty = unknown
    if pres_party not in ("DEM", "REP"):
        return 0.0
    inc = db.query_one("SELECT party_code FROM race_candidates WHERE race_id=? AND is_incumbent=1",
                       (race["id"],))
    penalty = -0.4 if pres_party == "DEM" else 0.4
    if inc and inc["party_code"] == pres_party:
        penalty *= 1.5
    return max(-1.0, min(1.0, penalty))


def _f_realignment(race: dict) -> float:
    if not race["state_fips"]:
        return 0.0
    rows = db.query(
        "SELECT cycle_year, dem_pct - rep_pct AS m FROM political_history WHERE tier='state' AND entity_id=? "
        "AND office='president' AND dem_pct IS NOT NULL ORDER BY cycle_year DESC LIMIT 4",
        (race["state_fips"],))
    if len(rows) < 3:
        return 0.0
    trend = (rows[0]["m"] - rows[-1]["m"]) / max(1, rows[0]["cycle_year"] - rows[-1]["cycle_year"])
    return max(-1.0, min(1.0, trend / 2.0))


def _f_generational(race: dict) -> float:
    """Generational-replacement index: post-Boomer share proxy from median age
    trend (deterministic, from the demographics table)."""
    tier, ent = ("state", race["state_fips"]) if race["state_fips"] else ("nation", "US")
    rows = db.query(
        "SELECT as_of, value FROM demographics WHERE tier=? AND entity_id=? AND variable='median_age' "
        "ORDER BY as_of DESC LIMIT 2", (tier, ent))
    if len(rows) < 2:
        return 0.0
    delta = rows[0]["value"] - rows[1]["value"]  # falling median age → replacement leftward drift
    return max(-1.0, min(1.0, -delta / 2.0))


def _f_coattail(race: dict) -> float:
    from modeling.correlation import _pearson, _movement_series
    if race["race_type"] == "president" or not race["state_fips"]:
        return 0.0
    top = db.query_one("SELECT id FROM races WHERE race_type='president' AND state_fips=? "
                       "ORDER BY cycle_year DESC LIMIT 1", (race["state_fips"],))
    if not top:
        return 0.0
    corr = _pearson(_movement_series(race["id"]), _movement_series(top["id"]))
    return round(corr, 3)


def _f_geography(race: dict) -> float:
    tier, ent = ("state", race["state_fips"]) if race["state_fips"] else ("nation", "US")
    own = db.query_one("SELECT value FROM demographics WHERE tier=? AND entity_id=? "
                       "AND variable='owner_occupied' ORDER BY as_of DESC LIMIT 1", (tier, ent))
    tot = db.query_one("SELECT value FROM demographics WHERE tier=? AND entity_id=? "
                       "AND variable='occupied_units' ORDER BY as_of DESC LIMIT 1", (tier, ent))
    if not own or not tot or not tot["value"]:
        return 0.0
    rate = own["value"] / tot["value"]  # homeownership as an urbanicity proxy; >0.7 rural-lean
    return max(-1.0, min(1.0, (rate - 0.65) * 4 * -1))


def _f_diploma_divide(race: dict) -> float:
    tier, ent = ("state", race["state_fips"]) if race["state_fips"] else ("nation", "US")
    ba = db.query_one("SELECT value FROM demographics WHERE tier=? AND entity_id=? AND variable='bachelors' "
                      "ORDER BY as_of DESC LIMIT 1", (tier, ent))
    pop = db.query_one("SELECT value FROM demographics WHERE tier=? AND entity_id=? AND variable='pop_25plus' "
                       "ORDER BY as_of DESC LIMIT 1", (tier, ent))
    if not ba or not pop or not pop["value"]:
        return 0.0
    share = ba["value"] / pop["value"]
    return max(-1.0, min(1.0, (share - 0.32) * 5))


def _f_ticket_splitting(race: dict) -> float:
    if not race["state_fips"]:
        return 0.0
    rows = db.query(
        "SELECT office, winner_party FROM political_history WHERE tier='state' AND entity_id=? "
        "AND cycle_year=(SELECT MAX(cycle_year) FROM political_history WHERE tier='state' AND entity_id=? "
        "AND office='president')", (race["state_fips"], race["state_fips"]))
    parties = {r["winner_party"] for r in rows if r["winner_party"]}
    return 0.5 if len(parties) > 1 else 0.0  # magnitude only: split-ticket propensity exists


def _f_market(race: dict) -> float:
    from ingestion.markets import market_snapshot
    name = (race["name"] or "").lower()
    for m in market_snapshot():
        title = (m.get("title") or "").lower()
        if any(w in title for w in name.split()[:3]) and m.get("yes_bid") is not None:
            return max(-1.0, min(1.0, (m["yes_bid"] / 100.0 - 0.5) * 2))
    return 0.0


def _f_fairness(race: dict) -> float:
    if not race["district_version_id"]:
        return 0.0
    from modeling.redistricting_fairness import for_district
    f = for_district(race["district_version_id"])
    if not f or f.get("efficiency_gap") is None:
        return 0.0
    return max(-1.0, min(1.0, f["efficiency_gap"] * 5))


def _f_third_party(race: dict) -> float:
    from modeling.averaging import latest_average
    avg = latest_average(race["id"])
    if not avg:
        return 0.0
    third = sum(v for k, v in avg["parties"].items() if k not in ("DEM", "REP"))
    return min(1.0, third / 15.0)  # magnitude: spoiler risk


def _f_open_seat(race: dict) -> float:
    inc = db.query_one("SELECT 1 FROM race_candidates WHERE race_id=? AND is_incumbent=1", (race["id"],))
    return 0.6 if inc is None else 0.0  # open seats run more volatile (magnitude flag)


def _f_endorsements(race: dict) -> float:
    facts = _facts_text(race["id"], ("endorsement",), days=60)
    return min(1.0, len(facts) / 10.0)


def _f_turnout_ground_game(race: dict) -> float:
    facts = _facts_text(race["id"], ("campaign_event",), days=30)
    text = " ".join(f["summary"].lower() for f in facts)
    hits = len(re.findall(r"field office|gotv|door.?knock|early vote|canvass", text))
    return min(1.0, hits / 5.0)


# taxonomy: key -> (name, family, grounding, method, impl_or_rubric)
FACTORS: dict[str, dict] = {
    "retrospective_economy": {
        "name": "Retrospective / economic voting (measured)", "family": "retrospective",
        "grounding": "Fiorina's retrospective-voting theory — the real indicator trend (BLS)",
        "method": "deterministic", "impl": lambda race: _f_economic(race)},
    "retrospective_sentiment": {
        "name": "Retrospective / economic voting (perceived)", "family": "retrospective",
        "grounding": "Fiorina — perceived economy, which can diverge from the raw indicator",
        "method": "llm_rubric",
        "rubric": "From the cited facts only: does perceived-economy sentiment in this race's coverage "
                  "favor the incumbent party (score toward its side) or punish it? " + RUBRIC_SCALE},
    "anti_incumbency": {
        "name": "Anti-incumbency / time-for-change", "family": "structural",
        "grounding": "Abramowitz structural midterm models; six-year itch", "method": "deterministic",
        "impl": _f_anti_incumbency},
    "generational_replacement": {
        "name": "Generational cohorts & replacement", "family": "demographic",
        "grounding": "Mannheim political generations; generational replacement", "method": "deterministic",
        "impl": _f_generational},
    "realignment": {
        "name": "Realignment / secular partisan change", "family": "structural",
        "grounding": "V.O. Key critical elections; Sundquist realignment", "method": "deterministic",
        "impl": _f_realignment},
    "valence_quality": {
        "name": "Valence & candidate quality", "family": "candidate",
        "grounding": "Stokes valence politics", "method": "llm_rubric",
        "rubric": "From the cited facts only: which side's candidate carries the stronger "
                  "competence/authenticity narrative? " + RUBRIC_SCALE},
    "coattails": {
        "name": "Coattail / reverse-coattail effects", "family": "structural",
        "grounding": "Down-ballot correlation with top of ticket", "method": "deterministic",
        "impl": _f_coattail},
    "factional_center": {
        "name": "Intra-party ideological factions", "family": "candidate",
        "grounding": "Factional politics, primary vs general electorates", "method": "llm_rubric",
        "rubric": "From the cited facts only: does either party show a damaging primary/faction split "
                  "in this race? Score toward the more unified side. " + RUBRIC_SCALE},
    "political_geography": {
        "name": "Political geography / urban-rural", "family": "demographic",
        "grounding": "Elazar political culture; Bishop's Big Sort", "method": "deterministic",
        "impl": _f_geography},
    "diploma_divide": {
        "name": "Socioeconomic / diploma divide", "family": "demographic",
        "grounding": "Educational realignment research", "method": "deterministic",
        "impl": _f_diploma_divide},
    "scandal": {
        "name": "Scandal typology & survival base rates", "family": "events",
        "grounding": "Scandal-typology and political-survival research", "method": "llm_rubric",
        "rubric": "From the cited facts only: classify any active scandal (type, severity 0-3, which side) "
                  "then score against the harmed side proportional to severity. " + RUBRIC_SCALE},
    "ticket_splitting": {
        "name": "Down-ballot / ticket-splitting propensity", "family": "structural",
        "grounding": "Split-ticket voting research", "method": "deterministic",
        "impl": _f_ticket_splitting},
    "ground_game": {
        "name": "Turnout / ground-game quality", "family": "campaign",
        "grounding": "Mobilization and GOTV research", "method": "deterministic",
        "impl": _f_turnout_ground_game},
    "policy_success": {
        "name": "Domestic policy success/failure", "family": "events",
        "grounding": "Performance voting applied to legislative outcomes", "method": "llm_rubric",
        "rubric": "From the cited facts only: do legislative outcomes tied to this race read as wins or "
                  "failures, and for which side? " + RUBRIC_SCALE},
    "foreign_policy": {
        "name": "Foreign policy impact", "family": "events",
        "grounding": "Rally-round-the-flag and war-fatigue research", "method": "llm_rubric",
        "rubric": "From the cited facts only: any crisis salience, and which side does the approval "
                  "direction favor? Typically short-lived — score conservatively. " + RUBRIC_SCALE},
    "market_signal": {
        "name": "Financial/market impact", "family": "signals",
        "grounding": "Markets as a political-signal channel", "method": "deterministic",
        "impl": _f_market},
    "media_environment": {
        "name": "Tech/media-environment impact", "family": "signals",
        "grounding": "Algorithmic amplification and virality research", "method": "llm_rubric",
        "rubric": "From the cited facts only: is either side's narrative going disproportionately viral "
                  "(flag only, never suppress)? " + RUBRIC_SCALE},
    "redistricting": {
        "name": "Redistricting/competitiveness", "family": "structural",
        "grounding": "Gerrymander-competitiveness research", "method": "deterministic",
        "impl": _f_fairness},
    "third_party": {
        "name": "Third-party / spoiler dynamics", "family": "structural",
        "grounding": "Spoiler-effect research in close races", "method": "deterministic",
        "impl": _f_third_party},
    "open_seat": {
        "name": "Open-seat vs incumbent-defended", "family": "structural",
        "grounding": "Open-seat competitiveness research", "method": "deterministic",
        "impl": _f_open_seat},
    "endorsement_cascade": {
        "name": "Endorsement cascades", "family": "campaign",
        "grounding": "Endorsement-effect research; backtest decides which types matter",
        "method": "deterministic", "impl": _f_endorsements},
}


def _f_economic(race: dict) -> float:
    """The measured half of retrospective voting: the BLS-derived, president-
    party-oriented index maintained by ingestion/economics.py."""
    row = db.query_one("SELECT value FROM app_meta WHERE key='economic_index'")
    return max(-1.0, min(1.0, float(row["value"]))) if row else 0.0


def _f_ethnic_composition(race: dict) -> float:
    """Deterministic composition (race/ethnicity share) — magnitude only, per
    the manual's explicit non-monolithic caution: never assumes a bloc's vote."""
    tier, ent = ("state", race["state_fips"]) if race["state_fips"] else ("nation", "US")
    white = db.query_one("SELECT value FROM demographics WHERE tier=? AND entity_id=? "
                         "AND variable='white_nh' ORDER BY is_synthetic, as_of DESC LIMIT 1", (tier, ent))
    total = db.query_one("SELECT value FROM demographics WHERE tier=? AND entity_id=? "
                         "AND variable='total_population' ORDER BY is_synthetic, as_of DESC LIMIT 1", (tier, ent))
    if not white or not total or not total["value"]:
        return 0.0
    nonwhite_share = 1.0 - white["value"] / total["value"]
    return min(1.0, max(0.0, nonwhite_share))  # salience magnitude, not direction


def _f_weather(race: dict) -> float:
    """Weather-and-turnout: deterministic NOAA correlation per the manual —
    returns neutral 0 until the NOAA historical import lands (honest absence,
    never a guess). The factor exists so the taxonomy and the ensemble's
    feature vector match the manual's full table."""
    row = db.query_one("SELECT value FROM app_meta WHERE key=?",
                       (f"weather_turnout:{race['state_fips'] or 'US'}",))
    return max(-1.0, min(1.0, float(row["value"]))) if row else 0.0


FACTORS.update({
    "religious_blocs": {
        "name": "Religious/denominational blocs", "family": "demographic",
        "grounding": "Denominational voting-bloc research (explicitly non-monolithic; "
                     "Pew Religious Landscape is the composition source once imported)",
        "method": "llm_rubric",
        "rubric": "From the cited facts only: is religious-community salience a live factor in this "
                  "race's coverage, and which side does it favor? Narrative salience only — never "
                  "assume a bloc votes as one. " + RUBRIC_SCALE},
    "ethnic_community": {
        "name": "Ethnic/community voting patterns", "family": "demographic",
        "grounding": "Voting-bloc research with the explicit non-monolithic caution",
        "method": "deterministic", "impl": _f_ethnic_composition},
    "charisma": {
        "name": "Candidate charisma / personality", "family": "candidate",
        "grounding": "Valence-politics candidate-quality research", "method": "llm_rubric",
        "rubric": "From the cited facts only: score tone/authenticity as portrayed in debate and "
                  "interview coverage — which candidate's personal appeal reads stronger? " + RUBRIC_SCALE},
    "weather_turnout": {
        "name": "Weather-and-turnout effects", "family": "events",
        "grounding": "Documented election-day weather/turnout correlation (NOAA historical data)",
        "method": "deterministic", "impl": _f_weather},
    "rhetoric_factcheck": {
        "name": "Rhetoric accuracy / fact-check exposure", "family": "signals",
        "grounding": "Misinformation-correction research: repeated documented falsehoods carry "
                     "late-cycle correction risk (addendum §7 — scored from cited rhetoric facts only)",
        "method": "llm_rubric",
        "rubric": "From the cited rhetoric/debate facts only: is either candidate's rhetoric drawing "
                  "documented fact-check contradictions IN THE CITED FACTS (never your own knowledge)? "
                  "Score toward the side with the cleaner record; 0 if no fact-check signal is cited. "
                  + RUBRIC_SCALE},
})


def score_race(race_id: int, as_of: str | None = None) -> list[dict]:
    """Re-score the full vector for one race. Deterministic factors always run;
    LLM-rubric factors run one call each against cited facts, cached until new
    facts land, neutral-0 when no provider. Every score is a data row until the
    deterministic ensemble touches it."""
    as_of = as_of or today()
    race = db.query_one("SELECT * FROM races WHERE id=?", (race_id,))
    if race is None:
        return []
    out = []
    try:
        from analyst.llm import complete_json, provider_available
        llm_ok = provider_available()
    except Exception:
        complete_json, llm_ok = None, False
    for key, spec in FACTORS.items():
        if spec["method"] == "deterministic":
            score = float(spec["impl"](race))
            method, rationale, cites = "deterministic", spec["grounding"], None
        else:
            facts = _facts_text(race_id, ("polling", "finance", "legislation", "endorsement",
                                          "scandal", "debate", "rhetoric", "campaign_event"))
            cached = db.query_one(
                "SELECT * FROM qualitative_factor_scores WHERE race_id=? AND factor_key=? "
                "ORDER BY as_of DESC, id DESC LIMIT 1", (race_id, key))
            newest_fact = facts[0]["id"] if facts else None
            # Cache validity keys on the newest fact that EXISTED at last scoring
            # (scored_against_fact_id), not on the LLM's cherry-picked citation set —
            # so an unchanged fact set is a real cache hit even when the model cited a
            # subset. A prior good (non-neutral) row is reused whenever no newer fact
            # has landed, which ALSO means a transient provider outage can't overwrite
            # it with a neutral 0.
            prior_ok = cached is not None and cached["method"] != "neutral_fallback"
            fresh = prior_ok and (newest_fact is None or (
                cached["scored_against_fact_id"] is not None
                and newest_fact <= cached["scored_against_fact_id"]))
            if fresh:
                out.append(dict(cached))
                continue
            if llm_ok and facts and complete_json:
                res = complete_json(
                    f"Score ONE factor for a US election race against a fixed rubric.\n"
                    f"Factor: {spec['name']} — {spec['rubric']}\n"
                    f"Cited facts (use nothing else): "
                    f"{[{'id': f['id'], 'summary': f['summary'][:200]} for f in facts[:15]]!r}\n"
                    'Return JSON {"score": float, "rationale": "one sentence", "fact_ids": [int,...]}.',
                    purpose="factor_scorecard")
                if res and isinstance(res.get("score"), (int, float)):
                    score = max(-1.0, min(1.0, float(res["score"])))
                    method = "llm_rubric"
                    rationale = str(res.get("rationale", ""))[:400]
                    cites = json.dumps([i for i in res.get("fact_ids", []) if isinstance(i, int)])
                else:
                    score, method, rationale, cites = 0.0, "neutral_fallback", "malformed LLM output", None
            else:
                score, method, rationale, cites = 0.0, "neutral_fallback", "no LLM provider reachable", None
            # A provider outage/malformed reply must not discard a still-valid prior
            # score: carry the last non-neutral row forward rather than writing a 0.
            if method == "neutral_fallback" and prior_ok:
                out.append(dict(cached))
                continue
        scored_against = newest_fact if spec["method"] != "deterministic" else None
        row_id = db.execute(
            "INSERT INTO qualitative_factor_scores(race_id,factor_key,as_of,score,method,citation_fact_ids,"
            "rationale,scored_against_fact_id) VALUES(?,?,?,?,?,?,?,?)",
            (race_id, key, as_of, round(score, 4), method, cites, rationale, scored_against))
        out.append({"id": row_id, "race_id": race_id, "factor_key": key, "as_of": as_of,
                    "score": round(score, 4), "method": method, "citation_fact_ids": cites,
                    "rationale": rationale})
    return out


def latest_vector(race_id: int, as_of: str | None = None) -> dict[str, float]:
    """Factor vector as of a date (default: newest). Passing as_of is what keeps
    the ensemble refit honest — it reconstructs each graded prediction's features
    from the snapshot that existed on the prediction's own as_of, never today's."""
    vec = {}
    bound = "AND as_of <= ? " if as_of else ""
    for key in FACTORS:
        params = (race_id, key, as_of) if as_of else (race_id, key)
        row = db.query_one(
            "SELECT score FROM qualitative_factor_scores WHERE race_id=? AND factor_key=? "
            + bound + "ORDER BY as_of DESC, id DESC LIMIT 1", params)
        vec[key] = row["score"] if row else 0.0
    return vec
