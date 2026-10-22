import os
import tempfile
import unittest
from unittest.mock import patch

from radar import config, db
from radar.social import douyin, growth


class GrowthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = patch.multiple(config, DATA_DIR=self.tmp.name, DB_PATH=os.path.join(self.tmp.name, "test.db"))
        self.paths.start()
        douyin.ensure()

    def tearDown(self):
        self.paths.stop()
        self.tmp.cleanup()

    def sample(self, ts, likes=None, vid="123456", tags=("游戏", "Forge Text")):
        record = {"aweme_id": vid, "author": {"uid": "a"}, "video": {},
                  "cha_list": [{"cha_name": t} for t in tags],
                  "text_extra": [{"hashtag_name": t} for t in tags], "statistics": {}}
        if likes is not None:
            record["statistics"]["digg_count"] = likes
        return growth.ingest([{"src": "fetch:video", "obj": record}], ts)

    def row(self, **kwargs):
        return next(r for r in growth.report(now=10000, **kwargs)["rows"]
                    if r["tag"] == "Forge Text" and r["metric"] == "likes" and r["scope"] == "observed_sample")

    def test_tags_dedup_keep_noise_no_title_guess(self):
        self.sample(10000)
        self.sample(10000)
        self.assertEqual(growth.report(10000)["tags"], 2)
        self.assertEqual(self.row()["videos"], 1)
        self.assertEqual(growth.hashtags({"desc": "#madeup Game", "video_tag": [{"tag_name": "other"}]}), [])

    def test_missing_counters_and_cold_start_not_zero(self):
        self.sample(10000, 800)
        self.assertIsNone(self.row()["hour_rate"])
        self.assertIsNone(self.row()["acceleration"])
        self.assertEqual(self.row()["state"], "cold_start")

    def test_comparable_hours_growth_and_share(self):
        for ts, value in ((2800, 100), (6400, 110), (10000, 190)):
            self.sample(ts, value)
        row = self.row(names={"forgetext": {"steam:1"}})
        self.assertEqual(row["hour_rate"], 80)
        self.assertEqual(row["growth_ratio"], 8)
        self.assertEqual(row["share"], 1)
        self.assertEqual(row["measured_videos"], 1)

    def test_reset_does_not_fabricate_growth(self):
        for ts, value in ((2800, 100), (6400, 110), (10000, 90)):
            self.sample(ts, value)
        self.assertIsNone(self.row()["hour_rate"])

    def test_ambiguous_archive_and_unverified_game_not_scored(self):
        self.sample(10000, 10)
        from radar import match
        names = {match.normalize("Forge Text"): {"one", "two"}}
        self.assertIsNone(self.row(names=names)["archive"])
        self.assertEqual(self.row(names=names)["ambiguous_archives"], 2)
        self.assertEqual(growth.archive_videos(10000, names), ({}, {}))

    def test_verified_aliases_do_not_double_count(self):
        self.sample(10000, 10, tags=("Forge Text", "ForgeText"))
        douyin.save_evidence({"123456": {"platform_game": True}})
        from radar import match
        names = {match.normalize(t): {"game1"} for t in ("Forge Text", "ForgeText")}
        videos, gids = growth.archive_videos(10000, names)
        self.assertEqual(len(videos), 1)
        self.assertEqual(gids["123456"], {"game1"})

    def test_skewed_or_missing_baseline_is_not_hourly(self):
        self.sample(2800, 1)
        self.sample(6000, 2)
        self.sample(10000, 3)
        self.assertIsNone(self.row()["hour_rate"])
