import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from radar import bili_daily as daily, bili_growth as growth, bili_latest as latest
from radar import bili_queue as queue, bili_stream as stream, bili_terms, config, db
from radar.sources import bili


class UnifiedQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.multiple(config, DATA_DIR=self.tmp.name,
                           DB_PATH=str(Path(self.tmp.name) / "queue.db"),
                           BVIDEO_STREAM_ENABLED=True, BVIDEO_DAILY_TIDS=[21],
                           BVIDEO_BACKLOG_HIGH_WATER=100, BVIDEO_BACKLOG_LOW_WATER=50),
            patch.object(config, "source_on", return_value=True),
            patch.object(stream, "_enrich_turn", 0),
            patch.object(stream, "_threads", {}),
            patch("radar.activity.say"),
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

    def add(self, number, recent=False):
        record = dict(bvid=f"BV1{number:09d}", date="2026-10-21", title="[Example]",
                      owner_id="123", owner_name="Author", tid=21, tid_v2=2079,
                      tags=["Example", "Other"], views=100, likes=10, coins=5,
                      favorites=4, shares=3, replies=2)
        with db.session() as c:
            c.execute("INSERT INTO bd_videos(bvid,day,title,owner_id,tid) VALUES(?,?,?,?,?)",
                      (record["bvid"], record["date"], record["title"], record["owner_id"], 21))
            queue.enqueue(c, record["bvid"], recent=recent)
        return record

    def test_daily_only_video_is_fetched_by_shared_workers_and_counted_globally(self):
        record = self.add(1)
        with patch.object(bili, "detail_with_tags", return_value=record) as request:
            self.assertTrue(stream.enrich_once())
            self.assertFalse(stream.enrich_once())
        self.assertEqual(request.call_count, 1)
        result = stream.summary()
        self.assertEqual((result["videos"], result["tagged"], result["pending_tags"]), (1, 1, 0))
        self.assertEqual(result["realtime"]["videos"], 0)
        self.assertEqual(result["tags_last_minute"], 1)
        self.assertEqual(result["statistics_scope"], "all_saved_videos")

    def test_migration_imports_all_debt_preserves_retry_and_never_requeues_success(self):
        first, second = self.add(1), self.add(2)
        deadline = time.time() + 21600
        with db.session() as c:
            c.execute("UPDATE bd_videos SET detail_retry_after=?,detail_attempts=7 WHERE bvid=?",
                      (deadline, first["bvid"]))
            c.execute("UPDATE bd_videos SET detail_state='ok' WHERE bvid=?", (second["bvid"],))
            c.execute("DELETE FROM bs_pending WHERE bvid=?", (first["bvid"],))
            c.execute("DELETE FROM kv WHERE k='bili_tag_queue_revision'")
        daily._ready.discard(config.DB_PATH)
        daily.init()
        daily._ready.discard(config.DB_PATH)
        daily.init()
        with db.session() as c:
            rows = c.execute("SELECT * FROM bs_pending").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bvid"], first["bvid"])
        self.assertEqual((rows[0]["retry_after"], rows[0]["attempts"]), (deadline, 7))
        self.assertEqual(stream.summary()["queue"]["deferred"], 1)

    def test_one_shared_backoff_clock_survives_restart(self):
        record = self.add(1)
        with patch.object(bili, "detail_with_tags", side_effect=RuntimeError("code=62012")) as get:
            self.assertFalse(stream.enrich_once())
            stream._ready.discard(config.DB_PATH)
            self.assertFalse(stream.enrich_once())
        self.assertEqual(get.call_count, 1)
        with db.session() as c:
            row = c.execute("""SELECT p.retry_after,p.attempts,v.detail_retry_after,v.detail_attempts
                FROM bs_pending p JOIN bd_videos v ON v.bvid=p.bvid""").fetchone()
        self.assertEqual(row["retry_after"], row["detail_retry_after"])
        self.assertEqual(row["attempts"], row["detail_attempts"])
        self.assertGreaterEqual(row["retry_after"], time.time() + 290)

    def test_archive_is_idempotent_retains_completed_tags_samples_and_retry_history(self):
        complete, pending, error = self.add(1), self.add(2), self.add(3)
        daily._save_detail(complete)
        growth.init()
        with db.session() as c:
            c.execute("UPDATE bd_videos SET detail_state='error',detail_attempts=7,"
                      "detail_error='saved failure',detail_retry_after=123 WHERE bvid=?",
                      (error["bvid"],))
        policy = queue.archive_pending(now=time.time())
        self.assertEqual((policy["archived"], policy["completed_retained"]), (2, 1))
        new = self.add(4, recent=True)
        self.assertEqual(queue.archive_pending(), policy)
        with db.session() as c:
            row = c.execute("SELECT * FROM bd_videos WHERE bvid=?", (error["bvid"],)).fetchone()
            self.assertEqual((row["detail_state"], row["detail_attempts"], row["detail_error"]),
                             ("archived", 7, "saved failure"))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bg_samples").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT tags FROM bd_videos WHERE bvid=?",
                                      (complete["bvid"],)).fetchone()[0], json.dumps(complete["tags"]))
            self.assertEqual(c.execute("SELECT bvid FROM bs_pending").fetchone()[0], new["bvid"])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bq_analysis").fetchone()[0], 1)
        result = stream.summary()
        self.assertEqual((result["videos"], result["tagged"], result["pending_tags"]), (2, 1, 1))
        self.assertTrue(result["queue"]["historical_disabled"])

    def test_archived_jobs_never_reappear_after_migration_rediscovery_or_manual_enrichment(self):
        record = self.add(1)
        queue.archive_pending()
        with db.session() as c:
            c.execute("DELETE FROM kv WHERE k='bili_tag_queue_revision'")
        daily._ready.discard(config.DB_PATH)
        daily.init()
        stream._save_sample(21, {"list": [record]})
        with patch.object(bili, "detail_with_tags") as request:
            self.assertTrue(daily.enrich_video(record["bvid"]))
            self.assertFalse(stream.enrich_once())
        request.assert_not_called()
        with db.session() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bs_pending").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT detail_state FROM bd_videos").fetchone()[0], "archived")

    def test_reset_blocks_directory_runs_and_gap_backfill(self):
        queue.archive_pending()
        with patch.object(bili, "dated_videos") as listing, patch.object(daily, "ensure_day") as ensure:
            self.assertEqual(daily.run_once()["status"], "archived")
            self.assertEqual(latest.register_gap_days(), [])
        listing.assert_not_called()
        ensure.assert_not_called()

    def test_manual_refresh_after_reset_never_falls_back_to_legacy_search(self):
        from radar import collect
        queue.archive_pending()
        with patch.object(config, "BVIDEO_DAILY_ENABLED", False), \
                patch.object(latest, "poll_once", return_value={"status": "latest", "new": 3}) as head, \
                patch("radar.video.collect") as legacy:
            self.assertEqual(collect._bvideo(0), (3, ""))
        head.assert_called_once_with(force=True)
        legacy.assert_not_called()

    def test_reset_excludes_pre_cutover_posts_but_keeps_new_head_continuity(self):
        now = int(time.time())
        queue.archive_pending(now=now)
        records = [dict(self.add(n), tid=17, published=now + offset) for n, offset in ((1, -1), (2, 1))]
        with db.session() as c:
            c.execute("DELETE FROM bs_pending")
        result = {"list": records, "earliest": now - 1, "latest": now + 1}
        with patch.object(bili, "latest_game_posts", return_value=result):
            head = latest.poll_once(force=True)
        self.assertEqual(head["new"], 1)
        self.assertEqual(len(head["bvids"]), 2)
        with db.session() as c:
            self.assertEqual(c.execute("SELECT bvid FROM bs_inbox").fetchone()[0], records[1]["bvid"])

    def test_raw_commit_does_not_run_derived_analysis_and_failed_analysis_never_refetches(self):
        record = self.add(1)
        with patch.object(bili_terms, "seed") as seed, patch.object(growth, "record") as index:
            daily._save_detail(record)
        seed.assert_not_called()
        index.assert_not_called()
        with patch.object(bili_terms, "seed", side_effect=RuntimeError("local failure")):
            self.assertEqual(queue.analyze_once(), 0)
        with db.session() as c:
            row = c.execute("SELECT * FROM bq_analysis").fetchone()
            self.assertEqual(row["attempts"], 1)
            self.assertIn("local failure", row["error"])
            self.assertEqual(c.execute("SELECT detail_state FROM bd_videos").fetchone()[0], "ok")
            c.execute("UPDATE bq_analysis SET retry_after=0")
        with patch.object(bili, "detail_with_tags") as fetch:
            self.assertFalse(stream.enrich_once())
            self.assertEqual(queue.analyze_once(), 1)
            self.assertEqual(queue.analyze_once(), 0)
        fetch.assert_not_called()
        with db.session() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bg_hits").fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bg_samples").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bq_analysis").fetchone()[0], 0)

    def test_outbox_failure_rolls_back_raw_completion_and_queue_exit(self):
        record = self.add(1)
        with patch.object(queue, "defer_analysis", side_effect=RuntimeError("disk failure")):
            with self.assertRaisesRegex(RuntimeError, "disk failure"):
                daily._save_detail(record)
        with db.session() as c:
            self.assertEqual(c.execute("SELECT detail_state FROM bd_videos").fetchone()[0], "pending")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bs_pending").fetchone()[0], 1)
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM bs_events WHERE kind='tag_completed'").fetchone()[0], 0)

    def test_backpressure_stops_only_directory_expansion_and_resumes_at_low_water(self):
        records = [self.add(n) for n in range(1, 102)]
        daily.ensure_day("2026-10-21")
        with patch.object(bili, "dated_videos") as listing, \
                patch.object(bili, "detail_with_tags") as details:
            result = daily.run_once("2026-10-21", 5, 10, enqueue_only=True)
        self.assertEqual(result["status"], "backpressure")
        listing.assert_not_called()
        details.assert_not_called()
        with patch.object(bili, "detail_with_tags", return_value=records[0]):
            self.assertTrue(stream.enrich_once())
        with db.session() as c:
            self.assertTrue(queue.directory_pressure(c))
            c.execute("DELETE FROM bs_pending WHERE rowid IN "
                      "(SELECT rowid FROM bs_pending LIMIT 51)")
            self.assertFalse(queue.directory_pressure(c))

    def test_directory_enqueue_mode_never_runs_a_second_detail_worker(self):
        daily.ensure_day("2026-10-21")
        page = {"total": 1, "pages": 1, "page_size": 100, "list": [
            {"bvid": "BV1000000001", "pubdate": "2026-10-21 23:59:00",
             "tid": 21, "title": "Example"}]}
        with patch.object(bili, "dated_videos", return_value=page), \
                patch.object(bili, "detail_with_tags") as details:
            result = daily.run_once("2026-10-21", 5, 10, enqueue_only=True)
        details.assert_not_called()
        self.assertEqual(result["status"], "waiting_details")
        with db.session() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bs_pending").fetchone()[0], 1)

    def test_urgent_recent_queue_gets_nine_slots_but_history_keeps_one(self):
        old = self.add(1)
        records = {old["bvid"]: old}
        for number in range(2, 12):
            record = self.add(number, recent=True)
            records[record["bvid"]] = record
        with db.session() as c:
            c.execute("UPDATE bs_pending SET queued=? WHERE lane='recent'", (time.time() - 180,))
        called = []
        def fetch(bvid, **options):
            called.append(bvid)
            return records[bvid]
        with patch.object(bili, "detail_with_tags", side_effect=fetch):
            for _ in range(10):
                self.assertTrue(stream.enrich_once())
        self.assertEqual(called[9], old["bvid"])
        self.assertNotIn(old["bvid"], called[:9])

    def test_playback_refresh_yields_when_recent_tag_latency_is_over_budget(self):
        self.add(1, recent=True)
        with db.session() as c:
            c.execute("UPDATE bs_pending SET queued=?", (time.time() - 180,))
        with patch.object(bili, "_plain") as request:
            self.assertFalse(growth.sample_once())
        request.assert_not_called()

    def test_job_selection_uses_queue_order_without_catalogue_scan_or_temp_sort(self):
        self.add(1)
        now = time.time()
        with db.session() as c:
            plan = [row["detail"] for row in c.execute(
                "EXPLAIN QUERY PLAN " + queue.READY_QUERY.format(window="1"),
                ("backlog", now, now, now, 5))]
        self.assertIn("bq_lane", " ".join(plan))
        self.assertNotIn("TEMP B-TREE", " ".join(plan))
        self.assertNotIn("SEARCH v USING INDEX bd_detail_state (detail_state=?)", " ".join(plan))

    def test_unchanged_word_catalogue_refresh_does_not_rewrite_every_term(self):
        daily._save_detail(self.add(1))
        queue.analyze_once()
        with db.session() as c:
            bili_terms.refresh(c)
            before = c.total_changes
            bili_terms.refresh(c)
            self.assertEqual(c.total_changes, before)

    def test_word_refresh_is_bounded_and_resumes_dirty_terms(self):
        daily._save_detail(self.add(1))
        queue.analyze_once()
        with db.session() as c:
            initial = c.execute("SELECT COUNT(*) FROM bd_term_dirty").fetchone()[0]
        self.assertGreater(initial, 1)
        self.assertEqual(bili_terms.refresh_dirty(limit=1), 1)
        with db.session() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bd_term_dirty").fetchone()[0], initial - 1)
        bili_terms.refresh_dirty()
        self.assertEqual(bili_terms.refresh_dirty(), 0)

    def test_bounded_catalogue_sweep_revisits_old_words_after_rules_change(self):
        daily._save_detail(self.add(1))
        queue.analyze_once()
        bili_terms.refresh_dirty()
        with patch.object(config, "BVIDEO_WORD_BLACKLIST", ["Example"]):
            bili_terms.refresh_dirty(sweep=10)
        with db.session() as c:
            self.assertEqual(c.execute(
                "SELECT status FROM bd_terms WHERE norm='example'").fetchone()[0], "blocked")


if __name__ == "__main__":
    unittest.main()
