"""X (Twitter) API v2 — gated exactly like AP Elections (addendum §8 tier 2).

The API is a paid tier, so the source ships disabled: ingestion.social_x.enabled
false → SourceNotConfigured (degraded forever, flat interval, honest health).
Enabled but no bearer token in the configured env var → same. With both, each
tick resolves the configured handles (/2/users/by) and pulls each account's
latest posts (/2/users/{id}/tweets, max_results=10), landing them through the
NORMAL extraction pipeline via ingestion.store.land_raw_item — external_id
'x:{tweet_id}' dedups across ticks, and posts become extracted facts exactly
like any news item. Live-untestable in this sandbox; the payload→item mapping
is unit-tested structurally.
"""
from __future__ import annotations

import json
import os

from core.config import cfg
from ingestion.http import SourceNotConfigured, get_json
from ingestion.scheduler import register
from ingestion.store import land_raw_item

X_API_BASE = "https://api.x.com/2"


def ensure_source() -> None:
    """Idempotent sources row (new adapters own their row). interval_key reuses
    'social'; the key env rides in config (ingestion.social_x.api_key_env)."""
    from core import db
    db.execute(
        "INSERT OR IGNORE INTO sources(name,source_type,interval_key,url,api_key_env,"
        "reliability_tier,is_active,config_json) VALUES(?,?,?,?,?,?,?,?)",
        ("X posts (gated)", "social_x", "social", X_API_BASE, "", 4, 1, json.dumps({})))


def posts_to_items(handle: str, tweets_payload: dict) -> list[tuple]:
    """Pure mapping: one /2/users/{id}/tweets payload → land_raw_item argument
    tuples (external_id, title, url, body, published_at)."""
    out: list[tuple] = []
    for t in tweets_payload.get("data") or []:
        tweet_id = str(t.get("id") or "").strip()
        text = (t.get("text") or "").strip()
        if not tweet_id or not text:
            continue
        out.append((f"x:{tweet_id}",
                    f"@{handle}: {text[:120]}",
                    f"https://x.com/{handle}/status/{tweet_id}",
                    text,
                    t.get("created_at")))
    return out


@register("social_x")
def run(source: dict) -> None:
    if not cfg("ingestion.social_x.enabled"):
        raise SourceNotConfigured(
            "X API disabled (ingestion.social_x.enabled=false) — paid tier, "
            "flip on once a bearer token exists")
    key_env = cfg("ingestion.social_x.api_key_env") or ""
    token = os.environ.get(key_env, "")
    if not token:
        raise SourceNotConfigured(
            f"ingestion.social_x.enabled but no bearer token in ${key_env or '(unset env var)'}")
    handles = [h.strip().lstrip("@") for h in (cfg("ingestion.social_x.handles") or [])
               if h and h.strip()]
    if not handles:
        raise SourceNotConfigured("ingestion.social_x.handles is empty — add tracked handles")

    headers = {"Authorization": f"Bearer {token}"}
    users = get_json(f"{X_API_BASE}/users/by", {"usernames": ",".join(handles)}, headers=headers)
    for u in users.get("data") or []:
        user_id, handle = u.get("id"), (u.get("username") or "").strip()
        if not user_id or not handle:
            continue
        tweets = get_json(f"{X_API_BASE}/users/{user_id}/tweets",
                          {"max_results": 10, "tweet.fields": "created_at"}, headers=headers)
        for external_id, title, url, body, published_at in posts_to_items(handle, tweets):
            land_raw_item(source["id"], external_id, title, url, body, published_at)


try:  # at boot migrate() runs before adapters are imported, so this lands the row;
    ensure_source()  # a bare unit-test import without a migrated DB skips quietly
except Exception:
    pass
