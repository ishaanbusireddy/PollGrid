"""Hand-rolled router: '/api/races/{id}' patterns → handler(request, **params).
No framework. Handlers return (status, payload) or payload (=200)."""
from __future__ import annotations

import re
from typing import Callable

_routes: list[tuple[str, re.Pattern, Callable]] = []


def route(method: str, pattern: str):
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def deco(fn):
        _routes.append((method.upper(), regex, fn))
        return fn
    return deco


def dispatch(method: str, path: str, req) -> tuple[int, object] | None:
    for m, regex, fn in _routes:
        if m != method.upper():
            continue
        match = regex.match(path)
        if match:
            result = fn(req, **match.groupdict())
            if isinstance(result, tuple):
                return result
            return 200, result
    return None
