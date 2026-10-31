import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from radar import config, db, steam_cycle as cycle


class SteamCycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.multiple(config, DB_PATH=os.path.join(self.tmp.name, "db"),
                           DATA_DIR=self.tmp.name, RAW_DIR=os.path.join(self.tmp.name, "raw"))
        p.start()
        self.addCleanup(p.stop)
        db.init()
        self.now = int(datetime(2026, 10, 30, 9, tzinfo=cycle.TZ).timestamp())
        self.slot = cycle.latest_slot(self.now)

    def test_slots_use_beijing_time_and_startup_catches_up(self):
        self.assertEqual(datetime.fromtimestamp(self.slot, cycle.TZ).hour, 8)
        self.assertEqual(datetime.fromtimestamp(cycle.next_slot(self.now), cycle.TZ).hour, 20)
        self.assertEqual(datetime.fromtimestamp(cycle.latest_slot(self.slot-1), cycle.TZ).hour, 20)

    def test_all_pages_then_all_games_and_restart_resume(self):
        row = lambda i, days: {"appid": i, "name": "Game " + str(i),
                               "released": self.slot-days*86400, "price": 3}
        with patch.object(cycle, "get_json", return_value={"success": 1, "results_html": "ok"}), \
                patch.object(cycle.history, "_parse_page",
                             side_effect=[[row(1, 1), row(2, 2)], [row(3, 29), row(4, 31)]]):
            cycle.tick(self.now)
            self.assertEqual(cycle.state()["phase"], "discover")
            cycle.tick(self.now)
        self.assertEqual(cycle.state()["total"], 3)
        with patch.object(cycle.steam, "current_players", return_value=0), \
                patch.object(cycle.steam, "review_total", return_value=0), \
                patch("radar.collect._save") as save:
            cycle.tick(self.now)
            self.assertEqual(cycle.state()["done"], 1)
            cycle.tick(self.now+1)
            cycle.tick(self.now+2)
            cycle.tick(self.now+3)
            self.assertEqual(save.call_count, 3)
            self.assertEqual(cycle.state()["phase"], "complete")
            self.assertEqual(db.kv_get("source_status")["steam"]["count"], 3)
            cycle.tick(self.now+4)
            self.assertEqual(save.call_count, 3)
        with patch.object(cycle, "get_json", side_effect=RuntimeError("offline")):
            cycle.tick(self.slot+12*3600)
        self.assertEqual(cycle.state()["phase"], "discover")
        self.assertEqual(db.kv_get(cycle.KEY)["completed_slot"], self.slot)

    def test_failures_do_not_complete_and_retry_preserves_success(self):
        game = {"appid": 1, "name": "One", "released": self.slot-86400, "done": False}
        db.kv_set(cycle.KEY, {"slot": self.slot, "started": self.now, "phase": "sample",
                             "games": {"1": game}})
        with patch.object(cycle.steam, "current_players", return_value=None), \
                patch.object(cycle.steam, "review_total", return_value=0):
            cycle.tick(self.now)
        self.assertEqual(cycle.state()["done"], 0)
        self.assertNotIn("completed_slot", db.kv_get(cycle.KEY))
        with patch.object(cycle.steam, "current_players", return_value=0), \
                patch.object(cycle.steam, "review_total", return_value=0), \
                patch("radar.collect._save"):
            cycle.tick(self.now+60)
            self.assertEqual(cycle.state()["done"], 0)
            cycle.tick(self.now+1801)
            self.assertEqual(cycle.state()["done"], 1)

    def test_empty_and_repeated_pages_do_not_claim_full_coverage(self):
        with patch.object(cycle, "get_json", return_value={"success": 1, "results_html": ""}):
            cycle.tick(self.now)
        self.assertEqual(cycle.state()["phase"], "discover")
        self.assertTrue(cycle.state()["error"])
        row = {"appid": 1, "name": "One", "released": self.slot-86400}
        with patch.object(cycle, "get_json", return_value={"success": 1}), \
                patch.object(cycle.history, "_parse_page", return_value=[row]):
            cycle.tick(self.now+121)
            cycle.tick(self.now+122)
        self.assertIn("重复", cycle.state()["error"])


if __name__ == "__main__":
    unittest.main()
