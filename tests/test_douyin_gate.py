import unittest
from radar.social import douyin


class DouyinGateTests(unittest.TestCase):
    def test_non_game_excluded_even_with_game_audience(self):
        self.assertEqual(douyin.classify({"platform_game": False, "audience_game_share": .95})[0], "excluded")

    def test_platform_game_included(self):
        self.assertEqual(douyin.classify({"platform_game": True})[0], "included")

    def test_missing_category_is_unknown_not_negative(self):
        self.assertEqual(douyin.classify({"audience_game_share": .95})[0], "unknown")

    def test_tag_does_not_invent_platform_category(self):
        evidence = douyin.observe([{"src": "fetch:observed", "obj": {
            "aweme_id": "12345", "desc": "#游戏", "video_tag": [{"tag_name": "游戏"}],
            "author": {"tags": ["游戏"]}}}])["12345"]
        self.assertEqual(evidence["raw_video_fields"]["video_tag"][0]["tag_name"], "游戏")
        self.assertIsNone(evidence["platform_game"])
        self.assertIsNone(evidence["audience_game_share"])
        self.assertEqual(douyin.classify(evidence)[0], "unknown")
