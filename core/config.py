"""Config loader: config.yaml + .env, no PyYAML dependency.

Hand-rolled minimal YAML subset parser — mappings, scalars, inline lists,
comments, 2-space indentation. That is the entire subset config.yaml uses;
anything fancier is a config bug and should fail loudly anyway.
"""
from __future__ import annotations

import os
import re
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")


def _scalar(raw: str) -> Any:
    s = raw.strip()
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [] if not inner else [_scalar(p) for p in inner.split(",")]
    if s.startswith(("'", '"')) and s.endswith(("'", '"')) and len(s) >= 2:
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d*\.\d+(e-?\d+)?", low):
        return float(s)
    return s


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def parse_yaml(text: str) -> dict:
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for lineno, rawline in enumerate(text.splitlines(), 1):
        line = _strip_comment(rawline)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if ":" not in line:
            raise ValueError(f"config.yaml:{lineno}: expected 'key: value', got {rawline!r}")
        key, _, rest = line.strip().partition(":")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if rest.strip() == "":
            child: dict = {}
            parent[key.strip()] = child
            stack.append((indent, child))
        else:
            parent[key.strip()] = _scalar(rest)
    return root


def _load_env() -> None:
    for name in (".env",):
        path = os.path.join(ROOT, name)
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def load_config() -> dict:
    _load_env()
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return parse_yaml(fh.read())


CONFIG = load_config()


def cfg(path: str, default: Any = KeyError) -> Any:
    """cfg('ingestion.resilience.max_backoff_seconds') — raises loudly on a missing
    key rather than inventing a default, unless one is explicitly passed."""
    node: Any = CONFIG
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            if default is KeyError:
                raise KeyError(f"missing config key: {path}")
            return default
        node = node[part]
    return node
