import os
import tempfile
import unittest
from unittest.mock import patch

from radar import config, db
from radar.social import extract, incremental
from radar.opp import bili_track


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = patch.multiple(config, DATA_DIR=self.tmp.name,
                                    DB_PATH=os.path.join(self.tmp.name, "test.db"))
        self.paths.start()
        incremental.ensure()

    def tearDown(self):
        self.paths.stop()
        self.tmp.cleanup()

    def test_retry_does_not_inflate_count(self):
        item = {"vid": "12345", "text": "Game", "ts": 199900, "author_id": "a"}
        self.assertEqual(incremental.ingest("douyin", [item], "Game", now=200000), 1)
        self.assertEqual(incremental.ingest("douyin", [item], "Game", now=200100), 0)
        report = incremental.report(200200)
        self.assertEqual(report["rows"][0]["windows"]["1"]["current"], 1)
        self.assertIsNone(report["rows"][0]["score"])
        self.assertFalse(report["totals"]["douyin"]["full_platform"])

    def test_unknown_and_future_dates_are_not_today(self):
        incremental.ingest("douyin", [{"vid": "a"}, {"vid": "b", "ts": 300000}], now=200000)
        report = incremental.report(200000)
        self.assertEqual(report["totals"]["douyin"]["observed_24h"], 0)
        self.assertEqual(report["totals"]["douyin"]["missing_publication"], 2)

    def test_album_is_not_video(self):
        caps = [{"obj": [{"mix_id": "123456", "title": "Unrelated album", "statistics": {"digg_count": 1}}]}]
        self.assertEqual(extract.video_items(caps, "douyin"), [])
        self.assertIsNone(extract.stable_video_id("douyin", {"url": "https://www.iesdouyin.com/share/mix/detail/123/"}))

    def test_kuaishou_photo_wrapper(self):
        caps = [{"obj": {"feeds": [{"photo": {"id": "3x1234567", "caption": "Game",
                 "timestamp": 1800000000000, "likeCount": 5, "coverUrl": "https://cdn/a.jpg"},
                 "author": {"id": "author1"}}]}}]
        item = extract.video_items(caps, "kuaishou")[0]
        self.assertEqual(item["ts"], 1800000000)
        self.assertEqual(item["author_id"], "author1")
        self.assertEqual(extract.stable_video_id("kuaishou", item), "3x1234567")

    def test_douyin_card_does_not_invent_exact_date(self):
        rows = extract.douyin_cards([{"url": "https://www.douyin.com/video/123456",
                                     "title": "Game", "likes": "1.6万", "age": "19小时前"}])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["ts"])
        self.assertEqual(rows[0]["nums"]["digg_count"], 16000)

    def test_canonical_id_and_publication_not_author_date(self):
        caps = [{"obj": {"aweme_id": "123456", "desc": "Game", "create_time": 1800000000,
                          "statistics": {"digg_count": 5}, "author": {"uid": "77", "create_time": 1700000000}}}]
        item = extract.video_items(caps, "douyin")[0]
        self.assertEqual(item["ts"], 1800000000)
        self.assertEqual(extract.stable_video_id("douyin", item), "123456")
        self.assertEqual(item["author_id"], "77")

    def test_xhs_wrapper_uses_note_id_not_cover(self):
        caps = [{"obj": {"id": "abcdef123456", "note_card": {"display_title": "Game",
                  "interact_info": {"liked_count": "12"}, "cover": {"url": "https://cdn.example/a.jpg"}}}}]
        item = extract.video_items(caps, "xiaohongshu")[0]
        self.assertEqual(item["url"], "https://www.xiaohongshu.com/explore/abcdef123456")
        self.assertIsNone(item["ts"])

    def test_failed_run_does_not_create_zero_video_evidence(self):
        incremental.record_run("douyin_kw", 100, [], 0, error="login required")
        r = incremental.report()
        self.assertEqual(r["totals"], {})
        self.assertEqual(r["sources"]["douyin_kw"]["ok"], 0)

    def test_capped_feed_resumes_without_advancing_watermark(self):
        import radar.opp as opp
        old_ready = opp._ready["done"]
        opp._ready["done"] = False
        now = 1800000000
        video = {"bvid": "BV1234567890", "title": "Game", "pubdate": now - 20,
                 "owner": {"mid": 1}, "stat": {}}
        try:
            with patch.object(bili_track, "budget_left", return_value=100), \
                 patch.object(bili_track, "_budget_ok", return_value=True), \
                 patch.object(bili_track.activity, "say"), \
                 patch.object(bili_track, "_sleep"), patch.object(bili_track, "status"), \
                 patch.object(bili_track.bili, "paused", return_value=False), \
                 patch.object(bili_track.bili, "warmup"), \
                 patch.object(bili_track.bili, "newlist", return_value=[video]) as fetch, \
                 patch.object(bili_track.time, "time", return_value=now):
                bili_track.discover_once(pages=2)
                progress = db.kv_get("publication_bili_discovery")
                self.assertNotIn("watermark", progress)
                self.assertEqual(progress["next_page"], 3)
                fetch.reset_mock()
                bili_track.discover_once(pages=3)
                self.assertEqual([c.args[0] for c in fetch.call_args_list], [1, 2, 3])
                self.assertEqual(incremental.report(now)["totals"]["bilibili"]["observed_24h"], 1)
        finally:
            opp._ready["done"] = old_ready


if __name__ == "__main__":
    unittest.main()
