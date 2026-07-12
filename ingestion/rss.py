"""Generic RSS/Atom firehose (wire services and political desks) + campaign
transcripts (same parser over per-campaign press-release feeds)."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from core import db
from ingestion.http import SourceNotConfigured, get
from ingestion.scheduler import register, stop_event
from ingestion.store import land_raw_item

_ATOM = "{http://www.w3.org/2005/Atom}"


def _repair_xml(raw: bytes) -> bytes:
    """Real-world feeds routinely violate strict XML: a BOM or stray bytes
    before the declaration, or a bare '&' that isn't part of an entity/charref
    (the single most common feed-generator bug). Neither changes what a
    well-formed feed would have parsed to — this only rescues feeds
    xml.etree would otherwise refuse outright."""
    text = raw.decode("utf-8", "replace").lstrip("﻿")
    start = text.find("<")
    if start > 0:
        text = text[start:]
    text = re.sub(r"&(?!#\d+;|#x[0-9a-fA-F]+;|[a-zA-Z][a-zA-Z0-9]*;)", "&amp;", text)
    return text.encode("utf-8")


def parse_feed(raw: bytes) -> list[dict]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        root = ET.fromstring(_repair_xml(raw))
    items = []
    for it in root.iter("item"):  # RSS 2.0
        items.append({
            "id": (it.findtext("guid") or it.findtext("link") or it.findtext("title") or "").strip(),
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "body": (it.findtext("description") or "").strip(),
            "published": (it.findtext("pubDate") or "").strip(),
        })
    if not items:
        for e in root.iter(f"{_ATOM}entry"):  # Atom
            link = e.find(f"{_ATOM}link")
            items.append({
                "id": (e.findtext(f"{_ATOM}id") or "").strip(),
                "title": (e.findtext(f"{_ATOM}title") or "").strip(),
                "link": link.get("href", "") if link is not None else "",
                "body": (e.findtext(f"{_ATOM}summary") or e.findtext(f"{_ATOM}content") or "").strip(),
                "published": (e.findtext(f"{_ATOM}updated") or "").strip(),
            })
    return items


def _ingest_feed(source: dict, url: str) -> int:
    n = 0
    for item in parse_feed(get(url)):
        if not item["id"]:
            continue
        if land_raw_item(source["id"], item["id"], item["title"], item["link"],
                         item["body"], item["published"], source_row=source):
            n += 1
    return n


@register("rss")
def run(source: dict) -> None:
    _ingest_feed(source, source["url"])


def _in_debate_window(now: datetime | None = None) -> bool:
    """True when any debate_schedule row's scheduled_at is within +/- its own
    window_hours of now (unparseable timestamps never open a window)."""
    now = now or datetime.now(timezone.utc)
    for row in db.query("SELECT scheduled_at, window_hours FROM debate_schedule"):
        try:
            ts = datetime.fromisoformat(str(row["scheduled_at"]).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if abs(now - ts) <= timedelta(hours=row["window_hours"] or 6):
            return True
    return False


@register("transcripts")
def run_transcripts(source: dict) -> None:
    """Campaign press-release / transcript feeds.

    Debate-window tight polling (addendum §7.1): when any debate_schedule row
    is within +/- window_hours of now, this tick loops — re-fetching every feed
    every cfg('ingestion.debate_window.poll_seconds') — instead of returning
    after one pass. The wait between fetches is scheduler.stop_event.wait(), so
    shutdown stays immediate, and the loop is capped at 10 iterations per tick
    before handing control back to the scheduler (which re-enters at the
    source's normal interval and re-checks the window)."""
    from core.config import cfg
    feeds = (json.loads(source["config_json"] or "{}")).get("feeds") or []
    if not feeds:
        raise SourceNotConfigured("no campaign press-release feeds configured (sources.config_json.feeds)")
    iterations = 10 if _in_debate_window() else 1
    poll_seconds = cfg("ingestion.debate_window.poll_seconds")
    for i in range(iterations):
        for url in feeds:
            _ingest_feed(source, url)
        if i + 1 < iterations and stop_event.wait(poll_seconds):
            return  # shutting down — never sleep past the stop event
