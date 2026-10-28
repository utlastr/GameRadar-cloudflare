import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from radar import bili_daily, bili_terms, config, db, server


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = patch.multiple(config, DATA_DIR=self.tmp.name,
                                    DB_PATH=str(Path(self.tmp.name) / "terms.db"))
        self.paths.start()
        bili_daily.init()
        with db.session() as c:
            for n in range(205):
                c.execute("INSERT INTO bd_terms(norm,name,status,kind,seed_videos,first_day,last_day) "
                          "VALUES(?,?,?,?,?,?,?)",
                          (f"term{n:03}", f"Term {n:03}", ["pending", "candidate", "known", "blocked"][n % 4],
                           "unknown", 205-n, "2015-01-01", "2026-10-27"))
            c.executemany("INSERT INTO bd_word_hits VALUES(?,?,?)",
                          [("term000", "BV1", "tag"), ("term000", "BV1", "bracket"),
                           ("term000", "BV2", "tag")])
            c.execute("INSERT INTO bd_terms(norm,name) VALUES('literal','100%_match')")

    def tearDown(self):
        self.paths.stop()
        self.tmp.cleanup()

    def test_all_statuses_dates_and_paging_without_game_threshold(self):
        first = bili_terms.catalogue()
        second = bili_terms.catalogue(page=2)
        self.assertEqual(first["total"], 206)
        self.assertEqual(first["saved_total"], 206)
        self.assertEqual(len(first["rows"]), 100)
        self.assertEqual(first["pages"], 3)
        self.assertEqual(first["rows"][0]["status"], "pending")
        self.assertEqual(first["rows"][0]["first_day"], "2015-01-01")
        self.assertFalse({r["norm"] for r in first["rows"]} &
                         {r["norm"] for r in second["rows"]})
        self.assertEqual(first["rows"][0]["sources"], {"tag": 2, "bracket": 1})
        self.assertEqual(bili_terms.catalogue(page=999)["page"], 3)

    def test_search_filters_and_literal_wildcards(self):
        self.assertEqual(bili_terms.catalogue(query="TERM 000")["total"], 1)
        self.assertEqual(bili_terms.catalogue(query="%_")["total"], 1)
        self.assertEqual(bili_terms.catalogue(query="not present")["total"], 0)
        result = bili_terms.catalogue(status="blocked", page_size=5)
        self.assertEqual(len(result["rows"]), 5)
        self.assertTrue(all(r["status"] == "blocked" for r in result["rows"]))
        with self.assertRaises(ValueError):
            bili_terms.catalogue(status="' OR 1=1")
        with db.session() as c:
            self.assertEqual(c.execute("SELECT kind FROM bd_terms WHERE norm='term000'").fetchone()[0],
                             "unknown")

    def test_http_route_pagination_and_bad_status(self):
        class Response:
            def _json(self, obj, code=200):
                self.result, self.code = obj, code

        response = Response()
        response.path = "/api/bili-terms?page=2&page_size=100"
        server.Handler.do_GET(response)
        self.assertEqual(response.code, 200)
        self.assertEqual(response.result["page"], 2)
        self.assertEqual(len(response.result["rows"]), 100)
        response.path = "/api/bili-terms?status=bogus"
        server.Handler.do_GET(response)
        self.assertEqual(response.code, 400)
        self.assertIn("error", response.result)

    def test_archive_names_aliases_and_ambiguous_names(self):
        with db.session() as c:
            c.executemany("INSERT INTO games(gid,name) VALUES(?,?)",
                          [("forge", "ForgeTax"), ("other", "Other Game")])
            c.executemany("INSERT INTO game_members(member,gid,name) VALUES(?,?,?)",
                          [("steam:123", "forge", "ForgeTax"),
                           ("taptap:9", "forge", "铸剑大师"),
                           ("steam:456", "other", "Other Game")])
            c.executemany("INSERT INTO entities(source,entity,name,extra) VALUES(?,?,?,?)",
                          [("steam", "123", "ForgeTax", '{"name_cn":"铸剑纳贡","name_en":"Shared"}'),
                           ("steam", "456", "Other Game", '{"name_en":"Shared"}')])
            c.execute("INSERT INTO labels(member,aliases) VALUES('steam:123','[\"锻刀模拟器\"]')")
            rows = [{"name": n} for n in
                    ["FORGETAX", "铸剑大师", "铸剑纳贡", "锻刀模拟器", "模拟器", "Shared", "ForgeTax攻略"]]
            bili_terms.archive_links(c, rows)
        for row in rows[:4]:
            self.assertEqual(row["archive"], {"gid": "forge", "member": "steam:123"})
        for row in rows[4:]:
            self.assertNotIn("archive", row)

    def test_matched_feed_does_not_require_growth_or_known_status(self):
        with db.session() as c:
            c.executemany("INSERT INTO games(gid,name) VALUES(?,?)",
                          [("pvz", "Term 001"), ("forge", "Term 000"),
                           ("ambiguous1", "Shared"), ("ambiguous2", "Shared")])
            c.executemany("INSERT INTO game_members(member,gid,name) VALUES(?,?,?)",
                          [("bili:pvz", "pvz", "Term 001"),
                           ("steam:123", "forge", "Term 000"),
                           ("steam:1", "ambiguous1", "Shared"),
                           ("steam:2", "ambiguous2", "Shared")])
            c.execute("INSERT INTO bd_terms(norm,name) VALUES('shared','Shared')")
        result = bili_terms.matched_catalogue()
        self.assertEqual(result["total"], 2)
        self.assertEqual({r["status"] for r in result["rows"]}, {"candidate", "pending"})
        self.assertEqual({r["archive"]["gid"] for r in result["rows"]}, {"pvz", "forge"})

        class Response:
            path = "/api/bili-terms?matched=1"

            def _json(self, obj, code=200):
                self.result, self.code = obj, code

        response = Response()
        server.Handler.do_GET(response)
        self.assertEqual(response.code, 200)
        self.assertEqual(response.result["total"], 2)


if __name__ == "__main__":
    unittest.main()
