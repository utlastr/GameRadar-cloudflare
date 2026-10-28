import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from radar import analyze, collect, config, db, history, video
from radar.sources import bili, steam


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = patch.multiple(config, DATA_DIR=self.tmp.name,
                                       DB_PATH=str(Path(self.tmp.name) / "test.db"),
                                       BVIDEO_SEARCH_TERMS=[], BVIDEO_WATCH_IDS=[],
                                       BVIDEO_DETAIL_PER_RUN=0)
        self.settings.start()
        db.init()
        video.init()
        with db.session() as c:
            c.executescript(history.SCHEMA)
        self.now = int(time.time())

    def tearDown(self):
        self.settings.stop()
        self.tmp.cleanup()

    def record(self, bvid="BV19gpw6XE95", **kwargs):
        return dict(bvid=bvid, title="Same name", owner_id="42", owner="Author",
                    published=self.now - 3600, views=60000, **kwargs)

    def test_first_sample_is_visible_but_not_claimed_as_growth(self):
        video.save([self.record()], "榜单", self.now)
        row = video.summary(self.now)["rows"][0]
        self.assertTrue(row["attention"])
        self.assertEqual(row["growth"], {})
        self.assertEqual(row["game_ids"], [])

    def test_real_elapsed_growth_and_counter_decreases(self):
        result = video.growth([(1000, 200), (8200, 1000)])
        self.assertEqual(result["rate"], 400)
        self.assertEqual(result["hours"], 2)
        self.assertEqual(video.growth([(1000, 1000), (8200, 200)]), {})
        self.assertEqual(video.growth([(1000, 200), (1100, 1000)]), {})

    def test_same_video_dedup_and_same_title_different_videos(self):
        video.save([self.record()], "榜单", self.now)
        video.save([self.record()], "搜索", self.now + 3600)
        video.save([self.record("BV1LyH26LEA5")], "榜单", self.now)
        result = video.summary(self.now + 3600)
        self.assertEqual(result["total"], 2)
        row = next(v for v in result["rows"] if v["bvid"] == "BV19gpw6XE95")
        self.assertEqual(set(row["origins"]), {"榜单", "搜索"})

    def test_explicit_store_url_only_and_stale_evidence(self):
        video.save([self.record(description="Same name ForgeTax")], "榜单", self.now - 10000)
        row = video.summary(self.now)["rows"][0]
        self.assertFalse(row["attention"])
        self.assertEqual(row["game_ids"], [])
        video.save([self.record(description="https://store.steampowered.com/app/5245810/")],
                   "详情", self.now)
        self.assertEqual(video.summary(self.now)["rows"][0]["game_ids"], ["steam:5245810"])

    def test_unfamiliar_video_is_saved_without_a_known_game_name(self):
        with patch.object(video.bili, "game_videos", return_value=([self.record()], [])), \
                patch.object(video.bili, "newlist", return_value=[]), \
                patch.object(video.bili, "video_detail", return_value=self.record()), \
                patch.object(video.time, "sleep"):
            video.collect()
        self.assertEqual(video.summary()["total"], 1)
        self.assertEqual(video.summary()["status"], "已采样")

    def test_collection_failure_does_not_report_normal(self):
        with patch.object(video.bili, "game_videos", side_effect=RuntimeError("blocked")), \
                patch.object(video.bili, "newlist", return_value=[]):
            with self.assertRaises(RuntimeError):
                video.collect()
        self.assertEqual(video.summary()["status"], "失败")

    def test_partition_records_survive_ranking_and_search_risk(self):
        raw = {"bvid": "BV19gpw6XE95", "title": "New game", "pubdate": self.now,
               "stat": {"view": 42}, "owner": {"mid": 7, "name": "Author"}}
        with patch.object(video.bili, "newlist", side_effect=[[raw], [raw], []]), \
                patch.object(video.bili, "game_videos", side_effect=bili.RiskError("ranking blocked")), \
                patch.object(video.bili, "search_videos", side_effect=bili.RiskError("search blocked")) as search, \
                patch.object(video.bili, "video_detail", return_value=self.record()), \
                patch.object(config, "BVIDEO_SEARCH_TERMS", ["first", "second"]), \
                patch.object(video.time, "sleep"):
            count, _ = video.collect()
        self.assertEqual(count, 1)
        self.assertEqual(search.call_count, 1)
        report = db.kv_get("video_collection")
        self.assertFalse(report["failed"])
        self.assertEqual(report["partition"]["count"], 1)
        self.assertFalse(report["partition"]["complete"])
        self.assertIn("游戏分区最新", video.summary()["rows"][0]["origins"])

    def test_global_pause_reports_once_without_requests(self):
        db.kv_set("bili_pause_until", self.now + 3600)
        with patch.object(video.bili, "newlist") as latest, \
                patch.object(video.bili, "game_videos") as ranking, \
                patch.object(video.bili, "search_videos") as search, \
                patch.object(video.bili, "video_detail") as detail:
            with self.assertRaises(RuntimeError):
                video.collect()
        for request in (latest, ranking, search, detail):
            request.assert_not_called()
        self.assertEqual(len(db.kv_get("video_collection")["errors"]), 1)

    def test_historical_release_enters_live_pool_by_id(self):
        with db.session() as c:
            c.execute("INSERT INTO hist_steam(appid,name,released,reviews) VALUES(?,?,?,?)",
                      (5245810, "ForgeTax", self.now - 86400, 989))
            c.execute("INSERT INTO hist_steam(appid,name,released,reviews) VALUES(?,?,?,?)",
                      (123, "ForgeTax", self.now + 86400, 1000))
        self.assertEqual(steam.seed_recent_releases(self.now), 1)
        self.assertIn(("steam", "5245810"), db.load_entities())
        self.assertNotIn(("steam", "123"), db.load_entities())
        self.assertEqual(steam.seed_recent_releases(self.now), 0)
        with db.session() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM metrics").fetchone()[0], 0)

    def test_early_growth_does_not_require_three_days(self):
        item = {"entity": "5245810", "name": "ForgeTax",
                "extra": {"release_ts": self.now - 86400}}
        with db.session() as c:
            db.upsert_entities(c, "steam", self.now - 7200, [item])
            db.put_metrics(c, self.now - 7200, "steam", [("5245810", "reviews", 30)])
            db.put_metrics(c, self.now, "steam", [("5245810", "reviews", 100)])
        signals, _ = analyze.compute_signals(db.load_entities())
        signal = next(s for s in signals if s["entity"] == "5245810" and s["metric"] == "reviews")
        self.assertGreater(signal["hot"], 0)
        self.assertEqual(signal["observed_growth"]["rate"], 35)
        self.assertIsNone(signal["g24"])

    def test_recent_samples_with_different_timestamps_are_not_dropped(self):
        with db.session() as c:
            for entity, offset in (("1", 0), ("2", 300)):
                db.upsert_entities(c, "steam", self.now - 7200, [{
                    "entity": entity, "name": "Same name",
                    "extra": {"release_ts": self.now - 86400}}])
                db.put_metrics(c, self.now - 7200 - offset, "steam", [(entity, "reviews", 30)])
                db.put_metrics(c, self.now - offset, "steam", [(entity, "reviews", 100)])
        signals, _ = analyze.compute_signals(db.load_entities())
        self.assertEqual({s["entity"] for s in signals if s["metric"] == "reviews"}, {"1", "2"})

    def test_release_pool_is_tracked_even_when_all_charts_fail(self):
        with db.session() as c:
            c.execute("INSERT INTO hist_steam(appid,name,released,reviews) VALUES(?,?,?,?)",
                      (5245810, "ForgeTax", self.now - 86400, 989))
        with patch.object(steam, "most_played", side_effect=RuntimeError("blocked")), \
                patch.object(steam, "featured_lists", side_effect=RuntimeError("blocked")), \
                patch.object(steam, "search_list", side_effect=RuntimeError("blocked")), \
                patch.object(steam, "detail", return_value=None), \
                patch.object(steam, "current_players", return_value=100), \
                patch.object(steam, "review_total", return_value=120), \
                patch.object(steam, "prices", return_value={}), \
                patch.object(collect.time, "sleep"):
            self.assertEqual(collect._steam(self.now), 1)
        with db.session() as c:
            self.assertEqual(c.execute(
                "SELECT value FROM metrics WHERE entity='5245810' AND metric='reviews'").fetchone()[0], 120)


if __name__ == "__main__":
    unittest.main()
