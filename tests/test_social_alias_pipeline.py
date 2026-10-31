"""Isolated pipeline tests: synthetic inputs only, no real searches or app restart."""
import json
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from radar import analyze, config, db, heat, match, social
from radar.social import batch, jobs, page, settings, store


class AliasPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (
            patch.multiple(config, DATA_DIR=self.tmp.name,
                           DB_PATH=os.path.join(self.tmp.name, "pipeline.db")),
            patch.object(settings, "_cache", {"data": dict(settings.DEFAULTS)}),
            patch.object(settings, "load", return_value=dict(settings.DEFAULTS)),
            patch.object(store, "_ready", {"done": False}),
        ):
            p.start()
            self.addCleanup(p.stop)
        db.init()
        store.ensure()
        self.created = int(time.time()) - 60
        self.snapshot = {
            "created": self.created,
            "games": [{"gid": "g1", "rank": 1, "name": "Game One",
                       "terms": ["Game One", "Alias, One", "G1"]}],
            "queries": [{"term": term, "gids": ["g1"]}
                        for term in ("Game One", "Alias, One", "G1")],
        }
        db.kv_set("radar_social_batch", self.snapshot)

    def lookup(self, job, term, ok, ts=None):
        with db.session() as c:
            c.execute("INSERT INTO soc_lookup VALUES(?,?,?,?,?,?)",
                      (job, term, ts or int(time.time()), int(ok),
                       '{"provenance_version":5}', "synthetic"))

    def test_aliases_remain_pending_on_both_platforms(self):
        p = batch.progress()
        self.assertEqual((p["game_count"], p["term_count"], p["total"]), (1, 3, 6))
        self.assertFalse(p["complete"])
        self.assertEqual(batch.next_query(p), ("douyin_kw", "Game One"))

    def test_old_success_is_not_current_batch_success(self):
        self.lookup("douyin_kw", "Game One", True, self.created - 1)
        self.assertEqual(batch.progress()["done"], 0)

    def test_three_failures_do_not_mean_completed(self):
        for _ in range(3):
            self.lookup("douyin_kw", "Game One", False)
        p = batch.progress(time.time() + 1000)
        self.assertEqual(p["failed"], 1)
        self.assertFalse(p["complete"])
        self.assertNotEqual(batch.next_query(p), ("douyin_kw", "Game One"))

    def test_success_is_retained_across_later_failed_attempt(self):
        self.lookup("douyin_kw", "Game One", True)
        self.lookup("douyin_kw", "Game One", False)
        p = batch.progress()
        self.assertEqual(p["done"], 1)
        self.assertFalse(p["complete"])

    def test_every_alias_and_platform_required_for_completion(self):
        for q in self.snapshot["queries"]:
            for job in batch.PLATFORMS:
                self.lookup(job, q["term"], True)
        p = batch.progress()
        self.assertTrue(p["complete"])
        self.assertEqual(p["games"][0]["completed_terms"], 3)
        self.assertIsNone(batch.next_query(p))

    def test_pacing_and_quotas_are_honored(self):
        self.lookup("douyin_kw", "Game One", False)
        self.assertEqual(batch.progress()["waits"]["douyin_kw"], "interval")
        with patch.object(store, "lookups_today", return_value=300):
            p = batch.progress()
        self.assertEqual(set(p["waits"].values()), {"daily_limit"})
        self.assertIsNone(batch.next_query(p))

    def test_verification_pauses_platform_until_explicit_resume(self):
        store.status("douyin_kw", False, err="平台弹了人机验证", url="https://www.douyin.com/")
        batch.pause_if_required("douyin_kw")
        p = batch.progress(time.time() + 1000)
        self.assertIsNotNone(p["platforms"]["douyin_kw"]["paused"])
        self.assertEqual(batch.next_query(p)[0], "tiktok_kw")
        self.assertTrue(batch.resume("douyin_kw")["ok"])
        self.assertNotIn("douyin_kw", db.kv_get(batch.PAUSE_KEY))

    def test_success_triggers_only_local_ranking_update(self):
        with patch.object(page, "available", return_value=True), \
                patch.object(jobs, "run_kw", return_value=True) as run, \
                patch.object(analyze, "run", return_value={}) as rank:
            social._tick()
        run.assert_called_once_with("douyin_kw", "Game One")
        rank.assert_called_once_with(allow_network=False, send_notify=False, allow_ai=False)
        self.assertEqual(db.kv_get("social_batch_ranking")["state"], "complete")

    def test_ranking_error_is_persisted_not_reported_as_success(self):
        with patch.object(analyze, "run", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                social._refresh_batch_ranking(self.created)
        self.assertEqual(db.kv_get("social_batch_ranking")["state"], "failed")

    def test_ranking_failure_is_retried_without_rerunning_search(self):
        for q in self.snapshot["queries"]:
            for job in batch.PLATFORMS:
                self.lookup(job, q["term"], True)
        with patch.object(analyze, "run", return_value={}) as rank, \
                patch.object(jobs, "run_kw") as run, \
                patch.object(page, "available", return_value=False), \
                patch.object(settings, "get", side_effect=lambda k:
                             False if k == "SOCIAL_AUTO_BROWSER" else settings.DEFAULTS.get(k)), \
                patch.object(social.funnel, "refresh"), \
                patch.object(social.funnel, "ai_round", return_value=0), \
                patch.object(jobs, "run_hot"):
            social._tick()
        rank.assert_called_once()
        run.assert_not_called()

    def test_top100_expands_all_aliases_but_excludes_game101(self):
        names = [f"game-{i}" for i in range(101)]
        groups = {g: [f"steam:{i}"] for i, g in enumerate(names)}
        entities = {("steam", str(i)): {"name": g, "extra": {"aliases": [g + "-alias"]}}
                    for i, g in enumerate(names)}
        with patch.object(db, "kv_get", return_value=None), patch.object(db, "kv_set") as save:
            analyze._social_terms(names, groups, entities)
        snapshot = save.call_args_list[0].args[1]
        self.assertEqual(len(snapshot["games"]), 100)
        self.assertEqual(len(snapshot["queries"]), 200)
        self.assertNotIn(names[100], [q["term"] for q in snapshot["queries"]])
        self.assertEqual(json.loads(json.dumps(snapshot["signature"])), snapshot["signature"])

    def test_incomplete_batch_not_replaced_after_three_failures(self):
        old = {**self.snapshot, "created": self.created - 86400}
        db.kv_set("radar_social_batch", old)
        for q in old["queries"]:
            for job in batch.PLATFORMS:
                for _ in range(3):
                    self.lookup(job, q["term"], False)
        analyze._social_terms(["other"], {"other": ["steam:9"]},
                              {("steam", "9"): {"name": "Other"}})
        self.assertEqual(db.kv_get("radar_social_batch")["created"], old["created"])

    def test_douyin_pages_are_merged_and_comments_preserved(self):
        def cap(ids):
            return {"src": "fetch:https://www.douyin.com/aweme/v1/web/general/search/",
                    "obj": {"status_code": 0, "has_more": 1, "data": [
                        {"aweme_info": {"aweme_id": str(i), "desc": "Synthetic video",
                                        "statistics": {"comment_count": i, "digg_count": 9}}}
                        for i in ids]}}
        result = jobs._douyin_search_list([cap([1, 2]), cap([2, 3])])
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(jobs.extract.video_stats(result["items"][0])["comments"], 1)
        self.assertTrue(result["has_more"])

    def test_douyin_page_parsed_payload_supported_without_url(self):
        caps = [{"src": "parse", "obj": {"status_code": 0, "data": [
            {"aweme_info": {"aweme_id": "7", "desc": "Synthetic",
                            "statistics": {"digg_count": 2, "comment_count": 6}}}]}}]
        result = jobs._douyin_search_list(caps)
        self.assertEqual(jobs.extract.video_stats(result["items"][0])["comments"], 6)
        self.assertIsNone(jobs._douyin_search_list([{"src": "parse", "obj": {"data": []}}]))

    def test_unseen_terms_take_priority_over_failed_terms(self):
        self.lookup("douyin_kw", "Game One", False)
        self.lookup("tiktok_kw", "Game One", False)
        p = batch.progress(time.time() + 1000)
        self.assertEqual(batch.next_query(p)[1], "Alias, One")

    def test_failed_queries_can_be_requeued_without_erasing_history(self):
        for _ in range(3):
            self.lookup("douyin_kw", "Game One", False)
        self.assertEqual(batch.progress()["failed"], 1)
        self.assertTrue(batch.retry_failed("douyin_kw")["ok"])
        self.assertEqual(batch.progress()["failed"], 0)
        with db.session() as c:
            count = c.execute("SELECT COUNT(*) FROM soc_lookup").fetchone()[0]
        self.assertEqual(count, 3)

    def test_future_videos_are_not_counted_as_recent(self):
        summary = jobs.extract.summarize_videos(
            {"items": [{"text": "Synthetic", "ts": 2001,
                        "nums": {"comment_count": 9}}]}, now=2000)
        self.assertEqual(summary["n_7d"], 0)
        self.assertEqual(summary["comments_sum"], 9)

    def test_tiktok_one_video_and_empty_response_are_valid(self):
        src = "fetch:https://www.tiktok.com/api/search/general/full/"
        result = jobs._tiktok_search_list([{"src": src, "obj": {"data": [
            {"type": 1, "item": {"id": "99", "video": {}, "isVideo": True,
                                "stats": {"commentCount": 7}}}]}}])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(jobs.extract.video_stats(result["items"][0])["comments"], 7)
        self.assertEqual(jobs._tiktok_search_list([{"src": src, "obj": {"data": []}}])["items"], [])
        self.assertIsNone(jobs._tiktok_search_list([{"src": src, "obj": {"status_code": 5, "data": []}}]))

    def test_no_browser_records_failed_attempt(self):
        with patch.object(page, "open_capture", side_effect=page.NoBrowser("offline")):
            self.assertFalse(jobs.run_kw("douyin_kw", "Game One"))
        self.assertFalse(store.lookup_states("douyin_kw", ["Game One"])["Game One"]["ok"])

    def test_browser_is_not_launched_when_existing_cdp_is_missing(self):
        with patch.object(page.cdp, "available", return_value=False), \
                patch.object(page.subprocess, "Popen") as launch:
            self.assertFalse(page.launch_browser(background=True)["ok"])
        launch.assert_not_called()

    def test_managed_page_contract_only_navigates_to_search_once(self):
        tab = MagicMock()
        tab.js.return_value = json.dumps({"caps": []})
        target = {"id": "target", "webSocketDebuggerUrl": "ws://127.0.0.1/target"}
        managed = json.dumps({"targetId": "target", "tabId": 77})
        with patch.object(page.cdp, "available", return_value=True), \
                patch.object(page.cdp, "targets", return_value=[target]), \
                patch.object(page.cdp, "Tab", return_value=tab), \
                patch.object(page.os.path, "exists", return_value=True), \
                patch.object(page.subprocess, "check_output", return_value=managed) as open_page, \
                patch.object(page.subprocess, "run"), \
                patch.object(page.time, "sleep"):
            page.open_capture("https://www.douyin.com/search/Synthetic", scroll=0)
        command = open_page.call_args.args[0]
        self.assertIn("http://127.0.0.1:", command[3])
        navigations = [c for c in tab.call.call_args_list if c.args[0] == "Page.navigate"]
        self.assertEqual(len(navigations), 1)
        self.assertEqual(navigations[0].args[1]["url"], "https://www.douyin.com/search/Synthetic")
        tab.close.assert_called_once()

    def test_challenge_page_is_retained_for_human_verification(self):
        tab = MagicMock()
        tab.js.return_value = json.dumps({"title": "验证码中间页", "text": "请完成验证", "caps": []})
        target = {"id": "target", "webSocketDebuggerUrl": "ws://127.0.0.1/target"}
        with patch.object(page.cdp, "available", return_value=True), \
                patch.object(page.cdp, "targets", return_value=[target]), \
                patch.object(page.cdp, "Tab", return_value=tab), \
                patch.object(page.os.path, "exists", return_value=True), \
                patch.object(page.subprocess, "check_output", return_value=json.dumps(
                    {"targetId": "target", "tabId": 77})), \
                patch.object(page.subprocess, "run") as close_page, \
                patch.object(page.time, "sleep"):
            result = page.open_capture("https://www.douyin.com/search/Synthetic", scroll=0)
        self.assertEqual(result["target_id"], "target")
        close_page.assert_not_called()
        tab.close.assert_called_once()

    def test_verified_page_resumes_queue_without_touching_other_pauses(self):
        db.kv_set(batch.PAUSE_KEY, {
            "douyin_kw": {"target_id": "verified", "url": "https://www.douyin.com/search/test"},
            "tiktok_kw": {"target_id": "blocked", "url": "https://www.tiktok.com/search?q=test"}})
        with patch.object(page, "verification_cleared",
                          side_effect=lambda target, url: target == "verified"):
            batch.resume_verified_pages()
        self.assertEqual(set(db.kv_get(batch.PAUSE_KEY)), {"tiktok_kw"})

    def test_login_button_does_not_block_valid_public_results(self):
        capture = {"caps": [], "text": "Log in " + "Synthetic page " * 10, "result_video_ids": ["77"]}
        videos = {"items": [{"text": "Game One", "url": "https://www.douyin.com/video/77",
                             "nums": {"digg_count": 7, "comment_count": 1}}]}
        with patch.object(page, "open_capture", return_value=capture), \
                patch.object(jobs, "_douyin_search_list", return_value=videos):
            self.assertTrue(jobs.run_kw("douyin_kw", "Game One"))

    def test_write_error_cannot_create_success_lookup(self):
        capture = {"caps": [], "text": "Synthetic page " * 10, "result_video_ids": ["77"]}
        videos = {"items": [{"text": "Game One", "url": "https://www.douyin.com/video/77",
                             "nums": {"digg_count": 7}}]}
        with patch.object(page, "open_capture", return_value=capture), \
                patch.object(jobs, "_douyin_search_list", return_value=videos), \
                patch.object(store, "save_videos", side_effect=RuntimeError("disk")):
            with self.assertRaises(RuntimeError):
                jobs.run_kw("douyin_kw", "Game One")
        self.assertEqual(store.lookup_states("douyin_kw", ["Game One"]), {})

    def test_alias_video_dedup_and_comma_name_reach_scoring(self):
        now = int(time.time())
        video = {"url": "https://www.douyin.com/video/123", "text": "Synthetic",
                 "ts": now - 86400, "nums": {"digg_count": 90, "comment_count": 12}}
        for platform in ("douyin", "tiktok"):
            for term in ("Game One", "Alias, One"):
                store.save_videos(platform, term, [video], ts=now, verified=True)
            vids, gids = heat.social_videos(platform, now, {match.normalize("Alias, One"): {"g1"}})
            self.assertEqual(len(vids), 1)
            self.assertEqual(gids[video["url"]], {"g1"})
            scores, _ = heat.platform_scores(vids, gids, now)
            self.assertIn("g1", scores)

    def test_short_archive_alias_has_explicit_game_mapping(self):
        index = heat.name_index({"g1": ["steam:1"]}, {("steam", "1"): {"name": "Game One"}})
        self.assertIn("g1", index[match.normalize("G1")])

    def test_program_search_store_and_combined_bili_score_pipeline(self):
        now = int(time.time())
        groups = {"g1": ["steam:1"], "g2": ["steam:2"]}
        entities = {("steam", "1"): {"name": "Game One", "extra": {}},
                    ("steam", "2"): {"name": "Game Two", "extra": {}}}
        snapshot = self.snapshot
        snapshot["games"].append({"gid": "g2", "rank": 2, "name": "Game Two",
                                  "terms": ["Game Two"]})
        snapshot["queries"].append({"term": "Game Two", "gids": ["g2"]})
        db.kv_set("radar_social_batch", snapshot)
        with db.session() as c:
            c.executescript(
                "CREATE TABLE opp_vsamples(bvid TEXT,ts INTEGER,view REAL,reply REAL,lk REAL,share REAL,fav REAL);"
                "CREATE TABLE opp_videos(bvid TEXT,pubdate INTEGER);"
                "CREATE TABLE opp_vmap(bvid TEXT,gid TEXT);")
            for i, value in ((1, 10), (2, 100)):
                c.execute("INSERT INTO opp_videos VALUES(?,?)", (str(i), now - 86400))
                c.execute("INSERT INTO opp_vsamples VALUES(?,?,?,?,?,?,?)",
                          (str(i), now, value, 1, 1, 1, 1))
                c.execute("INSERT INTO opp_vmap VALUES(?,?)", (str(i), f"steam:{i}"))
        before, _ = heat.score_groups(groups, entities)
        results = []

        def capture(url, **kwargs):
            is_two = "Game%20Two" in url
            vid, value = ("2", 10) if is_two else ("1", 1000)
            if "tiktok.com" in url:
                src = "fetch:https://www.tiktok.com/api/search/general/full/"
                obj = {"data": [{"type": 1, "item": {
                    "id": vid, "isVideo": True, "desc": "Synthetic only", "createTime": now - 86400,
                    "stats": {"playCount": value, "commentCount": 7, "diggCount": 9}}}]}
            else:
                src = "parse"
                obj = {"data": [{"aweme_info": {
                    "aweme_id": vid, "desc": "Game Two" if is_two else "Game One", "create_time": now - 86400,
                    "statistics": {"play_count": value, "comment_count": 7, "digg_count": 9}}}]}
            return {"caps": [{"src": src, "obj": obj}], "text": "Synthetic fixture " * 10,
                    "result_video_ids": [vid]}

        def rank(**kwargs):
            self.assertEqual(kwargs, {"allow_network": False, "send_notify": False, "allow_ai": False})
            results.append(heat.score_groups(groups, entities)[0])

        with patch.object(page, "available", return_value=True), \
                patch.object(page, "open_capture", side_effect=capture) as search, \
                patch.object(store, "last_lookup", return_value=0), \
                patch.object(store, "last_try", return_value=0), \
                patch.object(analyze, "run", side_effect=rank):
            for _ in range(8):
                social._tick()
        self.assertEqual(search.call_count, 8)
        self.assertTrue(batch.progress()["complete"])
        self.assertEqual({r["p"] for r in results[-1]["g1"]["videos"]},
                         {"bilibili", "douyin", "tiktok"})
        self.assertGreater(results[-1]["g1"]["raw"], before["g1"]["raw"])
        self.assertEqual(next(r for r in results[-1]["g1"]["videos"] if r["p"] == "douyin")["n"], 1)
        self.assertEqual(db.kv_get("social_batch_ranking")["state"], "complete")

    def test_recommendation_payload_cannot_be_saved_as_game_result(self):
        capture = {"caps": [], "text": "Synthetic page " * 10, "result_video_ids": ["77"]}
        videos = {"items": [
            {"text": "Game One actual result", "url": "https://www.douyin.com/video/77",
             "nums": {"digg_count": 7}},
            {"text": "Unrelated recommendation", "url": "https://www.douyin.com/video/88",
             "nums": {"digg_count": 900000}}]}
        with patch.object(page, "open_capture", return_value=capture), \
                patch.object(jobs, "_douyin_search_list", return_value=videos):
            self.assertTrue(jobs.run_kw("douyin_kw", "Game One"))
        with db.session() as c:
            self.assertEqual([r[0] for r in c.execute("SELECT vid FROM soc_videos")],
                             ["https://www.douyin.com/video/77"])

    def test_old_unverified_douyin_success_requires_fresh_search(self):
        with db.session() as c:
            c.execute("INSERT INTO soc_lookup VALUES(?,?,?,?,?,?)",
                      ("douyin_kw", "Game One", int(time.time()), 1, "{}", "old"))
        self.assertEqual(batch.progress()["done"], 0)

    def test_single_english_name_requires_game_context(self):
        for text in ("English lesson crack", "Player roster Sponge Crack",
                     "Crack Celestial Blue #music", "Steam cracking guide",
                     "《胡闹外科团|Crack Surgeons》#steam游戏", "【Crack Surgeons】STEAM联机游戏",
                     "游戏名：Crack Surgeons #steam"):
            self.assertFalse(jobs._matches_search_name(text, {"CRACK"}))
        for text in ("#CRACK #steam游戏", "游戏名：CRACK", "《CRACK》实况", "《破壳行动(Crack)》"):
            self.assertTrue(jobs._matches_search_name(text, {"CRACK"}))
        self.assertTrue(jobs._matches_search_name("【链兽角斗】通关经验分享", {"链兽角斗"}))

    def test_existing_match_table_migration_preserves_untrusted_history(self):
        with db.session() as c:
            c.execute("DROP TABLE soc_search_matches")
            c.execute("CREATE TABLE soc_search_matches(platform TEXT,vid TEXT,term TEXT,"
                      "PRIMARY KEY(platform,vid,term))")
            c.execute("INSERT INTO soc_search_matches VALUES('douyin','old','Game One')")
        store._ready["done"] = False
        store.ensure()
        with db.session() as c:
            self.assertEqual([tuple(r) for r in c.execute(
                "SELECT vid,provenance_version FROM soc_search_matches")],
                             [("old", 0)])

    def test_older_match_provenance_is_quarantined(self):
        now = int(time.time())
        item = {"url": "https://www.douyin.com/video/88", "text": "Old association",
                "ts": now - 86400, "nums": {"digg_count": 100}}
        store.save_videos("douyin", "Game One", [item], ts=now, verified=True)
        with db.session() as c:
            c.execute("UPDATE soc_search_matches SET provenance_version=0")
        vids, gids = heat.social_videos("douyin", now, {match.normalize("Game One"): {"g1"}})
        self.assertEqual(heat.platform_scores(vids, gids, now)[0], {})

    def test_exhausted_douyin_search_can_have_zero_matching_videos(self):
        capture = {"caps": [], "text": "Synthetic search page " * 10,
                   "result_video_ids": ["88"]}
        videos = {"has_more": 0, "items": [
            {"text": "Unrelated AI tutorial", "url": "https://www.douyin.com/video/88",
             "nums": {"digg_count": 900000}}]}
        with patch.object(page, "open_capture", return_value=capture), \
                patch.object(jobs, "_douyin_search_list", return_value=videos):
            self.assertTrue(jobs.run_kw("douyin_kw", "Game One"))
        summary = store.lookups_for("Game One")[0]["summary"]
        self.assertEqual((summary["n"], summary["captured_count"], summary["card_count"]),
                         (0, 1, 1))
        self.assertEqual(batch.progress()["platforms"]["douyin_kw"]["done"], 1)
        with db.session() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM soc_videos").fetchone()[0], 0)

    def test_unfinished_or_unrendered_search_does_not_prove_zero_matches(self):
        for has_more, ids in ((1, ["88"]), (None, ["88"]), (0, [])):
            capture = {"caps": [], "text": "Synthetic search page " * 10,
                       "result_video_ids": ids}
            videos = {"has_more": has_more, "items": [
                {"text": "Unrelated AI tutorial", "url": "https://www.douyin.com/video/88",
                 "nums": {"digg_count": 900000}}]}
            with self.subTest(has_more=has_more, ids=ids), \
                    patch.object(page, "open_capture", return_value=capture), \
                    patch.object(jobs, "_douyin_search_list", return_value=videos):
                self.assertFalse(jobs.run_kw("douyin_kw", "Game One"))

    def test_empty_payload_without_visible_empty_state_is_not_success(self):
        capture = {"caps": [], "text": "Synthetic search page " * 10,
                   "result_video_ids": []}
        with patch.object(page, "open_capture", return_value=capture), \
                patch.object(jobs, "_douyin_search_list", return_value={"has_more": 0, "items": []}):
            self.assertFalse(jobs.run_kw("douyin_kw", "Game One"))
        capture["text"] += "暂无搜索结果"
        with patch.object(page, "open_capture", return_value=capture), \
                patch.object(jobs, "_douyin_search_list", return_value={"has_more": 0, "items": []}):
            self.assertTrue(jobs.run_kw("douyin_kw", "Game One"))

    def test_unrelated_cards_at_end_of_search_are_rejected(self):
        capture = {"caps": [], "text": "Synthetic page " * 10,
                   "result_video_ids": ["77", "88"]}
        videos = {"items": [
            {"text": "Game One", "url": "https://www.douyin.com/video/77",
             "nums": {"digg_count": 7}},
            {"text": "Unrelated AI tutorial", "url": "https://www.douyin.com/video/88",
             "nums": {"digg_count": 900000}}]}
        with patch.object(page, "open_capture", return_value=capture), \
                patch.object(jobs, "_douyin_search_list", return_value=videos):
            self.assertTrue(jobs.run_kw("douyin_kw", "Game One"))
        self.assertEqual(store.lookups_for("Game One")[0]["summary"]["n"], 1)

    def test_unverified_douyin_association_is_excluded_from_ranking(self):
        now = int(time.time())
        video = {"url": "https://www.douyin.com/video/88", "text": "Unverified",
                 "ts": now - 86400, "nums": {"digg_count": 900000}}
        store.save_videos("douyin", "Game One", [video], ts=now)
        vids, gids = heat.social_videos("douyin", now, {match.normalize("Game One"): {"g1"}})
        scores, _ = heat.platform_scores(vids, gids, now)
        self.assertEqual(scores, {})
        with db.session() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM soc_videos").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
