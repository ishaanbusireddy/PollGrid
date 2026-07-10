"""Small shared helpers: time, ids, token estimates, deterministic text vectors."""
from __future__ import annotations

import hashlib
import math
import re
import uuid
from datetime import datetime, timezone, date


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return date.today().isoformat()


def new_metric_id(kind: str) -> str:
    return f"{kind}:{uuid.uuid4().hex[:12]}"


def est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


_WORD = re.compile(r"[a-z][a-z'\-]+")
_DIMS = 256


def embed(text: str) -> list[float]:
    """Deterministic hashing embedding: no model, no network, stable forever.
    Used by correlation/relevance ranking; upgraded transparently if
    sentence-transformers is ever installed (auto-detect in modeling/correlation)."""
    vec = [0.0] * _DIMS
    for w in _WORD.findall(text.lower()):
        h = int.from_bytes(hashlib.blake2b(w.encode(), digest_size=8).digest(), "big")
        vec[h % _DIMS] += 1.0 if (h >> 63) else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
