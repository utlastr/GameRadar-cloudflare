import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from radar import bili_latest, bili_realtime_client as client, config, db, health, ytfeed


class CollectorHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.multiple(config, DB_PATH=os.path.join(self.tmp.name, "test.db"),
                           DATA_DIR=self.tmp.name)
        p.start()
        self.addCleanup(p.stop)
        db.init()
        self.threads = [SimpleNamespace(name=t, is_alive=lambda: True)
                        for _, t, _ in health.WORKERS.values()]

    def report(self):
        with patch.object(health.threading, "enumerate", return_value=self.threads), \
                patch.object(health, "_started", time.time()), \
                patch.object(health, "_startup_errors", {}), \
                patch.object(config, "STEAM_ONLY", False):
            return health.snapshot()

    def test_dead_worker_is_urgent_even_with_old_success(self):
        db.kv_set("source_status", {"bvideo": {"ok_ts": time.time(), "count": 100}})
        self.threads = []
        self.assertIn("worker:bvideo", {a["id"] for a in self.report()["alerts"]})

    def test_stale_timestamp_is_not_healthy(self):
        db.kv_set("bili_latest_state", {"received": time.time() - 300, "status": "latest"})
        self.assertIn("stale:bvideo", {a["id"] for a in self.report()["alerts"]})

    def test_failed_platform_visible_without_waiting_for_analysis(self):
        db.kv_set("source_status", {"taptap": {"err_ts": 20, "ok_ts": 10, "error": "parse error"}})
        self.assertIn("source:taptap", {a["id"] for a in self.report()["alerts"]})

    def test_recovered_error_clears_automatically(self):
        db.kv_set("source_status", {"taptap": {"err_ts": 10, "ok_ts": 20, "error": "old error"}})
        self.assertNotIn("source:taptap", {a["id"] for a in self.report()["alerts"]})

    def test_startup_failure_does_not_prevent_other_collectors(self):
        started = []

        def load(name, package):
            if name == ".bili_latest":
                raise ImportError("missing dependency")
            return SimpleNamespace(start=lambda stop: started.append(name))

        with patch.object(health.importlib, "import_module", side_effect=load), \
                patch.object(health, "_startup_errors", {}):
            health.start_collectors()
            self.assertEqual(len(started), 3)
            self.assertIn("bili_latest", health._startup_errors)

    def test_denial_stops_requests_without_identity_rotation(self):
        db.kv_set("bili_realtime_denial", {"error": "verification required", "ts": time.time()})
        with patch.object(client, "get_json_strict") as request:
            with self.assertRaises(client.RiskError):
                client._plain("https://api.bilibili.com/x/web-interface/newlist", {})
            request.assert_not_called()

    def test_latest_poll_failure_records_platform_error(self):
        with patch.object(client, "latest_game_posts", side_effect=RuntimeError("network failed")):
            result = bili_latest.poll_once(force=True)
        self.assertEqual(result["status"], "error")
        self.assertIn("network failed", db.kv_get("source_status")["bvideo"]["error"])

    def test_youtube_missing_key_is_not_success(self):
        with patch.object(ytfeed, "_key", return_value=""):
            self.assertFalse(ytfeed.run_once()["ok"])

    def test_pipeline_error_visible_and_clears_on_success(self):
        health.worker_result("入库", "disk failed")
        self.assertIn("pipeline:入库", {a["id"] for a in self.report()["alerts"]})
        health.worker_result("入库")
        self.assertNotIn("pipeline:入库", {a["id"] for a in self.report()["alerts"]})

    def test_youtube_partial_error_visible_before_sweep_finishes(self):
        ytfeed._progress("region", "network failed")
        self.assertIn("youtube_feed_error", {a["id"] for a in self.report()["alerts"]})

    def test_backlog_and_stall_visible_with_live_threads(self):
        bili_latest.init()
        now = time.time()
        db.kv_set("bili_latest_state", {"received": now, "status": "latest"})
        with db.session() as c:
            c.execute("INSERT INTO bs_pending(bvid,queued,lane) VALUES('test',?,'recent')",
                      (now - 600,))
        with patch.object(health.threading, "enumerate", return_value=self.threads), \
                patch.object(health, "_started", now - 600):
            ids = {a["id"] for a in health.snapshot()["alerts"]}
        self.assertIn("bili_tags_stalled", ids)
        self.assertIn("bili_tags_backlog", ids)


if __name__ == "__main__":
    unittest.main()
