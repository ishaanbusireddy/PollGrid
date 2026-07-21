"""Stdlib HTTP for adapters. urllib honors HTTPS_PROXY/CA env; no requests dep."""
from __future__ import annotations

import json as _json
import urllib.error
import urllib.parse
import urllib.request


class SourceNotConfigured(Exception):
    """Raised when a required API key is absent → health degraded forever, flat interval."""


class BudgetExhausted(Exception):
    """Raised when a source's daily budget is spent → deliberately NOT a failure."""


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status  # HTTP status when known (e.g. 429 rate-limit), else None


_UA = "PollGrid/1.0 (+local research tool)"


def get(url: str, params: dict | None = None, headers: dict | None = None, timeout: float = 20.0) -> bytes:
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        # the response body often carries the actual reason (Census, FEC, etc.
        # all return a plain-text error message on 4xx/5xx) — surface it
        # instead of discarding it, or every downstream JSONDecodeError is a
        # dead end with no way to tell what actually went wrong.
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        detail = f": {body}" if body.strip() else ""
        raise FetchError(f"HTTP {e.code} for {url.split('?')[0]}{detail}", status=e.code) from e
    except Exception as e:
        raise FetchError(f"{type(e).__name__}: {e}") from e


def get_json(url: str, params: dict | None = None, **kw):
    raw = get(url, params, **kw).decode("utf-8", "replace")
    try:
        return _json.loads(raw)
    except _json.JSONDecodeError as e:
        raise FetchError(f"non-JSON response from {url.split('?')[0]}: {raw[:300]!r}") from e
