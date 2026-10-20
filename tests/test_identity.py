import sqlite3
import unittest
from unittest.mock import patch

from radar import analyze, db, launch, supply


class IdentityTests(unittest.TestCase):
    def entity(self, name="Same name", **extra):
        return {"name": name, "extra": extra}

    def test_same_name_aliases_and_saved_links_cannot_merge_ids(self):
        entities = {
            ("tapmaker", "969073"): self.entity(dev="A", track=True),
            ("tapmaker", "972478"): self.entity(dev="B", track=True),
            ("tapmaker", "978206"): self.entity(dev="C", track=False),
        }
        labels = {"tapmaker:969073": {"aliases": ["Same name"]}}
        links = [{"a": "tapmaker:969073", "b": "tapmaker:972478", "relation": "same"}]
        groups = analyze.build_groups(entities, labels, links)
        self.assertEqual(len(groups), 3)
        self.assertTrue(all(len(members) == 1 for members in groups.values()))

    def test_shared_taptap_id_merges_despite_rename(self):
        entities = {
            ("tapmaker", "123"): self.entity("Before"),
            ("taptap", "123"): self.entity("After"),
            ("steam", "123"): self.entity("After"),
        }
        groups = analyze.build_groups(entities, {}, [])
        self.assertEqual(set(groups), {"taptap:123", "steam:123"})
        self.assertEqual(len(groups["taptap:123"]), 2)

    def test_name_only_live_evidence_cannot_become_a_game(self):
        entities = {
            ("huya_t", "Same name"): self.entity(),
            ("huya", "Same name"): self.entity(),
            ("bvideo", "Same name"): self.entity(),
            ("tapmaker", "123"): self.entity(),
        }
        self.assertEqual(dict(analyze.build_groups(entities, {}, [])),
                         {"taptap:123": ["tapmaker:123"]})

    def test_same_name_score_history_keeps_both_ids(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(db.SCHEMA)
            for gid in ("taptap:969073", "taptap:972478"):
                conn.execute("INSERT INTO scores(ts,akey,name,final) VALUES(?,?,?,?)",
                             (1, gid, "Same name", 50))
            self.assertEqual(conn.execute("SELECT count(*) FROM scores").fetchone()[0], 2)
        finally:
            conn.close()

    def test_launch_date_does_not_use_name_cache(self):
        cards = [{"name": "Same name", "info": {}, "members": ["steam:123"]}]
        self.assertEqual(launch.fill(cards), 0)
        self.assertEqual(cards[0]["info"], {})

    def test_supply_cache_is_scoped_to_game_id(self):
        with patch.object(supply.db, "session") as session:
            session.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = None
            supply.check("appstore_cn", ["Same name"], [], {}, allow_network=False,
                         game_id="taptap:123")
            args = session.return_value.__enter__.return_value.execute.call_args.args
            self.assertEqual(args[1], ("appstore_cn", "taptap:123"))


if __name__ == "__main__":
    unittest.main()
