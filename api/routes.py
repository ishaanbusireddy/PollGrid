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
    from core.version import VERSION
    sources = db.query("SELECT id,name,source_type,health,last_run_at,last_error,is_active FROM sources")
    counts = {t: db.query_one(f"SELECT COUNT(*) c FROM {t}")["c"]
              for t in ("races", "polls", "candidates", "stories")}
    counts["facts"] = db.query_one("SELECT COUNT(*) c FROM extracted_facts")["c"]
    return {"version": VERSION, "sources": sources, "counts": counts,
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
        """SELECT s.fips_code, s.usps_code, s.name, s.is_territory, s.flag_url,
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


@route("GET", "/api/geo/senate_holders")
def senate_holders(req):
    """Latest known senate winner per state (real rows only) — the builder's
    'seats not up display fixed at their current holder' data."""
    out = {}
    for r in db.query(
            """SELECT entity_id, winner_party FROM political_history ph
               WHERE tier='state' AND office='senate' AND is_synthetic=0 AND winner_party IS NOT NULL
                 AND cycle_year = (SELECT MAX(cycle_year) FROM political_history
                                   WHERE tier='state' AND office='senate' AND is_synthetic=0
                                     AND entity_id=ph.entity_id)"""):
        out[r["entity_id"]] = r["winner_party"]
    return out


@route("GET", "/api/demographics/{tier}/{entity_id}")
def demographics(req, tier, entity_id):
    if tier == "trends":  # registered first, so it shadows the trends route — delegate
        return demographic_trends(req, entity_id)
    # demographics as_of is a source-vintage tag (acs5_2023, demo_2026), not a date:
    # per variable serve the best row — REAL beats synthetic, then latest vintage.
    all_rows = db.query(
        "SELECT category,variable,value,confidence,source,as_of,is_synthetic FROM demographics "
        "WHERE tier=? AND entity_id=? ORDER BY category,variable,is_synthetic,as_of DESC",
        (tier, entity_id))
    seen: dict[tuple, dict] = {}
    for r in all_rows:  # first row per (category,variable) wins given the ORDER BY
        seen.setdefault((r["category"], r["variable"]), r)
    rows = list(seen.values())
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
        "narrative": narrative.cached(int(id)),
        "backed_by": _backed_by([c["id"] for c in cands]) + db.query(
            """SELECT o.id org_id, o.name org, o.sector, s.spend_type, SUM(s.amount) amount
               FROM pac_candidate_spend s JOIN lobbying_orgs o ON o.id=s.org_id
               WHERE s.race_id=? AND s.candidate_id IS NULL
               GROUP BY o.id, s.spend_type""", (id,)),
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
    if req.query.get("methodology"):
        clauses.append("p.methodology LIKE ?"); params.append("%" + req.query["methodology"] + "%")
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
            "stances": stances, "backed_by": _backed_by([int(id)])}


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
        (entity_type, id, limit * 3 if req.query.get("sort") == "relevance" else limit))
    if req.query.get("sort") == "relevance" and rows:
        from modeling.correlation import relevance_rank
        label = _watch_label(entity_type, id)
        ranked = relevance_rank(label, [{**r, "summary": r["title"] or ""} for r in rows], 10**9)
        rows = [{k: v for k, v in r.items() if k != "summary"} for r in ranked][:limit]
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
    rows = db.query("SELECT * FROM stories WHERE updated_at>? AND date(created_at)<=? "
                    "ORDER BY updated_at DESC LIMIT 50", (since, _as_of(req)))
    for s in rows:
        s["facts"] = db.query(
            "SELECT f.id, f.summary, f.category FROM story_facts sf JOIN extracted_facts f ON f.id=sf.fact_id "
            "WHERE sf.story_id=? ORDER BY f.id DESC LIMIT 10", (s["id"],))
    return rows


@route("GET", "/api/stories/{id}")
def story_detail(req, id):
    """The event breakdown: the full fact cluster behind one story card."""
    story = db.query_one("SELECT * FROM stories WHERE id=?", (id,))
    if story is None:
        return 404, {"error": "story not found"}
    facts = db.query(
        """SELECT f.id, f.summary, f.category, f.occurred_at, f.created_at, ri.url,
                  COALESCE(json_extract(s.config_json,'$.outlet'), s.name) outlet, s.reliability_tier
           FROM story_facts sf
           JOIN extracted_facts f ON f.id = sf.fact_id
           LEFT JOIN raw_items ri ON ri.id = f.raw_item_id
           LEFT JOIN sources s ON s.id = ri.source_id
           WHERE sf.story_id=? ORDER BY COALESCE(f.occurred_at, f.created_at) DESC""", (id,))
    race = story["race_id"] and db.query_one("SELECT id, name FROM races WHERE id=?", (story["race_id"],))
    return {"story": dict(story), "facts": facts, "race": race and dict(race)}


@route("GET", "/api/briefings/latest")
def briefing_latest(req):
    row = db.query_one("SELECT as_of, body, model FROM daily_briefings WHERE as_of<=? "
                       "ORDER BY as_of DESC LIMIT 1", (_as_of(req),))
    if row is None:
        return 404, {"error": "no briefing yet — generated by the nightly job"}
    return dict(row)


# ------------------------------ watchlist ------------------------------

def _watch_label(entity_type: str, entity_id: str) -> str:
    if entity_type == "race":
        r = db.query_one("SELECT name FROM races WHERE id=?", (entity_id,))
        return r["name"] if r else f"race #{entity_id}"
    if entity_type == "state":
        s = db.query_one("SELECT name FROM states WHERE fips_code=?", (entity_id,))
        return s["name"] if s else entity_id
    if entity_type == "candidate":
        c = db.query_one("SELECT name FROM candidates WHERE id=?", (entity_id,))
        return c["name"] if c else f"candidate #{entity_id}"
    return f"{entity_type}:{entity_id}"


@route("GET", "/api/watchlist")
def watchlist(req):
    rows = db.query("SELECT entity_type, entity_id FROM watchlist_items ORDER BY added_at DESC")
    return [{"entity_type": r["entity_type"], "entity_id": r["entity_id"],
             "label": _watch_label(r["entity_type"], r["entity_id"])} for r in rows]


@route("POST", "/api/watchlist")
def watchlist_add(req):
    body = req.json or {}
    if not body.get("entity_type") or body.get("entity_id") in (None, ""):
        return 400, {"error": "entity_type and entity_id required"}
    from core.util import now_iso
    db.execute("INSERT OR IGNORE INTO watchlist_items(entity_type,entity_id,added_at) VALUES(?,?,?)",
               (body["entity_type"], str(body["entity_id"]), now_iso()))
    return 201, {"ok": True}


@route("POST", "/api/watchlist/delete")
def watchlist_delete(req):
    body = req.json or {}
    db.execute("DELETE FROM watchlist_items WHERE entity_type=? AND entity_id=?",
               (body.get("entity_type", ""), str(body.get("entity_id", ""))))
    return {"ok": True}


@route("GET", "/api/demographics/trends/{race_id}")
def demographic_trends(req, race_id):
    """The coalition detector's output: which demographic variables explain the
    most movement (API_CONTRACT.md route family that was missing until now)."""
    from modeling.coalition import compute, latest
    out = latest(int(race_id)) or compute(int(race_id))
    if out is None:
        return 404, {"error": "insufficient county history/demographics for this race yet"}
    return out


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
        # derived share modes: numerator/denominator computed from stored variables
        SHARES = {"bachelors_share": ("education", "bachelors", "education", "pop_25plus"),
                  "owner_share": ("housing_urbanicity", "owner_occupied", "housing_urbanicity", "occupied_units"),
                  "nonwhite_share": ("race_ethnicity", "white_nh", "population_age", "total_population"),
                  "foreign_born_share": ("social_nativity", "foreign_born", "population_age", "total_population")}

        def _var_map(cat, var):
            out = {}
            for r in db.query(
                    "SELECT entity_id, value, confidence FROM demographics WHERE tier=? AND category=? "
                    "AND variable=? ORDER BY is_synthetic DESC, as_of", (db_tier, cat, var)):
                out[r["entity_id"]] = r  # last row wins: real (is_synthetic=0) sorts last
            return out

        if variable in SHARES:
            ncat, nvar, dcat, dvar = SHARES[variable]
            nums, dens = _var_map(ncat, nvar), _var_map(dcat, dvar)
            for eid, n in nums.items():
                d = dens.get(eid)
                if not d or not d["value"]:
                    continue
                share = 100.0 * n["value"] / d["value"]
                if variable == "nonwhite_share":
                    share = 100.0 - share
                values[eid] = round(share, 1)
                confidence[eid] = n["confidence"]
        else:
            for eid, r in _var_map(category, variable).items():
                values[eid] = r["value"]
                confidence[eid] = r["confidence"]
        if db_tier == "congressional_district" and values:
            remapped, remapped_conf = {}, {}
            for eid, v in values.items():
                d = db.query_one("SELECT geoid FROM congressional_districts WHERE district_version_id=?", (eid,))
                remapped[d["geoid"] if d else eid] = v
                remapped_conf[d["geoid"] if d else eid] = confidence.get(eid)
            values, confidence = remapped, remapped_conf
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
    """Live pins: recent geocoded facts + fresh polls + race calls, pinned at
    the gazetteer centroid of their county (or state)."""
    from core.gazetteer import centroid
    pins = []
    for f in db.query(
            "SELECT id, summary, category, race_id, state_fips, county_geoid, created_at "
            "FROM extracted_facts WHERE (state_fips IS NOT NULL OR county_geoid IS NOT NULL) "
            "AND created_at >= datetime('now','-3 days') ORDER BY created_at DESC LIMIT 60"):
        pt = (f["county_geoid"] and centroid("county", f["county_geoid"])) or \
             (f["state_fips"] and centroid("state", f["state_fips"]))
        if pt:
            pins.append({"lat": round(pt[0], 4), "lon": round(pt[1], 4),
                         "kind": "poll" if f["category"] == "polling" else "story",
                         "label": f["summary"][:120], "race_id": f["race_id"], "ts": f["created_at"]})
    for c in db.query(
            "SELECT rc.race_id, rc.winner_party, rc.called_by, rc.called_at, r.state_fips "
            "FROM race_calls rc JOIN races r ON r.id=rc.race_id "
            "WHERE rc.called_at >= datetime('now','-3 days')"):
        pt = c["state_fips"] and centroid("state", c["state_fips"])
        if pt:
            pins.append({"lat": round(pt[0], 4), "lon": round(pt[1], 4), "kind": "call",
                         "label": f"CALLED {c['winner_party']} by {c['called_by']}",
                         "race_id": c["race_id"], "ts": c["called_at"]})
    return pins


# ------------------------------ export ------------------------------

def _export_tables() -> set[str]:
    """'Open data, not a walled garden' — every real table exports. Only the
    internal meta/chain-head store is excluded."""
    return {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")} - {"app_meta"}


# ------------------------------ influence ledger ------------------------------

def _backed_by(candidate_ids: list[int]) -> list[dict]:
    if not candidate_ids:
        return []
    ph = ",".join("?" * len(candidate_ids))
    return db.query(
        f"""SELECT o.id org_id, o.name org, o.sector, s.spend_type, SUM(s.amount) amount
            FROM pac_candidate_spend s JOIN lobbying_orgs o ON o.id = s.org_id
            WHERE s.candidate_id IN ({ph})
            GROUP BY o.id, s.spend_type ORDER BY SUM(s.amount) DESC LIMIT 30""", candidate_ids)


@route("GET", "/api/lobbies")
def lobbies(req):
    clauses, params = ["1=1"], []
    if req.query.get("sector"):
        clauses.append("o.sector=?"); params.append(req.query["sector"])
    order = "total_spend DESC" if req.query.get("sort", "spend") == "spend" else "o.name"
    rows = db.query(
        f"""SELECT o.id, o.name, o.sector, o.org_type, o.citation,
                   COALESCE((SELECT SUM(amount) FROM pac_candidate_spend WHERE org_id=o.id), 0)
                   + COALESCE((SELECT SUM(amount) FROM lobbying_disclosures WHERE org_id=o.id), 0)
                     AS total_spend,
                   (SELECT COUNT(DISTINCT candidate_id) FROM pac_candidate_spend WHERE org_id=o.id)
                     AS candidates_backed
            FROM lobbying_orgs o WHERE {' AND '.join(clauses)}
            ORDER BY {order} LIMIT 500""", params)
    return rows


@route("GET", "/api/lobbies/{id}")
def lobby_detail(req, id):
    org = db.query_one("SELECT * FROM lobbying_orgs WHERE id=?", (id,))
    if org is None:
        return 404, {"error": "organization not found"}
    disclosures = db.query(
        "SELECT period, client, issue_codes, amount, source, source_url FROM lobbying_disclosures "
        "WHERE org_id=? ORDER BY period DESC LIMIT 200", (id,))
    spend = db.query(
        """SELECT s.candidate_id, c.name candidate, s.race_id, r.name race, s.amount,
                  s.spend_type, s.cycle_year
           FROM pac_candidate_spend s
           LEFT JOIN candidates c ON c.id = s.candidate_id
           LEFT JOIN races r ON r.id = s.race_id
           WHERE s.org_id=? ORDER BY s.amount DESC LIMIT 200""", (id,))
    endorsements = db.query(
        """SELECT c.name candidate, r.name race, e.as_of, e.source_url
           FROM endorsements e JOIN candidates c ON c.id = e.candidate_id
           LEFT JOIN races r ON r.id = e.race_id WHERE e.org_id=? ORDER BY e.as_of DESC""", (id,))
    return {"org": dict(org), "disclosures": disclosures, "spend": spend, "endorsements": endorsements}


# ------------------------------ settings (API keys) ------------------------------

@route("GET", "/api/settings/keys")
def settings_keys(req):
    from core import keys
    from analyst.llm import current_provider
    return {"keys": keys.status(), "env_path": keys.ENV_PATH, "llm": current_provider()}


@route("POST", "/api/settings/keys")
def settings_save(req):
    from core import keys
    body = req.json or {}
    ok, detail = keys.save(body.get("name", ""), body.get("value", ""))
    if ok:
        # a working key wakes the matching source: clear its degraded state so
        # the next scheduler tick runs it instead of sitting on a flat backoff
        env_name = body.get("name", "")
        db.execute("UPDATE sources SET health='ok', consecutive_failures=0, last_error=NULL "
                   "WHERE api_key_env=?", (env_name,))
    return (200 if ok else 400), {"ok": ok, "detail": detail, "configured": ok}


@route("GET", "/api/export/{table}")
def export(req, table):
    if table not in _export_tables():
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
