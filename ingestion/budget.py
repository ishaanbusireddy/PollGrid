"""Daily request budgets for anything rate-capped. UTC-day counters in app_meta;
hitting the ceiling raises BudgetExhausted, which the scheduler treats as ok."""
from __future__ import annotations

from datetime import datetime, timezone

from core import db
from core.config import cfg
from ingestion.http import BudgetExhausted


def _key(name: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"budget:{name}:{day}"


def spend(name: str, n: int = 1) -> None:
    ceiling = cfg(f"ingestion.budgets.{name}", None)
    if ceiling is None:
        return
    key = _key(name)
    used = int(db.meta_get(key, "0") or 0)
    if used + n > ceiling:
        raise BudgetExhausted(f"{name}: daily budget {ceiling} exhausted")
    db.meta_set(key, str(used + n))


def remaining(name: str) -> int | None:
    ceiling = cfg(f"ingestion.budgets.{name}", None)
    if ceiling is None:
        return None
    return max(0, ceiling - int(db.meta_get(_key(name), "0") or 0))
