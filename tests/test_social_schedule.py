import datetime
import os
import tempfile
import unittest
import threading
from unittest.mock import patch

from radar import config, db
from radar.social import schedule


def ts(value):
    return int(datetime.datetime.fromisoformat(value + "+08:00").timestamp())


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = patch.multiple(config, DATA_DIR=self.tmp.name,
                                    DB_PATH=os.path.join(self.tmp.name, "test.db"))
        self.paths.start()
        self.tasks = patch.object(schedule, "_tasks", return_value=[
            {"job": "douyin_hot", "platform": "douyin", "url": "douyin_hot", "limit": 4}])
        self.tasks.start()

    def tearDown(self):
        self.tasks.stop()
        self.paths.stop()
        self.tmp.cleanup()

    def test_boundaries_use_china_timezone(self):
        self.assertEqual(schedule.latest_boundary(ts("2026-10-22T11:59:59")), ts("2026-10-22T00:00:00"))
        self.assertEqual(schedule.latest_boundary(ts("2026-10-22T12:00:00")), ts("2026-10-22T12:00:00"))

    def test_success_not_repeated_and_next_batch_waits(self):
        now = ts("2026-10-22T12:00:00")
        with patch.object(schedule, "_execute", return_value="sampled") as run:
            schedule.tick(now)
            self.assertEqual(run.call_args.args[1], (now - 43200, now))
            schedule.tick(now + 60)
            schedule.tick(now + 120)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(schedule.state(now)["last_end"], now)

    def test_offline_host_catches_up_oldest_missed_window(self):
        db.kv_set(schedule.KEY, {"last_end": ts("2026-10-21T12:00:00")})
        with patch.object(schedule, "_execute", return_value="sampled") as run:
            schedule.tick(ts("2026-10-22T09:00:00"))
            self.assertEqual(run.call_args.args[1],
                             (ts("2026-10-21T12:00:00"), ts("2026-10-22T00:00:00")))

    def test_failure_retries_are_bounded_and_not_each_minute(self):
        now = ts("2026-10-22T12:00:00")
        with patch.object(schedule, "_execute", return_value="retry") as run:
            for delta in (0, 60, 900, 1800, 1860, 1920):
                schedule.tick(now + delta)
            self.assertEqual(run.call_count, 3)
            batch = schedule.state(now)["last_batch"]
            self.assertEqual(batch["tasks"][0]["state"], "failed")
            self.assertEqual(batch["coverage"], "partial")

    def test_ten_platforms_execute_concurrently(self):
        barrier = threading.Barrier(10, timeout=5)
        def execute(task, window):
            barrier.wait()
            return "sampled"
        tasks = [{"job": str(i), "platform": str(i)} for i in range(10)]
        with patch.object(schedule, "_tasks", return_value=tasks), patch.object(schedule, "_execute", side_effect=execute):
            schedule.tick(ts("2026-10-22T12:00:00"))
        self.assertEqual(sum(t["state"] == "sampled" for t in schedule.state()["active"]["tasks"]), 10)

    def test_same_platform_index_waits_and_old_search_cancelled(self):
        tasks = [{"job": "hot", "platform": "douyin"},
                 {"job": "index", "platform": "douyin_index"},
                 {"job": "douyin_kw", "platform": "douyin", "term": "game"}]
        with patch.object(schedule, "_tasks", return_value=tasks), patch.object(schedule, "_execute", return_value="sampled") as run:
            now = ts("2026-10-22T12:00:00")
            schedule.tick(now)
            self.assertEqual(run.call_count, 1)
            schedule.tick(now)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(tasks[2]["state"], "cancelled")

    def test_pagination_does_not_consume_failure_retries(self):
        with patch.object(schedule, "_execute", return_value="pending") as run:
            for _ in range(5):
                schedule.tick(ts("2026-10-22T12:00:00"))
            self.assertEqual(run.call_count, 5)
            self.assertEqual(schedule.state()["active"]["tasks"][0]["attempts"], 0)

    def test_generated_tasks_never_contain_keywords(self):
        self.tasks.stop()
        self.assertTrue(all("term" not in task for task in schedule._tasks()))


if __name__ == "__main__":
    unittest.main()
