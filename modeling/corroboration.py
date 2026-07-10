"""Corroboration: a poll's implied direction checked against signals a model
can't hallucinate — FEC fundraising trend, campaign-event frequency, ad-spend
trend. Agreement across independently-sourced channels raises a badge."""
from __future__ import annotations

from core import db


def check(race_id: int) -> dict:
    signals = []
    avg = db.query(
        "SELECT as_of, avg_pct FROM poll_averages WHERE race_id=? AND party_code='DEM' ORDER BY as_of DESC LIMIT 2",
        (race_id,))
    poll_dir = 0
    if len(avg) == 2:
        poll_dir = 1 if avg[0]["avg_pct"] > avg[1]["avg_pct"] else (-1 if avg[0]["avg_pct"] < avg[1]["avg_pct"] else 0)
        signals.append({"channel": "poll_average", "direction": poll_dir})

    fin = db.query(
        "SELECT rc.party_code, SUM(d.total_amount) amt FROM race_candidates rc "
        "JOIN donors_aggregated d ON d.candidate_id=rc.candidate_id WHERE rc.race_id=? "
        "GROUP BY rc.party_code", (race_id,))
    amts = {r["party_code"]: r["amt"] or 0 for r in fin}
    if amts.get("DEM") or amts.get("REP"):
        fin_dir = 1 if amts.get("DEM", 0) > amts.get("REP", 0) else -1
        signals.append({"channel": "fec_fundraising", "direction": fin_dir})

    events = db.query_one(
        "SELECT COUNT(*) c FROM extracted_facts WHERE race_id=? AND category='campaign_event' "
        "AND created_at >= datetime('now','-14 days')", (race_id,))
    if events["c"]:
        signals.append({"channel": "campaign_event_frequency", "direction": 0, "count": events["c"]})

    spend = db.query_one("SELECT COUNT(*) c FROM ad_spend WHERE race_id=?", (race_id,))
    if spend["c"]:
        signals.append({"channel": "ad_spend", "direction": 0, "count": spend["c"]})

    directional = [s["direction"] for s in signals if s["direction"] != 0]
    badge = len(directional) >= 2 and len(set(directional)) == 1
    return {"badge": badge, "signals": signals,
            "note": "badge = ≥2 independently-sourced channels agreeing on direction"}
