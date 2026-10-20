import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from radar import collect, config, db, tapwatch
from radar.sources import taptap


class TapwatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = patch.multiple(config, DATA_DIR=self.tmp.name,
                                       RAW_DIR=str(Path(self.tmp.name) / "raw"),
                                       DB_PATH=str(Path(self.tmp.name) / "radar.db"),
                                       TAPTAP_WATCH_IDS=["961780"], TAPTAP_PRIORITY_PER_RUN=1)
        self.settings.start()
        db.init()

    def tearDown(self):
        self.settings.stop()
        self.tmp.cleanup()

    def page(self, appid=961780):
        game = {"@type": "VideoGame", "@id": f"https://www.taptap.cn/app/{appid}",
                "name": "Same name", "interactionStatistic": {"userInteractionCount": 276442},
                "datePublished": "2026-10-06T19:00:02+08:00",
                "author": {"name": "Correct studio"}, "genre": "TapTap制造"}
        # The first stat belongs to a recommendation; it must never be used.
        nuxt = [{"fans_count": 1}, 999999, {"id": 3, "stat": 4}, appid,
                {"fans_count": 5, "hits_total": 6}, 268472, 276442]
        return ('<script type="application/ld+json">' + json.dumps(game) + "</script>" +
                '<script id="__NUXT_DATA__">' + json.dumps(nuxt) + "</script>")

    def test_counters_belong_to_requested_id(self):
        app = taptap.parse_detail_html(961780, self.page())
        self.assertEqual(app["stat"]["fans_count"], 268472)
        self.assertEqual(app["stat"]["hits_total"], 276442)
        self.assertEqual(app["id"], 961780)

    def test_wrong_canonical_id_and_challenge_page_fail(self):
        for html in (self.page(970762), "<title>Verification</title>"):
            with self.assertRaises(RuntimeError):
                taptap.parse_detail_html(961780, html)

    def test_pinned_id_is_sampled_even_when_not_in_database(self):
        app = taptap.parse_detail_html(961780, self.page())
        self.assertEqual(tapwatch.targets(), ["961780"])
        with patch.object(taptap, "detail", return_value=app):
            result = tapwatch.run_once()
        self.assertEqual(len(result["results"]), 1)
        entity = db.load_entities()[("tapmaker", "961780")]
        self.assertEqual(entity["extra"]["dev"], "Correct studio")
        with db.session() as c:
            self.assertEqual(c.execute(
                "SELECT value FROM metrics WHERE entity='961780' AND metric='downloads'").fetchone()[0],
                276442)

    def test_failure_preserves_values_and_marks_error(self):
        tapwatch.record(taptap.parse_detail_html(961780, self.page()))
        with patch.object(taptap, "detail", side_effect=RuntimeError("network failure")):
            result = tapwatch.run_once()
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("network failure", db.load_entities()[("tapmaker", "961780")]["extra"]["fetch_error"])
        with db.session() as c:
            self.assertEqual(c.execute(
                "SELECT count(*) FROM metrics WHERE entity='961780' AND metric='downloads'").fetchone()[0], 1)

    def test_full_crawl_uses_observation_time_not_old_job_time(self):
        now = int(time.time())
        app = taptap.parse_detail_html(961780, self.page())
        with patch.object(taptap, "tag_list", return_value=[(1, {"id": 961780, "title": "Same name"})]), \
                patch.object(taptap, "detail", return_value=app), \
                patch.object(collect, "_between"), patch.object(collect, "_detail_pause"):
            collect._tapmaker(now - 21600)
        with db.session() as c:
            stamp = c.execute("SELECT MIN(ts) FROM metrics WHERE entity='961780'").fetchone()[0]
            self.assertGreaterEqual(stamp, now)


if __name__ == "__main__":
    unittest.main()
