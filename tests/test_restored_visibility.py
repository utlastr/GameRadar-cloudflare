import os
import tempfile
import time
import unittest
from unittest.mock import patch

from radar import analyze, charts, collect, config, db, main


class RestoredVisibilityTests(unittest.TestCase):
    def test_paused_collection_does_not_hide_platform_history(self):
        with patch.object(config, "STEAM_ONLY", True), \
                patch.object(config, "SOURCES_ENABLED", {"taptap": True}), \
                patch.object(db, "kv_get", return_value={"taptap": {"count": 110}}):
            self.assertTrue(config.source_on("taptap"))
            self.assertIn("taptap", collect.source_status())
            self.assertEqual(collect.collect_live(), 0)

    def test_historical_steam_game_does_not_require_new_top20_field(self):
        entities = {("steam", "1"): {"extra": {}, "name": "Example"}}
        with patch.object(db, "kv_get", return_value=1), \
                patch.object(db, "source_first_runs", return_value={}), \
                patch.object(analyze, "_game_age", return_value=(15, "steam")), \
                patch.object(analyze, "_big_game", return_value=None):
            result = analyze._new_games({"steam:1": ["steam:1"]}, entities, int(time.time()))
            self.assertIn("steam:1", result)

    def test_raw_includes_asynchronously_collected_games(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.multiple(config, DB_PATH=os.path.join(tmp, "radar.db"), DATA_DIR=tmp):
            db.init()
            with db.session() as c:
                c.executemany("INSERT INTO entities VALUES ('taptap', ?, ?, 1, 4, '{}')",
                              [("a", "A"), ("b", "B")])
                c.executemany("INSERT INTO metrics VALUES (?, 'taptap', ?, 'downloads', ?)",
                              [(1, "a", 10), (3, "a", 30), (2, "b", 20), (4, "b", 40)])
            result = charts.raw("taptap")
            rows = {r["id"]: r for r in result["rows"]}
            self.assertEqual(set(rows), {"a", "b"})
            self.assertEqual(rows["a"]["cur"], {"downloads": 30})
            self.assertEqual(rows["a"]["prev"], {"downloads": 10})
            self.assertEqual(rows["b"]["cur"], {"downloads": 40})
            self.assertEqual(rows["b"]["sampled_at"], 4)

    def test_steam_schedule_ignores_paused_platform_timestamps(self):
        now = time.time()
        last = {"live": 0, "slow": 0, "maint": now, "net_analyzed": True}
        with patch.object(config, "STEAM_ONLY", True), \
                patch.object(collect, "collect_live") as live, \
                patch.object(collect, "collect_slow", return_value=0), \
                patch.object(collect, "oldest_ok", return_value=now) as oldest, \
                patch.object(main.activity, "idle"):
            main._tick(last)
            live.assert_not_called()
            oldest.assert_called_once_with(["steam"])
            self.assertGreater(last["slow"], now - 2)


if __name__ == "__main__":
    unittest.main()
