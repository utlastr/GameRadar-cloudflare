import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from radar import collect, config, db, sampling


class SamplingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = patch.multiple(config, DATA_DIR=self.tmp.name,
                                  DB_PATH=str(Path(self.tmp.name) / "test.db"))
        self.cfg.start()
        db.init()

    def tearDown(self):
        self.cfg.stop()
        self.tmp.cleanup()

    def stamp(self, hour, minute=0, day=27):
        return int(datetime(2026, 10, day, hour, minute, tzinfo=sampling.TZ).timestamp())

    def test_fixed_slots_and_next_slot(self):
        self.assertEqual(sampling.latest_slot(self.stamp(8)), self.stamp(8))
        self.assertEqual(sampling.latest_slot(self.stamp(20)), self.stamp(20))
        self.assertEqual(sampling.latest_slot(self.stamp(7)), self.stamp(20, day=26))
        self.assertEqual(sampling.next_slot(self.stamp(8)), self.stamp(20))
        self.assertEqual(sampling.next_slot(self.stamp(20)), self.stamp(8, day=28))

    def test_startup_at_nine_catches_up_once_persistently(self):
        calls = []
        sampling.run_due("test", lambda: calls.append(1), now=self.stamp(9))
        sampling._locks.clear()
        sampling.run_due("test", lambda: calls.append(2), now=self.stamp(10))
        self.assertEqual(calls, [1])
        state = db.kv_get("sampling:test")
        self.assertEqual(state["completed_slot"], self.stamp(8))
        self.assertTrue(state["catch_up"])
        sampling.run_due("test", lambda: calls.append(3), now=self.stamp(20))
        self.assertEqual(calls, [1, 3])

    def test_failed_slot_retries_without_becoming_complete(self):
        with self.assertRaises(RuntimeError):
            sampling.run_due("test", lambda: (_ for _ in ()).throw(RuntimeError("timeout")),
                             now=self.stamp(9))
        calls = []
        sampling.run_due("test", lambda: calls.append(1), now=self.stamp(9, 10))
        self.assertEqual(calls, [])
        sampling.run_due("test", lambda: calls.append(1), now=self.stamp(10))
        self.assertEqual(calls, [1])

    def test_force_does_not_change_scheduled_slot(self):
        calls = []
        for force in (False, True, False):
            sampling.run_due("test", lambda: calls.append(1), now=self.stamp(9), force=force)
        self.assertEqual(calls, [1, 1])

    def test_sources_have_independent_persistent_completion(self):
        with patch.object(collect, "_run", return_value=True) as run:
            for source in ("haoyou", "haoyou", "dymini"):
                collect._collect_one(source, lambda: None, 0, scheduled=True)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(db.kv_get("sampling:platform:haoyou")["completed_slot"])
        self.assertTrue(db.kv_get("sampling:platform:dymini")["completed_slot"])

    def test_failed_source_is_not_marked_completed(self):
        with patch.object(collect, "_run", return_value=False):
            collect._collect_one("haoyou", lambda: None, 0, scheduled=True)
        state = db.kv_get("sampling:platform:haoyou")
        self.assertNotIn("completed_slot", state)
        self.assertIn("sampling failed", state["error"])


if __name__ == "__main__":
    unittest.main()
