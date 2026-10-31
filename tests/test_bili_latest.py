import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from radar import bili_latest as latest, bili_stream as stream, config, db
from radar import bili_realtime_client as bili


def bv(n):
    return f"BV1{n:09d}"


def batch(*ids, tid=17, start=None, step=1):
    now = int(time.time()) if start is None else start
    rows = [{"bvid": bv(n), "tid": tid, "tid_v2": 2079,
             "published": now-i*step, "date": bili.published_day(now-i*step),
             "title": "[Example]", "owner_id": "123", "owner_name": "Author"}
            for i, n in enumerate(ids)]
    return {"list": rows, "latest": rows[0]["published"], "earliest": rows[-1]["published"]}


class LatestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.multiple(config, DATA_DIR=self.tmp.name,
                           DB_PATH=str(Path(self.tmp.name) / "latest.db"),
                           BVIDEO_STREAM_ENABLED=True, BVIDEO_STREAM_PAGE_SIZE=100,
                           BVIDEO_LATEST_SECONDS=10),
            patch.object(config, "source_on", return_value=True),
            patch("radar.activity.say"),
            patch.object(bili, "_guard"),
            patch.object(stream, "_threads", {}),
            patch.object(stream, "_started", False),
            patch.object(stream, "_enrich_turn", 0),
            patch.object(latest, "_runtime_started", 0),
        ]
        for item in self.patches:
            item.start()
        db.init()
        latest.init()
        db.kv_set("bili_stream_mode", "latest_window")

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def poll(self, value):
        with patch.object(bili, "latest_game_posts", return_value=value):
            return latest.poll_once(force=True)

    def count(self, table):
        with db.session() as c:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_first_head_is_bootstrap_not_historical_completion(self):
        result = self.poll(batch(3, 2, 1))
        self.assertEqual(result["continuity"], "bootstrap")
        self.assertEqual(result["new"], 3)
        self.assertEqual(self.count("bs_gaps"), 0)
        self.assertEqual(self.count("bs_inbox"), 3)
        self.assertNotIn("complete", stream.summary()["status"])

    def test_overlap_deduplication_and_restart_checkpoint(self):
        now = int(time.time())
        self.poll(batch(3, 2, 1, start=now))
        latest._ready.discard(config.DB_PATH)
        result = self.poll(batch(4, 3, 2, start=now+1))
        self.assertEqual(result["overlap"], 2)
        self.assertEqual(result["new"], 1)
        self.assertEqual(self.count("bs_inbox"), 4)
        with patch.object(bili, "latest_game_posts") as request:
            self.assertTrue(latest.poll_once()["skipped"])
        request.assert_not_called()

    def test_repeated_head_does_not_queue_duplicate_details(self):
        self.poll(batch(3, 2, 1))
        result = self.poll(batch(3, 2, 1))
        self.assertEqual(result["new"], 0)
        self.assertEqual(result["overlap"], 3)
        self.assertEqual(latest.drain_partition(17), 3)
        self.assertEqual(latest.drain_partition(17), 0)
        self.assertEqual(stream.summary()["pending_tags"], 3)

    def test_missing_overlap_is_recorded_and_not_cleared_by_next_success(self):
        now = int(time.time())
        self.poll(batch(3, 2, 1, start=now))
        result = self.poll(batch(9, 8, 7, start=now+10))
        self.assertEqual(result["continuity"], "gap")
        self.assertEqual(stream.summary()["gaps"], 1)
        self.poll(batch(10, 9, 8, start=now+11))
        self.assertEqual(stream.summary()["gaps"], 1)
        self.assertEqual(latest.state()["continuity"], "overlap")

    def test_backward_head_does_not_replace_checkpoint(self):
        now = int(time.time())
        first = self.poll(batch(3, 2, 1, start=now))
        result = self.poll(batch(9, 8, 7, start=now-10))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["bvids"], first["bvids"])
        self.assertEqual(self.count("bs_inbox"), 3)

    def test_request_error_keeps_saved_queue_and_previous_head(self):
        first = self.poll(batch(3, 2, 1))
        with patch.object(bili, "latest_game_posts", side_effect=RuntimeError("failed")):
            result = latest.poll_once(force=True)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["bvids"], first["bvids"])
        self.assertEqual(self.count("bs_inbox"), 3)

    def test_risk_pause_preserves_queue_and_avoids_immediate_retry(self):
        self.poll(batch(3, 2, 1))
        with patch.object(bili, "latest_game_posts", side_effect=bili.RiskError("risk")) as request:
            result = latest.poll_once(force=True)
            self.assertTrue(latest.poll_once()["skipped"])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(self.count("bs_inbox"), 3)

    def test_checkpoint_and_inbox_are_atomic_on_storage_error(self):
        with patch.object(bili, "latest_game_posts", return_value=batch(3, 2, 1)), \
                patch.object(latest, "_write_state", side_effect=RuntimeError("disk error")):
            with self.assertRaisesRegex(RuntimeError, "disk error"):
                latest.poll_once(force=True)
        self.assertEqual(self.count("bs_inbox"), 0)
        self.assertEqual(self.count("bs_latest_seen"), 0)
        self.assertFalse(latest._poll_lock.locked())
        self.assertEqual(self.count("bd_lease"), 0)

    def test_partition_workers_only_drain_their_own_fifo_queue(self):
        first = batch(3, 2, 1, tid=17)
        second = batch(5, 4, tid=65)
        mixed = {"list": first["list"] + second["list"],
                 "latest": first["latest"], "earliest": first["earliest"]}
        self.poll(mixed)
        self.assertEqual(latest.drain_partition(17, limit=2), 2)
        with db.session() as c:
            saved = [row[0] for row in c.execute("SELECT bvid FROM bd_videos ORDER BY rowid")]
        self.assertEqual(saved, [bv(1), bv(2)])
        self.assertEqual(latest.drain_partition(17), 1)
        self.assertEqual(latest.drain_partition(65), 2)
        summary = stream.summary()
        self.assertEqual(summary["pending_tags"], 5)
        parts = {p["tid"]: p for p in summary["partitions"]}
        self.assertEqual(parts[17]["rows"], 3)
        self.assertEqual(parts[65]["rows"], 2)
        self.assertEqual(parts[136]["rows"], 0)

    def test_failed_partition_save_stays_queued_for_retry(self):
        self.poll(batch(3, 2, 1))
        with patch.object(stream, "_save_sample", side_effect=RuntimeError("save failed")):
            with self.assertRaisesRegex(RuntimeError, "save failed"):
                latest.drain_partition(17)
        self.assertEqual(self.count("bs_inbox"), 3)
        self.assertFalse(stream._partition_locks[17].locked())
        self.assertEqual(latest.drain_partition(17), 3)
        self.assertEqual(stream.summary()["videos"], 3)

    def test_active_latest_lease_blocks_duplicate_source_worker(self):
        self.assertTrue(latest._claim("another-process"))
        with patch.object(bili, "latest_game_posts") as request:
            self.assertTrue(latest.poll_once(force=True)["busy"])
        request.assert_not_called()

    def test_tag_queue_continues_fifo_with_a_bounded_timeout(self):
        data = batch(3, 2, 1, step=15)
        self.poll(data)
        latest.drain_partition(17)
        record = dict(data["list"][-1], tags=["Example"], views=10, likes=1,
                      coins=1, favorites=1, shares=0, replies=0)
        with patch.object(bili, "detail_with_tags", return_value=record) as request:
            self.assertTrue(stream.enrich_once())
        self.assertEqual(request.call_args.args, (bv(1),))
        self.assertEqual(request.call_args.kwargs["timeout"], 12)
        result = stream.summary()
        self.assertEqual(result["tags_last_minute"], 1)
        self.assertEqual(result["pending_tags"], 2)

    def test_recent_tags_take_four_slots_and_old_fifo_keeps_one(self):
        data = batch(10, 9, 8, 7, 6, 5, 4, 3, 2, 1)
        self.poll(data)
        latest.drain_partition(17)
        with db.session() as c:
            c.execute("UPDATE bs_pending SET queued=? WHERE bvid IN (?,?,?,?)",
                      (time.time() - 7200, bv(1), bv(2), bv(3), bv(4)))
        called = []
        def fetch(bvid, **options):
            called.append(bvid)
            row = next(row for row in data["list"] if row["bvid"] == bvid)
            return dict(row, tags=["Example"], views=10, likes=1, coins=1,
                        favorites=1, shares=0, replies=0)
        with patch.object(bili, "detail_with_tags", side_effect=fetch):
            for _ in range(10):
                self.assertTrue(stream.enrich_once())
        self.assertEqual(called[:5], [bv(5), bv(6), bv(7), bv(8), bv(1)])
        self.assertEqual(set(called), {bv(n) for n in range(1, 11)})
        self.assertEqual(stream.summary()["pending_tags"], 0)

    def test_live_pause_overrides_thread_liveness_without_modifying_checkpoint(self):
        self.poll(batch(3, 2, 1))
        latest.drain_partition(17)
        checkpoint = latest.state()
        checkpoint["error"] = "B站风控暂停中，还剩 91 分钟"
        db.kv_set("bili_latest_state", checkpoint)
        deadline = time.time() + 180
        db.kv_set("bili_pause_until", deadline)
        thread = threading.current_thread()
        with patch.object(stream, "_threads", {
                "bili-stream-latest": thread, "bili-stream-inbox": thread,
                "bili-stream-tags": thread}):
            result = stream.summary()
        self.assertEqual(result["worker_states"], {
            "list": "paused", "storage": "idle", "tags": "paused"})
        self.assertEqual(result["throughput"]["status"], "paused")
        self.assertLessEqual(result["pause_seconds"], 180)
        self.assertEqual(result["pause"]["until"], deadline)
        self.assertEqual(latest.state()["error"], checkpoint["error"])
        with patch.object(bili, "detail_with_tags") as request:
            self.assertFalse(stream.enrich_once())
        request.assert_not_called()

    def test_detail_scoped_pause_does_not_stop_latest_status(self):
        self.poll(batch(3, 2, 1))
        db.kv_set("bili_pause_until:视频详情及标签", time.time() + 180)
        thread = threading.current_thread()
        with patch.object(stream, "_threads", {
                "bili-stream-latest": thread, "bili-stream-tags": thread}):
            result = stream.summary()
        self.assertEqual(result["status"], "latest")
        self.assertEqual(result["worker_states"]["list"], "polling")
        self.assertEqual(result["worker_states"]["tags"], "paused")
        self.assertGreater(result["tag_pause_seconds"], 0)

    def test_poll_interval_tracks_window_not_a_fixed_minute(self):
        self.assertEqual(latest._interval(30), 10)
        self.assertEqual(latest._interval(15), 6)
        self.assertEqual(latest._interval(2), 3)

    def test_summary_exposes_age_overlap_and_source_specific_counts(self):
        first = self.poll(batch(3, 2, 1, step=10))
        summary = stream.summary()
        self.assertEqual(summary["source"], latest.SOURCE)
        self.assertEqual(summary["source_seen"], 3)
        self.assertEqual(summary["latest"]["returned"], 3)
        self.assertEqual(summary["pending_list"], 3)
        self.assertGreaterEqual(summary["latest_age_seconds"], 0)
        self.assertEqual(summary["latest"]["tracking_started"], first["received"])

    def test_disabled_collector_is_not_reported_as_stale(self):
        self.poll(batch(3, 2, 1, start=int(time.time())-3600))
        with patch.object(config, "BVIDEO_STREAM_ENABLED", False):
            self.assertEqual(stream.summary()["status"], "disabled")

    def test_due_head_no_longer_starves_details(self):
        data = batch(3, 2, 1)
        self.poll(data)
        latest.drain_partition(17)
        checkpoint = latest.state()
        checkpoint["next_poll"] = time.time() - 5
        db.kv_set("bili_latest_state", checkpoint)
        record = dict(data["list"][-1], tags=["Example"], views=10, likes=1,
                      coins=1, favorites=1, shares=0, replies=0)
        with patch.object(bili, "detail_with_tags", return_value=record) as request:
            self.assertTrue(stream.enrich_once())
        self.assertEqual(request.call_count, 1)
        self.assertEqual(stream.summary()["tagged"], 1)

    def test_daily_completion_is_counted_once_in_realtime_metrics(self):
        from radar import bili_daily
        data = batch(3, 2, 1)
        self.poll(data)
        latest.drain_partition(17)
        record = dict(data["list"][-1], tags=["Example"], views=10, likes=1,
                      coins=1, favorites=1, shares=0, replies=0)
        bili_daily._save_detail(record)
        bili_daily._save_detail(record)
        self.assertEqual(stream.summary()["tags_last_minute"], 1)

    def test_daily_completion_removes_only_its_pending_job_atomically(self):
        from radar import bili_daily
        data = batch(3, 2, 1)
        self.poll(data)
        latest.drain_partition(17)
        record = dict(data["list"][-1], tags=["Example"], views=10, likes=1,
                      coins=1, favorites=1, shares=0, replies=0)
        bili_daily._save_detail(record)
        with db.session() as c:
            pending = {r[0] for r in c.execute("SELECT bvid FROM bs_pending")}
        self.assertEqual(pending, {bv(2), bv(3)})

    def test_tag_hot_path_never_sweeps_the_completed_video_catalogue(self):
        data = batch(3, 2, 1)
        self.poll(data)
        latest.drain_partition(17)
        statements = []
        connect = db._connect
        def traced():
            connection = connect()
            connection.set_trace_callback(statements.append)
            return connection
        record = dict(data["list"][-1], tags=["Example"], views=10, likes=1,
                      coins=1, favorites=1, shares=0, replies=0)
        with patch.object(db, "_connect", side_effect=traced), \
                patch.object(bili, "detail_with_tags", return_value=record):
            self.assertTrue(stream.enrich_once())
        normalized = [" ".join(s.upper().split()) for s in statements]
        self.assertFalse(any("DELETE FROM BS_PENDING WHERE BVID IN" in s for s in normalized))
        self.assertTrue(any("DELETE FROM BS_PENDING WHERE BVID=" in s for s in normalized))
        self.assertEqual(stream.summary()["pending_tags"], 2)

    def test_unavailable_completion_clears_pending_without_counting_it_as_tagged(self):
        self.poll(batch(1))
        latest.drain_partition(17)
        with patch.object(bili, "detail_with_tags", side_effect=RuntimeError("code=-404")):
            self.assertTrue(stream.enrich_once())
        self.assertEqual(self.count("bs_pending"), 0)
        result = stream.summary()
        self.assertEqual(result["tagged"], 0)
        self.assertEqual(result["unavailable"], 1)

    def test_summary_does_not_present_storage_threads_as_network_partition_workers(self):
        self.poll(batch(3, 2, 1))
        result = stream.summary()
        self.assertFalse(result["full_coverage"])
        self.assertFalse(result["independent_partition_fetching"])
        self.assertEqual(result["partition_workers_running"], 0)
        self.assertTrue(all(not part["network_worker"] for part in result["partitions"]))
        self.assertEqual(result["collector_revision"], latest.REVISION)

    def test_throughput_reports_growing_queue_without_catchup_promise(self):
        self.poll(batch(3, 2, 1))
        latest.drain_partition(17)
        checkpoint = latest.state()
        checkpoint["tracking_started"] = time.time() - 600
        db.kv_set("bili_latest_state", checkpoint)
        with db.session() as c:
            c.execute("INSERT INTO bs_events VALUES(?,'discovered',500)", (time.time(),))
            c.execute("INSERT INTO bs_events VALUES(?,'tag_queued',500)", (time.time(),))
        health = stream.summary()["throughput"]
        self.assertEqual(health["status"], "growing")
        self.assertIsNone(health["catch_up_seconds"])
        self.assertGreater(health["discovered_per_minute"], health["tagged_per_minute"])

    def test_persisted_gap_registers_closed_day_for_backfill_but_stays_unresolved(self):
        from radar import bili_daily
        first = int(time.time()) - 2 * 86400
        with db.session() as c:
            c.execute("INSERT INTO bs_gaps VALUES('old-gap',?,?,?,?,0)",
                      (time.time(), first, first + 10, "missing overlap"))
        with patch.object(config, "BVIDEO_DAILY_ENABLED", True):
            days = latest.register_gap_days()
        expected = bili.published_day(first)
        self.assertIn(expected, days)
        self.assertTrue(bili_daily.summary(expected)["partitions"])
        self.assertEqual(stream.summary()["gaps"], 1)

    def test_equal_arrival_and_completion_is_balanced_not_stalled(self):
        self.poll(batch(3, 2, 1))
        latest.drain_partition(17)
        checkpoint = latest.state()
        checkpoint["tracking_started"] = time.time() - 600
        db.kv_set("bili_latest_state", checkpoint)
        with db.session() as c:
            c.execute("UPDATE bs_events SET ts=?", (time.time() - 700,))
            c.execute("INSERT INTO bs_events VALUES(?,'tag_queued',10)", (time.time(),))
            c.execute("INSERT INTO bs_events VALUES(?,'tag_completed',10)", (time.time(),))
        health = stream.summary()["throughput"]
        self.assertEqual(health["status"], "balanced")
        self.assertEqual(health["net_growth_per_minute"], 0)
        self.assertIsNone(health["catch_up_seconds"])

    def test_restart_throughput_excludes_the_previous_process_events(self):
        self.poll(batch(3, 2, 1))
        latest.drain_partition(17)
        now = time.time()
        with db.session() as c:
            c.execute("DELETE FROM bs_events")
            c.execute("INSERT INTO bs_events VALUES(?,'tag_queued',1000)", (now - 300,))
            c.execute("INSERT INTO bs_events VALUES(?,'tag_completed',10)", (now - 10,))
        checkpoint = latest.state()
        checkpoint["tracking_started"] = now - 600
        db.kv_set("bili_latest_state", checkpoint)
        with patch.object(latest, "_runtime_started", now - 120):
            result = stream.summary()["throughput"]
        self.assertEqual(result["window_seconds"], 120)
        self.assertEqual(result["queued_tags_per_minute"], 0)
        self.assertEqual(result["tagged_per_minute"], 5)
        self.assertEqual(result["status"], "draining")

    def test_four_tag_workers_fetch_distinct_jobs_concurrently(self):
        data = batch(4, 3, 2, 1)
        self.poll(data)
        latest.drain_partition(17)
        barrier, calls, errors, results = threading.Barrier(4), [], [], []
        def fetch(bvid, **options):
            calls.append(bvid)
            barrier.wait(5)
            row = next(row for row in data["list"] if row["bvid"] == bvid)
            return dict(row, tags=["Example"], views=10, likes=1, coins=1,
                        favorites=1, shares=0, replies=0)
        def enrich():
            try:
                results.append(stream.enrich_once())
            except Exception as exc:
                errors.append(exc)
        with patch.object(bili, "detail_with_tags", side_effect=fetch):
            threads = [threading.Thread(target=enrich) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(results, [True] * 4)
        self.assertEqual(len(set(calls)), 4)
        self.assertFalse(stream._enrich_active)
        self.assertEqual(stream.summary()["tagged"], 4)
        self.assertEqual(stream.summary()["pending_tags"], 0)


class LatestClientTests(unittest.TestCase):
    def raw(self):
        return {"bvid": bv(1), "tid": 17, "tidv2": 2079,
                "pubdate": int(time.time()), "title": "Example",
                "owner": {"mid": 123, "name": "Author"}}

    def test_parent_latest_request_stays_anonymous_and_uses_actual_size(self):
        with patch.object(bili, "_plain", return_value={
                "code": 0, "data": {"archives": [self.raw()]}}) as request:
            result = bili.latest_game_posts(100)
        self.assertEqual(request.call_args.args[1]["rid"], 4)
        self.assertEqual(request.call_args.args[1]["ps"], 100)
        self.assertFalse(request.call_args.kwargs["authenticated"])
        self.assertTrue(request.call_args.kwargs["persistent"])
        self.assertEqual(len(result["list"]), 1)
        self.assertNotIn("views", result["list"][0])

    def test_stale_wrong_category_and_unsorted_cards_are_rejected(self):
        raw = self.raw()
        cases = [[], [dict(raw, pubdate=int(time.time())-7200)],
                 [dict(raw, tid=184)], [raw, raw],
                 [dict(raw, pubdate=raw["pubdate"]-10), dict(raw, bvid=bv(2))]]
        for rows in cases:
            with self.subTest(rows=rows), patch.object(bili, "_plain", return_value={
                    "code": 0, "data": {"archives": rows}}):
                with self.assertRaises(RuntimeError):
                    bili.latest_game_posts()


if __name__ == "__main__":
    unittest.main()
