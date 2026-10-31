import threading
import unittest
from unittest.mock import patch

from radar import activity


class ActivityParallelTests(unittest.TestCase):
    def test_parallel_tasks_keep_independent_progress(self):
        with patch.object(activity, "_tasks", {}), patch.object(activity, "_state", {
            "busy": False, "task": "启动中", "detail": "", "done": 0, "total": 0,
            "started": None, "next_live": None, "next_slow": None, "rev": 0,
        }):
            ready = threading.Event()
            finish = threading.Event()

            def worker():
                activity.begin("Steam", "详情")
                activity.progress(3, 10, "读取详情")
                ready.set()
                finish.wait(2)
                activity.end()

            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(ready.wait(2))
            activity.begin("直播", "抓榜")
            activity.progress(2, 4, "第 2 页")
            tasks = activity.snapshot()["state"]["tasks"]
            self.assertEqual(
                {(t["task"], t["done"], t["total"], t["detail"]) for t in tasks},
                {("Steam", 3, 10, "读取详情"), ("直播", 2, 4, "第 2 页")},
            )
            finish.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual([t["task"] for t in activity.state()["tasks"]], ["直播"])


if __name__ == "__main__":
    unittest.main()
