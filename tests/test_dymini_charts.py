import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from radar import analyze, collect, config, db
from radar.sources import dymini


def payload(offset=0):
    return {"error": 0, "data": {"hasMore": False, "gameInfo": [
        {"schema": f"sslocal://microgame?app_id=tt{i + offset:018d}",
         "name": f"Game {i + offset}", "index": i, "rankData": []}
        for i in range(1, 201)
    ]}}


class DouyinChartTests(unittest.TestCase):
    def test_candidate_boundaries(self):
        for rank, expected in [(None, False), (1, False), (50, False),
                               (51, True), (200, True), (201, False)]:
            self.assertEqual(dymini.candidate(rank), expected)

    def test_exactly_200_unique_ranks_required(self):
        self.assertEqual(len(dymini._rows(payload())), 200)
        for count in (0, 20, 199):
            data = payload()
            data["data"]["gameInfo"] = data["data"]["gameInfo"][:count]
            with self.assertRaisesRegex(RuntimeError, "200"):
                dymini._rows(data)

    def test_duplicate_and_missing_id_rejected(self):
        data = payload()
        data["data"]["gameInfo"][1] = dict(data["data"]["gameInfo"][0])
        with self.assertRaisesRegex(RuntimeError, "重复"):
            dymini._rows(data)
        data = payload()
        data["data"]["gameInfo"][0]["schema"] = ""
        with self.assertRaisesRegex(RuntimeError, "app_id"):
            dymini._rows(data)

    def test_full_data_retained_but_only_150_candidates(self):
        tab = Mock()
        tab.js.return_value = payload()
        with patch.object(dymini.cdp, "attach", return_value=tab):
            items, errors = dymini.boards()
        self.assertEqual(errors, [])
        self.assertEqual(len(items), 200)
        self.assertEqual(sum(it["extra"]["new_candidate"] for it in items), 150)
        self.assertTrue(all(len(it["metrics"]) == 4 for it in items))
        self.assertEqual(tab.js.call_count, 4)
        tab.close.assert_called_once()

    def test_missing_login_and_partial_new_chart_fail(self):
        with patch.object(dymini.cdp, "attach", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "CDP"):
                dymini.boards()
        tab = Mock()
        tab.js.return_value = {"error": 0, "data": {"gameInfo": []}}
        with patch.object(dymini.cdp, "attach", return_value=tab):
            with self.assertRaisesRegex(RuntimeError, "新游榜采集失败"):
                dymini.boards()
        tab.close.assert_called_once()

    def test_collection_uses_official_source(self):
        with patch.object(collect.dymini, "boards", return_value=([], [])) as official, \
                patch.object(collect, "_board", return_value=(0, "")) as save:
            collect._dymini(123)
        official.assert_called_once()
        save.assert_called_once_with("dymini", [], [], 123)

    def test_other_boards_cannot_bypass_new_chart_window(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(config, "DB_PATH", str(Path(tmp) / "radar.db")), \
                patch.object(config, "RANK_BOARDS", {
                    ("dymini", "rank_new"): ("new", "New"),
                    ("dymini", "rank_popular"): ("new", "Popular"),
                }), patch.object(config, "source_on", return_value=True):
            db.init()
            now = int(time.time())
            with db.session() as conn:
                db.put_metrics(conn, now, "dymini", [
                    ("top", "rank_new", 50), ("top", "rank_popular", 1),
                    ("eligible", "rank_new", 51), ("eligible", "rank_popular", 2),
                    ("last", "rank_new", 200), ("old", "rank_popular", 3),
                    ("outside", "rank_new", 201),
                ])
            scores = analyze._rank_scores({})
            self.assertEqual(set(scores), {"dymini:eligible", "dymini:last"})


if __name__ == "__main__":
    unittest.main()
