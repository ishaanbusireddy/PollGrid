"""Correlation: second-order links (two races moving together with no obvious
shared cause get flagged for a plausible-common-cause check) and historical-
analog matching against the archive. Embedding similarity is the deterministic
hashing vector from core.util (upgraded transparently if sentence-transformers
is installed); thresholds from config — the LLM never touches them."""
from __future__ import annotations

from core import db
from core.config import cfg
from core.util import cosine, embed, today

try:  # optional heavy dependency, auto-detected
    from sentence_transformers import SentenceTransformer  # type: ignore
    _MODEL = SentenceTransformer("all-MiniLM-L6-v2")

    def _vec(text: str) -> list[float]:
        return list(map(float, _MODEL.encode([text])[0]))
except Exception:
    _vec = embed


def _movement_series(race_id: int, days: int = 21) -> list[float]:
    rows = db.query(
        "SELECT avg_pct FROM poll_averages WHERE race_id=? AND party_code='DEM' "
        "AND as_of >= date('now', ?) ORDER BY as_of", (race_id, f"-{days} days"))
    return [r["avg_pct"] for r in rows]


def _pearson(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = sum((x - ma) ** 2 for x in a) ** 0.5
    db_ = sum((y - mb) ** 2 for y in b) ** 0.5
    return num / (da * db_) if da and db_ else 0.0


def find_second_order_links(as_of: str | None = None) -> int:
    """Highly correlated poll movement between races that share neither state
    nor race type → flagged with a plausible-common-cause note drawn from
    recent facts touching both."""
    as_of = as_of or today()
    threshold = cfg("correlation.same_window_similarity_threshold")
    races = db.query("SELECT DISTINCT race_id id FROM poll_averages")
    series = {r["id"]: _movement_series(r["id"]) for r in races}
    series = {k: v for k, v in series.items() if len(v) >= 5}
    n = 0
    ids = sorted(series)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            ra = db.query_one("SELECT state_fips, race_type FROM races WHERE id=?", (a,))
            rb = db.query_one("SELECT state_fips, race_type FROM races WHERE id=?", (b,))
            if ra["state_fips"] == rb["state_fips"] and ra["race_type"] == rb["race_type"]:
                continue  # obvious shared cause
            corr = _pearson(series[a], series[b])
            if abs(corr) < threshold:
                continue
            if db.query_one("SELECT 1 FROM second_order_links WHERE race_a=? AND race_b=? AND as_of=?",
                            (a, b, as_of)):
                continue
            cause = db.query_one(
                "SELECT summary FROM extracted_facts WHERE race_id IN (?,?) "
                "AND created_at >= datetime('now','-7 days') ORDER BY created_at DESC LIMIT 1", (a, b))
            db.execute("INSERT INTO second_order_links(race_a,race_b,as_of,similarity,status,plausible_cause) "
                       "VALUES(?,?,?,?,?,?)",
                       (a, b, as_of, round(corr, 3), "flagged", cause and cause["summary"]))
            n += 1
    return n


def historical_analogs(race_id: int, limit: int = 5) -> list[dict]:
    """'Which past races looked like this one' — margin-trajectory + partisan-lean
    nearest neighbors from political_history, each carrying its actual outcome."""
    race = db.query_one("SELECT * FROM races WHERE id=?", (race_id,))
    if race is None or not race["state_fips"]:
        return []
    from modeling.fundamentals import partisan_lean
    lean = partisan_lean(race)
    rows = db.query(
        "SELECT * FROM political_history WHERE tier='state' AND office=? AND margin_pct IS NOT NULL "
        "AND NOT (entity_id=? AND cycle_year=?) ORDER BY cycle_year DESC LIMIT 400",
        (race["race_type"], race["state_fips"], race["cycle_year"]))
    scored = []
    for h in rows:
        d = abs((h["dem_pct"] or 0) - (h["rep_pct"] or 0))
        scored.append((abs(d - abs(lean)), h))
    scored.sort(key=lambda t: t[0])
    out = []
    for _, h in scored[:limit]:
        state = db.query_one("SELECT name FROM states WHERE fips_code=?", (h["entity_id"],))
        out.append({"cycle_year": h["cycle_year"], "state": state and state["name"],
                    "office": h["office"], "winner_party": h["winner_party"],
                    "margin_pct": h["margin_pct"], "confidence": h["confidence"]})
    return out


def relevance_rank(query_text: str, facts: list[dict], budget_tokens: int) -> list[dict]:
    """Relevance-ranked truncation for context packs: facts pre-scored by the
    same embedding mechanism, recency the tiebreak only among similar relevance —
    never a blind first-N cutoff."""
    qv = _vec(query_text)
    scored = sorted(facts, key=lambda f: (-round(cosine(qv, _vec(f.get("summary", ""))), 2),
                                          f.get("created_at", "")), )
    out, used = [], 0
    for f in scored:
        cost = max(1, len(f.get("summary", "")) // 4)
        if used + cost > budget_tokens:
            continue
        out.append(f)
        used += cost
    return out
