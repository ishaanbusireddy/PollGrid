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
    pass


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
        raise FetchError(f"HTTP {e.code} for {url.split('?')[0]}") from e
    except Exception as e:
        raise FetchError(f"{type(e).__name__}: {e}") from e


def get_json(url: str, params: dict | None = None, **kw):
    return _json.loads(get(url, params, **kw).decode("utf-8", "replace"))
