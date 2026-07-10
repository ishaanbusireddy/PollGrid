"""Generic RSS/Atom firehose (wire services and political desks) + campaign
transcripts (same parser over per-campaign press-release feeds)."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from ingestion.http import SourceNotConfigured, get
from ingestion.scheduler import register
from ingestion.store import land_raw_item

_ATOM = "{http://www.w3.org/2005/Atom}"


def parse_feed(raw: bytes) -> list[dict]:
    root = ET.fromstring(raw)
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
                         item["body"], item["published"]):
            n += 1
    return n


@register("rss")
def run(source: dict) -> None:
    _ingest_feed(source, source["url"])


@register("transcripts")
def run_transcripts(source: dict) -> None:
    feeds = (json.loads(source["config_json"] or "{}")).get("feeds") or []
    if not feeds:
        raise SourceNotConfigured("no campaign press-release feeds configured (sources.config_json.feeds)")
    for url in feeds:
        _ingest_feed(source, url)
