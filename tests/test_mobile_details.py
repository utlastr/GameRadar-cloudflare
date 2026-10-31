import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from radar import analyze, config, db, heat, mobile_details
from radar.sources import taptap


class StoreMetadataTests(unittest.TestCase):
    def page(self, entity=20, developer="小工作室", publisher="腾讯", size=None):
        fields = f"<td><em>开发商</em><p><a>{developer}</a></p></td>"
        fields += f"<td><em>发行商</em><p><a>{publisher}</a></p></td>"
        if size is not None:
            fields += f"<td><em>游戏大小</em><p>{size}</p></td>"
        return (f'<link rel="canonical" href="https://www.3839.com/a/{entity}.htm">'
                f"<title>预约游戏</title><table>{fields}</table>"
                '<div>推荐游戏：网易、腾讯</div>')

    def test_preorder_company_does_not_require_size(self):
        extra = mobile_details.parse_haoyou(20, self.page())
        self.assertNotIn("size_gb", extra)
        self.assertEqual(extra["publisher"], "腾讯")
        entities = {("haoyou", "20"): {"name": "Synthetic", "extra": extra}}
        reason = heat.big_reason(["haoyou:20"], entities, [], [])
        self.assertIn("大厂", reason)
        info = analyze.group_info(["haoyou:20"], entities)
        self.assertIn("腾讯", info["devs"])

    def test_package_and_developer_are_independent(self):
        extra = mobile_details.parse_haoyou(20, self.page(size="1.73G"))
        self.assertEqual(extra["size_gb"], 1.73)
        self.assertEqual(extra["dev"], "小工作室")

    def test_recommendations_cannot_supply_company(self):
        extra = mobile_details.parse_haoyou(
            20, self.page(developer="小工作室", publisher="小发行商"))
        entities = {("haoyou", "20"): {"name": "Synthetic", "extra": extra}}
        self.assertIsNone(heat.big_reason(["haoyou:20"], entities, [], []))

    def test_summary_company_link_is_supported(self):
        html = ('<li><span>开发：</span><a href="//www.3839.com/cp/47.html">'
                '网易游戏</a></li><li><a href="/cp/154.html">推荐腾讯</a></li>')
        extra = mobile_details.parse_haoyou(20, html)
        self.assertEqual(extra["developers"], ["网易游戏"])
        self.assertNotIn("publisher", extra)

    def test_wrong_id_or_challenge_cannot_enrich_game(self):
        for html in (self.page(entity=21), "<title>验证码</title>", "<title>404</title>"):
            with self.assertRaises(ValueError):
                mobile_details.parse_haoyou(20, html)

    def test_taptap_string_and_list_companies(self):
        developers, publishers = mobile_details.taptap_companies(
            {"author": "网易游戏", "publisher": [{"name": "网易雷火"}, "发行团队"]}, None, 20)
        self.assertEqual(developers, ["网易游戏"])
        self.assertEqual(publishers, ["网易雷火", "发行团队"])

    def test_nuxt_company_must_belong_to_requested_id(self):
        data = [{"id": 1, "publisher": 2, "title": 3}, 999, {"name": 4},
                "Recommended", "腾讯", {"id": 6, "publisher": 7, "stat": 8}, 20,
                {"name": 9}, {}, "小发行商"]
        developers, publishers = mobile_details.taptap_companies({}, json.dumps(data), 20)
        self.assertEqual(developers, [])
        self.assertEqual(publishers, ["小发行商"])

    def test_taptap_parser_retains_companies_without_package(self):
        game = {"@type": "VideoGame", "@id": "https://www.taptap.cn/app/20",
                "name": "Reservation", "author": "小工作室", "publisher": {"name": "腾讯"}}
        html = '<script type="application/ld+json">' + json.dumps(game) + "</script>"
        app = taptap.parse_detail_html(20, html)
        extra = taptap.parse_app(app)["extra"]
        self.assertTrue(extra["preorder"])
        self.assertIsNone(extra["size_gb"])
        self.assertEqual(extra["dev"], "小工作室")
        self.assertEqual(extra["publisher"], "腾讯")


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = patch.multiple(config, DATA_DIR=self.tmp.name,
                                  DB_PATH=os.path.join(self.tmp.name, "test.db"))
        override.start()
        self.addCleanup(override.stop)
        db.init()
        with db.session() as c:
            db.upsert_entities(c, "haoyou", 100, [
                {"entity": "20", "name": "Synthetic", "extra": {"tags": ["射击"], "preorder": True}},
                {"entity": "21", "name": "Other", "extra": {}}])
            db.put_metrics(c, 100, "haoyou", [("20", "rank_hot", 1)])

    def test_rank_refresh_preserves_metadata_and_new_rank_tags(self):
        mobile_details._save("haoyou", "20", {"publisher": "腾讯", "dev": "小工作室"}, now=200)
        with db.session() as c:
            db.upsert_entities(c, "haoyou", 300, [
                {"entity": "20", "name": "Synthetic", "extra": {"tags": ["新标签"], "preorder": True}}])
        extra = db.load_entities()[("haoyou", "20")]["extra"]
        self.assertEqual(extra["publisher"], "腾讯")
        self.assertEqual(extra["tags"], ["新标签"])
        self.assertEqual(extra["store_detail"]["checked_at"], 200)

    def test_failed_fetch_does_not_destroy_metadata_or_touch_metrics(self):
        mobile_details._save("haoyou", "20", {"dev": "小工作室", "publisher": "腾讯"}, now=200)
        mobile_details._save("haoyou", "20", {}, error="HTTP 503", now=300)
        extra = db.load_entities()[("haoyou", "20")]["extra"]
        self.assertEqual(extra["publisher"], "腾讯")
        self.assertEqual(extra["store_detail"]["checked_at"], 200)
        with db.session() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM metrics").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM labels").fetchone()[0], 0)

    def test_visible_game_is_prioritized_and_retry_is_delayed(self):
        db.kv_set("dashboard", {"cards": [{"members": ["haoyou:21"]}]})
        self.assertEqual(mobile_details.pending()[0][3], "21")
        now = int(time.time())
        mobile_details._save("haoyou", "21", {}, error="HTTP 503", now=now)
        self.assertNotIn("21", [r[3] for r in mobile_details.pending(now + 60)])
        self.assertIn("21", [r[3] for r in mobile_details.pending(now + 6 * 3600)])

    def test_program_batch_enriches_then_requests_local_ranking(self):
        with patch.object(mobile_details, "_fetch", return_value={"publisher": "腾讯"}) as fetch, \
                patch("radar.server.reanalyze_soon") as rank, \
                patch("radar.activity.say"), \
                patch.object(threading.Event, "wait", return_value=False):
            self.assertEqual(mobile_details.run_batch(threading.Event(), limit=1), 1)
        fetch.assert_called_once()
        rank.assert_called_once()
        extra = db.load_entities()[("haoyou", "20")]["extra"]
        self.assertEqual(extra["publisher"], "腾讯")


if __name__ == "__main__":
    unittest.main()
