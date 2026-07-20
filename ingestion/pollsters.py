"""Poll ingestion. Polls need no NLP — they parse straight into the chained
polls fact. The adapter framework normalizes any configured machine-readable
pollster feed (CSV/JSON, spec in sources.config_json.feeds) into one Poll
shape: candidate/party, race, sample size, MoE, field dates, population
(lv/rv/a), methodology, release URL. ingest_poll() is also the single entry
point used by the manual-entry API route and scripts/seed_demo.py.

Two adapter paths:
  pollster_feed    — machine-readable CSV feeds (rare; config_json.feeds).
  pollster_release — real release/blog RSS feeds from the highest-quality,
    methodologically-transparent shops (POLLSTER_FEEDS below; each is its own
    sources row, so a wrong URL degrades only that shop). Releases land in
    raw_items via the shared store path and flow through extraction; when an
    item extracts as a polling fact WITH a matched race, a deterministic,
    deliberately conservative topline parser (parse_release_toplines) may
    additionally yield a real poll row — never fabricating: no race match or
    fewer than two resolvable toplines means skip silently."""
from __future__ import annotations

import csv
import email.utils
import io
import json
import re

from core import db, provenance
from core.util import now_iso, today
from ingestion.http import SourceNotConfigured, get
from ingestion.scheduler import register
from ingestion.store import land_raw_item

# The release feeds promised by the manual + addendum §1.1 breadth. Best-known
# URLs; each is seeded as its OWN sources row (see ingestion/sources_seed.py),
# so a moved or wrong feed degrades that single source and nothing else — that
# is the design. Third field: a DECLARED house-effect prior in Dem lean points
# (positive = shop's toplines historically run Dem-favorable), from documented
# public reputations (538/RCP-style house-effect write-ups). The prior is
# written to pollster_ratings with grade='prior' at seed time and is superseded
# by any real grading row (newer as_of) the moment one exists.
POLLSTER_FEEDS: list[tuple[str, str, float]] = [
    ("Marist", "https://maristpoll.marist.edu/feed/", 0.0),
    ("Monmouth", "https://www.monmouth.edu/polling-institute/feed/", 0.0),
    ("Emerson College Polling", "https://emersoncollegepolling.com/feed/", 0.0),
    ("Quinnipiac", "https://poll.qu.edu/rss", 0.0),
    ("Pew Research Politics", "https://www.pewresearch.org/politics/feed/", 0.0),
    ("AP-NORC", "https://apnorc.org/feed/", 0.0),
    ("Gallup News", "https://news.gallup.com/rss/topic/all_gallup_headlines.aspx", 0.0),
    ("Marquette Law Poll", "https://law.marquette.edu/poll/feed/", 0.0),
    ("Siena College Research Institute", "https://scri.siena.edu/feed/", 0.0),
    ("YouGov US politics", "https://today.yougov.com/rss/topics/politics", 0.0),
    ("Data for Progress", "https://www.dataforprogress.org/blog?format=rss", 1.0),
    ("Civiqs", "https://civiqs.com/blog/feed", 0.5),
    # addendum §1.1 breadth — declared priors per documented public reputation
    ("Public Policy Polling", "https://www.publicpolicypolling.com/feed/", 1.0),
    ("Rasmussen Reports", "https://www.rasmussenreports.com/public_content/politics/rss", -1.5),
    ("Morning Consult", "https://morningconsult.com/feed/", 0.0),
    ("Ipsos US", "https://www.ipsos.com/en-us/rss.xml", 0.0),
    ("Fox News Poll", "https://moxie.foxnews.com/google-publisher/politics.xml", 0.0),
    ("Suffolk University Political Research Center", "https://www.suffolk.edu/news-features/feed", 0.0),
    ("Trafalgar Group", "https://www.thetrafalgargroup.org/feed/", -1.5),
    ("InsiderAdvantage", "https://insideradvantage.com/feed/", -1.0),
    ("Cygnal", "https://www.cygn.al/feed/", -0.5),
    ("co/efficient", "https://coefficient.org/feed/", -0.5),
    ("Big Data Poll", "https://bigdatapoll.com/feed/", -1.0),
    ("Echelon Insights", "https://echeloninsights.com/feed/", 0.0),
    ("Beacon Research / Shaw & Company", "https://beaconresearch.com/feed/", 0.0),
    ("Split Ticket", "https://split-ticket.org/feed/", 0.0),
    ("Noble Predictive Insights", "https://noblepredictiveinsights.com/feed/", 0.0),
]

# A prior's as_of is a fixed sentinel far in the past, so ANY real grading row
# (modeling/pollster_ratings.refresh writes as_of=today) is newer and wins in
# every latest-as_of lookup; INSERT OR IGNORE keeps re-seeding idempotent.
PRIOR_AS_OF = "2000-01-01"


def seed_pollster_priors() -> None:
    """Write each feed's declared house-effect prior into pollster_ratings:
    grade='prior', n_graded=0, weight_multiplier=1.0, region='national'."""
    with db.write() as conn:
        for name, url, prior in POLLSTER_FEEDS:
            row = conn.execute("SELECT id FROM pollsters WHERE name=?", (name,)).fetchone()
            pid = row["id"] if row else conn.execute(
                "INSERT INTO pollsters(name,url) VALUES(?,?)", (name, url)).lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO pollster_ratings(pollster_id,as_of,n_graded,grade,"
                "house_effect_dem,weight_multiplier,region) VALUES(?,?,0,'prior',?,1.0,'national')",
                (pid, PRIOR_AS_OF, prior))


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


# --------------------------------------------------------------------------
# Deterministic topline parser. Deliberately conservative: percentages 1–99,
# at most 5 accepted numbers (more smells like a crosstab dump — bail), and a
# number only counts when a capitalized name or party word co-occurs within
# 40 characters. No LLM anywhere near this path — regexes only.
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")

_PARTY_WORDS = {
    "dem": "DEM", "dems": "DEM", "democrat": "DEM", "democrats": "DEM", "democratic": "DEM",
    "rep": "REP", "republican": "REP", "republicans": "REP", "gop": "REP",
}

_TITLE_PREFIX = re.compile(
    r"^(?:Gov|Sen|Rep|Sec|Pres|President|Senator|Governor|Dr|Mr|Ms|Mrs)\.?\s+")

# capitalized tokens that are never a topline key on their own
_NOISE = {
    "the", "a", "an", "and", "in", "on", "at", "of", "for", "with", "but", "while", "however",
    "he", "she", "it", "they", "we", "who", "his", "her", "their", "new", "latest", "today",
    "poll", "polls", "polling", "pollster", "survey", "voters", "adults", "respondents",
    "approval", "disapproval", "favorability", "margin", "error", "percent", "points", "point",
    "among", "about", "nearly", "overall", "according", "just", "likely", "registered",
    "undecided", "other", "others", "sampling", "race", "lead", "leads", "school", "law",
    "university", "college", "institute", "center", "research", "finds", "shows",
}

_NAME_TOKEN = r"[A-Z][A-Za-z'’.\-]+"
_NAME = rf"((?:{_NAME_TOKEN}\s+)?{_NAME_TOKEN})"
_PCT = r"(?<![\d.])(\d{1,2})(?:\.\d+)?(?!\d)\s*(?:%|percent\b)"

# "Smith 48%", "Biden at 44%", "Whitmer with 51 percent", "Smith: 48%"
_PAT_NAME_PCT = re.compile(
    _NAME + r"(?:\s+(?:is|now|stands|polls?))?(?:\s+(?:at|with)|:|,)?\s+" + _PCT)

# "48% - 45%", "48% to 45%", "leads 48-45" (names resolved by 40-char lookback)
_PAT_PAIR = re.compile(
    r"(?<![\d.])(\d{1,2})(?:\.\d+)?(?!\d)\s*%?\s*(?:-|–|—|\bto\b)\s*"
    r"(?<![\d.])(\d{1,2})(?:\.\d+)?(?!\d)\s*(%|percent\b)?")

_LEAD_WORDS = re.compile(r"\b(leads?|leading|led|ahead|edges?|tops?|trails?|behind)\b", re.I)
_NAME_ONLY = re.compile(_NAME)


def _normalize(text: str) -> str:
    import html as _html
    text = _html.unescape(text or "")
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_name(raw: str) -> str | None:
    name = _TITLE_PREFIX.sub("", raw.strip()).strip()
    toks = [t for t in (re.sub(r"[’']s$", "", w.strip(".,'’-")) for w in name.split()) if t]
    while toks and toks[0].lower() in _NOISE:   # "The Democrats" → "Democrats"
        toks = toks[1:]
    if not toks:
        return None
    if all(t.lower() in _NOISE for t in toks):
        return None
    if len(toks) == 1 and len(toks[0]) < 2:
        return None
    return " ".join(toks)


def _pair_names(window: str) -> list[str]:
    """Names for a bare 'NN-MM' pair, from the 40-char lookback window. When a
    lead-word is present the name before it is the leader (first number) and a
    name after it the trailer; otherwise the last two names in order."""
    names = [n for n in (_clean_name(m.group(1)) for m in _NAME_ONLY.finditer(window)) if n]
    lead = None
    for m in _LEAD_WORDS.finditer(window):
        lead = m
    if lead:
        before = [n for n in (_clean_name(m.group(1))
                              for m in _NAME_ONLY.finditer(window[:lead.start()])) if n]
        after = [n for n in (_clean_name(m.group(1))
                             for m in _NAME_ONLY.finditer(window[lead.end():])) if n]
        out = ([before[-1]] if before else []) + ([after[-1]] if after else [])
        return out
    return names[-2:]


def parse_release_toplines(title: str, body: str) -> list[dict[str, float]]:
    """Parse horse-race toplines out of a release title+body. Returns a list of
    single-key dicts [{name_or_party: pct}, ...] in order of appearance; [] when
    nothing parses cleanly (the caller must then skip silently, never guess)."""
    text = _normalize(f"{title or ''}. {body or ''}")
    consumed: list[tuple[int, int]] = []
    entries: list[tuple[str, float]] = []

    for m in _PAT_PAIR.finditer(text):
        p1, p2 = int(m.group(1)), int(m.group(2))
        if not (1 <= p1 <= 99 and 1 <= p2 <= 99):
            continue
        window = text[max(0, m.start() - 40):m.start()]
        if "%" not in m.group(0) and not m.group(3) and not _LEAD_WORDS.search(window):
            continue  # a bare NN-MM with no % needs a lead-verb to count
        names = _pair_names(window)
        if not names:
            continue  # no co-occurring name/party within 40 chars
        consumed.append((m.start(), m.end()))
        for name, pct in zip(names, (p1, p2)):
            entries.append((name, float(pct)))

    for m in _PAT_NAME_PCT.finditer(text):
        if any(s < m.end() and m.start() < e for s, e in consumed):
            continue  # already handled as part of a pair
        pct = int(m.group(2))
        if not (1 <= pct <= 99):
            continue
        name = _clean_name(m.group(1))
        if not name:
            continue
        # "Smith leads Jones 48%": adjacency lies — 48 belongs to the subject,
        # not the nearest name. Conservative: skip rather than misattribute.
        if _LEAD_WORDS.search(text[max(0, m.start() - 12):m.start()]):
            continue
        entries.append((name, float(pct)))

    out: list[dict[str, float]] = []
    seen: set[str] = set()
    for name, pct in entries:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({name: pct})
    if len(out) > 5:
        return []  # crosstab dump, not a topline — refuse the lot
    return out


def _resolve_toplines(race_id: int, entries: list[dict[str, float]]) -> dict[str, float] | None:
    """Canonicalize parsed keys against the race's actual candidates (full or
    unambiguous last name) or party words. <2 resolved → None: never fabricate
    a poll from a partial parse."""
    by_full: dict[str, str] = {}
    by_last: dict[str, str | None] = {}          # None marks an ambiguous surname
    for c in db.query(
            "SELECT c.name FROM race_candidates rc JOIN candidates c ON c.id=rc.candidate_id "
            "WHERE rc.race_id=?", (race_id,)):
        full = c["name"].strip()
        by_full[full.lower()] = full
        last = full.split()[-1].lower()
        if len(last) > 2:
            by_last[last] = None if last in by_last else full
    results: dict[str, float] = {}
    for entry in entries:
        (key, pct), = entry.items()
        kl = key.strip().lower()
        if kl in _PARTY_WORDS:
            results.setdefault(_PARTY_WORDS[kl], pct)
            continue
        canon = by_full.get(kl) or by_last.get(kl.split()[-1])
        if canon:
            results.setdefault(canon, pct)
    return results if len(results) >= 2 else None


def _published_date(published: str | None) -> str:
    if published:
        try:
            return email.utils.parsedate_to_datetime(published).date().isoformat()
        except Exception:
            pass
        m = re.search(r"\d{4}-\d{2}-\d{2}", published)
        if m:
            return m.group(0)
    return today()


def _maybe_ingest_toplines(source: dict, outlet: str, raw_item_id: int, item: dict) -> None:
    """Only after extraction produced a polling-category fact WITH a matched
    race: attempt the conservative topline parse; anything short of two
    resolved entries is a silent skip."""
    fact = db.query_one(
        "SELECT category, race_id FROM extracted_facts WHERE raw_item_id=? ORDER BY id DESC LIMIT 1",
        (raw_item_id,))
    if not fact or fact["category"] != "polling" or not fact["race_id"]:
        return
    entries = parse_release_toplines(item.get("title") or "", item.get("body") or "")
    if not entries:
        return
    results = _resolve_toplines(fact["race_id"], entries)
    if not results:
        return
    day = _published_date(item.get("published"))
    ingest_poll(pollster=outlet, race_id=fact["race_id"], field_start=day, field_end=day,
                results=results, sample_size=None, population=None, moe=None,
                methodology="auto-parsed from release", release_url=item.get("link") or None,
                source_id=source["id"])


@register("pollster_release")
def run_releases(source: dict) -> None:
    """Real pollster release RSS → raw_items via the shared store path (so
    releases flow through extraction as polling facts), then the topline parse."""
    from ingestion.rss import parse_feed  # deferred: keeps the import graph flat
    conf = json.loads(source["config_json"] or "{}")
    outlet = conf.get("outlet") or source["name"]
    for item in parse_feed(get(source["url"])):
        if not item["id"]:
            continue
        rid = land_raw_item(source["id"], item["id"], item["title"], item["link"],
                            item["body"], item["published"], source_row=source)
        if rid is None:
            continue  # dedup — already seen
        _maybe_ingest_toplines(source, outlet, rid, item)


# --------------------------------------------------------------------------
# PR-wire poll path (addendum §1.2). Registered here (not adapters.py) so the
# whole path lives in one module. Per active competitive race, a keyless
# site-restricted Google News RSS query over the two big US press-release
# wires; hits land via the shared store path and then flow through the SAME
# conservative topline parser as pollster releases — same never-fabricate
# rules, same silent skip.
# --------------------------------------------------------------------------

_PR_WIRE_SITES = "(site:prnewswire.com OR site:businesswire.com)"


def _competitive_races() -> list[dict]:
    return db.query(
        "SELECT id, name FROM races WHERE competitiveness IN ('tossup','lean') "
        "AND status IN ('upcoming','live') ORDER BY CASE competitiveness "
        "WHEN 'tossup' THEN 0 ELSE 1 END, id")


@register("pr_wire_polls")
def run_pr_wire(source: dict) -> None:
    import urllib.parse

    from ingestion import budget
    from ingestion.http import FetchError
    from ingestion.rss import parse_feed as _parse  # deferred: keeps the import graph flat

    conf = json.loads(source["config_json"] or "{}")
    outlet = conf.get("outlet") or source["name"]
    for race in _competitive_races():
        budget.spend("targeted_search")  # shared keyless Google News RSS budget
        query = f'"{race["name"]}" poll {_PR_WIRE_SITES}'
        url = f"{source['url']}?q={urllib.parse.quote(query)}&hl=en-US&gl=US&ceid=US:en"
        try:
            items = _parse(get(url))
        except FetchError as e:
            if "429" in str(e) or "403" in str(e):
                raise  # scheduler marks degraded; never retried in a tight loop
            items = []
        for item in items[:20]:
            if not item["id"]:
                continue
            rid = land_raw_item(source["id"], item["id"], item["title"], item["link"],
                                item["body"], item["published"], source_row=source)
            if rid is None:
                continue
            _maybe_ingest_toplines(source, outlet, rid, item)


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
        for row in csv.DictReader(io.StringIO(raw, newline="")):  # newline="" — tolerate lone \r
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
