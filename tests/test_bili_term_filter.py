import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from radar import ai, bili_daily, bili_term_filter as filtering, bili_terms, config, db

DAY = "2026-10-25"


def response(items, usage=None):
    return {"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"items": items}, ensure_ascii=False)}}], "usage": usage or {}}


class TermFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.multiple(config, DATA_DIR=self.tmp.name,
            DB_PATH=str(Path(self.tmp.name) / "test.db"),
            BVIDEO_WORD_BLACKLIST=[], BVIDEO_PERSON_NAMES=[], BVIDEO_NAME_ALIASES={},
            KW_BIG_GAMES=["炉石", "崩坏", "星穹", "暗区", "王者荣耀"],
            AI_KEY="test-key", AI_DAILY_LIMIT_YUAN=3,
            BVIDEO_CANDIDATE_MIN_VIDEOS=3, BVIDEO_CANDIDATE_MIN_AUTHORS=2)
        self.patch.start()
        db.init()
        bili_daily.init()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def seed(self, word, n=3, tags=None):
        with db.session() as c:
            for i in range(n):
                bvid = word + str(i)
                title = f"【{word}】试玩"
                raw_tags = [word] if tags is None else tags
                c.execute("""INSERT INTO bd_videos(bvid,day,title,tags,owner_id,views)
                    VALUES(?,?,?,?,?,?)""",
                    (bvid, DAY, title, json.dumps(raw_tags), str(i), 100))
                bili_terms.seed(c, {"bvid": bvid, "date": DAY, "title": title, "tags": raw_tags})
            bili_terms.refresh(c)

    def test_exact_word_rules_do_not_filter_longer_names(self):
        self.assertEqual(filtering.exact_kind("三国"), "theme")
        self.assertEqual(filtering.exact_kind("MMORPG"), "genre")
        self.assertEqual(filtering.exact_kind("ＤＬＣ"), "content_form")
        self.assertEqual(filtering.exact_kind("Roblox"), "platform")
        for word in ("炉石传说", "恶龙传说", "三国杀", "三国志", "手机帝国",
                     "剧情模拟器", "DLC Quest", "Control", "Inside", "KARDS"):
            self.assertIsNone(filtering.exact_kind(word), word)

    def test_no_substring_deletion_or_whole_video_veto(self):
        found = {key for key, _, _ in bili_terms.candidates(
            "[崩坏星穹铁道] [恶龙传说] [王者荣耀]",
            ["暗区突围", "炉石传说", "剧情", "手机", "MMORPG", "Roblox", "DLC"])}
        self.assertTrue({"崩坏星穹铁道", "恶龙传说", "暗区突围", "炉石传说"} <= found)
        self.assertFalse({"铁道", "突围", "传说", "王者荣耀", "剧情", "手机",
                          "mmorpg", "roblox", "dlc"} & found)

    def test_full_names_preserve_colons_and_ampersands(self):
        found = {key for key, _, _ in bili_terms.candidates(
            "[游戏：新大陆] [Dungeons & Dragons]",
            ["游戏：新大陆", "DLC Quest"])}
        self.assertIn("游戏新大陆", found)
        self.assertIn("dungeonsdragons", found)
        self.assertIn("dlcquest", found)
        self.assertNotIn("新大陆", found)

    def test_unknowns_are_not_presented_as_games_and_raw_tags_survive(self):
        self.seed("不确定的词", tags=["不确定的词", "王者荣耀", "DLC"])
        with db.session() as c:
            self.assertEqual(len(bili_terms.topics(c)), 1)
            self.assertEqual(bili_terms.topics(c, game_only=True), [])
            self.assertIn("DLC", json.loads(c.execute(
                "SELECT tags FROM bd_videos LIMIT 1").fetchone()[0]))

    def test_catalog_games_bypass_ai_and_known_non_games_are_filtered(self):
        self.seed("具体新游戏")
        with db.session() as c:
            c.execute("INSERT INTO entities VALUES('steam','1','具体新游戏',0,0,'{}')")
            c.execute("INSERT INTO bd_terms(norm,name) VALUES('mmorpg','MMORPG')")
            bili_terms.refresh(c)
            self.assertEqual(bili_terms.topics(c, game_only=True)[0]["kind_source"], "catalog")
            self.assertEqual(c.execute(
                "SELECT status FROM bd_terms WHERE norm='mmorpg'").fetchone()[0], "blocked")
        with patch.object(ai, "_post") as post:
            filtering.ai_round()
        post.assert_not_called()

    def test_context_classification_is_cached_and_charge_recorded(self):
        self.seed("恶龙传说")
        payload = response([{"id": 0, "kind": "game", "confidence": 0.95,
                             "evidence": "【恶龙传说】试玩", "reason": "试玩的具体作品"}],
                           {"prompt_tokens": 100, "completion_tokens": 20})
        with patch.object(ai, "_post", return_value=payload) as post:
            result = filtering.ai_round()
            again = filtering.ai_round()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["kinds"], {"game": 1})
        self.assertEqual(again["status"], "idle")
        with db.session() as c:
            self.assertEqual(bili_terms.topics(c, game_only=True)[0]["name"], "恶龙传说")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bd_lease").fetchone()[0], 0)
        self.assertGreater(ai.status()["spent"], 0)

    def test_changed_context_can_reopen_previously_rejected_word(self):
        self.seed("传说")
        non_game = response([{"id": 0, "kind": "common", "confidence": 0.95,
                              "evidence": "传说", "reason": "普通词"}])
        with patch.object(ai, "_post", return_value=non_game):
            filtering.ai_round()
        with db.session() as c:
            c.execute("UPDATE bd_videos SET title='独立游戏《传说》首发试玩'")
            c.execute("UPDATE bd_term_judgments SET next_check=0")
        game = response([{"id": 0, "kind": "game", "confidence": 0.95,
                          "evidence": "独立游戏《传说》首发试玩", "reason": "新语境是作品"}])
        with patch.object(ai, "_post", return_value=game) as post:
            result = filtering.ai_round()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(result["kinds"], {"game": 1})

    def test_unchanged_rejected_context_is_not_repeatedly_billed(self):
        self.seed("新普通词")
        payload = response([{"id": 0, "kind": "common", "confidence": 0.95,
                             "evidence": "新普通词", "reason": "普通词"}])
        with patch.object(ai, "_post", return_value=payload):
            filtering.ai_round()
        with db.session() as c:
            c.execute("UPDATE bd_term_judgments SET next_check=0")
        with patch.object(ai, "_post") as post:
            filtering.ai_round()
        post.assert_not_called()

    def test_no_key_or_exhausted_budget_makes_no_network_request(self):
        self.seed("恶龙传说")
        with patch.object(ai, "_post") as post:
            with patch.object(config, "AI_KEY", ""):
                self.assertEqual(filtering.ai_round()["status"], "no_ai_key")
            db.kv_set("ai_status", {"date": ai._today(), "spent": 3.0})
            self.assertEqual(filtering.ai_round()["status"], "budget_reached")
        post.assert_not_called()

    def test_malformed_response_does_not_cache_a_negative_judgment(self):
        self.seed("恶龙传说")
        bad = response([{"id": 9, "kind": "common", "confidence": 0.99,
                         "evidence": "恶龙传说"}])
        with patch.object(ai, "_post", return_value=bad):
            with self.assertRaises(ValueError):
                filtering.ai_round()
        with db.session() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bd_term_judgments").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bd_lease").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT status FROM bd_terms").fetchone()[0], "candidate")

    def test_unquoted_evidence_or_low_confidence_stays_unknown(self):
        self.seed("恶龙传说")
        for evidence, score in (("模型编造的内容", 0.99), ("恶龙传说", 0.5)):
            with db.session() as c:
                c.execute("DELETE FROM bd_term_judgments")
            payload = response([{"id": 0, "kind": "common", "confidence": score,
                                 "evidence": evidence, "reason": "不可靠"}])
            with patch.object(ai, "_post", return_value=payload):
                filtering.ai_round()
            with db.session() as c:
                self.assertEqual(c.execute(
                    "SELECT kind FROM bd_term_judgments").fetchone()[0], "unknown")

    def test_bounded_title_rescan_resumes_without_marking_all_titles_finished(self):
        self.seed("恶龙传说", n=15)
        with db.session() as c:
            c.execute("INSERT INTO entities VALUES('steam','1','恶龙传说',0,0,'{}')")
            bili_terms.rescan(c, DAY, game_only=True, title_limit=4)
            row = c.execute("SELECT scanned,scan_after FROM bd_terms").fetchone()
            self.assertEqual(row["scanned"], 0)
            self.assertTrue(row["scan_after"])
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM bd_videos WHERE title_scanned=1").fetchone()[0], 2)
        for _ in range(9):
            with db.session() as c:
                bili_terms.rescan(c, DAY, game_only=True, title_limit=4)
        with db.session() as c:
            self.assertEqual(c.execute("SELECT scanned FROM bd_terms").fetchone()[0], 1)
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM bd_videos WHERE title_scanned=1").fetchone()[0], 15)

    def test_ai_request_contains_explicit_task_even_if_relay_drops_system_message(self):
        self.seed("恶龙传说")
        payload = response([{"id": 0, "kind": "game", "confidence": 0.95,
                             "evidence": "恶龙传说", "reason": "作品"}])
        with patch.object(ai, "_post", return_value=payload) as post:
            filtering.ai_round()
        body = post.call_args.args[0]
        self.assertIn("现在执行分类", body["messages"][-1]["content"])
        self.assertIn('"kind":"game"', body["messages"][-1]["content"])

    def test_exclusion_summary_includes_filtered_words_without_seed_hits(self):
        with db.session() as c:
            c.execute("INSERT INTO bd_terms(norm,name) VALUES('三国','三国')")
            bili_terms.refresh(c)
            self.assertEqual(filtering.summary(c)["excluded"], 1)

    def test_rebuild_removes_phantom_hits_but_keeps_raw_metadata(self):
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "tools" / "filter_bili_terms.py"
        spec = importlib.util.spec_from_file_location("filter_rebuild_test", path)
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        self.seed("炉石传说")
        with db.session() as c:
            c.execute("INSERT INTO bd_terms(norm,name,status) VALUES('传说','传说','candidate')")
            c.execute("INSERT INTO bd_word_hits SELECT '传说',bvid,'tag' FROM bd_videos")
            before = [tuple(r) for r in c.execute("SELECT bvid,title,tags,views FROM bd_videos")]
        tool.rebuild()
        with db.session() as c:
            after = [tuple(r) for r in c.execute("SELECT bvid,title,tags,views FROM bd_videos")]
            self.assertEqual(before, after)
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM bd_word_hits WHERE norm='传说'").fetchone()[0], 0)
            self.assertEqual(c.execute(
                "SELECT seed_videos FROM bd_terms WHERE norm='传说'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
