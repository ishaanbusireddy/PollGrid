"""Seed the sources table. Every source is a row the scheduler threads re-read
each tick; adapters key off source_type. api_key_env empty = keyless source."""
from __future__ import annotations

import json

from core import db

SOURCES = [
    # name, source_type, interval_key, url, api_key_env, reliability_tier, active, config
    ("Census ACS", "census", "census", "https://api.census.gov/data", "CENSUS_API_KEY", 1, 1, {}),
    ("OpenFEC", "fec", "fec", "https://api.open.fec.gov/v1", "FEC_API_KEY", 1, 1, {}),
    ("Congress.gov", "congress_gov", "congress_gov", "https://api.congress.gov/v3", "CONGRESS_GOV_API_KEY", 1, 1, {}),
    ("Politico", "rss", "news_rss", "https://rss.politico.com/politics-news.xml", "", 2, 1, {"outlet": "Politico"}),
    ("The Hill", "rss", "news_rss", "https://thehill.com/feed/", "", 2, 1, {"outlet": "The Hill"}),
    ("NPR Politics", "rss", "news_rss", "https://feeds.npr.org/1014/rss.xml", "", 1, 1, {"outlet": "NPR"}),
    ("Targeted race search (Google News RSS)", "targeted_search", "targeted_search",
     "https://news.google.com/rss/search", "", 3, 1, {}),
    ("Kalshi markets", "markets", "markets", "https://api.elections.kalshi.com/trade-api/v2", "", 3, 1, {}),
    ("Social signal", "social", "social", "", "SOCIAL_API_KEY", 5, 1, {}),
    ("Campaign transcripts", "transcripts", "news_rss", "", "", 2, 0,
     {"note": "configure per-campaign press-release feeds in config_json.feeds"}),
    ("Pollster feeds", "pollster_feed", "polls", "", "", 1, 1,
     {"note": "machine-readable pollster releases; config_json.feeds = [{pollster,url,format}]", "feeds": []}),
    ("State results (tier 1 native)", "results_native", "results_native", "", "", 1, 1,
     {"note": "per-state feeds via ingestion/results/tier1_native.py; file-drop dir data/results_native/"}),
    ("OpenElections (tier 2)", "results_openelections", "census", "https://raw.githubusercontent.com/openelections", "", 1, 0,
     {"states": [], "cycles": []}),
    ("AP Elections (tier 3, gated)", "results_ap", "results_native", "https://api.ap.org/v3", "AP_ELECTIONS_API_KEY", 1, 1, {}),
]

OUTLETS = [
    ("Politico", "https://politico.com", 2, "center"),
    ("The Hill", "https://thehill.com", 2, "center"),
    ("NPR", "https://npr.org", 1, "center-left"),
    ("Associated Press", "https://apnews.com", 1, "center"),
    ("Google News (mixed)", "https://news.google.com", 3, "mixed"),
]


def seed() -> None:
    with db.write() as conn:
        for name, stype, ikey, url, key_env, tier, active, config in SOURCES:
            conn.execute(
                "INSERT OR IGNORE INTO sources(name,source_type,interval_key,url,api_key_env,"
                "reliability_tier,is_active,config_json) VALUES(?,?,?,?,?,?,?,?)",
                (name, stype, ikey, url, key_env, tier, active, json.dumps(config)))
        for name, url, tier, leaning in OUTLETS:
            conn.execute("INSERT OR IGNORE INTO media_outlets(name,url,reliability_tier,leaning) VALUES(?,?,?,?)",
                         (name, url, tier, leaning))
