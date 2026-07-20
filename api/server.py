"""HTTP server: stdlib ThreadingHTTPServer serving the buildless frontend, the
REST API, and the hand-rolled WebSocket. Boot order: validate config LOUDLY →
migrate → seed → start ingestion threads + nightly job → serve."""
from __future__ import annotations

import json
import mimetypes
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core.config import CONFIG, ROOT, cfg
from core.config_schema import validate_config

FRONTEND_DIR = os.path.join(ROOT, "frontend")


class Request:
    def __init__(self, handler, query: dict, body: bytes | None):
        self.handler = handler
        self.query = query
        self._body = body

    @property
    def json(self):
        if not self._body:
            return None
        try:
            return json.loads(self._body.decode("utf-8"))
        except json.JSONDecodeError:
            return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PollGrid/1.0"

    def log_message(self, fmt, *args):  # quiet: one line per request is noise at poll cadence
        pass

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_raw(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        rel = path.lstrip("/")
        if path == "/" or not rel:
            rel = "index.html"
        if rel.startswith("static/"):
            full = os.path.join(FRONTEND_DIR, rel)
        else:
            full = os.path.join(FRONTEND_DIR, rel)
        full = os.path.realpath(full)
        if not full.startswith(os.path.realpath(FRONTEND_DIR)) or not os.path.isfile(full):
            # SPA fallback: unknown non-file paths get the shell
            index = os.path.join(FRONTEND_DIR, "index.html")
            if os.path.isfile(index) and "." not in os.path.basename(rel):
                full = index
            else:
                self._send_json(404, {"error": "not found"})
                return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".js"):
            ctype = "text/javascript"
        with open(full, "rb") as fh:
            self._send_raw(200, ctype, fh.read())

    def _dispatch(self, method: str) -> None:
        from api import routes  # noqa: F401 — ensures registration
        from api.router import dispatch
        parsed = urllib.parse.urlparse(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        body = None
        if method == "POST":
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else None
        req = Request(self, query, body)
        try:
            result = dispatch(method, parsed.path, req)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json(500, {"error": f"{type(e).__name__}: {e}"})
            return
        if result is None:
            self._send_json(404, {"error": "no such route"})
            return
        status, payload = result
        if isinstance(payload, tuple) and len(payload) == 2 and payload[0] == "text/csv":
            self._send_raw(status, "text/csv; charset=utf-8", payload[1].encode())
        else:
            self._send_json(status, payload)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/ws/feed":
            from api import websocket
            if websocket.handshake(self):
                self.close_connection = True
                websocket.serve_client(self)
            return
        if path.startswith("/api/"):
            self._dispatch("GET")
        else:
            self._serve_static(path)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/"):
            self._dispatch("POST")
        else:
            self._send_json(404, {"error": "not found"})


class App:
    def __init__(self, server: ThreadingHTTPServer, port: int):
        self._server = server
        self.port = port

    def serve_forever(self):
        self._server.serve_forever()

    def shutdown(self):
        from ingestion.scheduler import shutdown as stop_ingestion
        stop_ingestion()
        self._server.shutdown()


def bootstrap(start_ingestion: bool = False) -> None:
    """Everything up to (not including) binding the port: validate config
    loudly, migrate, seed, optionally start ingestion + nightly threads."""
    validate_config(CONFIG)  # fails the whole process loudly before migrate() runs

    from core import db
    db.migrate()

    from domain import entities, geography, influence, races
    geography.seed()
    entities.seed()
    races.seed()
    influence.seed()
    # real electoral baselines seed at BOOT (the GlobeGrid pattern): hand-seeded
    # certified national history + transcribed state presidential toplines, both
    # INSERT OR IGNORE and zero-network — so the map colors on first run without
    # any script, key, or fetch. Certified imports supersede the state rows.
    from scripts.backfill_history import run as seed_national_history
    from scripts.seed_state_presidentials import run as seed_state_history
    seed_national_history()
    seed_state_history()
    # current officeholders (50 governors + 100 senators, hand-transcribed) and
    # the 2026 election calendar — zero-network; the Congress.gov member sync
    # refreshes the roster + adds all 435 House reps once its key is present
    from scripts.seed_officeholders import run as seed_officeholders
    seed_officeholders()
    from ingestion import sources_seed
    sources_seed.seed()

    # Loudly report which API keys the .env/env actually delivered — the fix for
    # "I added a key and nothing happened": you can now SEE it was picked up (a
    # hand-edited .env only takes effect on restart, which this print confirms).
    from core import keys as _keys
    _active = [k["name"] for k in _keys.status() if k["configured"]]
    if _active:
        print(f"API keys active: {', '.join(_active)}")
    else:
        print("API keys active: none — all sources run on keyless/DEMO_KEY fallbacks "
              "(add keys in the Settings tab or repo-root .env, then restart)")

    checks = geography.phase_a_checks()
    if not checks["ok"]:
        print(f"WARNING: Phase-A checks not clean: {checks}")

    if start_ingestion:
        from ingestion.scheduler import start_all, stop_event
        n = start_all()
        print(f"ingestion: {n} source thread(s) running")
        from modeling.nightly import start_thread
        start_thread(stop_event)
        # continuous deterministic recompute (averages/forecasts/map) so derived
        # data moves as raw data lands — the LLM-heavy nightly stays once/day
        from modeling.live_recompute import start_thread as start_recompute
        start_recompute(stop_event)


def create_app(port: int | None = None, start_ingestion: bool = True) -> App:
    bootstrap(start_ingestion=start_ingestion)
    port = port or cfg("server.port")
    server = ThreadingHTTPServer((cfg("server.host"), port), Handler)
    return App(server, port)
