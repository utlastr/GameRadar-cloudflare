import json
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from radar import bili_daily as daily, bili_queue, bili_terms as terms, collect, config, db, server, video
from radar import bili_realtime_client as bili

DAY = "2026-10-21"
SWORD = "\u94f8\u5251\u5927\u5e08"
BIG = "\u738b\u8005\u8363\u8000"


def bv(n):
    return "BV%010d" % n


def rows(start, count, tid=21, day=DAY):
    return [{"bvid": bv(i), "title": "[%s]" % SWORD, "mid": i % 2 + 1,
             "pubdate": day + " 23:59:00", "tid": tid}
            for i in range(start, start + count)]


def page(items, total=None, size=100):
    total = len(items) if total is None else total
    return {"list": items, "total": total, "pages": (total + size - 1) // size,
            "page_size": size}


def detail(n, title=None, tags=None, day=DAY):
    return dict(bvid=bv(n), date=day, title=title or "[%s]" % SWORD,
                owner_id=str(n % 2 + 1), owner_name="Author", tid=21, tid_v2=2079,
                tags=tags if tags is not None else [SWORD, BIG],
                views=100, likes=10, coins=5, favorites=4, shares=3, replies=2)


class DatedApiTests(unittest.TestCase):
    def setUp(self):
        self.values = {}
        self.patches = [
            patch.object(bili.db, "kv_get", side_effect=lambda k, default=None: self.values.get(k, default)),
            patch.object(bili.db, "kv_set", side_effect=lambda k, v: self.values.__setitem__(k, v)),
            patch.object(bili, "REQUEST_GAP", 0), patch("radar.activity.say")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_date_directory_requests_100_without_keywords_or_cookies(self):
        payload = {"code": 0, "data": {"numResults": 101, "numPages": 2,
                                      "result": rows(0, 100)}}
        with patch.object(bili, "get_json_strict", return_value=payload) as request, \
                patch.object(bili, "cookie") as cookie:
            result = bili.dated_videos(21, DAY, 1, 100)
        args = request.call_args.args[1]
        self.assertEqual(args["pagesize"], 100)
        self.assertEqual(args["time_from"], "20261021")
        self.assertEqual(args["time_to"], "20261021")
        self.assertEqual(args["cate_id"], 21)
        self.assertNotIn("keyword", args)
        cookie.assert_not_called()
        self.assertEqual(len(result["list"]), 100)
        self.assertEqual(bili._endpoint(request.call_args.args[0]), "\u65e5\u671f\u76ee\u5f55")

    def test_wrong_day_or_missing_counts_is_not_empty_success(self):
        for payload in [
            {"result": []},
            {"numResults": 2, "numPages": 1, "result": []},
            {"numResults": 1, "numPages": 1, "result": rows(0, 1, day="2026-10-20")},
        ]:
            with self.subTest(payload=payload), \
                    patch.object(bili, "_plain", return_value={"code": 0, "data": payload}):
                with self.assertRaises(RuntimeError):
                    bili.dated_videos(21, DAY)

    def test_zero_code_voucher_is_risk_not_zero_results(self):
        with patch.object(bili, "get_json_strict", return_value={
                "code": 0, "data": {"v_voucher": "challenge"}}):
            with self.assertRaises(bili.RiskError):
                bili.dated_videos(21, DAY)
        self.assertTrue(bili.paused("\u65e5\u671f\u76ee\u5f55"))
        self.assertEqual(self.values["bili_risk_hits:\u65e5\u671f\u76ee\u5f55"], 1)

    def payload(self):
        return {"code": 0, "data": {
            "View": {"bvid": bv(1), "pubdate": DAY + " 12:00:00", "title": SWORD,
                     "owner": {"mid": 7, "name": "Author"}, "tid": 184, "tid_v2": 2079,
                     "stat": dict(view=100, like=10, coin=5, favorite=4, share=3, reply=2, danmaku=99)},
            "Tags": [{"tag_name": SWORD}, {"tag_name": BIG}]}}

    def test_one_detail_call_gets_all_tags_and_six_counters(self):
        with patch.object(bili, "get_json_strict", return_value=self.payload()) as request:
            result = bili.detail_with_tags(bv(1))
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result["tags"], [SWORD, BIG])
        self.assertEqual(result["tid"], 184)
        self.assertEqual(result["tid_v2"], 2079)
        self.assertEqual(result["date"], DAY)
        self.assertNotIn("danmaku", result)
        self.assertNotIn("url", result)
        self.assertEqual(result["replies"], 2)

    def test_missing_tags_or_counter_is_error(self):
        for missing in ("Tags", "share"):
            payload = self.payload()
            if missing == "Tags":
                del payload["data"]["Tags"]
            else:
                del payload["data"]["View"]["stat"][missing]
            with patch.object(bili, "_plain", return_value=payload):
                with self.assertRaises(RuntimeError):
                    bili.detail_with_tags(bv(1))

    def test_publication_day_is_beijing_not_utc(self):
        self.assertEqual(bili.published_day("2026-10-20T18:00:00Z"), DAY)
        self.assertEqual(bili.published_day("2026-10-21 12:00:00"), DAY)


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch.multiple(config, DATA_DIR=self.tmp.name,
                           DB_PATH=str(Path(self.tmp.name) / "test.db"),
                           BVIDEO_DAILY_ENABLED=True, BVIDEO_DAILY_TIDS=[21], BVIDEO_DAILY_PAGE_SIZE=100,
                           BVIDEO_WORD_BLACKLIST=[], BVIDEO_PERSON_NAMES=["SomeHost"],
                           BVIDEO_NAME_ALIASES={}, KW_BIG_GAMES=[BIG],
                           BVIDEO_CANDIDATE_MIN_VIDEOS=3, BVIDEO_CANDIDATE_MIN_AUTHORS=2),
            patch("radar.activity.say"), patch.object(bili, "_guard")]
        for p in self.patches:
            p.start()
        db.init()
        daily.init()
        daily.ensure_day(DAY)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def job(self, tid=21):
        with db.session() as c:
            return dict(c.execute("SELECT * FROM bd_partitions WHERE day=? AND tid=?", (DAY, tid)).fetchone())

    def insert(self, n, **kwargs):
        record = detail(n, **kwargs)
        with db.session() as c:
            c.execute("INSERT INTO bd_videos(bvid,day,title,owner_id,tid) VALUES(?,?,?,?,?)",
                      (record["bvid"], DAY, record["title"], record["owner_id"], 21))
        daily._save_detail(record)
        bili_queue.analyze_once()

    def test_full_page_and_tail_commit_exact_unique_count(self):
        daily._save_page(self.job(), page(rows(0, 100), 103))
        self.assertEqual(self.job()["next_page"], 2)
        daily._save_page(self.job(), page(rows(100, 3), 103))
        self.assertEqual(self.job()["state"], "complete")
        self.assertEqual(daily.summary(DAY)["videos"], 103)

    def test_repeat_page_rolls_back_without_advancing_checkpoint(self):
        daily._save_page(self.job(), page(rows(0, 100), 200))
        with self.assertRaisesRegex(RuntimeError, "overlap"):
            daily._save_page(self.job(), page(rows(0, 100), 200))
        self.assertEqual(self.job()["next_page"], 2)
        self.assertEqual(daily.summary(DAY)["videos"], 100)

    def test_wrong_category_and_count_cap_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "category"):
            daily._save_page(self.job(), page(rows(0, 1, tid=65)))
        result = page(rows(0, 100), 10000)
        result["pages"] = 50
        with self.assertRaisesRegex(RuntimeError, "capped"):
            daily._save_page(self.job(), result)
        self.assertEqual(daily.summary(DAY)["videos"], 0)

    def test_changed_counts_restart_page_checkpoint_but_keep_saved_videos(self):
        daily._save_page(self.job(), page(rows(0, 100), 200))
        with patch.object(bili, "dated_videos", return_value=page(rows(100, 99), 199)):
            result = daily.run_once(DAY, 1, 60)
        self.assertEqual(result["status"], "in_progress")
        self.assertEqual(result["videos"], 100)
        self.assertEqual(self.job()["next_page"], 1)
        self.assertEqual(self.job()["state"], "waiting_index")
        self.assertIsNone(self.job()["total"])
        with db.session() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bd_partition_items").fetchone()[0], 0)

    def test_budget_resumes_without_refetching_lists_or_completed_details(self):
        with patch.object(bili, "dated_videos", return_value=page(rows(1, 2))) as listing, \
                patch.object(bili, "detail_with_tags", side_effect=lambda b, **kw: detail(int(b[2:]))) as get:
            self.assertEqual(daily.run_once(DAY, 1, 60)["status"], "in_progress")
            self.assertEqual(daily.run_once(DAY, 1, 60)["tagged"], 1)
            self.assertEqual(daily.run_once(DAY, 5, 60)["status"], "complete")
        self.assertEqual(listing.call_count, 1)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(listing.call_args.args, (21, DAY, 1, 100))

    def test_same_bv_in_two_directories_only_fetches_details_once(self):
        with patch.object(config, "BVIDEO_DAILY_TIDS", [21, 138]), \
                patch.object(bili, "dated_videos", side_effect=lambda tid, *a, **kw: page(rows(1, 1, tid=tid))), \
                patch.object(bili, "detail_with_tags", return_value=detail(1)) as get:
            result = daily.run_once(DAY, 10, 60)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["videos"], 1)
        self.assertEqual(get.call_count, 1)

    def test_stale_directory_saves_videos_and_tags_without_blocking_other_categories(self):
        stale = page(rows(0, 1, tid=17))
        stale["list"][0]["pubdate"] = DAY + " 01:02:30"
        with patch.object(config, "BVIDEO_DAILY_TIDS", [17, 21]), \
                patch.object(bili, "dated_videos", side_effect=lambda tid, *a, **kw:
                             stale if tid == 17 else page(rows(1, 1))), \
                patch.object(bili, "detail_with_tags", side_effect=lambda b, **kw: detail(int(b[2:]))) as get:
            result = daily.run_once(DAY, 10, 60)
            daily.run_once(DAY, 10, 60)
        self.assertEqual(result["status"], "waiting_index")
        self.assertEqual(result["requests"], 4)
        self.assertEqual(result["videos"], 2)
        self.assertEqual(result["tagged"], 2)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(self.job(17)["state"], "waiting_index")
        self.assertEqual(self.job(21)["state"], "complete")
        self.assertIn("01:02:30", self.job(17)["latest"])

    def test_late_video_on_later_page_passes_whole_directory_watermark(self):
        with patch.object(config, "BVIDEO_DAILY_TIDS", [17]):
            daily.ensure_day(DAY)
        early = rows(0, 100, tid=17)
        for row in early:
            row["pubdate"] = DAY + " 01:00:00"
        daily._save_page(self.job(17), page(early, 101))
        self.assertEqual(self.job(17)["state"], "listing")
        daily._save_page(self.job(17), page(rows(100, 1, tid=17), 101))
        self.assertEqual(self.job(17)["state"], "complete")
        self.assertIn("23:59:00", self.job(17)["latest"])
        self.assertIn("01:00:00", self.job(17)["earliest"])

    def test_stale_retry_rebuilds_snapshot_not_successful_tags(self):
        with patch.object(config, "BVIDEO_DAILY_TIDS", [17]):
            daily.ensure_day(DAY)
        early = rows(0, 1, tid=17)
        early[0]["pubdate"] = DAY + " 01:00:00"
        daily._save_page(self.job(17), page(early))
        daily._save_detail(detail(0))
        daily._reset_due_partitions(DAY)
        self.assertEqual(self.job(17)["state"], "waiting_index")
        with db.session() as c:
            c.execute("UPDATE bd_partitions SET retry_after=0 WHERE tid=17")
        daily._reset_due_partitions(DAY)
        self.assertEqual(self.job(17)["next_page"], 1)
        self.assertIsNone(self.job(17)["latest"])
        self.assertEqual(daily.summary(DAY)["tagged"], 1)
        daily._save_page(self.job(17), page(rows(0, 1, tid=17)))
        self.assertEqual(self.job(17)["state"], "complete")

    def test_large_directories_interleave_with_tags(self):
        with patch.object(bili, "dated_videos", side_effect=lambda tid, day, pn, size, **kw:
                          page(rows((pn - 1) * 100, 100), 1000)) as listing, \
                patch.object(bili, "detail_with_tags", side_effect=lambda b, **kw: detail(int(b[2:]))) as get:
            result = daily.run_once(DAY, 4, 60)
        self.assertEqual(listing.call_count, 2)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(result["videos"], 200)
        self.assertEqual(result["tagged"], 2)

    def test_index_guard_accepts_late_first_page_and_rejects_zero(self):
        daily._index_guard(17, DAY, page(rows(0, 1, tid=17)))
        with self.assertRaises(daily.IndexPending):
            daily._index_guard(65, DAY, page([]))

    def test_risk_stops_immediately_and_preserves_saved_page(self):
        with patch.object(bili, "dated_videos", return_value=page(rows(1, 2))), \
                patch.object(bili, "pause_left", return_value=1800), \
                patch.object(bili, "detail_with_tags", side_effect=bili.RiskError("blocked")) as get:
            result = daily.run_once(DAY, 10, 60)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["videos"], 2)
        self.assertEqual(result["tagged"], 0)
        self.assertEqual(get.call_count, 1)

    def test_deleted_video_is_partial_not_complete(self):
        with patch.object(bili, "dated_videos", return_value=page(rows(1, 1))), \
                patch.object(bili, "detail_with_tags", side_effect=RuntimeError("code=-404")):
            result = daily.run_once(DAY, 10, 60)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["unavailable"], 1)
        self.assertEqual(result["tagged"], 0)

    def test_invisible_video_does_not_block_remaining_tags(self):
        with patch.object(bili, "dated_videos", return_value=page(rows(1, 2))), \
                patch.object(bili, "detail_with_tags", side_effect=[
                    RuntimeError("code=62002 unavailable"), detail(2)]):
            result = daily.run_once(DAY, 10, 60)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["unavailable"], 1)
        self.assertEqual(result["tagged"], 1)

    def test_missing_tags_error_is_not_counted_as_complete(self):
        with patch.object(bili, "dated_videos", return_value=page(rows(1, 1))), \
                patch.object(bili, "detail_with_tags", side_effect=RuntimeError("missing Tags")):
            result = daily.run_once(DAY, 10, 60)
        self.assertEqual(result["status"], "waiting_details")
        self.assertEqual(result["tagged"], 0)
        self.assertEqual(result["detail_errors"], 1)

    def test_62012_does_not_block_later_videos_and_has_shared_durable_retry(self):
        with patch.object(bili, "dated_videos", return_value=page(rows(1, 2))), \
                patch.object(bili, "detail_with_tags", side_effect=[
                    RuntimeError("code=62012"), detail(2)]) as request:
            result = daily.run_once(DAY, 10, 60)
            again = daily.run_once(DAY, 10, 60)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result["status"], "waiting_details")
        self.assertEqual(result["tagged"], 1)
        self.assertEqual(result["unavailable"], 0)
        self.assertEqual(result["detail_errors"], 1)
        self.assertEqual(again["tagged"], 1)
        with db.session() as c:
            bad = c.execute("SELECT * FROM bd_videos WHERE bvid=?", (bv(1),)).fetchone()
            self.assertEqual(bad["detail_attempts"], 1)
            self.assertGreater(bad["detail_retry_after"], 0)
            c.execute("UPDATE bd_days SET retry_after=0")
            c.execute("UPDATE bd_videos SET detail_retry_after=0 WHERE bvid=?", (bv(1),))
        with patch.object(bili, "detail_with_tags", return_value=detail(1)), \
                patch.object(bili, "dated_videos") as listing:
            self.assertEqual(daily.run_once(DAY, 10, 60)["status"], "complete")
        listing.assert_not_called()

    def test_repeated_business_failure_has_long_backoff_but_is_not_discarded(self):
        daily._save_page(self.job(), page(rows(1, 1)))
        for attempts, minimum_delay in ((0, 300), (1, 21600), (3, 86400)):
            with self.subTest(attempts=attempts):
                with db.session() as c:
                    c.execute("""UPDATE bd_videos SET detail_attempts=?,detail_retry_after=0
                        WHERE bvid=?""", (attempts, bv(1)))
                before = time.time()
                with patch.object(bili, "detail_with_tags", side_effect=RuntimeError("code=62012")):
                    with self.assertRaisesRegex(RuntimeError, "62012"):
                        daily.enrich_video(bv(1))
                with db.session() as c:
                    row = c.execute("SELECT * FROM bd_videos WHERE bvid=?", (bv(1),)).fetchone()
                self.assertEqual(row["detail_state"], "error")
                self.assertEqual(row["detail_attempts"], attempts + 1)
                self.assertGreaterEqual(row["detail_retry_after"], before + minimum_delay)
                with patch.object(bili, "detail_with_tags") as request:
                    self.assertFalse(daily.enrich_video(bv(1)))
                request.assert_not_called()

    def test_unstable_partition_does_not_abort_another_partition(self):
        daily._save_page(self.job(), page(rows(0, 100), 200))
        with patch.object(config, "BVIDEO_DAILY_TIDS", [21, 138]):
            daily.ensure_day(DAY)
        def fetch(tid, *args, **kwargs):
            return page(rows(100, 99), 199) if tid == 21 else page(rows(999, 1, tid=138))
        with patch.object(bili, "dated_videos", side_effect=fetch), \
                patch.object(bili, "detail_with_tags", side_effect=lambda b, **kw: detail(int(b[2:]))):
            result = daily.run_once(DAY, 6, 60)
        self.assertEqual(result["status"], "in_progress")
        self.assertEqual(self.job(21)["state"], "waiting_index")
        self.assertEqual(self.job(138)["state"], "complete")
        self.assertGreaterEqual(result["tagged"], 1)
        self.assertEqual(result["videos"], 101)

    def test_daily_logging_is_readable_and_same_state_is_throttled(self):
        result = dict(day=DAY, status="in_progress", videos=200, tagged=3, pending_details=197)
        with patch.object(daily.activity, "say") as log:
            daily._log_progress(result)
            daily._log_progress(result)
        self.assertEqual(log.call_count, 1)
        self.assertNotIn("complete Tags", log.call_args.args[0])
        self.assertNotIn("videos", log.call_args.args[0])

    def test_migration_releases_old_detail_backoff_without_clearing_risk_pause(self):
        with db.session() as c:
            c.execute("""INSERT INTO bd_videos(bvid,day,detail_state,detail_error)
                VALUES(?,?,'error','code=62012')""", (bv(1), DAY))
            c.execute("""UPDATE bd_days SET status='error',retry_after=9999999999,
                error=? WHERE day=?""", ("RuntimeError: B\u7ad9\u89c6\u9891\u8be6\u60c5\u53ca\u6807\u7b7e code=62012", DAY))
        db.kv_set("bili_pause_until", 9999999999)
        daily._ready.discard(config.DB_PATH)
        daily.init()
        result = daily.summary(DAY)
        self.assertEqual(result["status"], "paused")
        with db.session() as c:
            self.assertEqual(c.execute("SELECT status FROM bd_days WHERE day=?",
                                      (DAY,)).fetchone()[0], "in_progress")
        self.assertEqual(result["retry_after"], 0)
        self.assertEqual(db.kv_get("bili_pause_until"), 9999999999)

    def test_stale_index_log_explains_partition_without_internal_exception_name(self):
        result = dict(day=DAY, status="waiting_index", videos=200, tagged=3,
                      pending_details=197,
                      error=f"Old directory watermark is too early: {DAY}T01:00:00+08:00, tid=17")
        with patch.object(daily.activity, "say") as log:
            daily._log_progress(result)
        self.assertNotIn("watermark", log.call_args.args[0])
        self.assertNotIn("tid=17", log.call_args.args[0])
        self.assertIn("\u5355\u673a\u6e38\u620f", log.call_args.args[0])

    def test_explicit_day_does_not_run_another_older_job(self):
        daily.ensure_day("2026-10-20")
        with patch.object(bili, "dated_videos", return_value=page(rows(1, 1))) as get:
            result = daily.run_once(DAY, 1, 60)
        self.assertEqual(get.call_args.args[1], DAY)
        self.assertEqual(result["day"], DAY)

    def test_lease_released_when_local_rescan_fails(self):
        with patch.object(bili, "dated_videos", return_value=page(rows(1, 1))), \
                patch.object(terms, "rescan", side_effect=RuntimeError("local failure")):
            with self.assertRaisesRegex(RuntimeError, "local failure"):
                daily.run_once(DAY, 1, 60)
        with db.session() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bd_lease").fetchone()[0], 0)

    def test_bracket_sources_exclude_date_host_numbers_and_stream_noise(self):
        title = "[2026-10-21] [SomeHost] [3] [Steam] [%s] (Forge Tax)" % SWORD
        result = terms.candidates(title, ["\u76f4\u64ad\u56de\u653e", BIG])
        self.assertEqual({r[0] for r in result}, {SWORD, "forgetax"})

    def test_word_blacklist_preserves_mixed_video_and_full_raw_tags(self):
        for n in range(1, 4):
            self.insert(n)
        with db.session() as c:
            terms.rescan(c, "2026-10-24")
            results = terms.topics(c)
            raw = json.loads(c.execute("SELECT tags FROM bd_videos LIMIT 1").fetchone()[0])
        self.assertEqual([r["name"] for r in results], [SWORD])
        self.assertEqual(results[0]["videos"], 3)
        self.assertEqual(results[0]["views"], 300)
        self.assertEqual(raw, [SWORD, BIG])

    def test_same_author_repetition_does_not_promote(self):
        for n in range(1, 4):
            self.insert(n)
        with db.session() as c:
            c.execute("UPDATE bd_videos SET owner_id='same'")
            terms.rescan(c, "2026-10-24")
            self.assertEqual(terms.topics(c), [])

    def test_new_candidate_recovers_early_unbracketed_title(self):
        self.insert(0, title=SWORD + " first look", tags=[])
        with db.session() as c:
            terms.rescan(c, "2026-10-24")
            self.assertEqual(terms.topics(c), [])
        for n in range(1, 4):
            self.insert(n)
        with db.session() as c:
            terms.rescan(c, "2026-10-24")
            result = terms.topics(c)[0]
        self.assertEqual(result["videos"], 4)
        self.assertEqual(result["views"], 400)

    def test_multiple_evidence_dedup_and_new_titles_incremental(self):
        for n in range(1, 4):
            self.insert(n)
        with db.session() as c:
            terms.rescan(c, "2026-10-24")
            self.assertEqual(terms.topics(c)[0]["views"], 300)
        self.insert(4, title="Playing " + SWORD, tags=[])
        with db.session() as c:
            terms.rescan(c, "2026-10-24")
            self.assertEqual(terms.topics(c)[0]["videos"], 4)
            with patch.object(terms.Matcher, "find") as find:
                terms.rescan(c, "2026-10-24")
                find.assert_not_called()

    def test_aliases_match_without_merging_unrelated_names(self):
        with patch.object(config, "BVIDEO_NAME_ALIASES", {SWORD: ["Forge Tax", "ForgeTax"]}):
            found = terms.candidates("[Forge Tax]", ["ForgeTax"])
            self.assertEqual({r[0] for r in found}, {SWORD})
            self.assertEqual(terms.Matcher({SWORD}).find("ForgeTax\u521d\u4f53\u9a8c"), {SWORD})
            self.assertEqual(terms.Matcher({SWORD}).find("Forge Tax first look"), {SWORD})
            self.assertEqual(terms.Matcher({SWORD}).find("ForgeTax: gameplay"), {SWORD})
        self.assertEqual(terms.Matcher({"forgetax"}).find("notforgetaxword"), set())

    def test_known_entity_exact_names_only_unknowns_remain_candidates(self):
        self.insert(1)
        with db.session() as c:
            c.execute("INSERT INTO entities VALUES(?,?,?,?,?,?)", ("steam", "7", SWORD, 0, 0, "{}"))
            c.execute("INSERT INTO entities VALUES(?,?,?,?,?,?)", ("steam", "8", "Other", 0, 0, "broken"))
            terms.rescan(c, "2026-10-24")
            found = terms.topics(c)[0]
        self.assertEqual(found["status"], "known")
        self.assertEqual(json.loads(found["known_ids"]), ["steam:7"])

    def test_two_topics_each_count_same_video_once(self):
        for n in range(1, 4):
            self.insert(n, tags=[SWORD, "OtherGame"])
        with db.session() as c:
            terms.rescan(c, "2026-10-24")
            found = terms.topics(c)
        self.assertEqual(len(found), 2)
        self.assertEqual([t["views"] for t in found], [300, 300])
        self.assertEqual(daily.summary(DAY)["videos"], 3)

    def test_daily_metadata_in_existing_summary(self):
        self.assertEqual(video.summary()["daily"]["day"], DAY)

    def test_manual_refresh_routes_to_daily_not_old_sampler(self):
        with patch.object(daily, "run_once", return_value={"status": "in_progress", "videos": 12}), \
                patch.object(video, "collect") as legacy:
            self.assertEqual(collect._bvideo(0), (12, ""))
        legacy.assert_not_called()

    def test_manual_refresh_reports_stale_index_as_failure(self):
        with patch.object(daily, "run_once", return_value={
                "status": "waiting_index", "error": "stale directory"}):
            with self.assertRaisesRegex(RuntimeError, "stale directory"):
                collect._bvideo(0)

    def test_daily_worker_does_not_start_an_extra_steam_sampling_timer(self):
        with patch.object(video, "_started", False), patch.object(daily, "start") as start, \
                patch("radar.bili_stream.start"), \
                patch("radar.bili_term_filter.start") as filter_start, \
                patch.object(video.threading, "Thread") as thread:
            video.start()
        start.assert_called_once_with(None)
        filter_start.assert_called_once_with(None)
        thread.assert_not_called()

    def test_http_daily_status_returns_saved_day(self):
        http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        worker = threading.Thread(target=http.serve_forever, daemon=True)
        worker.start()
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%s/api/bili-daily?day=%s" % (http.server_port, DAY)) as response:
                result = json.load(response)
            self.assertEqual(result["day"], DAY)
            self.assertEqual(result["page_size"], 100)
        finally:
            http.shutdown()
            http.server_close()
            worker.join(5)


if __name__ == "__main__":
    unittest.main()
