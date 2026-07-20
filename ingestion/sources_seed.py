"""Seed the sources table. Every source is a row the scheduler threads re-read
each tick; adapters key off source_type. api_key_env empty = keyless source."""
from __future__ import annotations

import json

from core import db
from ingestion.cvap import DEFAULT_CVAP_URL
from ingestion.pollsters import POLLSTER_FEEDS

# OpenElections deep-archive defaults: ALL 50 states + DC. Coverage is genuinely
# uneven upstream (some state/cycle files 404, others are precinct-only) — the
# tier-2 sync warns-and-continues per state/cycle, so a missing file costs nothing
# and every state that DOES publish gets imported. Battlegrounds lead the list so
# the highest-value states land first under a budget cap.
OPENELECTIONS_DEFAULT_STATES = [
    "GA", "PA", "MI", "WI", "AZ", "NV", "NC", "ME",            # battlegrounds first
    "AL", "AK", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "MD", "MA", "MN", "MS", "MO", "MT", "NE", "NH", "NJ",
    "NM", "NY", "ND", "OH", "OK", "OR", "RI", "SC", "SD", "TN", "TX", "UT", "VT",
    "VA", "WA", "WV", "WY"]
OPENELECTIONS_DEFAULT_CYCLES = [2016, 2018, 2020, 2022, 2024]

TRANSCRIPTS_DEFAULT_FEEDS = ["https://www.whitehouse.gov/briefing-room/feed/"]

SOURCES = [
    # name, source_type, interval_key, url, api_key_env, reliability_tier, active, config
    ("Census ACS", "census", "census", "https://api.census.gov/data", "CENSUS_API_KEY", 1, 1, {}),
    ("BLS economic indicators", "economics", "census", "https://api.bls.gov/publicAPI/v2",
     "BLS_API_KEY", 1, 1, {"series": "LNS14000000"}),
    ("Census CVAP special tabulation", "cvap", "census", "", "", 1, 1,
     {"url": DEFAULT_CVAP_URL, "note": "CVAP is its own special-tabulation product (manual §05)"}),
    ("OpenFEC", "fec", "fec", "https://api.open.fec.gov/v1", "FEC_API_KEY", 1, 1, {}),
    ("Congress.gov", "congress_gov", "congress_gov", "https://api.congress.gov/v3", "CONGRESS_GOV_API_KEY", 1, 1, {}),
    ("Politico", "rss", "news_rss", "https://rss.politico.com/politics-news.xml", "", 2, 1, {"outlet": "Politico"}),
    ("The Hill", "rss", "news_rss", "https://thehill.com/feed/", "", 2, 1, {"outlet": "The Hill"}),
    ("NPR Politics", "rss", "news_rss", "https://feeds.npr.org/1014/rss.xml", "", 1, 1, {"outlet": "NPR"}),
    ("Google News US politics", "rss", "news_rss",
     "https://news.google.com/rss/headlines/section/topic/POLITICS?hl=en-US&gl=US&ceid=US:en", "", 3, 1,
     {"outlet": "Google News (mixed)"}),
    ("The Guardian US politics", "rss", "news_rss",
     "https://www.theguardian.com/us-news/us-politics/rss", "", 2, 1, {"outlet": "The Guardian"}),
    ("CBS News politics", "rss", "news_rss",
     "https://www.cbsnews.com/latest/rss/politics", "", 2, 1, {"outlet": "CBS News"}),
    ("ABC News politics", "rss", "news_rss",
     "https://abcnews.go.com/abcnews/politicsheadlines", "", 2, 1, {"outlet": "ABC News"}),
    ("NBC News politics", "rss", "news_rss",
     "https://feeds.nbcnews.com/nbcnews/public/politics", "", 2, 1, {"outlet": "NBC News"}),
    ("Roll Call", "rss", "news_rss", "https://rollcall.com/feed/", "", 2, 1, {"outlet": "Roll Call"}),
    ("Targeted race search (Google News RSS)", "targeted_search", "targeted_search",
     "https://news.google.com/rss/search", "", 3, 1, {}),
    ("Kalshi markets", "markets", "markets", "https://api.elections.kalshi.com/trade-api/v2", "", 3, 1, {}),
    ("Social signal", "social", "social", "", "SOCIAL_API_KEY", 5, 1, {}),
    ("Campaign transcripts", "transcripts", "news_rss", "", "", 2, 1,
     {"note": "add per-campaign press-release feeds to config_json.feeds",
      "feeds": TRANSCRIPTS_DEFAULT_FEEDS}),
    # Inactive by default: an opt-in extension point for a pollster's own
    # machine-readable CSV feed, not part of the real pipeline (that's the
    # per-shop "<Name> releases" rows below). Left permanently degraded would
    # just be Status-page noise for something nobody configured.
    ("Pollster feeds", "pollster_feed", "polls", "", "", 1, 0,
     {"note": "machine-readable pollster releases; config_json.feeds = [{pollster,url,format}]; "
              "flip is_active=1 once configured", "feeds": []}),
    ("State results (tier 1 native)", "results_native", "results_native", "", "", 1, 1,
     {"note": "per-state feeds via ingestion/results/tier1_native.py; file-drop dir data/results_native/"}),
    ("OpenElections (tier 2)", "results_openelections", "census", "https://raw.githubusercontent.com/openelections", "", 1, 1,
     {"states": OPENELECTIONS_DEFAULT_STATES, "cycles": OPENELECTIONS_DEFAULT_CYCLES}),
    ("AP Elections (tier 3, gated)", "results_ap", "results_native", "https://api.ap.org/v3", "AP_ELECTIONS_API_KEY", 1, 1, {}),
    # PR-wire poll path (addendum §1.2): keyless site-restricted Google News
    # queries over prnewswire/businesswire, budget 'targeted_search'; the
    # adapter lives in ingestion/pollsters.py.
    ("PR wire poll releases", "pr_wire_polls", "targeted_search",
     "https://news.google.com/rss/search", "", 3, 1, {"outlet": "PR wire release"}),
] + [
    # Real pollster release feeds — one sources row per shop so each degrades
    # independently; the outlet name rides in config_json for ingest_poll().
    (f"{name} releases", "pollster_release", "polls", url, "", 1, 1, {"outlet": name})
    for name, url, _prior in POLLSTER_FEEDS
]

# Mixed/global firehoses whose items must pass the deterministic US-relevance
# gate in ingestion/store.py (addendum §4). Trusted US political desks keep
# the default us_domestic=1 and are never gated.
NON_US_DOMESTIC_SOURCES = [
    "Google News US politics",                 # topic firehose — syndicates world coverage
    "Targeted race search (Google News RSS)",  # open web search — same mixed pool
    "The Guardian US politics",                # international desk — its 'US politics' RSS
                                               # carries heavy world coverage; gate it
]

OUTLETS = [
    ("Politico", "https://politico.com", 2, "center"),
    ("The Hill", "https://thehill.com", 2, "center"),
    ("NPR", "https://npr.org", 1, "center-left"),
    ("Associated Press", "https://apnews.com", 1, "center"),
    ("Google News (mixed)", "https://news.google.com", 3, "mixed"),
    ("The Guardian", "https://theguardian.com", 2, "center-left"),
    ("CBS News", "https://cbsnews.com", 2, "center"),
    ("ABC News", "https://abcnews.go.com", 2, "center"),
    ("NBC News", "https://nbcnews.com", 2, "center-left"),
    ("Roll Call", "https://rollcall.com", 2, "center"),
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
        for name in NON_US_DOMESTIC_SOURCES:  # applies on fresh AND existing DBs
            conn.execute("UPDATE sources SET us_domestic=0 WHERE name=?", (name,))
        _upgrade_existing(conn)
    from ingestion.pollsters import seed_pollster_priors
    seed_pollster_priors()


def _upgrade_existing(conn) -> None:
    """One-time upgrades for rows an earlier seed already inserted (INSERT OR
    IGNORE never touches them). Only defaults are filled in — a source someone
    has actually configured is left alone."""
    row = conn.execute("SELECT id, config_json FROM sources WHERE source_type='transcripts'").fetchone()
    if row:
        config = json.loads(row["config_json"] or "{}")
        if not config.get("feeds"):
            config["feeds"] = TRANSCRIPTS_DEFAULT_FEEDS
            conn.execute("UPDATE sources SET is_active=1, config_json=? WHERE id=?",
                         (json.dumps(config), row["id"]))
    row = conn.execute("SELECT id, config_json FROM sources WHERE source_type='results_openelections'").fetchone()
    if row:
        config = json.loads(row["config_json"] or "{}")
        _OLD_DEFAULT_STATES = ["GA", "PA", "MI", "WI", "AZ", "NV", "NC", "ME"]
        # widen an existing DB from the old 8-battleground default to all 50+DC,
        # but never touch a list someone deliberately customized to something else
        if not config.get("states") or config.get("states") == _OLD_DEFAULT_STATES:
            config["states"] = OPENELECTIONS_DEFAULT_STATES
            config["cycles"] = OPENELECTIONS_DEFAULT_CYCLES
            conn.execute("UPDATE sources SET is_active=1, config_json=? WHERE id=?",
                         (json.dumps(config), row["id"]))
    row = conn.execute("SELECT id, config_json FROM sources WHERE source_type='pollster_feed'").fetchone()
    if row:
        config = json.loads(row["config_json"] or "{}")
        if not config.get("feeds"):  # still unconfigured — quiet the permanent-degraded noise
            conn.execute("UPDATE sources SET is_active=0 WHERE id=?", (row["id"],))
    conn.execute("UPDATE sources SET is_active=1 WHERE source_type='cvap' AND is_active=0")
