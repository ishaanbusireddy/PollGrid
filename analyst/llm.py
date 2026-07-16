"""The LLM boundary, enforced here: local Ollama first (free, private), cloud
providers as automatic fallback. Callers get prose/JSON for narratives,
dossiers, rubric scores, and Analyst answers — nothing in modeling/ imports
this for math. Every generative feature has a deterministic fallback the
pipeline actually exercises when this module returns None.

Concurrency: Ollama serves one generation at a time per loaded model, so the
real design problem is queueing. Two-lane priority: a background generation
yields the lane if an interactive request is already waiting, and an interactive
request signals _bg_cancel so a background result is discarded rather than used.
Note: with stream:False the background HTTP call can't be interrupted mid-flight
(urlopen only returns once generation completes), so an interactive request can
still wait out one in-progress background generation — the cancel prevents a
stale result, it does not drop the socket. Streaming would be needed for true
mid-generation preemption."""
from __future__ import annotations

import json as _json
import os
import re
import threading
import urllib.error
import urllib.request

from core.config import cfg

_ollama_lock = threading.Lock()          # one generation at a time per model
_interactive_waiting = threading.Event()  # background calls check this and yield
_bg_cancel = threading.Event()


def _ollama_reachable() -> bool:
    try:
        req = urllib.request.Request(cfg("llm_provider.ollama.host") + "/api/tags")
        with urllib.request.urlopen(req, timeout=1.5):
            return True
    except Exception:
        return False


def provider_available() -> bool:
    if _ollama_reachable():
        return True
    return any(_cloud_key(p) for p in cfg("llm_provider.fallback_order"))


def current_provider() -> dict:
    if _ollama_reachable():
        return {"provider": "ollama_local", "model": cfg("llm_provider.ollama.default_model"), "reachable": True}
    for p in cfg("llm_provider.fallback_order"):
        if _cloud_key(p):
            return {"provider": p, "model": _CLOUD[p][1], "reachable": True}
    return {"provider": None, "model": None, "reachable": False}


def _cloud_key(provider: str) -> str | None:
    env = {"groq_free": "GROQ_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
           "openrouter": "OPENROUTER_API_KEY"}.get(provider)
    return os.environ.get(env or "") or None


# provider -> (endpoint, default model, header builder)
_CLOUD = {
    "groq_free": ("https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile",
                  lambda k: {"Authorization": f"Bearer {k}"}),
    "anthropic": ("https://api.anthropic.com/v1/messages", "claude-haiku-4-5-20251001",
                  lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01"}),
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "meta-llama/llama-3.3-70b-instruct",
                   lambda k: {"Authorization": f"Bearer {k}"}),
}


def _post(url: str, payload: dict, headers: dict, timeout: float = 120.0,
          cancel: threading.Event | None = None) -> dict | None:
    data = _json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if cancel is not None and cancel.is_set():
                return None
            return _json.loads(resp.read().decode())
    except Exception:
        return None


def _stream_ollama(model: str, prompt: str, timeout: float,
                   cancel: threading.Event | None) -> str | None:
    """Streamed /api/generate: reads Ollama's newline-delimited JSON and checks the
    cancel flag BETWEEN chunks — so a background generation is genuinely dropped the
    moment an interactive request arrives (real two-lane preemption, which stream:False
    could never do), and a long generation is interruptible instead of a 300s block."""
    data = _json.dumps({"model": model, "prompt": prompt, "stream": True}).encode()
    req = urllib.request.Request(cfg("llm_provider.ollama.host") + "/api/generate",
                                 data=data, headers={"Content-Type": "application/json"})
    chunks: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for line in resp:
                if cancel is not None and cancel.is_set():
                    return None  # closing resp here drops the socket → frees the lock early
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                if obj.get("response"):
                    chunks.append(obj["response"])
                if obj.get("done"):
                    break
    except Exception:
        pass
    return "".join(chunks) or None


def _complete_ollama(prompt: str, interactive: bool) -> str | None:
    if not interactive:
        if _interactive_waiting.is_set():
            return None  # yield the lane
        _bg_cancel.clear()
    else:
        _interactive_waiting.set()
        _bg_cancel.set()  # cancel any in-flight background generation (now real, via streaming)
    # a full context pack is a large prompt for a local model to prefill+generate
    # against; the default 120s _post() timeout is tuned for short rubric/prose
    # calls and is too tight for this — give interactive calls real headroom.
    timeout = cfg("llm_provider.ollama.interactive_timeout_seconds") if interactive else 120.0
    try:
        with _ollama_lock:
            out = _stream_ollama(cfg("llm_provider.ollama.default_model"), prompt, timeout,
                                 None if interactive else _bg_cancel)
            if out is None and interactive:
                out = _stream_ollama(cfg("llm_provider.ollama.fallback_model"), prompt, timeout, None)
            return out
    finally:
        if interactive:
            _interactive_waiting.clear()


def _complete_cloud(provider: str, prompt: str) -> str | None:
    key = _cloud_key(provider)
    if not key:
        return None
    url, model, hdr = _CLOUD[provider]
    if provider == "anthropic":
        out = _post(url, {"model": model, "max_tokens": 1500,
                          "messages": [{"role": "user", "content": prompt}]}, hdr(key))
        if out and out.get("content"):
            return "".join(b.get("text", "") for b in out["content"])
        return None
    out = _post(url, {"model": model, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 1500}, hdr(key))
    try:
        return out["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None


def complete(prompt: str, purpose: str = "general", interactive: bool = False) -> str | None:
    """Ollama-first, cloud fallback chain. Returns None when nothing is
    reachable — callers MUST have a deterministic fallback."""
    if _ollama_reachable():
        text = _complete_ollama(prompt, interactive)
        if text:
            return text
    for p in cfg("llm_provider.fallback_order"):
        text = _complete_cloud(p, prompt)
        if text:
            return text
    return None


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def complete_json(prompt: str, purpose: str = "general", interactive: bool = False) -> dict | None:
    text = complete(prompt, purpose, interactive)
    if not text:
        return None
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        out = _json.loads(m.group(0))
        return out if isinstance(out, dict) else None
    except _json.JSONDecodeError:
        return None
