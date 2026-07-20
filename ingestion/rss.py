"""Generic RSS/Atom firehose (wire services and political desks) + campaign
transcripts (same parser over per-campaign press-release feeds)."""
from __future__ import annotations

import html
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


_ITEM_BLOCK_RE = re.compile(r"<(item|entry)\b[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
# RSS 1.0 content module: WordPress/pollster feeds put the FULL post body (where
# horse-race toplines actually live) in <content:encoded>, not the teaser
# <description>. Reading it is what lets the deterministic topline parser see the
# numbers at all.
_CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"


def _clean_html(s: str) -> str:
    """Strip tags + unescape entities + collapse whitespace — content:encoded is
    real HTML, and the topline regex wants plain text. Matches what the tolerant
    parser already does, so strict and fallback paths yield the same shape."""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", s or ""))).strip()


def _text_field(block: str, *tag_names: str) -> str:
    for tag in tag_names:
        m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", block, re.IGNORECASE | re.DOTALL)
        if not m:
            continue
        val = m.group(1)
        cdata = _CDATA_RE.search(val)
        if cdata:
            val = cdata.group(1)
        val = html.unescape(_TAG_RE.sub(" ", val))
        return re.sub(r"\s+", " ", val).strip()
    return ""


def _href_field(block: str, tag: str) -> str:
    m = re.search(rf"<{tag}\b[^>]*\bhref=[\"']([^\"']+)[\"']", block, re.IGNORECASE)
    return m.group(1) if m else ""


def _parse_feed_tolerant(raw: bytes) -> list[dict]:
    """Last-resort fallback when strict XML parsing fails even after the BOM/
    entity repair above. The single most common real-world cause is raw,
    non-CDATA-wrapped HTML inside <description>/<content> (unclosed <br>,
    bare &nbsp;, self-closing tags without a slash) — content that a strict
    parser rejects but that a human reader would call a perfectly normal RSS
    item. Rather than build a DOM (which one bad field anywhere kills), this
    extracts each <item>/<entry> block with a tolerant regex and pulls fields
    out of the raw text — a single malformed field can't break the others."""
    text = raw.decode("utf-8", "replace")
    items = []
    for m in _ITEM_BLOCK_RE.finditer(text):
        block = m.group(2)
        link = _text_field(block, "link") or _href_field(block, "link")
        items.append({
            "id": _text_field(block, "guid", "id") or link or _text_field(block, "title"),
            "title": _text_field(block, "title"),
            "link": link,
            "body": _text_field(block, "description", "summary", "content:encoded", "content"),
            "published": _text_field(block, "pubDate", "updated", "published"),
        })
    return items


def parse_feed(raw: bytes) -> list[dict]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        try:
            root = ET.fromstring(_repair_xml(raw))
        except ET.ParseError:
            return _parse_feed_tolerant(raw)
    items = []
    for it in root.iter("item"):  # RSS 2.0
        # prefer content:encoded (full body) over description (teaser)
        body = it.findtext(_CONTENT_NS) or it.findtext("description") or ""
        items.append({
            "id": (it.findtext("guid") or it.findtext("link") or it.findtext("title") or "").strip(),
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "body": _clean_html(body),
            "published": (it.findtext("pubDate") or "").strip(),
        })
    if not items:
        for e in root.iter(f"{_ATOM}entry"):  # Atom — content is fuller than summary
            link = e.find(f"{_ATOM}link")
            body = e.findtext(f"{_ATOM}content") or e.findtext(f"{_ATOM}summary") or ""
            items.append({
                "id": (e.findtext(f"{_ATOM}id") or "").strip(),
                "title": (e.findtext(f"{_ATOM}title") or "").strip(),
                "link": link.get("href", "") if link is not None else "",
                "body": _clean_html(body),
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
