"""Shared landing path: raw_items dedup → extraction pipeline → fact chain."""
from __future__ import annotations

from core import db
from core.util import now_iso


def land_raw_item(source_id: int, external_id: str, title: str | None, url: str | None,
                  body: str | None, published_at: str | None,
                  matched_profile_id: int | None = None) -> int | None:
    """Insert one raw item (UNIQUE(source_id, external_id) dedup — an article both
    the firehose and a targeted search find is never double-counted). Returns the
    raw_item id if new, None if already seen."""
    existing = db.query_one("SELECT id, matched_profile_id FROM raw_items WHERE source_id=? AND external_id=?",
                            (source_id, external_id))
    if existing:
        if matched_profile_id and not existing["matched_profile_id"]:
            db.execute("UPDATE raw_items SET matched_profile_id=? WHERE id=?",
                       (matched_profile_id, existing["id"]))
        return None
    rid = db.execute(
        "INSERT INTO raw_items(source_id,external_id,fetched_at,title,url,body,published_at,matched_profile_id) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (source_id, external_id, now_iso(), title, url, body, published_at, matched_profile_id))
    from processing.extraction import process_raw_item  # deferred: avoids import cycle
    try:
        process_raw_item(rid)
    except Exception:
        import traceback
        traceback.print_exc()  # extraction failure never kills ingestion of the next item
    return rid
