"""Daily briefing: what moved in the last day, one per date. LLM prose when a
provider is reachable; a structured bulleted briefing (the manual's named
deterministic fallback) otherwise — the pipeline exercises both paths."""
from __future__ import annotations

from core import db
from core.util import today


def _movers(limit: int = 6) -> list[dict]:
    rows = db.query(
        """SELECT race_id, MAX(as_of) latest FROM poll_averages WHERE party_code='DEM'
           GROUP BY race_id HAVING COUNT(DISTINCT as_of) >= 2 LIMIT 200""")
    movers = []
    for r in rows:
        pair = db.query(
            "SELECT as_of, avg_pct FROM poll_averages WHERE race_id=? AND party_code='DEM' "
            "ORDER BY as_of DESC LIMIT 2", (r["race_id"],))
        if len(pair) < 2:
            continue
        delta = pair[0]["avg_pct"] - pair[1]["avg_pct"]
        if abs(delta) < 0.05:
            continue
        race = db.query_one("SELECT name FROM races WHERE id=?", (r["race_id"],))
        movers.append({"race": race["name"], "dem_delta_pts": round(delta, 2), "as_of": pair[0]["as_of"]})
    movers.sort(key=lambda m: -abs(m["dem_delta_pts"]))
    return movers[:limit]


def _deterministic_body(movers: list[dict], stories: list[dict], calls: list[dict]) -> str:
    lines = [f"PollGrid daily briefing — {today()} (deterministic: built from stored numbers only)", ""]
    lines.append("Poll-average movement:")
    if movers:
        for m in movers:
            direction = "toward DEM" if m["dem_delta_pts"] > 0 else "toward REP"
            lines.append(f"  • {m['race']}: {abs(m['dem_delta_pts'])} pts {direction} (as of {m['as_of']})")
    else:
        lines.append("  • no qualifying movement in the last snapshots")
    lines.append("")
    lines.append("Top story clusters:")
    if stories:
        for s in stories:
            lines.append(f"  • [{s['category']}] {s['headline']}")
    else:
        lines.append("  • no new story clusters")
    if calls:
        lines.append("")
        lines.append("Race calls:")
        for c in calls:
            lines.append(f"  • {c['name']}: {c['winner_party']} — called by {c['called_by']}")
    return "\n".join(lines)


def generate(as_of: str | None = None) -> dict | None:
    as_of = as_of or today()
    if db.query_one("SELECT 1 FROM daily_briefings WHERE as_of=?", (as_of,)):
        return None  # one per date, append-only
    movers = _movers()
    stories = db.query("SELECT headline, category FROM stories "
                       "WHERE updated_at >= datetime('now','-1 day') ORDER BY score DESC LIMIT 6")
    calls = db.query("SELECT r.name, rc.winner_party, rc.called_by FROM race_calls rc "
                     "JOIN races r ON r.id=rc.race_id WHERE rc.called_at >= datetime('now','-1 day')")
    body, model = None, "deterministic"
    try:
        from analyst.llm import complete_json, current_provider, provider_available
        if provider_available():
            out = complete_json(
                "Write a crisp 4-8 sentence daily political briefing STRICTLY from this data — no "
                f"outside facts, no predictions beyond the numbers given: movers={movers!r}, "
                f"stories={[dict(s) for s in stories]!r}, calls={[dict(c) for c in calls]!r}. "
                'Return JSON {"briefing": "..."}.', purpose="daily_briefing")
            if out and out.get("briefing"):
                body = str(out["briefing"])
                model = current_provider().get("model") or "llm"
    except Exception:
        pass
    if body is None:
        body = _deterministic_body(movers, [dict(s) for s in stories], [dict(c) for c in calls])
    db.execute("INSERT OR IGNORE INTO daily_briefings(as_of,body,model) VALUES(?,?,?)", (as_of, body, model))
    return {"as_of": as_of, "body": body, "model": model}
