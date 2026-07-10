"""Official results, the honest tiered strategy (§05).

tier 1 — native state feeds, live on election night (plus a file-drop directory
         so any state feed you can script lands the same way);
tier 2 — OpenElections bulk CSVs (lagged; the historical backbone);
tier 3 — the real AP Elections API, gated behind config, default OFF;
manual — an authenticated internal entry path, every row tagged source_tier='manual'.

Every ingested row carries source_tier so the UI can honestly label which tier
is live for a given race — never implying AP-grade calling speed from a tier-1
feed that can't deliver it.
"""
from __future__ import annotations

import csv
import io
import json
import os

from core import db
from core.config import ROOT, cfg
from core.util import now_iso
from ingestion.http import SourceNotConfigured, get
from ingestion.scheduler import register

DROP_DIR = os.path.join(ROOT, "data", "results_native")


def upsert_result(race_id: int, county_geoid: str | None, party_code: str, votes: int,
                  pct_reporting: float | None, source_tier: str, is_synthetic: bool = False) -> None:
    db.execute(
        "INSERT INTO results_live(race_id,county_geoid,party_code,votes,pct_reporting,source_tier,"
        "updated_at,is_synthetic) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(race_id,county_geoid,party_code) DO UPDATE SET votes=excluded.votes, "
        "pct_reporting=excluded.pct_reporting, source_tier=excluded.source_tier, updated_at=excluded.updated_at",
        (race_id, county_geoid, party_code, votes, pct_reporting, source_tier, now_iso(), int(is_synthetic)))
    try:
        from api.websocket import broadcast
        broadcast({"type": "results", "payload": {"race_id": race_id}})
    except Exception:
        pass


@register("results_native")
def run_tier1(source: dict) -> None:
    """File-drop tier-1: any JSON file in data/results_native/ shaped
    {race_id, results:[{county_geoid,party,votes,pct_reporting}]} is ingested
    then archived. Per-state HTTP feeds plug in via config_json.feeds the same
    way (a minority of states publish clean machine-readable results)."""
    ingested = 0
    if os.path.isdir(DROP_DIR):
        for fname in sorted(os.listdir(DROP_DIR)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(DROP_DIR, fname)
            payload = json.load(open(path, encoding="utf-8"))
            for r in payload.get("results", []):
                upsert_result(payload["race_id"], r.get("county_geoid"), r["party"],
                              int(r["votes"]), r.get("pct_reporting"), "native")
                ingested += 1
            os.rename(path, path + ".done")
    feeds = (json.loads(source["config_json"] or "{}")).get("feeds") or []
    for spec in feeds:  # {url, race_id} → same JSON shape over HTTP
        payload = json.loads(get(spec["url"]).decode("utf-8", "replace"))
        for r in payload.get("results", []):
            upsert_result(spec.get("race_id", payload.get("race_id")), r.get("county_geoid"),
                          r["party"], int(r["votes"]), r.get("pct_reporting"), "native")
    from modeling.race_calling import evaluate_callable
    evaluate_callable()


@register("results_openelections")
def run_tier2(source: dict) -> None:
    """Bulk-sync OpenElections county-level general results for configured
    state/cycle pairs into political_history (the deep archive path)."""
    conf = json.loads(source["config_json"] or "{}")
    pairs = [(s, c) for s in conf.get("states", []) for c in conf.get("cycles", [])]
    if not pairs:
        raise SourceNotConfigured("configure sources.config_json.states + .cycles for OpenElections sync")
    from domain.geography import USPS_TO_FIPS
    for usps, cycle in pairs:
        key = f"openelections_done:{usps}:{cycle}"
        if db.meta_get(key):
            continue
        url = (f"{source['url']}/openelections-data-{usps.lower()}/master/"
               f"{cycle}/{cycle}1105__{usps.lower()}__general__county.csv")
        try:
            raw = get(url).decode("utf-8", "replace")
        except Exception:
            db.meta_set(key, "unavailable")
            continue
        _import_openelections_csv(raw, USPS_TO_FIPS[usps], cycle)
        db.meta_set(key, now_iso())


def _import_openelections_csv(raw: str, state_fips: str, cycle: int) -> None:
    per_county: dict[tuple, dict[str, int]] = {}
    for row in csv.DictReader(io.StringIO(raw)):
        office = (row.get("office") or "").strip().lower()
        office_key = {"president": "president", "u.s. senate": "senate", "governor": "governor",
                      "u.s. house": "house"}.get(office)
        if not office_key:
            continue
        county = (row.get("county") or "").strip()
        party = (row.get("party") or "OTH").strip().upper()[:3] or "OTH"
        try:
            votes = int(float(row.get("votes") or 0))
        except ValueError:
            continue
        crow = db.query_one("SELECT geoid FROM county_equivalents WHERE state_fips=? AND name LIKE ?",
                            (state_fips, county + "%"))
        if not crow:
            continue
        bucket = per_county.setdefault((crow["geoid"], office_key), {})
        bucket[party] = bucket.get(party, 0) + votes
    for (geoid, office_key), parties in per_county.items():
        total = sum(parties.values()) or 1
        dem, rep = parties.get("DEM", 0), parties.get("REP", 0)
        winner = max(parties, key=parties.get)
        db.execute(
            "INSERT OR IGNORE INTO political_history(tier,entity_id,office,seat,cycle_year,winner_party,"
            "dem_pct,rep_pct,other_pct,margin_pct,confidence,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("county_equivalent", geoid, office_key, "regular", cycle, winner,
             100 * dem / total, 100 * rep / total, 100 * (total - dem - rep) / total,
             100 * abs(dem - rep) / total, "measured", f"openelections:{cycle}"))


@register("results_ap")
def run_tier3(source: dict) -> None:
    if not cfg("ingestion.ap_elections.enabled"):
        raise SourceNotConfigured("AP Elections API disabled (ingestion.ap_elections.enabled=false); "
                                  "flip only once an AP account exists")
    if not os.environ.get(cfg("ingestion.ap_elections.api_key_env"), ""):
        raise SourceNotConfigured("AP enabled but AP_ELECTIONS_API_KEY not set")
    raise SourceNotConfigured("AP adapter scaffolded; wire the licensed endpoints when credentials exist")


def manual_entry(race_id: int, county_geoid: str | None, party_code: str, votes: int,
                 pct_reporting: float | None, entered_by: str) -> None:
    """The manual-entry tool: provenance stays visible, never laundered to look automated."""
    if not entered_by or entered_by.lower() in ("system", "model", "auto", "ai"):
        raise ValueError("manual entry requires a real human identifier")
    upsert_result(race_id, county_geoid, party_code, votes, pct_reporting, "manual")
    db.execute("INSERT INTO annotations(entity_type,entity_id,body,created_at) VALUES(?,?,?,?)",
               ("race", str(race_id), f"manual result entry by {entered_by}", now_iso()))
