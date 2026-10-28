import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from radar import charts, config, db, steamwatch
from radar.sources import taptap


class PlatformGrowthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = patch.multiple(config, DATA_DIR=self.tmp.name,
                                  DB_PATH=str(Path(self.tmp.name) / "test.db"))
        self.cfg.start()
        db.init()
        charts.clear()
        self.now = int(time.time())

    def tearDown(self):
        charts.clear()
        self.cfg.stop()
        self.tmp.cleanup()

    def entity(self, ident, source="steam", release=None, **extra):
        with db.session() as c:
            db.upsert_entities(c, source, self.now, [{
                "entity": ident, "name": ident,
                "extra": {"release_ts": self.now - 86400 if release is None else release, **extra}}])

    def point(self, ident, metric, value, age=0, source="steam"):
        with db.session() as c:
            db.put_metrics(c, self.now - age, source, [(ident, metric, value)])

    def test_all_entities_and_per_metric_timestamps_and_zero(self):
        for ident in ("one", "two", "pending"):
            self.entity(ident)
        self.point("one", "players", 0)
        self.point("two", "players", 2, 200)
        self.point("two", "reviews", 3, 400)
        data = charts.raw("steam")
        rows = {r["id"]: r for r in data["rows"]}
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows["one"]["cur"]["players"], 0)
        self.assertEqual(rows["two"]["cur"], {"players": 2, "reviews": 3})
        self.assertNotEqual(rows["two"]["sampled"]["players"], rows["two"]["sampled"]["reviews"])
        self.assertEqual(rows["pending"]["cur"], {})
        self.assertEqual(data["pending_count"], 1)

    def test_growth_uses_day_baseline_not_immediate_previous_minutes(self):
        self.entity("rising")
        self.point("rising", "players", 10, 86400)
        self.point("rising", "players", 19, 60)
        self.point("rising", "players", 20)
        row = charts.rising("steam")["rows"][0]
        self.assertEqual(row["growth"], 1)
        self.assertEqual(row["previous"], 10)

    def test_filters_old_and_big_without_dropping_pending_new_games(self):
        self.entity("old", release=self.now - 30 * 86400)
        self.entity("big", developers=["腾讯"])
        self.entity("pending")
        self.entity("small")
        self.point("small", "players", 2, 86400)
        self.point("small", "players", 4)
        data = charts.rising("steam")
        self.assertEqual([r["id"] for r in data["rows"]], ["small", "pending"])
        self.assertEqual(data["rows"][1]["state"], "sample_pending")
        self.assertEqual(data["excluded"], {"old": 1, "big": 1})

    def test_native_rank_change_is_measured_not_a_download_count(self):
        self.entity("new", source="haoyou", release=0, marks={"rank_new": 20})
        self.point("new", "rank_new", 10, source="haoyou")
        row = charts.rising("haoyou")["rows"][0]
        self.assertEqual(row["delta"], 20)
        self.assertEqual(row["evidence"], "platform_rank_change")
        self.assertEqual(row["kind"], "rank")

    def test_old_metric_is_not_freshened_by_another_new_metric(self):
        self.entity("old-rank", source="haoyou")
        self.point("old-rank", "rank_new", 20, 4 * 86400, source="haoyou")
        self.point("old-rank", "rank_new", 100, 5 * 86400, source="haoyou")
        self.point("old-rank", "rank_hot", 50, 86400, source="haoyou")
        self.point("old-rank", "rank_hot", 50, source="haoyou")
        self.assertEqual(charts.rising("haoyou")["measured"], 0)

    def test_full_steam_targets_are_not_limited_to_five_or_forty(self):
        for i in range(55):
            self.entity(str(i + 1))
        self.assertEqual(len(steamwatch._targets([], [], self.now, full=True)), 55)

    def test_legacy_zero_is_not_a_verified_growth_baseline(self):
        self.entity("zero")
        self.point("zero", "players", 0, 86400)
        self.point("zero", "players", 10)
        row = charts.rising("steam")["rows"][0]
        self.assertEqual(row["state"], "baseline_pending")
        self.assertEqual(charts.rising("steam")["measured"], 0)
        self.entity("zero", players_zero_verified=[self.now - 86400])
        charts.clear()
        self.assertEqual(charts.rising("steam")["rows"][0]["state"], "zero_baseline")

    def test_unknown_release_is_labeled_discovery_not_invented_release(self):
        self.entity("wx-new", source="wxmini", release=0)
        self.point("wx-new", "rank_popular", 50, 86400, source="wxmini")
        self.point("wx-new", "rank_popular", 20, source="wxmini")
        row = charts.rising("wxmini")["rows"][0]
        self.assertIn("发行日期待核实", row["why"])
        self.assertEqual(row["release"], 0)

    def test_taptap_only_new_chart_and_honest_pagination(self):
        responses = [
            {"data": {"list": [{"app": {"id": i, "title": str(i)}} for i in range(1, 11)],
                      "next_page": "next", "total": 11}},
            {"data": {"list": [{"app": {"id": 11, "title": "Eleven"}}],
                      "next_page": "", "total": 11}}]
        with patch.object(taptap, "_get", side_effect=[(200, json.dumps(r)) for r in responses]) as fetch, \
                patch.object(taptap.time, "sleep"):
            rows = taptap.board_lists()
        self.assertEqual(len(rows), 11)
        self.assertEqual(rows[11]["lists"], [("新品榜", 11)])
        self.assertTrue(all("type_name=new" in c.args[0] for c in fetch.call_args_list))
        self.assertTrue(db.kv_get("taptap_new_coverage")["complete"])

    def test_modern_taptap_parser_ignores_other_columns(self):
        html = """<a class="ranking-card" data-scroll-restore-anchor="pop:1" href="/app/1">
        <span class="ranking-card__title">Old</span></a>
        <a class="ranking-card" data-scroll-restore-anchor="new:2" href="/app/2">
        <span class="ranking-card__title">New</span></a>"""
        self.assertEqual(taptap._board_games(html), [("New", 2)])


if __name__ == "__main__":
    unittest.main()
