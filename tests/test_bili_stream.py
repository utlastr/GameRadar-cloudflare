import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from radar import bili_daily as daily, bili_stream as stream, config, db, video
from radar.sources import bili


def feed_row(tid=17, aid=117493779665219):
    return {"goto": "av", "param": str(aid), "rid": tid, "title": "[ForgeTax]",
            "name": "Author", "pubdate": 1792803600, "play": 100,
            "like": 8, "favourite": 5, "reply": 2}


class FeedApiTests(unittest.TestCase):
    def test_archive_conversion_matches_saved_official_pairs(self):
        for aid, bvid in ((117493779665219, "BV1SeHw6VEyw"),
                          (117493779728183, "BV1MeHw65EQZ"),
                          (170001, "BV17x411w7KC")):
            self.assertEqual(bili.aid_to_bvid(aid), bvid)

    def test_anonymous_feed_uses_app_host_and_100_rows(self):
        values = {}
        with patch.object(bili, "_anonymous_json", return_value={
                "code": 0, "data": {"new": [feed_row()]}}) as request, \
                patch.object(bili, "cookie") as cookie, \
                patch.object(bili.db, "kv_get", side_effect=lambda k, d=None: values.get(k, d)), \
                patch.object(bili.db, "kv_set", side_effect=lambda k, v: values.__setitem__(k, v)), \
                patch.object(bili, "REQUEST_GAP", 0):
            result = bili.legacy_region_feed(17)
        self.assertEqual(result["list"][0]["bvid"], "BV1SeHw6VEyw")
        self.assertEqual(result["list"][0]["date"], "2026-10-24")
        self.assertEqual(request.call_args.args[1]["ps"], 100)
        self.assertIn("app.bilibili.com", request.call_args.args[0])
        self.assertEqual(bili._endpoint(request.call_args.args[0]), bili.REGION_FEED_SCOPE)
        cookie.assert_not_called()
        self.assertNotIn("danmaku", result["list"][0])

    def test_wrong_category_missing_array_or_bad_id_rejected(self):
        for data in ({}, {"new": [feed_row(65)]},
                     {"new": [dict(feed_row(), param="not-an-id")]}):
            with self.subTest(data=data), patch.object(
                    bili, "_plain", return_value={"code": 0, "data": data}):
                with self.assertRaises((ValueError, RuntimeError)):
                    bili.legacy_region_feed(17)

    def test_empty_feed_is_empty_not_full_coverage(self):
        with patch.object(bili, "_plain", return_value={"code": 0, "data": {"new": []}}):
            self.assertEqual(bili.legacy_region_feed(17)["list"], [])


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.multiple(config, DATA_DIR=self.tmp.name,
                           DB_PATH=str(Path(self.tmp.name) / "test.db"),
                           BVIDEO_STREAM_ENABLED=True, BVIDEO_STREAM_SECONDS=60,
                           BVIDEO_STREAM_PAGE_SIZE=100),
            patch.object(config, "source_on", return_value=True),
            patch("radar.activity.say"),
            patch.object(bili, "_guard"),
            patch.object(stream, "_threads", {}),
            patch.object(stream, "_started", False),
        ]
        for p in self.patches:
            p.start()
        db.init()
        stream.init()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def sample(self, tid, *args):
        return {"list": [{"bvid": "BV%010d" % tid, "date": "2026-10-24",
                         "title": "[ForgeTax]", "owner_id": "", "owner_name": "Author",
                         "tid": tid, "views": 100, "likes": 8, "favorites": 5, "replies": 2}],
                "latest": "2026-10-24T09:00:00+08:00"}

    def tagged(self, bvid, **kwargs):
        tid = int(bvid[2:])
        return {"bvid": bvid, "date": "2026-10-24", "title": "[ForgeTax]",
                "owner_id": "123", "owner_name": "Author", "tid": tid, "tid_v2": 2079,
                "tags": ["ForgeTax", "some tag"], "views": 200, "likes": 10,
                "coins": 3, "favorites": 9, "shares": 4, "replies": 5}

    def test_all_eight_children_polled_once_then_not_before_deadline(self):
        with patch.object(bili, "legacy_region_feed", side_effect=self.sample) as request:
            result = stream.run_once()
            again = stream.run_once()
        self.assertEqual([c.args[0] for c in request.call_args_list],
                         list(bili.LEGACY_GAME_SECTIONS))
        self.assertEqual(result["videos"], 8)
        self.assertEqual(result["pending_tags"], 8)
        self.assertTrue(again["skipped"])
        self.assertEqual(result["round"]["polled"], 8)
        self.assertNotIn("complete", result["status"])

    def test_repeated_feed_deduplicates_videos_and_queue(self):
        with patch.object(bili, "legacy_region_feed", side_effect=self.sample):
            stream.run_once(force=True)
            result = stream.run_once(force=True)
        self.assertEqual(result["videos"], 8)
        self.assertEqual(result["pending_tags"], 8)
        self.assertTrue(all(p["new_videos"] == 1 for p in result["partitions"]))

    def test_slow_round_does_not_add_an_extra_full_minute(self):
        def slow(tid, *args):
            clock[0] += 9
            return self.sample(tid)
        clock = [time.time()]
        with patch.object(stream.time, "time", side_effect=lambda: clock[0]), \
                patch.object(bili, "legacy_region_feed", side_effect=slow):
            result = stream.run_once()
        self.assertEqual(result["round"]["seconds"], 72)
        self.assertEqual(result["next_poll"], clock[0] + 1)

    def test_restart_reuses_persisted_deadline_and_tags(self):
        with patch.object(bili, "legacy_region_feed", side_effect=self.sample):
            stream.run_once()
        with patch.object(bili, "detail_with_tags", side_effect=self.tagged) as get:
            self.assertTrue(stream.enrich_once())
            with patch.object(bili, "legacy_region_feed", side_effect=self.sample):
                stream.run_once(force=True)
            self.assertTrue(stream.enrich_once())
        self.assertEqual(get.call_count, 2)
        self.assertNotEqual(get.call_args_list[0].args, get.call_args_list[1].args)
        self.assertEqual(stream.summary()["tagged"], 2)
        with patch.object(bili, "legacy_region_feed") as request:
            self.assertTrue(stream.run_once()["skipped"])
        request.assert_not_called()

    def test_all_tags_and_counters_share_daily_storage(self):
        stream._save_sample(17, self.sample(17))
        db.kv_set("bili_stream_next", time.time() + 60)
        with patch.object(bili, "detail_with_tags", side_effect=self.tagged):
            self.assertTrue(stream.enrich_once())
        with db.session() as c:
            row = c.execute("SELECT * FROM bd_videos").fetchone()
        self.assertEqual(json.loads(row["tags"]), ["ForgeTax", "some tag"])
        self.assertEqual((row["tid"], row["tid_v2"], row["coins"], row["shares"]), (17, 2079, 3, 4))

    def test_daily_and_stream_do_not_enrich_the_same_bv_concurrently(self):
        stream._save_sample(17, self.sample(17))
        db.kv_set("bili_stream_next", time.time() + 60)
        with daily.detail_claim("BV0000000017") as claimed, \
                patch.object(bili, "detail_with_tags") as get:
            self.assertTrue(claimed)
            self.assertFalse(stream.enrich_once())
        get.assert_not_called()

    def test_risk_stops_remaining_children_without_clearing_queue(self):
        with patch.object(bili, "legacy_region_feed", side_effect=[
                self.sample(17), bili.RiskError("risk", bili.REGION_FEED_SCOPE)]) as request:
            result = stream.run_once()
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["videos"], 1)
        self.assertEqual(result["pending_tags"], 1)

    def test_shared_cooldown_prevents_every_request(self):
        with patch.object(bili, "_guard", side_effect=bili.RiskError("paused")), \
                patch.object(bili, "legacy_region_feed") as request:
            result = stream.run_once()
        request.assert_not_called()
        self.assertEqual(result["status"], "paused")

    def test_one_child_error_does_not_skip_other_children(self):
        def fetch(tid, size):
            if tid == 65:
                raise RuntimeError("temporary error")
            return self.sample(tid)
        with patch.object(bili, "legacy_region_feed", side_effect=fetch) as request:
            result = stream.run_once()
        self.assertEqual(request.call_count, 8)
        self.assertEqual(result["videos"], 7)
        self.assertEqual(result["status"], "partial")

    def test_failed_tags_remain_pending_with_backoff(self):
        stream._save_sample(17, self.sample(17))
        db.kv_set("bili_stream_next", time.time() + 60)
        with patch.object(bili, "detail_with_tags", side_effect=RuntimeError("Tags missing")) as get:
            self.assertFalse(stream.enrich_once())
            self.assertFalse(stream.enrich_once())
        self.assertEqual(get.call_count, 1)
        self.assertEqual(stream.summary()["pending_tags"], 1)
        self.assertEqual(stream.summary()["tagged"], 0)

    def test_disabled_source_and_active_lease_do_not_request(self):
        with patch.object(config, "source_on", return_value=False), \
                patch.object(bili, "legacy_region_feed") as request:
            self.assertEqual(stream.run_once()["status"], "disabled")
        request.assert_not_called()
        self.assertTrue(stream._claim("other-process"))
        with patch.object(bili, "legacy_region_feed") as request:
            self.assertTrue(stream.run_once()["busy"])
        request.assert_not_called()

    def test_stream_worker_starts_once_and_is_integrated_with_video_start(self):
        with patch.object(stream, "_started", False), patch.object(stream.threading, "Thread") as thread:
            stream.start()
            stream.start()
        self.assertEqual(thread.call_count, 8)
        self.assertEqual([c.kwargs["name"] for c in thread.call_args_list],
                         ["bili-stream-latest", "bili-stream-inbox", "bili-stream-tags",
                          "bili-stream-tags-2", "bili-stream-tags-3", "bili-stream-tags-4",
                          "bili-stream-maintenance", "bili-stream-analysis"])
        self.assertEqual(thread.return_value.start.call_count, 8)
        with patch.object(video, "_started", False), patch.object(stream, "start") as start, \
                patch.object(daily, "start"), patch.object(video.threading, "Thread"):
            video.start()
        start.assert_called_once_with(None)

    def test_each_partition_has_an_independent_persisted_deadline(self):
        with patch.object(bili, "legacy_region_feed", side_effect=self.sample) as request:
            first = stream.run_once(tid=17)
            stream.run_once(tid=65)
            again = stream.run_once(tid=17)
        self.assertEqual([c.args[0] for c in request.call_args_list], [17, 65])
        self.assertTrue(again["skipped"])
        parts = {p["tid"]: p for p in first["partitions"]}
        self.assertGreater(parts[17]["next_poll"], time.time())
        self.assertEqual(parts[65]["next_poll"], 0)
        self.assertEqual(parts[17]["runtime_pid"], first["runtime_pid"])

    def test_partition_lease_blocks_only_its_own_child_and_manual_sweep(self):
        self.assertTrue(stream._claim("other", tid=17))
        with patch.object(bili, "legacy_region_feed", side_effect=self.sample) as request:
            self.assertTrue(stream.run_once(tid=17)["busy"])
            self.assertTrue(stream.run_once()["busy"])
            result = stream.run_once(tid=65)
        self.assertEqual([c.args[0] for c in request.call_args_list], [65])
        self.assertEqual(result["videos"], 1)

    def test_legacy_round_lease_blocks_independent_workers(self):
        self.assertTrue(stream._claim("legacy-process"))
        with patch.object(bili, "legacy_region_feed") as request:
            self.assertTrue(stream.run_once(tid=17)["busy"])
            self.assertTrue(stream.run_once(tid=65)["busy"])
        request.assert_not_called()

    def test_slow_child_does_not_set_other_children_deadlines(self):
        clock = [time.time()]
        def slow(tid, *args):
            clock[0] += 70
            return self.sample(tid)
        with patch.object(stream.time, "time", side_effect=lambda: clock[0]), \
                patch.object(bili, "legacy_region_feed", side_effect=slow):
            result = stream.run_once(tid=17)
        parts = {p["tid"]: p for p in result["partitions"]}
        self.assertEqual(parts[17]["next_poll"], clock[0] + 1)
        self.assertEqual(parts[65]["next_poll"], 0)

    def test_due_child_takes_precedence_over_tag_enrichment(self):
        stream._save_sample(17, self.sample(17))
        with db.session() as c:
            c.execute("UPDATE bs_partitions SET next_poll=?", (time.time() + 60,))
            c.execute("UPDATE bs_partitions SET next_poll=? WHERE tid=65", (time.time() - 1,))
        with patch.object(bili, "detail_with_tags") as request:
            self.assertFalse(stream.enrich_once())
        request.assert_not_called()

    def test_threads_share_a_bounded_pool_and_deduplicate(self):
        barrier, count_lock = threading.Barrier(8), threading.Lock()
        active, maximum, errors = [0], [0], []
        def fetch(tid, *args):
            with count_lock:
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
            time.sleep(0.01)
            with count_lock:
                active[0] -= 1
            return self.sample(tid)
        def poll(tid):
            try:
                barrier.wait(timeout=10)
                stream.run_once(tid=tid)
            except Exception as exc:
                errors.append(exc)
        with patch.object(bili, "legacy_region_feed", side_effect=fetch):
            threads = [threading.Thread(target=poll, args=(tid,))
                       for tid in bili.LEGACY_GAME_SECTIONS]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertGreater(maximum[0], 1)
        self.assertLessEqual(maximum[0], bili.MAX_INFLIGHT)
        self.assertEqual(stream.summary()["videos"], 8)
        self.assertEqual(stream.summary()["pending_tags"], 8)
        self.assertTrue(all(p["polls"] == 1 for p in stream.summary()["partitions"]))

    def test_queued_workers_observe_shared_risk_pause(self):
        barrier, errors = threading.Barrier(8), []
        def guard(scope):
            if bili.paused(scope):
                raise bili.RiskError("paused", scope)
        def blocked(*args):
            bili.note_risk("HTTP Error 412")
            raise bili.RiskError("risk")
        def poll(tid):
            try:
                barrier.wait(timeout=10)
                stream.run_once(tid=tid)
            except Exception as exc:
                errors.append(exc)
        with patch.object(bili, "_guard", side_effect=guard), \
                patch.object(bili, "legacy_region_feed", side_effect=blocked) as request:
            threads = [threading.Thread(target=poll, args=(tid,))
                       for tid in bili.LEGACY_GAME_SECTIONS]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertGreaterEqual(request.call_count, 1)
        self.assertLessEqual(request.call_count, bili.MAX_INFLIGHT)
        self.assertGreater(stream.summary()["pause_seconds"], 0)
        self.assertEqual(stream.summary()["videos"], 0)

    def test_log_failure_does_not_leave_child_locked_or_leased(self):
        with patch.object(bili, "legacy_region_feed", side_effect=self.sample), \
                patch.object(db, "kv_set", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                stream.run_once(tid=17)
        self.assertFalse(stream._partition_locks[17].locked())
        with db.session() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM bd_lease WHERE name='stream:17'").fetchone())
        with patch.object(bili, "legacy_region_feed", side_effect=self.sample):
            self.assertEqual(stream.run_once(tid=17, force=True)["videos"], 1)

    def test_real_workers_report_liveness_stop_and_allow_restart(self):
        for _ in range(2):
            stop = threading.Event()
            with patch.object(config, "source_on", return_value=False), \
                    patch.object(bili, "legacy_region_feed") as request:
                stream.start(stop)
                threads = list(stream._threads.values())
                try:
                    result = stream.summary()
                    self.assertEqual(result["mode"], "latest_window")
                    self.assertEqual(result["partition_workers_running"], 0)
                    self.assertTrue(result["storage_worker_running"])
                    self.assertTrue(result["tag_worker_running"])
                finally:
                    stop.set()
                    for thread in threads:
                        thread.join(timeout=10)
                request.assert_not_called()
            self.assertFalse(stream._started)
            self.assertEqual(stream._threads, {})

    def test_enabled_workers_distribute_shared_parent_without_claiming_child_requests(self):
        stop = threading.Event()
        now = int(time.time())
        records = [dict(self.sample(tid)["list"][0], published=now-index)
                   for index, tid in enumerate(bili.LEGACY_GAME_SECTIONS)]
        with patch.object(bili, "latest_game_posts", return_value={
                "list": records, "latest": now, "earliest": now-7}) as request, \
                patch.object(bili, "legacy_region_feed") as legacy, \
                patch.object(stream, "enrich_once", return_value=False), \
                patch.object(stream.bili_terms, "rescan"):
            stream.start(stop)
            threads = list(stream._threads.values())
            try:
                deadline = time.monotonic() + 10
                while stream.summary()["videos"] < 8 and time.monotonic() < deadline:
                    time.sleep(0.01)
            finally:
                stop.set()
                for thread in threads:
                    thread.join(timeout=10)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(request.call_count, 1)
        legacy.assert_not_called()
        self.assertEqual(stream.summary()["videos"], 8)
        self.assertEqual(stream.summary()["pending_tags"], 8)

    def test_legacy_table_migration_preserves_progress_and_tag_queue(self):
        with patch.object(config, "DB_PATH", str(Path(self.tmp.name) / "legacy.db")):
            with db.session() as c:
                c.executescript(stream.SCHEMA)
                c.execute("INSERT INTO bs_partitions(tid,polls) VALUES(17,42)")
                c.execute("INSERT INTO bs_pending(bvid) VALUES('BV0000000017')")
            stream.init()
            with db.session() as c:
                row = c.execute("SELECT * FROM bs_partitions WHERE tid=17").fetchone()
                pending = c.execute("SELECT COUNT(*) FROM bs_pending").fetchone()[0]
            self.assertEqual(row["polls"], 42)
            self.assertEqual(row["next_poll"], 0)
            self.assertEqual(pending, 1)

    def test_stop_before_round_does_not_fetch_or_call_it_complete(self):
        stop = threading.Event()
        stop.set()
        with patch.object(bili, "legacy_region_feed") as request:
            result = stream.run_once(stop_event=stop)
        request.assert_not_called()
        self.assertEqual(result["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
