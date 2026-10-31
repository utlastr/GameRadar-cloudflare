import json
import os
import sqlite3
import tempfile
import time
import unittest

from tools.restore_steam_cleanup import restore


SCHEMA = """
CREATE TABLE entities(source TEXT, entity TEXT, name TEXT, first_seen INTEGER, last_seen INTEGER, extra TEXT,
                      PRIMARY KEY(source, entity));
CREATE TABLE metrics(ts INTEGER, source TEXT, entity TEXT, metric TEXT, value REAL,
                     PRIMARY KEY(source, entity, metric, ts));
CREATE TABLE metrics_hourly(hour_ts INTEGER, source TEXT, entity TEXT, metric TEXT, vmin REAL, vavg REAL,
                            vmax REAL, PRIMARY KEY(source, entity, metric, hour_ts));
CREATE TABLE hist_steam(appid INTEGER PRIMARY KEY, name TEXT, released INTEGER, price REAL, tags TEXT,
                        reviews INTEGER, grp TEXT, why TEXT, status TEXT, sample INTEGER, prio REAL,
                        start INTEGER, weeks INTEGER, total INTEGER, fetched INTEGER, tries INTEGER);
CREATE TABLE revisit(member TEXT PRIMARY KEY, last_ts INTEGER, interval_h REAL, n INTEGER, last_v REAL,
                     hist TEXT, sig TEXT, reason TEXT);
CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT);
"""


class RestorePlatformDataTests(unittest.TestCase):
    def test_restores_recent_sources_and_snapshot_without_overwriting_current_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            target = os.path.join(temp, "target.db")
            backup = os.path.join(temp, "backup.db")
            now = int(time.time())
            for path in (target, backup):
                c = sqlite3.connect(path)
                try:
                    c.executescript(SCHEMA)
                finally:
                    c.close()

            c = sqlite3.connect(backup)
            try:
                c.execute("INSERT INTO entities VALUES ('steam','1','Excluded',?,?,?)",
                          (now, now, json.dumps({"release_ts": 1790726400})))
                c.execute("INSERT INTO hist_steam(appid,name,released,reviews) VALUES (1,'Excluded',1790726400,100)")
                c.execute("INSERT INTO entities VALUES ('steam','2','Keep',?,?, '{}')", (now, now))
                c.execute("INSERT INTO hist_steam(appid,name,released,reviews) VALUES (2,'Keep',?,100)",
                          (now - 10 * 86400,))
                c.execute("INSERT INTO entities VALUES ('taptap','g','Tap Game',?,?, '{}')", (now, now))
                c.execute("INSERT INTO metrics VALUES (?,?,?,?,?)", (now, "steam", "2", "players", 40))
                c.execute("INSERT INTO metrics VALUES (?,?,?,?,?)", (now, "taptap", "g", "downloads", 500))
                c.execute("INSERT INTO metrics_hourly VALUES (?,?,?,?,?,?,?)",
                          (now, "taptap", "g", "downloads", 1, 2, 3))
                c.execute("INSERT INTO kv VALUES ('dashboard', ?)",
                          (json.dumps({"updated": now, "cards": [{"name": "Recovered"}]}),))
                c.execute("INSERT INTO kv VALUES ('source_status', ?)",
                          (json.dumps({"taptap": {"count": 1}}),))
                c.commit()
            finally:
                c.close()

            c = sqlite3.connect(target)
            try:
                c.execute("INSERT INTO entities VALUES ('steam','2','Current Name',?,?, '{}')", (now, now))
                c.execute("INSERT INTO metrics VALUES (?,?,?,?,?)", (now, "steam", "2", "players", 99))
                c.execute("INSERT INTO kv VALUES ('source_status', ?)",
                          (json.dumps({"steam": {"count": 2}}),))
                c.commit()
            finally:
                c.close()

            result = restore(target, backup)
            c = sqlite3.connect(target)
            try:
                self.assertEqual(c.execute("SELECT name FROM entities WHERE source='steam' AND entity='2'").fetchone()[0],
                                 "Current Name")
                self.assertIsNone(c.execute("SELECT 1 FROM entities WHERE source='steam' AND entity='1'").fetchone())
                self.assertIsNotNone(c.execute("SELECT 1 FROM entities WHERE source='taptap' AND entity='g'").fetchone())
                self.assertEqual(c.execute("SELECT value FROM metrics WHERE source='steam' AND entity='2'").fetchone()[0],
                                 99)
                self.assertEqual(c.execute("SELECT value FROM metrics WHERE source='taptap' AND entity='g'").fetchone()[0],
                                 500)
                self.assertEqual(json.loads(c.execute("SELECT v FROM kv WHERE k='dashboard'").fetchone()[0])["cards"][0]["name"],
                                 "Recovered")
                statuses = json.loads(c.execute("SELECT v FROM kv WHERE k='source_status'").fetchone()[0])
                self.assertIn("steam", statuses)
                self.assertIn("taptap", statuses)
            finally:
                c.close()
            self.assertGreaterEqual(result["eligible"], 2)


if __name__ == "__main__":
    unittest.main()
