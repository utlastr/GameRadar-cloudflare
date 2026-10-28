import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from radar import config, db, heat
from radar import social
from radar.social import extract, funnel, jobs, page, settings, store, validation


def captured(obj, src="parse"):
    return extract.find([{"obj": obj, "src": src}])


def hot_words(start=1):
    return [{"word": f"Example {n}", "position": n, "hot_value": 1000-n}
            for n in range(start, start+3)]


def index_words(start=1):
    return [{"word": f"Example {n}", "rank": n, "topic_index": 5000-n, "vv": 9999999}
            for n in range(start, start+3)]


class ContractsTests(unittest.TestCase):
    def test_area_codes_and_perf_logs_are_not_rankings(self):
        for obj in (
            {"all_area_list": [{"name": s, "value": n} for n, s in
                               enumerate(("乌鲁木齐", "克拉玛依", "苗栗"), 650100)]},
            [{"name": f"middleware_{n}", "duration": n*100} for n in range(5)],
            {"data": {"articles": [{"title": f"让生活开挂 {n}"} for n in range(5)]}},
        ):
            for job in ("douyin_hot", "douyin_index_hot", "bili_index_home"):
                self.assertEqual(validation.hot_lists(captured(obj), job, "https://www.douyin.com/"), [])

    def test_community_feed_and_live_cards_are_not_kuaishou_hot(self):
        obj = {"data": {"acfunFeedData": {"feeds": hot_words()},
                        "gameLiveCardList": hot_words()}}
        self.assertEqual(validation.hot_lists(captured(obj), "kuaishou_hot",
                                             "https://www.kuaishou.com/"), [])

    def test_pinned_item_does_not_shift_official_positions(self):
        obj = {"current": [{"word": "Pinned", "topic_index": 0}]+index_words(4)}
        lists = validation.hot_lists(captured(obj), "douyin_index_hot", "https://creator.douyin.com/")
        self.assertEqual([it["rank"] for it in lists[0]["items"]], [4, 5, 6])
        self.assertEqual(lists[0]["items"][0]["value"], 4996)

    def test_index_keeps_list_identity_and_exact_metric(self):
        found = captured({"current": index_words(), "rocketing": index_words()})
        lists = validation.hot_lists(found, "douyin_index_hot", "https://creator.douyin.com/")
        self.assertEqual([x["name"] for x in lists], ["实时热点", "飙升热点"])
        self.assertEqual(lists[0]["items"][0]["value"], 4999)

    def test_wrong_page_or_network_host_is_rejected(self):
        obj = {"current": index_words()}
        self.assertEqual(validation.hot_lists(captured(obj), "douyin_index_hot", "https://notdouyin.com"), [])
        self.assertEqual(validation.hot_lists(captured(obj, "xhr:https://fake.invalid/data"),
                                             "douyin_index_hot", "https://www.douyin.com"), [])

    def test_duplicate_rank_nan_negative_and_missing_metric_are_rejected(self):
        for key, value in (("rank", 1), ("topic_index", float("nan")),
                           ("topic_index", -1), ("rank", 1.5)):
            rows = index_words()
            rows[1][key] = value
            found = captured({"current": rows})
            self.assertEqual(validation.hot_lists(found, "douyin_index_hot", "https://www.douyin.com"), [])
        rows = index_words()
        del rows[1]["topic_index"]
        self.assertEqual(validation.hot_lists(captured({"current": rows}),
                                             "douyin_index_hot", "https://www.douyin.com"), [])

    def test_removed_douyin_hot_cannot_be_scheduled_or_captured(self):
        self.assertNotIn("douyin_hot", [j[0] for j in jobs.HOT_JOBS])
        self.assertEqual(validation.hot_lists(captured({"data": {"word_list": hot_words()}}),
                                             "douyin_hot", "https://www.douyin.com/hot"), [])
        with patch.object(page, "open_capture") as capture, patch.object(store, "status"):
            self.assertEqual(jobs.run_hot("douyin_hot", "douyin", "douyin_hot", 4), 0)
        capture.assert_not_called()
        self.assertEqual(settings.url("douyin_hot"), "")

    def test_extractor_does_not_invent_rank_from_array_position(self):
        found = captured([{"title": f"Item {n}", "value": n*100} for n in range(3)])
        self.assertIsNone(found["lists"][0]["items"][0]["rank"])

    def test_future_video_is_not_counted_as_last_seven_days(self):
        result = extract.summarize_videos(
            {"items": [{"text": "Future", "ts": 2000, "nums": {"like_count": 10}}]}, now=1000)
        self.assertEqual(result["n_7d"], 0)

    def test_unverified_jobs_and_keywords_do_not_open_browser(self):
        with patch.object(page, "open_capture") as capture, patch.object(store, "status"):
            self.assertEqual(jobs.run_hot("kuaishou_hot", "kuaishou", "kuaishou_hot", 4), 0)
            self.assertFalse(jobs.run_kw("bili_index_kw", "Example"))
        capture.assert_not_called()

    def test_existing_browser_is_checked_without_launching(self):
        with patch.object(page.cdp, "available", return_value=False):
            result = page.launch_browser(background=True)
        self.assertFalse(result["ok"])
        self.assertIn("未启动", result["note"])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = patch.multiple(config, DATA_DIR=self.tmp.name,
                                    DB_PATH=str(Path(self.tmp.name)/"test.db"))
        self.paths.start()
        self.settings = patch.object(settings, "_cache", {"data": dict(settings.DEFAULTS)})
        self.settings.start()
        db.init()
        store.ensure()

    def tearDown(self):
        self.settings.stop()
        self.paths.stop()
        self.tmp.cleanup()

    def valid(self):
        return validation.hot_lists(captured({"current": index_words()}),
                                    "douyin_index_hot", "https://creator.douyin.com/")

    def test_old_rows_remain_on_disk_but_do_not_appear_in_api(self):
        with db.session() as c:
            c.execute("INSERT INTO soc_hot VALUES(?,?,?,?,?,?,?,?,?)",
                      ("bili_index", "bili_index_home", int(time.time()), 1,
                       "让生活开挂", None, "{}", None, "$.data.articles"))
            c.execute("INSERT INTO soc_lookup VALUES(?,?,?,?,?,?)",
                      ("bili_index_kw", "让生活开挂", int(time.time()), 1, "{}", ""))
        self.assertEqual(store.latest_hot("bili_index_home")["items"], [])
        self.assertEqual(store.latest_lookups(), [])
        self.assertEqual(store.lookups_for("让生活开挂"), [])
        self.assertEqual(store.legacy_counts()["hot"], 1)
        self.assertEqual(store.legacy_counts()["lookup"], 1)

    def test_verified_hot_is_visible_with_provenance(self):
        self.assertEqual(store.save_hot("douyin_index", "douyin_index_hot", self.valid()), 3)
        items = store.latest_hot("douyin_index_hot")["items"]
        self.assertEqual(items[0]["rank"], 1)
        self.assertEqual(items[0]["source_url"], "https://creator.douyin.com/")
        self.assertEqual(store.legacy_counts()["hot"], 0)

    def test_unverified_hot_cannot_be_written(self):
        with self.assertRaisesRegex(ValueError, "Unverified"):
            store.save_hot("douyin_index", "douyin_index_hot", captured({"current": index_words()})["lists"])
        with db.session() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM soc_hot").fetchone()[0], 0)

    def test_reused_row_id_does_not_trust_a_replacement_record(self):
        store.save_hot("douyin_index", "douyin_index_hot", self.valid())
        with db.session() as c:
            c.execute("UPDATE soc_hot SET text='乌鲁木齐',value=650100")
        self.assertEqual(store.latest_hot("douyin_index_hot")["items"], [])

    def test_unverified_lookup_is_saved_as_failure_not_success(self):
        store.save_lookup("bili_index_kw", "Example", True, {"metrics": [{"last": 999}]})
        row = store.latest_lookups()[0]
        self.assertFalse(row["ok"])
        self.assertEqual(row["summary"], {})

    def test_keyword_series_and_title_identity_are_rejected(self):
        with self.assertRaises(ValueError):
            store.save_series("bili_index_kw", "Example", [{"metric": "x", "points": []}])
        with self.assertRaises(ValueError):
            store.save_videos("douyin", "Example", [{"text": "Title", "nums": {"like_count": 10}}])

    def test_old_terms_do_not_enter_buckets_queue_or_ai(self):
        funnel.set_label("Bogus city", "game")
        with db.session() as c:
            c.execute("UPDATE soc_terms SET label=NULL,cls='game',age='new'")
        self.assertFalse(any(funnel.buckets().values()))
        with patch.object(db, "kv_get", return_value=[]):
            self.assertEqual(funnel.queue(), [])

    def test_refresh_replaces_polluted_old_rank_and_metric(self):
        funnel.set_label("Example 1", "game")
        with db.session() as c:
            c.execute("UPDATE soc_terms SET best_rank=1,best_value=999999999,sources='[\"bad\"]'")
        store.save_hot("douyin_index", "douyin_index_hot", self.valid())
        with patch.object(funnel, "classify", return_value=("game", "verified", "new", "")):
            funnel.refresh()
        with db.session() as c:
            row = c.execute("SELECT * FROM soc_terms WHERE term='Example 1'").fetchone()
        self.assertEqual(row["best_value"], 4999)
        self.assertNotIn("bad", row["sources"])

    def test_removed_hot_cache_and_provenance_are_purged_without_other_data(self):
        store.save_hot("douyin_index", "douyin_index_hot", self.valid())
        funnel.set_label("Manual", "game")
        with db.session() as c:
            row = c.execute("INSERT INTO soc_hot VALUES(?,?,?,?,?,?,?,?,?)",
                            ("douyin", "douyin_hot", 123, 1, "Junk", 9999, "{}", None, "$.data.word_list"))
            store._verify(c, "hot", row.lastrowid, {"job": "douyin_hot"})
            c.execute("INSERT INTO soc_terms(norm,term,sources) VALUES('junk','Junk','[\"douyin_hot\"]')")
            c.execute("UPDATE soc_terms SET sources='[\"douyin_hot\"]' WHERE norm='manual'")
            c.execute("INSERT INTO soc_terms(norm,term,sources,best_value) VALUES"
                      "('example1','Example 1','[\"douyin_hot\",\"douyin_index_hot\"]',999999)")
            c.execute("DELETE FROM kv WHERE k='social:removed_douyin_hot:v1'")
        db.kv_set("social_status", {"douyin_hot": {"ok_ts": 123}, "douyin_index_hot": {"ok_ts": 124}})
        store._ready.discard(config.DB_PATH)
        store.ensure()
        store.ensure()
        with db.session() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM soc_hot WHERE job='douyin_hot'").fetchone()[0], 0)
            self.assertIsNone(c.execute("SELECT * FROM soc_terms WHERE norm='junk'").fetchone())
            self.assertEqual(c.execute("SELECT label FROM soc_terms WHERE norm='manual'").fetchone()[0], "game")
            self.assertEqual(c.execute("SELECT best_value FROM soc_terms WHERE norm='example1'").fetchone()[0], 4999)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM soc_verified WHERE kind='hot'").fetchone()[0], 3)
        self.assertNotIn("douyin_hot", store.status_all())
        self.assertEqual(len(store.latest_hot("douyin_index_hot")["items"]), 3)
        self.assertEqual(store.latest_hot("douyin_hot")["items"], [])
        with self.assertRaisesRegex(ValueError, "Removed"):
            store.save_hot("douyin", "douyin_hot", self.valid())
        with patch.object(page, "available", return_value=True):
            result = social.api_state()
        self.assertNotIn("douyin_hot", result["hot"])
        self.assertNotIn("douyin_hot", [j["job"] for j in result["jobs"]])

    def test_unknown_game_age_is_not_a_new_game(self):
        self.assertEqual(funnel.bucket_of({"cls": "game", "age": ""}), "related")

    def test_old_search_video_is_excluded_from_heat(self):
        now = int(time.time())
        with db.session() as c:
            c.execute("INSERT INTO soc_videos VALUES(?,?,?,?,?,?,?,?,?,?)",
                      ("douyin", "v1", "Example", now, now-100, 100, 10, 20, 0, 0))
        self.assertEqual(heat.social_videos("douyin", now, {"example": {"g1"}}), ({}, {}))

    def test_api_masks_pre_validation_success_and_explains_readiness(self):
        store.status("douyin_index_hot", True, 147, note="6 lists")
        with patch.object(page, "available", return_value=True):
            result = social.api_state()
        jobs_by_name = {j["job"]: j for j in result["jobs"]}
        self.assertIsNone(jobs_by_name["douyin_index_hot"]["ok_ts"])
        self.assertFalse(jobs_by_name["bili_index_home"]["ready"])
        self.assertIn("营销报告", jobs_by_name["bili_index_home"]["ready_reason"])
        self.assertEqual(result["validation_version"], validation.VERSION)


if __name__ == "__main__":
    unittest.main()
