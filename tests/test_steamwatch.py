import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from radar import config, db, steamwatch
from radar.sources import steam


TREND_HTML = """
<table id="top-games"><tr><td><a href="/app/730">Big game</a></td>
<td>+999%</td><td></td><td>100000</td></tr></table>
<table id="trending-recent"><tbody><tr><td><a href="/app/42">Small &amp; Rising</a></td>
<td>&#43;120.5%</td><td></td><td>1,234</td></tr></tbody></table>
<script>var now = new Date("2026-10-26T12:59:59Z");</script>
"""


class SteamSourceTests(unittest.TestCase):
    def test_trending_only_reads_player_growth_table(self):
        with patch.object(steam, "get_text", return_value=TREND_HTML):
            rows = steam.trending()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["appid"], 42)
        self.assertEqual(rows[0]["provider_players"], 1234)
        self.assertAlmostEqual(rows[0]["provider_g24"], 1.205)
        self.assertEqual(rows[0]["name"], "Small & Rising")
        self.assertGreater(rows[0]["provider_sampled_at"], 0)

    def test_changed_trending_page_is_failure_not_empty_success(self):
        with patch.object(steam, "get_text", return_value="<html>Blocked</html>"):
            with self.assertRaises(RuntimeError):
                steam.trending()

    def test_popularnew_reads_actual_tab_not_ignored_search_filter(self):
        html = """<a data-ds-appid="730">Unrelated</a>
        <div id="tab_newreleases_content"><div class="tab_content_items">
        <a data-ds-appid="42"><div class="tab_item_content">
        <div class="tab_item_name">Small &amp; New</div></div></a></div></div>
        <div><a data-ds-appid="99">Another tab</a></div>"""
        with patch.object(steam, "get_text", return_value=html), \
                patch.object(steam, "get_json") as search:
            rows = steam.store_tab("popularnew")
            self.assertEqual(steam.search_list("popularnew"), [42])
        search.assert_not_called()
        self.assertEqual(rows, [{"appid": 42, "name": "Small & New"}])

    def test_review_summary_keeps_valid_zero_and_rejects_missing(self):
        self.addCleanup(setattr, steam, "_reviews_api_retry_after", steam._reviews_api_retry_after)
        steam._reviews_api_retry_after = 0
        with patch.object(steam, "get_json", return_value={
                "success": 1, "query_summary": {"total_reviews": 0,
                                                "total_positive": 0, "total_negative": 0}}):
            self.assertEqual(steam.review_summary(42)["reviews"], 0)
        with patch.object(steam, "get_json", return_value={"success": 0}):
            self.assertEqual(steam.review_summary(42), {})

    def test_store_fallback_never_mixes_review_scopes(self):
        html = """<a class="user_reviews_summary_row"
          data-tooltip-html="84% of the 113 user reviews in the last 30 days are positive."></a>
          <a class="user_reviews_summary_row"
          data-tooltip-html="90% of the 1,808 user reviews are positive."></a>
          <meta itemprop="reviewCount" content="1808">"""
        self.addCleanup(setattr, steam, "_reviews_api_retry_after", steam._reviews_api_retry_after)
        steam._reviews_api_retry_after = 0
        with patch.object(steam, "get_json", return_value=None), \
                patch.object(steam, "get_text", return_value=html):
            result = steam.review_summary(42)
        self.assertEqual(result, {"store_reviews": 1808, "store_positive_pct": 90,
                                  "store_recent_reviews": 113})
        self.assertNotIn("reviews", result)

    def test_player_missing_is_not_zero(self):
        with patch.object(steam, "get_json", return_value={"response": {"result": 2}}):
            self.assertIsNone(steam.current_players(42, retries=0, timeout=10))
        with patch.object(steam, "get_json", return_value={
                "response": {"result": 1, "player_count": 0}}):
            self.assertEqual(steam.current_players(42), 0)


class SteamWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = patch.multiple(
            config, DATA_DIR=self.tmp.name, DB_PATH=str(Path(self.tmp.name) / "test.db"),
            RAW_DIR=str(Path(self.tmp.name) / "raw"), STEAM_PRIORITY_PER_RUN=4)
        self.settings.start()
        db.init()

    def tearDown(self):
        self.settings.stop()
        self.tmp.cleanup()

    def test_official_players_are_separate_from_provider_snapshot(self):
        app = {"appid": 42, "name": "Small game", "provider": "SteamCharts",
               "provider_players": 100, "provider_g24": 1.2,
               "provider_sampled_at": int(time.time())}
        with patch.object(steam, "trending", return_value=[app]), \
                patch.object(steam, "store_tab", return_value=[]), \
                patch.object(steam, "current_players", return_value=150), \
                patch.object(steam, "review_summary", return_value={"reviews": 0}):
            report = steamwatch.run_once()
        self.assertFalse(report["running"])
        row = steamwatch.summary()["trending"][0]
        self.assertEqual(row["players"], 150)
        self.assertEqual(row["provider_players"], 100)
        with db.session() as c:
            metrics = dict(c.execute("SELECT metric,value FROM metrics WHERE entity='42'"))
        self.assertEqual(metrics, {"players": 150, "reviews": 0})
        self.assertNotIn("players_g24", metrics)

    def test_failed_live_sample_never_uses_provider_count_as_official(self):
        app = {"appid": 42, "name": "Small game", "provider_players": 100,
               "provider_g24": 1.2}
        with patch.object(steam, "trending", return_value=[app]), \
                patch.object(steam, "store_tab", return_value=[]), \
                patch.object(steam, "current_players", side_effect=RuntimeError("timeout")), \
                patch.object(steam, "review_summary", return_value={}):
            report = steamwatch.run_once()
        self.assertEqual(report["results"], [])
        self.assertTrue(report["errors"])
        self.assertIsNone(steamwatch.summary()["trending"][0]["players"])
        with db.session() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM metrics").fetchone()[0], 0)

    def test_feed_failure_is_visible(self):
        with patch.object(steam, "trending", side_effect=RuntimeError("blocked")), \
                patch.object(steam, "store_tab", side_effect=RuntimeError("changed")):
            report = steamwatch.run_once()
        self.assertFalse(report["feeds"]["trending"]["ok"])
        self.assertIn("blocked", report["feeds"]["trending"]["error"])
        self.assertFalse(report["running"])

    def test_bounded_targets_rotate_recent_releases(self):
        now = int(time.time())
        with db.session() as c:
            for entity in range(1, 10):
                db.upsert_entities(c, "steam", now, [{
                    "entity": str(entity), "name": f"Game {entity}",
                    "extra": {"release_ts": now - 86400, "steam_priority_at": entity}}])
        rows = steamwatch._targets([{"appid": 99, "name": "Trending"}], [], now)
        self.assertEqual([r["appid"] for r in rows], [99, 1, 2, 3])


if __name__ == "__main__":
    unittest.main()
