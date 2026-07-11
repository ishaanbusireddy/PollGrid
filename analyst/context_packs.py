"""Context packs: deterministically-built, cited bundles — everything the
Analyst is allowed to reason over, nothing it has to guess. Sync jobs
INVALIDATE (mark stale) but never eagerly rebuild; a pack is rebuilt the next
time someone asks about that entity and cached from then on. A precinct nobody
looks at stays stale and unbuilt indefinitely, at zero cost.

Token budget ~30k (config), split per the manual; overflow policy for tagged
facts is relevance-ranked truncation via the correlation embedding — never a
blind first-N cutoff."""
from __future__ import annotations

import json
import threading

from core import db
from core.config import cfg
from core.util import est_tokens, now_iso


def invalidate(entity_type: str, entity_id: str) -> None:
    db.execute("INSERT INTO context_packs(entity_type,entity_id,stale) VALUES(?,?,1) "
               "ON CONFLICT(entity_type,entity_id) DO UPDATE SET stale=1", (entity_type, str(entity_id)))


def invalidate_for_state(state_fips: str) -> None:
    invalidate("state", state_fips)
    for r in db.query("SELECT id FROM races WHERE state_fips=?", (state_fips,)):
        invalidate("race", str(r["id"]))


_rescore_lock = threading.Lock()
_rescore_pending: set[int] = set()


def _kick_factor_rescore(race_id: int) -> None:
    """Event-driven scorecard refresh (addendum §7): new facts landing for a race
    re-run its qualitative factor vector without waiting for the nightly job —
    debounced through app_meta (min-gap, config) so an ingestion burst costs one
    rescore, and run on a daemon thread so ingestion never blocks on an LLM."""
    key = f"factor_rescore_at:{race_id}"
    min_gap = cfg("genius_layer.event_rescore_min_gap_minutes")
    last = db.meta_get(key)
    if last:
        row = db.query_one("SELECT (julianday(?) - julianday(?)) * 1440 AS mins", (now_iso(), last))
        if row and row["mins"] is not None and row["mins"] < min_gap:
            return
    with _rescore_lock:
        if race_id in _rescore_pending:
            return  # a rescore for this race is already in flight
        _rescore_pending.add(race_id)
    db.meta_set(key, now_iso())

    def _run():
        try:
            from modeling.factors_taxonomy import score_race
            score_race(race_id)
        except Exception as e:  # never let a rescore failure ripple into ingestion
            print(f"event rescore failed for race {race_id}: {type(e).__name__}: {e}")
        finally:
            with _rescore_lock:
                _rescore_pending.discard(race_id)

    threading.Thread(target=_run, name=f"factor-rescore-{race_id}", daemon=True).start()


def invalidate_for_race(race_id: int) -> None:
    invalidate("race", str(race_id))
    _kick_factor_rescore(race_id)


def _demographics_block(tier: str, entity_id: str) -> list[dict]:
    rows = db.query(
        "SELECT category, variable, value, confidence, source, as_of, is_synthetic FROM demographics "
        "WHERE tier=? AND entity_id=? ORDER BY category, variable, is_synthetic, as_of DESC",
        (tier, entity_id))
    seen: dict[tuple, dict] = {}
    for r in rows:  # real beats synthetic, then latest vintage (ORDER BY does the work)
        seen.setdefault((r["category"], r["variable"]), r)
    return list(seen.values())


def _history_block(tier: str, entity_id: str) -> list[dict]:
    return db.query(
        "SELECT office, seat, cycle_year, winner_party, dem_pct, rep_pct, margin_pct, turnout_pct, confidence "
        "FROM political_history WHERE tier=? AND entity_id=? ORDER BY cycle_year DESC LIMIT 40",
        (tier, entity_id))


def build(entity_type: str, entity_id: str) -> dict:
    """Assemble the full bundle. Thin tiers say so honestly instead of the model
    quietly filling the gap."""
    budget = cfg("genius_layer.context_pack_token_budget")
    pack: dict = {"entity_type": entity_type, "entity_id": entity_id, "built_at": now_iso(),
                  "thin_coverage_notes": []}

    if entity_type == "race":
        race = db.query_one("SELECT * FROM races WHERE id=?", (int(entity_id),))
        if race is None:
            return pack
        from modeling.averaging import latest_average
        from modeling.coalition import latest as coalition_latest
        from modeling.factors_taxonomy import latest_vector
        from modeling.forecasting import category_visible, latest as forecast_latest
        from modeling.fundamentals import latest as fund_latest
        pack["race"] = dict(race)
        pack["poll_average"] = latest_average(race["id"])
        pack["fundamentals"] = fund_latest(race["id"])
        vis, reason = category_visible(race["race_type"])
        f = forecast_latest(race["id"])
        pack["forecast"] = {"row": f and dict(f), "visible": vis, "gate_reason": reason}
        pack["factor_scorecard"] = latest_vector(race["id"])
        pack["coalition"] = coalition_latest(race["id"])
        pack["candidates"] = db.query(
            "SELECT c.id, c.name, c.party_code, c.bio, rc.is_incumbent FROM race_candidates rc "
            "JOIN candidates c ON c.id=rc.candidate_id WHERE rc.race_id=?", (race["id"],))
        if race["state_fips"]:
            pack["state_demographics"] = _demographics_block("state", race["state_fips"])
            pack["state_history"] = _history_block("state", race["state_fips"])
        pack["national_demographics"] = _demographics_block("nation", "US")
        facts = db.query("SELECT id, summary, category, created_at FROM extracted_facts WHERE race_id=? "
                         "ORDER BY created_at DESC LIMIT 200", (int(entity_id),))
        from modeling.correlation import relevance_rank
        fact_budget = int(budget * 0.33)  # ~10k of 30k, the largest single allocation
        pack["tagged_facts"] = relevance_rank(race["name"], [dict(f) for f in facts], fact_budget)
        if not pack["poll_average"]:
            pack["thin_coverage_notes"].append("no qualifying polls for this race yet")
    else:
        tier = entity_type
        pack["demographics"] = _demographics_block(tier, entity_id)
        pack["history"] = _history_block(tier, entity_id)
        if tier == "state":
            row = db.query_one("SELECT * FROM states WHERE fips_code=?", (entity_id,))
            pack["entity"] = row and dict(row)
            pack["national_demographics"] = _demographics_block("nation", "US")
        if tier == "precinct":
            pack["thin_coverage_notes"].append(
                "precinct figures are DERIVED (population-weighted areal interpolation), never measured")
        if not pack["demographics"]:
            pack["thin_coverage_notes"].append(f"no demographic rows for this {tier} yet")
        if not pack["history"]:
            pack["thin_coverage_notes"].append(f"no electoral history imported for this {tier} yet")

    blob = json.dumps(pack, default=str)
    pack["token_estimate"] = est_tokens(blob)
    db.execute(
        "INSERT INTO context_packs(entity_type,entity_id,built_at,stale,pack_json,token_estimate) "
        "VALUES(?,?,?,0,?,?) ON CONFLICT(entity_type,entity_id) DO UPDATE SET built_at=excluded.built_at, "
        "stale=0, pack_json=excluded.pack_json, token_estimate=excluded.token_estimate",
        (entity_type, str(entity_id), pack["built_at"], blob, pack["token_estimate"]))
    return pack


def get(entity_type: str, entity_id: str) -> tuple[dict, bool]:
    """→ (pack, was_stale). Lazy: rebuild only when missing or stale."""
    row = db.query_one("SELECT * FROM context_packs WHERE entity_type=? AND entity_id=?",
                       (entity_type, str(entity_id)))
    if row and not row["stale"] and row["pack_json"]:
        return json.loads(row["pack_json"]), False
    return build(entity_type, str(entity_id)), row is not None and bool(row["stale"])
