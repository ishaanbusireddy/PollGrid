"""Thread-per-source scheduler. One daemon thread per active source; each tick
re-reads that source's own DB row, so toggling a source off or editing its
interval takes effect next cycle with no restart. Threads wait on a shared
stop-event (immediate shutdown), never a bare sleep.

Failure isolation (§Ingestion layer):
  outcome                health                        next interval
  success                ok, failure counter reset     base
  missing API key        degraded forever              flat, capped — never escalates
  daily budget spent     ok — deliberately not failure flat, capped
  any other exception    degraded, down after N        exponential, capped
"""
from __future__ import annotations

import threading
import traceback

from core import db
from core.config import cfg
from core.util import now_iso
from ingestion.http import BudgetExhausted, SourceNotConfigured

stop_event = threading.Event()
_threads: dict[int, threading.Thread] = {}

# source_type -> callable(source_row) registered by adapters at import time
ADAPTERS: dict[str, "callable"] = {}


def register(source_type: str):
    def deco(fn):
        ADAPTERS[source_type] = fn
        return fn
    return deco


def _interval_for(source: dict) -> float:
    key = source["interval_key"]
    base = cfg(f"ingestion.intervals_seconds.{key}")
    if key == "results_native" and db.meta_get("election_night_mode") == "1":
        return cfg("ingestion.election_night.results_native_seconds")
    # FEC roster catch-up: until the candidate roster has been walked once
    # (fec_roster_synced_at), drain pages back-to-back so race_candidates — which
    # poll resolution requires — fills in minutes instead of ~1 page/hour. Reverts
    # to the hourly base the instant the roster completes.
    if key == "fec" and not db.meta_get("fec_roster_synced_at"):
        return cfg("ingestion.intervals_seconds.fec_catchup")
    return base


def _set_health(source_id: int, health: str, failures: int, error: str | None) -> None:
    db.execute("UPDATE sources SET health=?, consecutive_failures=?, last_error=?, last_run_at=? WHERE id=?",
               (health, failures, error, now_iso(), source_id))


def _source_loop(source_id: int) -> None:
    multiplier = cfg("ingestion.resilience.backoff_multiplier")
    max_backoff = cfg("ingestion.resilience.max_backoff_seconds")
    down_after = cfg("ingestion.resilience.down_after_failures")
    while not stop_event.is_set():
        source = db.query_one("SELECT * FROM sources WHERE id=?", (source_id,))
        if source is None:
            return
        if not source["is_active"]:
            stop_event.wait(30)
            continue
        base = _interval_for(source)
        failures = source["consecutive_failures"]
        adapter = ADAPTERS.get(source["source_type"])
        try:
            if adapter is None:
                raise SourceNotConfigured(f"no adapter registered for {source['source_type']}")
            adapter(source)
            failures = 0
            _set_health(source_id, "ok", 0, None)
            interval = base
        except SourceNotConfigured as e:
            _set_health(source_id, "degraded", failures, str(e))
            interval = max_backoff                      # flat, capped — never escalates
        except BudgetExhausted as e:
            _set_health(source_id, "ok", failures, str(e))  # deliberately not a failure
            interval = max_backoff
        except Exception as e:
            from ingestion.http import FetchError
            # A rate-limit (429) or transient 503 is NOT a source failure — it means
            # "you're going too fast." Stay healthy, wait it out, and resume, rather
            # than marking the source down (which is what silently killed FEC when the
            # catch-up cadence hammered it). No failure-count escalation.
            if isinstance(e, FetchError) and getattr(e, "status", None) in (429, 503):
                _set_health(source_id, "ok", failures,
                            f"rate-limited (HTTP {e.status}) — backing off, will resume")
                interval = min(max(base * 6, 300), max_backoff)
            else:
                failures += 1
                health = "degraded" if failures < down_after else "down"
                _set_health(source_id, health, failures, f"{type(e).__name__}: {e}")
                interval = min(base * (multiplier ** failures), max_backoff)
                if not isinstance(e, FetchError):  # network flake is noise; real bugs get a trace
                    traceback.print_exc()
        stop_event.wait(interval)


def start_all() -> int:
    import ingestion.adapters  # noqa: F401 — registers every adapter
    started = 0
    for source in db.query("SELECT id FROM sources WHERE is_active=1"):
        sid = source["id"]
        if sid in _threads and _threads[sid].is_alive():
            continue
        t = threading.Thread(target=_source_loop, args=(sid,), daemon=True, name=f"src-{sid}")
        t.start()
        _threads[sid] = t
        started += 1
    return started


def shutdown() -> None:
    stop_event.set()
