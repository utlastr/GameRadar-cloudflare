import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from unittest.mock import patch

from radar import config, db, history, maintenance


class MaintenanceRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        settings = patch.multiple(
            config, DB_PATH=os.path.join(self.tmp.name, "radar.db"),
            DATA_DIR=self.tmp.name, RAW_DIR=os.path.join(self.tmp.name, "raw"),
            RAW_KEEP_DAYS=30, STEAM_REVIEW_MIN=20)
        settings.start()
        self.addCleanup(settings.stop)
        db.init()
        with db.session() as c:
            c.executescript(history.SCHEMA)

    def add_game(self, appid, extra=None, review=None, release=None):
        now = int(time.time())
        with db.session() as c:
            c.execute("INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?)",
                      ("steam", str(appid), "Game " + str(appid), now, now,
                       json.dumps(extra or {})))
            if review is not None:
                c.execute("INSERT INTO hist_steam(appid, name, released, reviews) VALUES (?, ?, ?, ?)",
                          (appid, "Game " + str(appid), release, review))

    def test_age_cleanup_keeps_unknown_counts_and_historical_games(self):
        old = int(time.time()) - 40 * 86400
        self.add_game(1, review=50, release=old)
        self.add_game(2, extra={"review_seed": 8}, review=8, release=old)
        self.add_game(3, release=old)
        with db.session() as c:
            c.execute("INSERT INTO entities VALUES ('taptap', 'x', 'Tap Game', ?, ?, '{}')",
                      (old, int(time.time())))
            c.executemany(
                "INSERT INTO metrics(ts, source, entity, metric, value) VALUES (?, 'steam', ?, 'reviews', ?)",
                [(old, "1", 50), (old, "2", 8)])
            c.executemany(
                "INSERT INTO metrics(ts, source, entity, metric, value) VALUES (?, 'taptap', 'x', 'downloads', ?)",
                [(old, 10), (int(time.time()), 20)])

        maintenance.downsample()

        with db.session() as c:
            entities = {r[0] for r in c.execute("SELECT entity FROM entities WHERE source='steam'")}
            self.assertEqual(entities, {"1", "2", "3"})
            self.assertEqual([r[0] for r in c.execute("SELECT appid FROM hist_steam ORDER BY appid")], [1, 2])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM metrics WHERE source='steam'").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT entity FROM entities WHERE source='taptap'").fetchone()[0], "x")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM metrics WHERE source='taptap'").fetchone()[0], 1)

    def test_calendar_day_boundary(self):
        for now, expected in [
            (datetime(2026, 10, 31, 18), datetime(2026, 10, 1)),
            (datetime(2026, 11, 1, 12), datetime(2026, 10, 2)),
            (datetime(2026, 11, 2, 23), datetime(2026, 10, 3)),
        ]:
            self.assertEqual(maintenance.retention_cutoff(now.timestamp()), int(expected.timestamp()))

    def test_keeps_boundary_and_newer_samples_for_same_game(self):
        self.add_game(1, review=8, release=int(time.time()) - 400 * 86400)
        cutoff = maintenance.retention_cutoff()
        with db.session() as c:
            c.executemany(
                "INSERT INTO metrics VALUES (?, 'steam', '1', 'reviews', 8)",
                [(cutoff - 1,), (cutoff,), (cutoff + 86400,)])
        maintenance.downsample()
        with db.session() as c:
            self.assertEqual([r[0] for r in c.execute("SELECT ts FROM metrics ORDER BY ts")],
                             [cutoff, cutoff + 86400])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 1)

    def test_auxiliary_samples_expire_without_losing_video_metadata(self):
        cutoff = maintenance.retention_cutoff()
        with db.session() as c:
            c.executescript("""
                CREATE TABLE video_samples (bvid TEXT, ts INTEGER, views INTEGER);
                CREATE TABLE video_records (bvid TEXT PRIMARY KEY, published INTEGER);
            """)
            c.execute("INSERT INTO video_records VALUES ('v', ?)", (cutoff - 86400,))
            c.executemany("INSERT INTO video_samples VALUES ('v', ?, 10)",
                          [(cutoff - 1,), (cutoff,), (cutoff + 1,)])
        maintenance.downsample()
        with db.session() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM video_samples").fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM video_records").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
