"""Race narrative: one LLM call per race update, strict JSON contract, one
retry, then a deterministic templated summary built straight from the numbers.
A devil's-advocate pass may only LOWER stated confidence — enforced by a rank
comparison in code, never by trusting the model's own claim."""
from __future__ import annotations

from core import db
from modeling.averaging import latest_average
from modeling.forecasting import latest as latest_forecast

_CONF_RANK = {"low": 0, "medium": 1, "high": 2}

CONTRACT = ('Return ONLY JSON: {"what_changed": str, "why_it_might_have_changed": str '
            '(grounded in the cited facts, never invented), "what_to_watch": str, '
            '"confidence": "low"|"medium"|"high"}')


def _deterministic(race: dict, avg: dict | None, forecast: dict | None) -> dict:
    parts = []
    if avg:
        margin = avg["parties"].get("DEM", 0) - avg["parties"].get("REP", 0)
        leader = "DEM" if margin > 0 else "REP"
        parts.append(f"{leader} leads the {avg['n_polls']}-poll average by {abs(margin):.1f} pts "
                     f"as of {avg['as_of']}.")
    if forecast:
        parts.append(f"The quantitative model puts DEM at {forecast['dem_prob']:.0%}.")
    if not parts:
        parts.append("No qualifying polls or model output yet for this race.")
    return {"what_changed": " ".join(parts),
            "why_it_might_have_changed": "Deterministic summary — computed from the stored numbers only.",
            "what_to_watch": "New polls, filings, and certified results as they land.",
            "confidence": "low", "generated_by": "deterministic"}


def generate(race_id: int) -> dict:
    race = db.query_one("SELECT * FROM races WHERE id=?", (race_id,))
    if race is None:
        return {}
    avg = latest_average(race_id)
    forecast = latest_forecast(race_id)
    fallback = _deterministic(race, avg, forecast)
    try:
        from analyst.llm import complete_json, provider_available
        if not provider_available():
            return fallback
    except Exception:
        return fallback
    facts = db.query("SELECT id, summary, category FROM extracted_facts WHERE race_id=? "
                     "ORDER BY created_at DESC LIMIT 15", (race_id,))
    prompt = (f"Race: {race['name']}. Poll average: {avg}. Forecast: {forecast}. "
              f"Recent cited facts: {[dict(f) for f in facts]!r}. "
              f"Write the race narrative. {CONTRACT}")
    out = None
    for _ in range(2):  # exactly one retry on malformed output
        out = complete_json(prompt, purpose="race_narrative")
        if out and all(k in out for k in ("what_changed", "why_it_might_have_changed",
                                          "what_to_watch", "confidence")) \
                and out["confidence"] in _CONF_RANK:
            break
        out = None
    if out is None:
        return fallback
    out["generated_by"] = "llm"
    # devil's-advocate: may only lower confidence (rank comparison in code)
    da = complete_json(
        f"Devil's advocate: given the same facts, argue the weakest point of this narrative and state "
        f"whether its confidence should be lower. Narrative: {out!r}. "
        'Return ONLY JSON {"weakens": bool, "revised_confidence": "low"|"medium"|"high"}.',
        purpose="devils_advocate")
    if da and da.get("weakens") and da.get("revised_confidence") in _CONF_RANK:
        if _CONF_RANK[da["revised_confidence"]] < _CONF_RANK[out["confidence"]]:
            out["confidence_pre_devils_advocate"] = out["confidence"]
            out["confidence"] = da["revised_confidence"]
    return out
