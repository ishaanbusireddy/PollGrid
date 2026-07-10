"""Election night: a deterministic CALLABLE flag — margin versus estimated
remaining vote — that a human, and ONLY a human, turns into a call.

AUTO_PUBLISH_CALLS is hardcoded False in this file, not just config. There is
no code path that inserts into race_calls without a human identifier; the
schema additionally rejects called_by in ('system','model','auto','ai')."""
from __future__ import annotations

from core import db
from core.util import now_iso

AUTO_PUBLISH_CALLS = False  # hardcoded. Changing this constant does nothing: no auto-call path exists.

CALLABLE_MARGIN_FACTOR = 1.0  # callable when leader's margin > remaining votes * factor


def _sync_election_night_mode() -> None:
    """Election-night mode (tightens results_native polling; lights up the UI)
    tracks whether any race is actually live — deterministic, no clock guessing."""
    live = db.query_one("SELECT 1 FROM races WHERE status IN ('live','callable') LIMIT 1")
    db.meta_set("election_night_mode", "1" if live else "0")


def evaluate_callable() -> int:
    """Recompute the CALLABLE flag for every live race with results. Estimated
    remaining vote from pct_reporting; historical county-reporting curves refine
    this once the archive holds them."""
    n = 0
    for race in db.query("SELECT id, status FROM races WHERE status IN ('upcoming','live','callable')"):
        rows = db.query("SELECT party_code, SUM(votes) v, AVG(COALESCE(pct_reporting,0)) rep "
                        "FROM results_live WHERE race_id=? GROUP BY party_code", (race["id"],))
        if not rows:
            continue
        totals = {r["party_code"]: r["v"] for r in rows}
        reporting = max(r["rep"] for r in rows)
        counted = sum(totals.values())
        if counted == 0 or reporting <= 0:
            continue
        est_total = counted / (reporting / 100.0) if reporting > 1 else counted
        remaining = max(0.0, est_total - counted)
        ranked = sorted(totals.items(), key=lambda t: -t[1])
        margin = ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0)
        called = db.query_one("SELECT 1 FROM race_calls WHERE race_id=?", (race["id"],))
        if called:
            new_status = "called"
        elif margin > remaining * CALLABLE_MARGIN_FACTOR:
            new_status = "callable"  # flag for human review — and stop there
        else:
            new_status = "live"
        if new_status != race["status"]:
            db.execute("UPDATE races SET status=? WHERE id=?", (new_status, race["id"]))
            n += 1
    _sync_election_night_mode()
    return n


def submit_call(race_id: int, winner_party: str, called_by: str, notes: str | None = None) -> int:
    """The only way a race gets called. called_by must be a real human identifier."""
    if not called_by or not called_by.strip():
        raise ValueError("called_by is required — race calls are made by named humans")
    if called_by.strip().lower() in ("system", "model", "auto", "ai"):
        raise ValueError("race calls are never automated; called_by must be a human identifier")
    call_id = db.execute("INSERT INTO race_calls(race_id,called_at,winner_party,called_by,notes) VALUES(?,?,?,?,?)",
                         (race_id, now_iso(), winner_party, called_by.strip(), notes))
    db.execute("UPDATE races SET status='called' WHERE id=?", (race_id,))
    try:
        from api.websocket import broadcast
        broadcast({"type": "race_call", "payload": {"race_id": race_id, "winner_party": winner_party,
                                                    "called_by": called_by.strip()}})
    except Exception:
        pass
    return call_id
