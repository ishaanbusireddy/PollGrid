"""The Analyst: grounded Q&A over an entity's context pack, Ollama-first.
It reasons over real data — every number in its context pack was computed
before the model ever saw it. Deterministic fallback answer when no provider
is reachable: never an error, honestly labeled."""
from __future__ import annotations

import json

from core import db
from core.util import now_iso
from analyst import llm
from analyst.context_packs import get as get_pack

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


def query(entity_type: str, entity_id: str, question: str, session_id: int | None = None) -> dict:
    pack, was_stale = get_pack(entity_type, entity_id)
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
    history = db.query("SELECT role, content FROM analyst_messages WHERE session_id=? "
                       "ORDER BY id DESC LIMIT 6", (session_id,))
    prompt = (f"{SYSTEM}\n\nCONTEXT PACK:\n{json.dumps(pack, default=str)[:110000]}\n\n"
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
