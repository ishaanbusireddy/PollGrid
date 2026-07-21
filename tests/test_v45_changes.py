"""v4.5 tests: no-cache headers on the frontend (the stale-JS fix), FEC rate-limit
safety (skip the finance burst during catch-up; treat 429 as non-fatal), and the
light/heavy split of the live recompute loop."""
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("POLLGRID_TEST", "1")
import core.config as config_mod  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="pollgrid-v45-")
config_mod.CONFIG["database"]["path"] = os.path.join(_tmp, "test.db")

import core.db as db  # noqa: E402
db.DB_PATH = os.path.join(_tmp, "test.db")


def _bootstrap():
    from api.server import bootstrap
    bootstrap(start_ingestion=False)


class TestNoCacheHeaders(unittest.TestCase):
    def test_frontend_assets_served_no_cache(self):
        import socket
        from api.server import create_app
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        app = create_app(port=port, start_ingestion=False)
        t = threading.Thread(target=app.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/src/App.js", timeout=10) as r:
                cc = r.headers.get("Cache-Control", "")
            self.assertIn("no-cache", cc)  # in-place updates must never serve stale JS
            self.assertIn("no-store", cc)
        finally:
            app.shutdown()
            try:
                app._server.server_close()
            except Exception:
                pass


class TestFecRateLimitSafety(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _bootstrap()

    def test_catchup_skips_finance_burst_until_roster_synced(self):
        import ingestion.fec as fec
        calls = {"finance": 0}

        def fake_get_json(url, params=None, **kw):
            return {"results": [], "pagination": {"pages": 5}}  # page 1 < 5 → no end burst

        src = {"url": "http://x", "api_key_env": "NOPE_KEY"}
        db.execute("DELETE FROM app_meta WHERE key IN ('fec_roster_synced_at','fec_page','fec_cycle_idx')")
        with patch.object(fec, "_priority_finance_pass", lambda *a: calls.__setitem__("finance", calls["finance"] + 1)), \
             patch.object(fec, "get_json", fake_get_json), \
             patch.object(fec.budget, "spend", lambda k: None):
            fec.run(src)
            self.assertEqual(calls["finance"], 0)  # roster not synced → no finance burst
            db.meta_set("fec_roster_synced_at", "2026-07-20T00:00:00")
            fec.run(src)
            self.assertEqual(calls["finance"], 1)  # synced → enrichment allowed
        db.execute("DELETE FROM app_meta WHERE key IN ('fec_roster_synced_at','fec_page','fec_cycle_idx')")

    def test_fetcherror_carries_status(self):
        from ingestion.http import FetchError
        self.assertEqual(FetchError("HTTP 429 rate limited", status=429).status, 429)
        self.assertIsNone(FetchError("dns fail").status)

    def test_rate_limit_keeps_source_healthy_not_down(self):
        import ingestion.scheduler as sch
        from ingestion.http import FetchError
        db.execute("INSERT INTO sources(name,source_type,interval_key,url,is_active,health,"
                   "consecutive_failures) VALUES('rl_test','rl_type','news_rss','http://x',1,'ok',0)")
        sid = db.query_one("SELECT id FROM sources WHERE name='rl_test'")["id"]
        sch.ADAPTERS["rl_type"] = lambda s: (_ for _ in ()).throw(FetchError("HTTP 429", status=429))
        ev = threading.Event()
        ev.wait = lambda t: ev.set()  # first sleep ends the loop after one iteration
        saved = sch.stop_event
        sch.stop_event = ev
        try:
            sch._source_loop(sid)
        finally:
            sch.stop_event = saved
            sch.ADAPTERS.pop("rl_type", None)
        row = db.query_one("SELECT health, consecutive_failures FROM sources WHERE id=?", (sid,))
        self.assertEqual(row["health"], "ok")          # 429 is not a failure
        self.assertEqual(row["consecutive_failures"], 0)


class TestLiveRecomputeSplit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _bootstrap()

    def test_light_is_cheap_and_heavy_computes(self):
        import time
        from modeling import live_recompute as lr
        r_light = lr.light_pass()
        self.assertEqual(r_light["kind"], "light")
        self.assertNotIn("chamber_senate", r_light)  # light pass never runs chamber sims
        t0 = time.monotonic()
        r_heavy = lr.heavy_pass()
        self.assertLess(time.monotonic() - t0, 60)     # heavy still finishes reasonably
        self.assertEqual(r_heavy["kind"], "heavy")
        self.assertIn("chamber_senate", r_heavy)
        self.assertGreater(db.query_one("SELECT COUNT(*) c FROM forecasts")["c"], 0)

    def test_stamps_recompute_time(self):
        from modeling import live_recompute as lr
        db.execute("DELETE FROM app_meta WHERE key='live_recompute_at'")
        lr.light_pass()
        self.assertIsNotNone(db.meta_get("live_recompute_at"))


if __name__ == "__main__":
    unittest.main()
