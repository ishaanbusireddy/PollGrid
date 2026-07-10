"""Every REST route from docs/API_CONTRACT.md. Handlers are thin: deterministic
reads over the modeling/domain layers; every read route honors ?as_of."""
from __future__ import annotations

import csv
import io
import json

from core import db, provenance
from core.config import CONFIG
from core.util import today
from api.router import route


def _as_of(req) -> str:
    return req.query.get("as_of") or today()


# ------------------------------ status & meta ------------------------------

@route("GET", "/api/status")
def status(req):
    sources = db.query("SELECT id,name,source_type,health,last_run_at,last_error,is_active FROM sources")
    counts = {t: db.query_one(f"SELECT COUNT(*) c FROM {t}")["c"]
              for t in ("races", "polls", "candidates", "stories")}
    counts["facts"] = db.query_one("SELECT COUNT(*) c FROM extracted_facts")["c"]
    return {"sources": sources, "counts": counts,
            "election_night_mode": db.meta_get("election_night_mode") == "1"}


@route("GET", "/api/config")
def config_mirror(req):
    return CONFIG


@route("GET", "/api/diagnostics")
def diagnostics(req):
    from analyst.llm import current_provider
    from core.db import DB_PATH, run_integrity_checks
    synth = sum(db.query_one(f"SELECT COUNT(*) c FROM {t} WHERE is_synthetic=1")["c"]
                for t in ("polls", "raw_items", "extracted_facts", "demographics", "races",
                          "results_live", "stories", "political_history"))
    return {"llm": current_provider(),
            "chains": [{"table": t, "ok": ok, "detail": d} for t, ok, d in provenance.verify_all()],
            "integrity": run_integrity_checks(), "db_path": DB_PATH, "synthetic_rows": synth}


# ------------------------------ geography ------------------------------

@route("GET", "/api/geo/states")
def geo_states(req):
    return db.query(
        """SELECT s.fips_code, s.usps_code, s.name, s.is_territory,
                  a.electoral_votes, a.elector_method
           FROM states s LEFT JOIN electoral_vote_allocations a
             ON a.state_fips = s.fips_code AND a.cycle_to IS NULL ORDER BY s.name""")


@route("GET", "/api/geo/counties")
def geo_counties(req):
    st = req.query.get("state")
    where = "AND state_fips=?" if st else ""
    return db.query(f"SELECT geoid,name,type,state_fips FROM county_equivalents "
                    f"WHERE effective_to IS NULL {where} ORDER BY name", (st,) if st else ())


@route("GET", "/api/geo/districts")
def geo_districts(req):
    st = req.query.get("state")
    where = "AND state_fips=?" if st else ""
    return db.query(f"SELECT district_version_id,geoid,district_number,is_voting,congress_number,state_fips "
                    f"FROM congressional_districts WHERE effective_to IS NULL {where} "
                    "ORDER BY state_fips, district_number", (st,) if st else ())


@route("GET", "/api/demographics/{tier}/{entity_id}")
def demographics(req, tier, entity_id):
    # demographics as_of is a source-vintage tag (acs5_2023, demo_2026), not a date:
    # serve the latest vintage per variable; the vintage itself is in each row.
    rows = db.query(
        "SELECT category,variable,value,confidence,source,MAX(as_of) as_of FROM demographics "
        "WHERE tier=? AND entity_id=? GROUP BY category,variable ORDER BY category,variable",
        (tier, entity_id))
    return {"tier": tier, "entity_id": entity_id, "as_of": _as_of(req), "rows": rows,
            "thin_coverage": len(rows) == 0}


@route("GET", "/api/entities/{tier}/{id}/history")
def entity_history(req, tier, id):
    rows = db.query(
        "SELECT office,seat,cycle_year,winner_party,dem_pct,rep_pct,margin_pct,turnout_pct,confidence "
        "FROM political_history WHERE tier=? AND entity_id=? ORDER BY office, cycle_year DESC", (tier, id))
    events = []
    if tier == "congressional_district":
        d = db.query_one("SELECT state_fips FROM congressional_districts WHERE district_version_id=?", (id,))
        if d:
            events = db.query("SELECT congress_number,effective_from,note FROM redistricting_events "
                              "WHERE state_fips=? ORDER BY effective_from", (d["state_fips"],))
    elif tier == "state":
        events = db.query("SELECT congress_number,effective_from,note FROM redistricting_events "
                          "WHERE state_fips=? ORDER BY effective_from", (id,))
    return {"rows": rows, "boundary_events": events}


# ------------------------------ races ------------------------------

def _race_row(r: dict) -> dict:
    from modeling.averaging import latest_average
    out = dict(r)
    avg = latest_average(r["id"])
    if avg and avg["parties"]:
        ranked = sorted(avg["parties"].items(), key=lambda t: -t[1])
        out["leader_party"] = ranked[0][0]
        out["leader_margin"] = round(ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0), 1)
    else:
        out["leader_party"] = out["leader_margin"] = None
    d = r.get("district_version_id") and db.query_one(
        "SELECT district_number FROM congressional_districts WHERE district_version_id=?",
        (r["district_version_id"],))
    out["district_number"] = d["district_number"] if d else None
    return out


@route("GET", "/api/races")
def races(req):
    clauses, params = ["1=1"], []
    for key, col in (("cycle", "cycle_year"), ("type", "race_type"), ("state", "state_fips"),
                     ("status", "status")):
        if req.query.get(key):
            clauses.append(f"{col}=?")
            params.append(req.query[key])
    if req.query.get("competitive"):
        clauses.append("competitiveness IN ('tossup','lean')")
    rows = db.query(f"SELECT * FROM races WHERE {' AND '.join(clauses)} ORDER BY race_type, name LIMIT 700",
                    params)
    return [_race_row(r) for r in rows]


@route("GET", "/api/races/{id}")
def race_detail(req, id):
    race = db.query_one("SELECT * FROM races WHERE id=?", (id,))
    if race is None:
        return 404, {"error": "race not found"}
    from modeling import corroboration, narrative
    from modeling.averaging import latest_average
    from modeling.forecasting import category_visible, latest as forecast_latest
    from modeling.fundamentals import latest as fund_latest
    from modeling.genius_ensemble import live_model_for
    from modeling.ideology import latest as ideology_latest
    from modeling.volatility import latest as vol_latest
    as_of = _as_of(req)
    cands = db.query(
        "SELECT c.id, c.name, rc.party_code, rc.is_incumbent FROM race_candidates rc "
        "JOIN candidates c ON c.id=rc.candidate_id WHERE rc.race_id=?", (id,))
    for c in cands:
        ide = ideology_latest(c["id"])
        c["ideology_score"] = ide and ide["score"]
    live = live_model_for(race["race_type"])
    vis, reason = category_visible(race["race_type"], "quantitative")
    f = forecast_latest(int(id), live, as_of) or forecast_latest(int(id), "quantitative", as_of)
    return {
        "race": dict(race), "candidates": cands,
        "average": latest_average(int(id), as_of),
        "fundamentals": fund_latest(int(id), as_of),
        "forecast": {"model": live, "dem_prob": f and f["dem_prob"], "rep_prob": f and f["rep_prob"],
                     "metric_id": f and f["metric_id"], "visible": vis, "gate_reason": reason},
        "narrative": narrative.generate(int(id)),
        "corroboration": corroboration.check(int(id)),
        "volatility": vol_latest(f"race:{id}", as_of),
    }


@route("GET", "/api/races/{id}/framing")
def race_framing(req, id):
    from modeling.rhetoric import framing_matrix
    spend = db.query("SELECT sponsor, medium, SUM(amount) amount FROM ad_spend WHERE race_id=? "
                     "GROUP BY sponsor, medium", (id,))
    return {"matrix": framing_matrix(int(id)), "ad_spend": spend}


@route("GET", "/api/factors/{race_id}")
def factors(req, race_id):
    from modeling.factors_taxonomy import FACTORS
    out = []
    for key, spec in FACTORS.items():
        row = db.query_one(
            "SELECT * FROM qualitative_factor_scores WHERE race_id=? AND factor_key=? "
            "ORDER BY as_of DESC, id DESC LIMIT 1", (race_id, key))
        out.append({"key": key, "name": spec["name"], "family": spec["family"],
                    "grounding": spec["grounding"],
                    "method": row["method"] if row else spec["method"],
                    "score": row["score"] if row else None,
                    "rationale": row and row["rationale"],
                    "citations": json.loads(row["citation_fact_ids"]) if row and row["citation_fact_ids"] else []})
    return {"factors": out, "as_of": _as_of(req)}


@route("GET", "/api/forecast/ensemble/{race_id}")
def ensemble(req, race_id):
    from modeling.forecasting import latest as forecast_latest
    from modeling.genius_ensemble import live_model_for
    race = db.query_one("SELECT race_type FROM races WHERE id=?", (race_id,))
    if race is None:
        return 404, {"error": "race not found"}
    q = forecast_latest(int(race_id), "quantitative", _as_of(req))
    e = forecast_latest(int(race_id), "ensemble", _as_of(req))
    bt = db.query_one("SELECT brier_quant,brier_ensemble,n_graded FROM ensemble_backtest_results "
                      "WHERE category=? ORDER BY as_of DESC LIMIT 1", (race["race_type"],))
    return {"quantitative": q and {"dem_prob": q["dem_prob"]}, "ensemble": e and {"dem_prob": e["dem_prob"]},
            "live_model": live_model_for(race["race_type"]), "category": race["race_type"],
            "backtest": bt and dict(bt)}


# ------------------------------ polls ------------------------------

@route("GET", "/api/polls")
def polls(req):
    clauses, params = ["1=1"], []
    if req.query.get("race_id"):
        clauses.append("p.race_id=?"); params.append(req.query["race_id"])
    if req.query.get("race_type"):
        clauses.append("r.race_type=?"); params.append(req.query["race_type"])
    if req.query.get("state"):
        clauses.append("r.state_fips=?"); params.append(req.query["state"])
    if req.query.get("pollster"):
        clauses.append("ps.name LIKE ?"); params.append("%" + req.query["pollster"] + "%")
    if req.query.get("population"):
        clauses.append("p.population=?"); params.append(req.query["population"])
    if req.query.get("from"):
        clauses.append("p.field_end>=?"); params.append(req.query["from"])
    if req.query.get("to"):
        clauses.append("p.field_end<=?"); params.append(req.query["to"])
    clauses.append("p.field_end<=?"); params.append(_as_of(req))
    where = " AND ".join(clauses)
    limit = min(int(req.query.get("limit", 100)), 500)
    offset = int(req.query.get("offset", 0))
    total = db.query_one(f"SELECT COUNT(*) c FROM polls p JOIN races r ON r.id=p.race_id "
                         f"JOIN pollsters ps ON ps.id=p.pollster_id WHERE {where}", params)["c"]
    rows = db.query(
        f"""SELECT p.*, ps.name pollster, r.name race_name FROM polls p
            JOIN races r ON r.id=p.race_id JOIN pollsters ps ON ps.id=p.pollster_id
            WHERE {where} ORDER BY p.field_end DESC LIMIT ? OFFSET ?""", params + [limit, offset])
    out = []
    for p in rows:
        results = {r["party_code"]: r["pct"] for r in
                   db.query("SELECT party_code, pct FROM poll_results WHERE poll_id=?", (p["id"],))}
        grade_row = db.query_one(
            "SELECT grade, house_effect_dem FROM pollster_ratings WHERE pollster_id=? "
            "ORDER BY as_of DESC LIMIT 1", (p["pollster_id"],))
        lean = grade_row["house_effect_dem"] if grade_row else 0.0
        adjusted = {k: round(v - lean, 1) if k == "DEM" else (round(v + lean, 1) if k == "REP" else v)
                    for k, v in results.items()}
        out.append({"id": p["id"], "pollster": p["pollster"],
                    "pollster_grade": grade_row["grade"] if grade_row else "provisional",
                    "race_id": p["race_id"], "race_name": p["race_name"],
                    "field_start": p["field_start"], "field_end": p["field_end"],
                    "sample_size": p["sample_size"], "population": p["population"], "moe": p["moe"],
                    "results": results, "adjusted": adjusted, "release_url": p["release_url"],
                    "is_synthetic": p["is_synthetic"]})
    return {"total": total, "rows": out}


# ------------------------------ candidates & parties ------------------------------

@route("GET", "/api/candidates")
def candidates(req):
    clauses, params = ["1=1"], []
    if req.query.get("query"):
        clauses.append("name LIKE ?"); params.append("%" + req.query["query"] + "%")
    if req.query.get("state"):
        clauses.append("state_fips=?"); params.append(req.query["state"])
    if req.query.get("office"):
        clauses.append("office=?"); params.append(req.query["office"])
    limit = min(int(req.query.get("limit", 50)), 200)
    return db.query(f"SELECT id,name,party_code,state_fips,office,first_cycle,last_cycle,curated "
                    f"FROM candidates WHERE {' AND '.join(clauses)} ORDER BY name LIMIT ?",
                    params + [limit])


@route("GET", "/api/candidates/{id}")
def candidate_detail(req, id):
    c = db.query_one("SELECT * FROM candidates WHERE id=?", (id,))
    if c is None:
        return 404, {"error": "candidate not found"}
    import threading
    from domain.entities import ai_fill_candidate
    if not c["ai_filled_at"]:
        threading.Thread(target=ai_fill_candidate, args=(c["id"],), daemon=True).start()  # never blocks
    from modeling.ideology import latest as ideology_latest
    races_ = db.query("SELECT r.id, r.name, r.cycle_year, r.status, rc.is_incumbent FROM race_candidates rc "
                      "JOIN races r ON r.id=rc.race_id WHERE rc.candidate_id=? ORDER BY r.cycle_year DESC", (id,))
    fin = db.query_one("SELECT total_amount, cycle_year FROM donors_aggregated WHERE candidate_id=? "
                       "AND contributor_name='__totals__' ORDER BY cycle_year DESC LIMIT 1", (id,))
    stances = db.query("SELECT topic, stance, method, as_of FROM topic_stance_scores WHERE candidate_id=? "
                       "ORDER BY as_of DESC LIMIT 20", (id,))
    return {"candidate": dict(c), "races": races_, "ideology": ideology_latest(int(id)),
            "finance": fin and {"total_receipts": fin["total_amount"], "as_of": fin["cycle_year"]},
            "stances": stances}


@route("GET", "/api/parties")
def parties(req):
    return db.query("SELECT * FROM parties ORDER BY id")


@route("GET", "/api/parties/{id}")
def party_detail(req, id):
    p = db.query_one("SELECT * FROM parties WHERE id=? OR code=?", (id, id))
    return (404, {"error": "party not found"}) if p is None else p


# ------------------------------ articles ------------------------------

@route("GET", "/api/articles/{entity_type}/{id}")
def articles(req, entity_type, id):
    limit = min(int(req.query.get("limit", 50)), 200)
    rows = db.query(
        """SELECT ri.id raw_item_id, ri.title, ri.url, ri.published_at,
                  COALESCE(json_extract(s.config_json,'$.outlet'), s.name) outlet, s.reliability_tier
           FROM article_entity_links l JOIN raw_items ri ON ri.id=l.raw_item_id
           JOIN sources s ON s.id=ri.source_id
           WHERE l.entity_type=? AND l.entity_id=? ORDER BY ri.published_at DESC LIMIT ?""",
        (entity_type, id, limit))
    return rows


# ------------------------------ intelligence ------------------------------

@route("GET", "/api/forecast/chamber/{chamber}")
def chamber(req, chamber):
    from modeling.chamber_simulation import latest, run
    out = latest(chamber, _as_of(req)) or run(chamber)
    return out or (404, {"error": "no modeled races for this chamber yet"})


@route("GET", "/api/forecast/scorecard")
def scorecard(req):
    from modeling.forecasting import category_visible
    rows = db.query(
        "SELECT category, model, brier, n_graded, passed FROM backtest_results b "
        "WHERE as_of=(SELECT MAX(as_of) FROM backtest_results WHERE category=b.category AND model=b.model)")
    for r in rows:
        r["live"] = category_visible(r["category"], r["model"])[0]
    cats = {r["race_type"] for r in db.query("SELECT DISTINCT race_type FROM races")}
    for cat in cats - {r["category"] for r in rows}:
        rows.append({"category": cat, "model": "quantitative", "brier": None, "n_graded": 0,
                     "passed": False, "live": False})
    return rows


@route("GET", "/api/pollsters/ratings")
def pollster_ratings_all(req):
    return db.query(
        """SELECT ps.id, ps.name, pr.grade, pr.avg_abs_error, pr.n_graded, pr.house_effect_dem,
                  pr.weight_multiplier
           FROM pollsters ps LEFT JOIN pollster_ratings pr ON pr.pollster_id=ps.id
             AND pr.as_of=(SELECT MAX(as_of) FROM pollster_ratings WHERE pollster_id=ps.id)
           ORDER BY ps.name""")


@route("GET", "/api/pollsters/{id}/rating")
def pollster_rating(req, id):
    ps = db.query_one("SELECT * FROM pollsters WHERE id=?", (id,))
    if ps is None:
        return 404, {"error": "pollster not found"}
    history = db.query("SELECT * FROM pollster_ratings WHERE pollster_id=? ORDER BY as_of DESC", (id,))
    return {"pollster": dict(ps), "current": history[0] if history else None, "history": history}


@route("GET", "/api/districts/{id}/fairness")
def fairness(req, id):
    from modeling.redistricting_fairness import for_district
    out = for_district(int(id))
    return out or (404, {"error": "district not found"})


@route("GET", "/api/audit/{metric_id}")
def audit(req, metric_id):
    row = db.query_one("SELECT * FROM computation_audit_log WHERE metric_id=?", (metric_id,))
    if row is None:
        return 404, {"error": "metric not found"}
    return {"metric_id": row["metric_id"], "metric_type": row["metric_type"], "scope": row["scope"],
            "formula": row["formula"], "inputs": json.loads(row["inputs_json"]),
            "output": json.loads(row["output_json"]), "created_at": row["created_at"]}


@route("GET", "/api/counterfactual")
def counterfactual(req):
    from modeling.counterfactual import generate
    race_id = req.query.get("race_id")
    if not race_id:
        return 400, {"error": "race_id required"}
    return generate(int(race_id), req.query.get("scenario", ""))


@route("GET", "/api/volatility")
def volatility(req):
    from modeling.volatility import compute, latest
    scope = req.query.get("scope", "national")
    return latest(scope, _as_of(req)) or compute(scope)


@route("GET", "/api/stories")
def stories(req):
    since = req.query.get("since", "1970-01-01")
    rows = db.query("SELECT * FROM stories WHERE updated_at>? ORDER BY updated_at DESC LIMIT 50", (since,))
    for s in rows:
        s["facts"] = db.query(
            "SELECT f.id, f.summary, f.category FROM story_facts sf JOIN extracted_facts f ON f.id=sf.fact_id "
            "WHERE sf.story_id=? ORDER BY f.id DESC LIMIT 10", (s["id"],))
    return rows


# ------------------------------ analyst ------------------------------

@route("POST", "/api/analyst/query")
def analyst_query(req):
    body = req.json or {}
    for k in ("entity_type", "entity_id", "question"):
        if not body.get(k):
            return 400, {"error": f"{k} required"}
    from analyst.engine import query as analyst
    return analyst(body["entity_type"], str(body["entity_id"]), body["question"], body.get("session_id"))


# ------------------------------ election night ------------------------------

@route("GET", "/api/electionnight/live")
def en_live(req):
    where, params = "", []
    if req.query.get("race_id"):
        where = "AND r.id=?"
        params.append(req.query["race_id"])
    races_ = db.query(f"SELECT r.* FROM races r WHERE r.status IN ('live','callable','called') {where} "
                      "ORDER BY r.name LIMIT 100", params)
    out = []
    for r in races_:
        counties: dict[str, dict] = {}
        tier = "native"
        totals: dict[str, int] = {}
        for row in db.query("SELECT * FROM results_live WHERE race_id=?", (r["id"],)):
            tier = row["source_tier"]
            totals[row["party_code"]] = totals.get(row["party_code"], 0) + row["votes"]
            if row["county_geoid"]:
                c = counties.setdefault(row["county_geoid"],
                                        {"geoid": row["county_geoid"], "party_votes": {},
                                         "pct_reporting": row["pct_reporting"]})
                c["party_votes"][row["party_code"]] = row["votes"]
        call = db.query_one("SELECT winner_party,called_by,called_at FROM race_calls WHERE race_id=? "
                            "ORDER BY called_at DESC LIMIT 1", (r["id"],))
        out.append({"race_id": r["id"], "name": r["name"], "callable": r["status"] == "callable",
                    "called": call and dict(call), "counties": list(counties.values()),
                    "source_tier": tier, "total_votes": totals})
    return {"races": out}


@route("POST", "/api/electionnight/call")
def en_call(req):
    body = req.json or {}
    from modeling.race_calling import submit_call
    try:
        call_id = submit_call(int(body.get("race_id", 0)), body.get("winner_party", ""),
                              body.get("called_by", ""), body.get("notes"))
    except (ValueError, TypeError) as e:
        return 400, {"error": str(e)}
    return 201, {"call_id": call_id}


@route("POST", "/api/electionnight/manual")
def en_manual(req):
    body = req.json or {}
    from ingestion.results_tiers import manual_entry
    try:
        manual_entry(int(body["race_id"]), body.get("county_geoid"), body["party_code"],
                     int(body["votes"]), body.get("pct_reporting"), body.get("entered_by", ""))
    except (KeyError, ValueError) as e:
        return 400, {"error": str(e)}
    return 201, {"ok": True}


# ------------------------------ map ------------------------------

@route("GET", "/api/map/values")
def map_values(req):
    mode = req.query.get("mode", "partisan_lean")
    tier = req.query.get("tier", "state")
    as_of = _as_of(req)
    values: dict[str, float] = {}
    confidence: dict[str, str] = {}
    label = mode
    if mode.startswith("demo:"):
        _, category, variable = mode.split(":", 2)
        db_tier = {"state": "state", "county": "county_equivalent", "district": "congressional_district"}[tier]
        for r in db.query(
                "SELECT entity_id, value, confidence, MAX(as_of) FROM demographics "
                "WHERE tier=? AND category=? AND variable=? GROUP BY entity_id", (db_tier, category, variable)):
            key = r["entity_id"]
            if db_tier == "congressional_district":
                d = db.query_one("SELECT geoid FROM congressional_districts WHERE district_version_id=?",
                                 (key,))
                key = d["geoid"] if d else key
            values[key] = r["value"]
            confidence[key] = r["confidence"]
        label = variable
    elif mode in ("partisan_lean", "average_margin", "forecast", "turnout"):
        rt = req.query.get("race_type", "senate" if tier == "state" else "house")
        races_ = db.query("SELECT * FROM races WHERE race_type=? AND phase='general'", (rt,))
        from modeling.averaging import latest_average
        from modeling.forecasting import latest as forecast_latest
        from modeling.fundamentals import partisan_lean
        for r in races_:
            key = r["state_fips"]
            if r["district_version_id"]:
                d = db.query_one("SELECT geoid FROM congressional_districts WHERE district_version_id=?",
                                 (r["district_version_id"],))
                key = d["geoid"] if d else None
            if not key:
                continue
            if mode == "partisan_lean":
                values[key] = partisan_lean(r)
            elif mode == "average_margin":
                avg = latest_average(r["id"], as_of)
                if avg:
                    values[key] = round(avg["parties"].get("DEM", 0) - avg["parties"].get("REP", 0), 1)
            elif mode == "forecast":
                f = forecast_latest(r["id"], as_of=as_of)
                if f:
                    values[key] = f["dem_prob"]
            elif mode == "turnout":
                h = db.query_one("SELECT turnout_pct FROM political_history WHERE tier='state' AND entity_id=? "
                                 "AND office=? ORDER BY cycle_year DESC LIMIT 1", (r["state_fips"], rt))
                if h and h["turnout_pct"] is not None:
                    values[key] = h["turnout_pct"]
    elif mode == "volatility":
        for r in db.query("SELECT DISTINCT race_id FROM poll_averages"):
            race = db.query_one("SELECT state_fips FROM races WHERE id=?", (r["race_id"],))
            from modeling.volatility import latest as vol_latest
            v = vol_latest(f"race:{r['race_id']}", as_of)
            if race and race["state_fips"] and v:
                values[race["state_fips"]] = max(values.get(race["state_fips"], 0), v["score"])
    elif mode == "fundraising":
        for row in db.query(
                "SELECT r.state_fips k, SUM(d.total_amount) v FROM donors_aggregated d "
                "JOIN race_candidates rc ON rc.candidate_id=d.candidate_id "
                "JOIN races r ON r.id=rc.race_id WHERE r.state_fips IS NOT NULL GROUP BY r.state_fips"):
            values[row["k"]] = row["v"]
    nums = [v for v in values.values() if v is not None]
    return {"mode": mode, "tier": tier, "values": values, "confidence": confidence,
            "legend": {"min": min(nums) if nums else 0, "max": max(nums) if nums else 1, "label": label}}


@route("GET", "/api/map/pins")
def map_pins(req):
    return []  # populated as geocoded facts accumulate; kept cheap for now


# ------------------------------ export ------------------------------

_EXPORT_ALLOWED = {
    "polls", "poll_results", "poll_averages", "races", "candidates", "parties", "demographics",
    "political_history", "forecasts", "predictions", "backtest_results", "pollster_ratings",
    "qualitative_factor_scores", "ensemble_weights", "ensemble_backtest_results", "stories",
    "extracted_facts", "results_live", "race_calls", "volatility_scores", "chamber_simulations",
    "redistricting_fairness_scores", "computation_audit_log", "states", "county_equivalents",
    "congressional_districts", "electoral_vote_allocations", "second_order_links", "coalition_models",
}


@route("GET", "/api/export/{table}")
def export(req, table):
    if table not in _EXPORT_ALLOWED:
        return 404, {"error": "unknown table"}
    limit = min(int(req.query.get("limit", 10000)), 100000)
    rows = db.query(f"SELECT * FROM {table} LIMIT ?", (limit,))
    if req.query.get("format", "json") == "csv":
        buf = io.StringIO()
        if rows:
            writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        return 200, ("text/csv", buf.getvalue())
    return rows
