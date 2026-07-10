"""Poll ingestion. Polls need no NLP — they parse straight into the chained
polls fact. The adapter framework normalizes any configured machine-readable
pollster feed (CSV/JSON, spec in sources.config_json.feeds) into one Poll
shape: candidate/party, race, sample size, MoE, field dates, population
(lv/rv/a), methodology, release URL. ingest_poll() is also the single entry
point used by the manual-entry API route and scripts/seed_demo.py."""
from __future__ import annotations

import csv
import io
import json

from core import db, provenance
from core.util import now_iso
from ingestion.http import SourceNotConfigured, get
from ingestion.scheduler import register


def ensure_pollster(name: str, url: str | None = None, methodology: str | None = None) -> int:
    row = db.query_one("SELECT id FROM pollsters WHERE name=?", (name,))
    if row:
        return row["id"]
    return db.execute("INSERT INTO pollsters(name,url,methodology) VALUES(?,?,?)", (name, url, methodology))


def ingest_poll(*, pollster: str, race_id: int, field_start: str, field_end: str,
                results: dict[str, float], sample_size: int | None = None,
                population: str | None = "lv", moe: float | None = None,
                methodology: str | None = None, release_url: str | None = None,
                source_id: int | None = None, is_synthetic: bool = False,
                created_at: str | None = None) -> int | None:
    """Normalize + hash-chain one poll. results: {party_code_or_candidate: pct}.
    Dedup on (pollster, race, field dates, url)."""
    pid = ensure_pollster(pollster)
    if db.query_one("SELECT 1 FROM polls WHERE pollster_id=? AND race_id=? AND field_start=? AND field_end=? "
                    "AND COALESCE(release_url,'')=COALESCE(?,'')",
                    (pid, race_id, field_start, field_end, release_url)):
        return None
    poll_id = provenance.chained_insert("polls", {
        "source_id": source_id, "raw_item_id": None, "pollster_id": pid, "race_id": race_id,
        "field_start": field_start, "field_end": field_end, "sample_size": sample_size,
        "population": population, "moe": moe, "methodology": methodology,
        "release_url": release_url, "created_at": created_at or now_iso(),
        "is_synthetic": int(is_synthetic),
    })
    rows = []
    for key, pct in results.items():
        cand = db.query_one("SELECT id, party_code FROM candidates WHERE name=?", (key,))
        if cand:
            rows.append((poll_id, cand["id"], cand["party_code"], float(pct)))
        else:
            rows.append((poll_id, None, key.upper()[:3], float(pct)))
    db.executemany("INSERT INTO poll_results(poll_id,candidate_id,party_code,pct) VALUES(?,?,?,?)", rows)
    _notify(poll_id, race_id)
    return poll_id


def _notify(poll_id: int, race_id: int) -> None:
    try:
        from api.websocket import broadcast
        from analyst.context_packs import invalidate_for_race
        invalidate_for_race(race_id)
        broadcast({"type": "poll", "payload": {"poll_id": poll_id, "race_id": race_id}})
    except Exception:
        pass


@register("pollster_feed")
def run(source: dict) -> None:
    feeds = (json.loads(source["config_json"] or "{}")).get("feeds") or []
    if not feeds:
        raise SourceNotConfigured(
            "no machine-readable pollster feeds configured (sources.config_json.feeds: "
            "[{pollster,url,format:'csv',race_column,…}])")
    for spec in feeds:
        raw = get(spec["url"]).decode("utf-8", "replace")
        if spec.get("format", "csv") != "csv":
            continue
        for row in csv.DictReader(io.StringIO(raw)):
            race = db.query_one("SELECT id FROM races WHERE name=?", (row.get(spec.get("race_column", "race"), ""),))
            if not race:
                continue
            results = {k[4:].upper(): float(v) for k, v in row.items()
                       if k.startswith("pct_") and v not in ("", None)}
            ingest_poll(pollster=spec["pollster"], race_id=race["id"],
                        field_start=row.get("field_start", ""), field_end=row.get("field_end", ""),
                        results=results,
                        sample_size=int(row["sample_size"]) if row.get("sample_size") else None,
                        population=row.get("population", "lv"),
                        moe=float(row["moe"]) if row.get("moe") else None,
                        release_url=row.get("url"), source_id=source["id"])
