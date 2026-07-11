"""Managed API keys — the .env-backed settings store (mirrors GlobeGrid's
approach: keys live in the repo-root .env, never in the database, so a shared
or exported pollgrid.db never carries secrets). The Settings UI reads masked
status here and saves through here; a save writes the .env line, applies the
value live to os.environ (no restart), and reports a real working/not-working
test result rather than accepting-and-hoping."""
from __future__ import annotations

import json as _json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from core.config import ROOT

ENV_PATH = os.path.join(ROOT, ".env")

# name -> {label, required, enables, signup, test}
# 'test' selects the live validation strategy in _test_key().
MANAGED_KEYS: dict[str, dict] = {
    "CENSUS_API_KEY": {
        "label": "Census API key (demographics)", "required": False,
        "enables": "Reliable Census ACS demographics at every tier — keyless works but is "
                   "rate-limited and often returns errors under load.",
        "signup": "https://api.census.gov/data/key_signup.html — free, instant.",
        "test": "census"},
    "CONGRESS_GOV_API_KEY": {
        "label": "Congress.gov API key (legislation)", "required": False,
        "enables": "Bill sponsorship and legislative activity ingestion — this source stays "
                   "fully degraded (no legislation facts) without a key.",
        "signup": "https://api.congress.gov/sign-up/ — free, instant.",
        "test": "congress"},
    "FEC_API_KEY": {
        "label": "OpenFEC key (campaign finance)", "required": False,
        "enables": "Higher-rate FEC finance & ad-spend ingestion — DEMO_KEY works but is "
                   "throttled to ~30/hour.",
        "signup": "https://api.open.fec.gov/developers/ — free, instant.",
        "test": "fec"},
    "BLS_API_KEY": {
        "label": "BLS key (economic indicators)", "required": False,
        "enables": "Higher-rate BLS economic data (the fundamentals economic index) — keyless "
                   "works at 25 requests/day.",
        "signup": "https://data.bls.gov/registrationEngine/ — free, instant.",
        "test": "bls"},
    "ANTHROPIC_API_KEY": {
        "label": "Claude API key (Analyst — cloud)", "required": False,
        "enables": "Cloud AI for the Analyst, narratives, and factor scoring when local Ollama "
                   "isn't running. Every AI feature has a deterministic fallback regardless.",
        "signup": "https://console.anthropic.com/settings/keys",
        "test": "anthropic"},
    "GROQ_API_KEY": {
        "label": "Groq key (Analyst — free cloud fallback)", "required": False,
        "enables": "Free, fast cloud AI fallback behind local Ollama — Llama 3.3 70B.",
        "signup": "https://console.groq.com/keys — free, no card.",
        "test": "openai:https://api.groq.com/openai/v1/chat/completions:llama-3.3-70b-versatile"},
    "OPENROUTER_API_KEY": {
        "label": "OpenRouter key (Analyst — free fallback)", "required": False,
        "enables": "Backup cloud AI provider aggregating many free-tier models.",
        "signup": "https://openrouter.ai/keys — free account.",
        "test": "openai:https://openrouter.ai/api/v1/chat/completions:meta-llama/llama-3.3-70b-instruct"},
}

_UA = "PollGrid/1.0 (+local research tool)"


def _mask(value: str) -> str:
    return value[:5] + "…" + value[-3:] if len(value) > 10 else "set"


def status() -> list[dict]:
    out = []
    for name, meta in MANAGED_KEYS.items():
        value = os.environ.get(name, "")
        out.append({"name": name, "label": meta["label"], "required": meta["required"],
                    "enables": meta["enables"], "signup": meta["signup"],
                    "configured": bool(value), "masked": _mask(value) if value else None})
    return out


def write_env_key(name: str, value: str) -> None:
    """Update or append KEY=VALUE in the repo-root .env, preserving everything
    else — the user never hand-edits the file."""
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    pattern = re.compile(rf"^\s*{re.escape(name)}\s*=")
    replaced = False
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{name}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{name}={value}")
    with open(ENV_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def apply_live(name: str, value: str) -> None:
    """Effective without a restart: os.environ is what every adapter reads."""
    os.environ[name] = value


def _http_ok(req: urllib.request.Request, timeout: float = 20.0) -> tuple[bool, str, bytes]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, "", resp.read()
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read()
        except Exception:
            pass
        text = raw.decode("utf-8", "replace").strip()
        try:
            msg = _json.loads(text).get("error", {})
            msg = msg.get("message") if isinstance(msg, dict) else msg
            if msg:
                return False, str(msg)[:200], raw
        except Exception:
            pass
        return False, f"HTTP {e.code}: {text[:160]}" if text else f"HTTP {e.code}", raw
    except Exception as e:
        return False, f"could not reach provider: {e}", b""


def test_key(name: str, value: str) -> tuple[bool, str]:
    """A visible working/not-working check against the real provider."""
    meta = MANAGED_KEYS.get(name)
    if not meta:
        return False, "unknown key"
    kind = meta["test"]
    try:
        if kind == "census":
            url = ("https://api.census.gov/data/2023/acs/acs5?get=NAME,B01003_001E"
                   f"&for=state:06&key={urllib.parse.quote(value)}")
            ok, detail, raw = _http_ok(urllib.request.Request(url, headers={"User-Agent": _UA}))
            if ok and raw.strip().startswith(b"["):
                return True, "key accepted by the Census API"
            return False, detail or f"unexpected response: {raw[:120]!r}"
        if kind == "congress":
            url = f"https://api.congress.gov/v3/bill?api_key={urllib.parse.quote(value)}&limit=1"
            ok, detail, raw = _http_ok(urllib.request.Request(url, headers={"User-Agent": _UA}))
            return (True, "key accepted by the Congress.gov API") if ok else (False, detail)
        if kind == "fec":
            url = f"https://api.open.fec.gov/v1/candidates/?api_key={urllib.parse.quote(value)}&per_page=1"
            ok, detail, raw = _http_ok(urllib.request.Request(url, headers={"User-Agent": _UA}))
            return (True, "key accepted by the OpenFEC API") if ok else (False, detail)
        if kind == "bls":
            body = _json.dumps({"seriesid": ["LNS14000000"], "registrationkey": value}).encode()
            req = urllib.request.Request("https://api.bls.gov/publicAPI/v2/timeseries/data/",
                                         data=body, headers={"Content-Type": "application/json",
                                                             "User-Agent": _UA})
            ok, detail, raw = _http_ok(req)
            if ok and b'"status":"REQUEST_SUCCEEDED"' in raw:
                return True, "key accepted by the BLS API"
            return (False, detail or "BLS rejected the request")
        if kind == "anthropic":
            body = _json.dumps({"model": "claude-haiku-4-5-20251001", "max_tokens": 1,
                                "messages": [{"role": "user", "content": "ping"}]}).encode()
            req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
                                         headers={"content-type": "application/json", "x-api-key": value,
                                                  "anthropic-version": "2023-06-01", "User-Agent": _UA})
            ok, detail, _ = _http_ok(req)
            return (True, "key accepted by the Anthropic API") if ok else (False, detail)
        if kind.startswith("openai:"):
            _, url, model = kind.split(":", 2)
            body = _json.dumps({"model": model, "max_tokens": 1,
                                "messages": [{"role": "user", "content": "ping"}]}).encode()
            req = urllib.request.Request(url, data=body,
                                         headers={"content-type": "application/json",
                                                  "authorization": f"Bearer {value}", "User-Agent": _UA})
            ok, detail, _ = _http_ok(req)
            return (True, "key accepted by the provider") if ok else (False, detail)
    except Exception as e:
        return False, f"test failed: {e}"
    return True, "saved — validated on the next fetch cycle"


def save(name: str, value: str) -> tuple[bool, str]:
    value = (value or "").strip()
    if name not in MANAGED_KEYS or not value:
        return False, "name must be a managed key and value must be non-empty"
    ok, detail = test_key(name, value)
    if ok:
        write_env_key(name, value)
        apply_live(name, value)
    return ok, detail
