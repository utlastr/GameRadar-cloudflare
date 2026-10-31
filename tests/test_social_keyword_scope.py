import unittest
from unittest.mock import patch

from radar import analyze
from radar.social import funnel, jobs, settings


class SocialKeywordScopeTests(unittest.TestCase):
    def test_radar_terms_are_limited_and_deduplicated(self):
        names = [f"Game {i}" for i in range(1, 103)]
        groups = {name: [f"steam:{i}"] for i, name in enumerate(names)}
        entities = {("steam", str(i)): {"name": name}
                    for i, name in enumerate(names)}
        saved = {}

        with patch.object(analyze.db, "kv_get", return_value=None), \
                patch.object(analyze.db, "kv_set", side_effect=lambda key, value: saved.update({key: value})):
            analyze._social_terms(names, groups, entities, n=100)

        self.assertEqual(len(saved["radar_social_terms"]), 100)
        self.assertEqual(saved["radar_social_terms"], names[:100])

    def test_search_queue_keeps_alias_expansion_and_ignores_manual_terms(self):
        radar = [f"Radar Game {i}" for i in range(1, 105)]
        with patch.object(funnel.db, "kv_get", return_value=radar), \
                patch.object(funnel.settings, "get", return_value=["manual term"]):
            queue = funnel.queue()

        self.assertEqual(queue, radar)
        self.assertNotIn("manual term", queue)

    def test_tiktok_compact_response_is_recognized_as_video_results(self):
        payload = {
            "data": [{
                "type": 1,
                "item": {
                    "isVideo": True,
                    "id": "12345",
                    "desc": "A game video",
                    "createTime": 1791554580,
                    "share_url": "https://www.tiktok.com/video/12345",
                    "stats": {"playCount": 34900, "commentCount": 12, "diggCount": 1196,
                              "shareCount": 43, "collectCount": 180},
                }
            } for i in range(3)] + [{"type": 4, "item": {"id": "audio", "isVideo": False}}],
            "has_more": 1,
            "cursor": 12,
        }
        videos = jobs._tiktok_search_list([
            {"src": "fetch:https://www.tiktok.com/api/search/general/full/", "obj": payload}
        ])

        self.assertIsNotNone(videos)
        self.assertEqual(len(videos["items"]), 1)
        self.assertEqual(jobs.extract.video_stats(videos["items"][0]),
                         {"plays": 34900, "comments": 12, "likes": 1196,
                          "shares": 43, "favs": 180})
        self.assertIn("tiktok", __import__("radar.heat", fromlist=["SOCIAL"]).SOCIAL)

    def test_tiktok_uses_keyword_video_search_and_existing_video_store_path(self):
        self.assertIn(("tiktok_kw", "tiktok", "videos"), jobs.KW_JOBS)
        self.assertEqual(
            settings.url("tiktok_kw", kw="A game"),
            "https://www.tiktok.com/search?q=A%20game")
        self.assertEqual(
            dict((job, platform) for job, platform, _ in jobs.KW_JOBS)["tiktok_kw"],
            "tiktok")


if __name__ == "__main__":
    unittest.main()
