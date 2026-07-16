"""The Analyst: grounded Q&A over an entity's context pack, Ollama-first.
It reasons over real data — every number in its context pack was computed
before the model ever saw it. Deterministic fallback answer when no provider
is reachable: never an error, honestly labeled."""
from __future__ import annotations

import json
import re

from core import db
from core.util import now_iso
from analyst import llm
from analyst.context_packs import get as get_pack

# A trivial greeting/pleasantry must NOT trigger a full ~30k-token pack round-trip
# against a local model — that is the "hi runs on and on" bug. These answer locally.
_GREETING_RE = re.compile(
    r"^(hi|hey+|hello|yo|sup|howdy|greetings|hiya|good\s?(morning|afternoon|evening)|"
    r"thanks|thank\s?you|ty|thx|ok|okay|k|cool|nice|great|test|testing|ping)[!.…\s]*$", re.I)


def _is_trivial(question: str) -> bool:
    q = (question or "").strip()
    return not q or bool(_GREETING_RE.match(q)) or len(q) < 3

SYSTEM = (
    "You are PollGrid's Analyst. Answer ONLY from the context pack below. Every number you "
    "repeat must match the pack exactly — you may not invent, extrapolate, or move any "
    "probability, average, or call. Cite what you use as [kind:ref] markers, e.g. "
    "[poll_average:race:12] [demographic:state:06:median_age] [fact:123]. If the pack is thin, "
    "say so plainly. If asked to predict, restate the deterministic forecast and its gate "
    "status; add reasoning about the data but always end with the real stored number.")


def _deterministic_answer(pack: dict, question: str) -> dict:
    lines, cites = [], []
    if pack.get("race"):
        r = pack["race"]
        lines.append(f"{r['name']} (status: {r['status']}).")
        if pack.get("poll_average"):
            a = pack["poll_average"]
            lines.append(f"Poll average as of {a['as_of']}: "
                         + ", ".join(f"{k} {v}%" for k, v in a["parties"].items())
                         + f" over {a['n_polls']} polls.")
            cites.append({"kind": "poll_average", "ref": f"race:{r['id']}", "label": a["as_of"]})
        f = (pack.get("forecast") or {})
        if f.get("row"):
            lines.append(f"Quantitative forecast: DEM {f['row']['dem_prob']:.0%} "
                         f"(visible: {f['visible']} — {f['gate_reason']}).")
            cites.append({"kind": "forecast", "ref": f["row"].get("metric_id", ""), "label": "forecast"})
    for note in pack.get("thin_coverage_notes", []):
        lines.append(f"Note: {note}.")
    if not lines:
        lines.append("No data assembled for this entity yet — coverage is honestly thin.")
    from analyst.llm import provider_available
    reason = ("no LLM provider reachable" if not provider_available() else
              "the reachable provider didn't return a usable answer in time "
              "(a full context pack is a lot for a local model to generate against — "
              "try again, or increase llm_provider.ollama.interactive_timeout_seconds)")
    lines.append(f"(Deterministic answer: {reason}; the numbers above are the "
                 "platform's own stored computations.)")
    return {"answer": " ".join(lines), "citations": cites, "model": "deterministic"}


def _extract_citations(text: str) -> list[dict]:
    import re
    out = []
    for m in re.finditer(r"\[([a-z_]+):([^\]\s]+)\]", text):
        out.append({"kind": m.group(1), "ref": m.group(2), "label": m.group(0)})
    return out


def _entity_name(entity_type: str, entity_id: str) -> str:
    """Cheap name lookup — avoids building the whole context pack just to greet."""
    try:
        if entity_type == "race":
            r = db.query_one("SELECT name FROM races WHERE id=?", (int(entity_id),))
        elif entity_type == "candidate":
            r = db.query_one("SELECT name FROM candidates WHERE id=?", (int(entity_id),))
        elif entity_type == "party":
            r = db.query_one("SELECT name FROM parties WHERE id=? OR code=?", (entity_id, entity_id))
        elif entity_type == "state":
            r = db.query_one("SELECT name FROM states WHERE fips_code=?", (str(entity_id),))
        else:
            r = None
        return (r["name"] if r else None) or "this entity"
    except Exception:
        return "this entity"


def query(entity_type: str, entity_id: str, question: str, session_id: int | None = None) -> dict:
    # never trust a client-supplied session_id blindly — a browser tab can hold
    # one from before a database reset/reseed, and inserting a message against
    # a session row that no longer exists is a FOREIGN KEY crash, not a 500 the
    # user caused. Verify it first; silently start a fresh session if it's gone.
    if session_id is not None and db.query_one(
            "SELECT 1 FROM analyst_sessions WHERE id=?", (session_id,)) is None:
        session_id = None
    if session_id is None:
        session_id = db.execute("INSERT INTO analyst_sessions(started_at,entity_type,entity_id) VALUES(?,?,?)",
                                (now_iso(), entity_type, str(entity_id)))
    db.execute("INSERT INTO analyst_messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
               (session_id, "user", question, now_iso()))
    # greeting fast-path: answer instantly WITHOUT building the (expensive) context
    # pack or calling the LLM — this is the whole fix for "hi runs on and on"
    if _is_trivial(question):
        ent = _entity_name(entity_type, entity_id)
        result = {"answer": f"Hi — I'm PollGrid's Analyst, looking at {ent}. Ask me about its poll "
                            f"average, forecast, demographics, or key factors and I'll answer only "
                            f"from the platform's own cited data.",
                  "citations": [], "model": "greeting"}
        db.execute("INSERT INTO analyst_messages(session_id,role,content,citations_json,model,created_at) "
                   "VALUES(?,?,?,?,?,?)", (session_id, "analyst", result["answer"], "[]", "greeting", now_iso()))
        result["session_id"] = session_id
        result["pack_stale"] = False
        return result
    pack, was_stale = get_pack(entity_type, entity_id)
    history = db.query("SELECT role, content FROM analyst_messages WHERE session_id=? "
                       "ORDER BY id DESC LIMIT 6", (session_id,))
    # scale the context to the question: a short question doesn't need the full
    # ~30k-token pack (which dominates prefill time on a local model)
    pack_chars = 110000 if len(question.strip()) >= 40 else 24000
    prompt = (f"{SYSTEM}\n\nCONTEXT PACK:\n{json.dumps(pack, default=str)[:pack_chars]}\n\n"
              + "".join(f"{m['role']}: {m['content']}\n" for m in reversed(history[1:]))
              + f"user question: {question}\nanalyst:")
    text = llm.complete(prompt, purpose="analyst_qa", interactive=True)
    if text:
        result = {"answer": text.strip(), "citations": _extract_citations(text),
                  "model": llm.current_provider().get("model") or "unknown"}
    else:
        result = _deterministic_answer(pack, question)
    db.execute("INSERT INTO analyst_messages(session_id,role,content,citations_json,model,created_at) "
               "VALUES(?,?,?,?,?,?)",
               (session_id, "analyst", result["answer"], json.dumps(result["citations"]),
                result["model"], now_iso()))
    result["session_id"] = session_id
    result["pack_stale"] = was_stale
    return result
