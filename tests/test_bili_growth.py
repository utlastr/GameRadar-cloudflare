import json
import io
import math
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from radar import bili_daily, bili_growth as growth, bili_queue, bili_latest, config, db, server
from radar import bili_realtime_client as bili


def stamp(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=bili.BEIJING).timestamp())


class FormulaTests(unittest.TestCase):
    def test_recent_average_preserves_short_pullback_and_seven_day_median(self):
        values = [10, 10, 10, 1000, 10, 10, 10, 100, 100, 10]
        self.assertAlmostEqual(growth.growth_index(values, 100), (70 + 100) / 110)
        self.assertIsNone(growth.growth_index([None] + values[1:], 100))
        self.assertIsNone(growth.growth_index(values[1:], 100))

    def test_interpolation_no_extrapolation_and_reset(self):
        self.assertEqual(growth.boundary([(0, 10), (10, 30)], 5, 20), (20, True))
        self.assertEqual(growth.boundary([(0, 10), (10, 30)], 10, 20), (30, False))
        self.assertIsNone(growth.boundary([(0, 10)], 5, 20)[0])
        self.assertIsNone(growth.boundary([(0, 10), (100, 30)], 50, 20)[0])
        self.assertIsNone(growth.boundary([(0, 30), (10, 10)], 5, 20)[0])

    def test_counter_reset_inside_window_and_new_video_zero(self):
        day, start = "2026-10-26", stamp("2026-10-26")
        video = {"day": day}
        result = growth.daily_play(video, [(start+3600, 10), (start+86400, 100)], day, 108000)
        self.assertEqual(result["views"], 100)
        reset = growth.daily_play(video, [(start, 100), (start+3600, 10),
                                          (start+86400, 200)], day, 108000)
        self.assertIsNone(reset["views"])

    def test_remove_entire_top_author_not_top_video(self):
        day = "2026-10-26"
        videos = {"v1": {"day": day, "owner_id": "a"},
                  "v2": {"day": day, "owner_id": "a"},
                  "v3": {"day": day, "owner_id": "b"},
                  "v4": {"day": day, "owner_id": "c"}}
        plays = {key: {day: {"views": views, "estimated": False}}
                 for key, views in zip(videos, [1000, 1000, 100, 200])}
        result = growth.metric(set(videos), videos, plays, [day], 100)["today"]
        self.assertEqual(result["views"], 2300)
        self.assertEqual(result["remaining_views"], 300)
        self.assertEqual(result["remaining_authors"], 2)
        self.assertAlmostEqual(result["heat"], math.sqrt(600))
        single = growth.metric({"v1", "v2"}, videos, plays, [day], 100)["today"]
        self.assertEqual(single["heat"], 0)
        self.assertEqual(single["remaining_authors"], 0)


class GrowthStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = patch.multiple(
            config, DATA_DIR=self.tmp.name, DB_PATH=str(Path(self.tmp.name) / "growth.db"),
            BVIDEO_GROWTH_CANDIDATES=300, BVIDEO_GROWTH_SMOOTHING=100.0,
            BVIDEO_GROWTH_SAMPLE_HOURS=6, BVIDEO_GROWTH_MAX_GAP_HOURS=30,
            BVIDEO_GROWTH_TRACK_DAYS=30, BVIDEO_GROWTH_ENABLED=True)
        self.paths.start()
        growth.init()
        self.day = "2026-10-26"
        self.first = datetime.fromisoformat("2026-10-17")

    def tearDown(self):
        self.paths.stop()
        self.tmp.cleanup()

    def add(self, number, tags, owner=None, snapshots=True):
        bvid = f"BV1{number:09d}"
        owner = str(number if owner is None else owner)
        with db.session() as c:
            c.execute("""INSERT INTO bd_videos(bvid,day,title,owner_id,tags,views,detail_state,detail_updated)
                VALUES(?,?,'Title',?,?,100,'ok',?)""",
                      (bvid, self.first.date().isoformat(), owner, json.dumps(tags), stamp(self.day)))
            growth.record(c, {"bvid": bvid, "tags": tags})
            if snapshots:
                total = 0
                for i in range(11):
                    day = (self.first + timedelta(days=i)).date().isoformat()
                    growth.record_sample(c, bvid, total, stamp(day))
                    total += 10 if i < 7 else 100
        return bvid

    def fixture(self):
        for number in range(1, 4):
            self.add(number, ["ForgeTax", "铸剑大师", "Forge Tax"])
        for number in range(4, 7):
            self.add(number, ["ForgeTax", "铸剑纳贡"])

    def test_new_baseline_keeps_raw_history_but_excludes_it_from_growth_and_sampling(self):
        self.fixture()
        bili_latest.init()
        cutoff = stamp("2026-10-25") + 3600
        bili_queue.archive_pending(now=cutoff)
        fresh = self.add(7, ["FreshGame"], snapshots=False)
        with db.session() as c:
            c.execute("UPDATE bd_videos SET day='2026-10-26' WHERE bvid=?", (fresh,))
            c.execute("INSERT INTO bs_latest_seen VALUES(?,?,?,?)", (fresh, 17, cutoff + 1, cutoff + 2))
            c.execute("UPDATE bg_refresh SET active=1,next_due=0 WHERE bvid!=?", (fresh,))
        result = growth.build(self.day)
        self.assertEqual(result["baseline_started"], cutoff)
        self.assertEqual([t["name"] for t in result["tags"]], ["FreshGame"])
        self.assertIsNone(result["tags"][0]["index"])
        with db.session() as c:
            c.execute("UPDATE bg_refresh SET active=CASE WHEN bvid=? THEN 0 ELSE 1 END,next_due=0",
                      (fresh,))
        with patch.object(config, "source_on", return_value=True), \
                patch.object(bili, "_plain") as request:
            self.assertFalse(growth.sample_once())
        request.assert_not_called()
        with db.session() as c:
            self.assertGreater(c.execute("SELECT COUNT(*) FROM bg_samples").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bd_videos").fetchone()[0], 7)

    def test_transitive_group_and_bv_dedup_without_title_or_alias_dictionary(self):
        self.fixture()
        result = growth.build(self.day)
        self.assertEqual(result["selected_count"], 3)
        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        self.assertEqual(set(group["keys"]), {"forgetax", "铸剑大师", "铸剑纳贡"})
        self.assertEqual(group["videos"], 6)
        self.assertEqual(group["authors"], 6)
        self.assertEqual(group["today"]["views"], 600)
        self.assertEqual(group["today"]["remaining_views"], 500)
        self.assertEqual(group["name"], "ForgeTax")
        self.assertGreater(group["index"], 1)
        self.assertEqual({e["authors"] for e in group["edges"]}, {3})
        self.assertEqual(len(group["edges"]), 2)

    def test_three_videos_by_one_author_are_not_three_independent_votes(self):
        for number in range(1, 4):
            self.add(number, ["ForgeTax", "铸剑大师"], owner=1)
        result = growth.build(self.day)
        self.assertEqual(len(result["groups"]), 2)
        self.assertTrue(all(not g["edges"] for g in result["groups"]))
        self.assertTrue(all(g["today"]["heat"] == 0 for g in result["groups"]))

    def test_warmup_pairs_are_evidence_not_false_automatic_merges(self):
        self.fixture()
        result = growth.build("2026-10-17")
        self.assertGreater(result["deferred_edges"], 0)
        self.assertEqual(len(result["groups"]), 3)
        self.assertTrue(all(g["index"] is None for g in result["groups"]))

    def test_backfill_catches_concurrent_old_bv_below_saved_cursor(self):
        self.add(20, ["First"], snapshots=False)
        growth.backfill()
        self.add(1, ["Late"], snapshots=False)
        with db.session() as c:
            c.execute("DELETE FROM bg_indexed WHERE bvid='BV1000000001'")
            c.execute("DELETE FROM bg_hits WHERE bvid='BV1000000001'")
        self.assertEqual(growth.backfill(), 1)
        with db.session() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bg_hits WHERE bvid='BV1000000001'").fetchone()[0], 1)

    def test_reject_edge_splits_and_undo_survives_rebuild(self):
        self.fixture()
        growth.build(self.day)
        growth.edge_decision("ForgeTax", "铸剑大师", True)
        result = growth.payload()
        self.assertEqual(len(result["groups"]), 2)
        self.assertEqual(len(result["decisions"]), 1)
        growth.edge_decision("ForgeTax", "铸剑大师", False)
        self.assertEqual(len(growth.payload()["groups"]), 1)

    def test_cannot_link_applies_to_transitive_path(self):
        self.fixture()
        growth.build(self.day)
        growth.edge_decision("铸剑大师", "铸剑纳贡", True)
        result = growth.payload()
        self.assertFalse(any({"铸剑大师", "铸剑纳贡"} <= set(g["keys"]) for g in result["groups"]))
        self.assertEqual(len(result["conflict_edges"]), 1)

    def test_missing_snapshot_not_zero_and_backfill_preserves_observation_date(self):
        self.add(1, ["三角洲行动", "普通词"], snapshots=False)
        growth.backfill()
        with db.session() as c:
            samples = list(c.execute("SELECT ts FROM bg_samples"))
            tags = {r[0] for r in c.execute("SELECT norm FROM bg_tags")}
        self.assertEqual(samples[0][0], stamp(self.day))
        self.assertEqual(tags, {"三角洲行动", "普通词"})
        result = growth.build("2026-10-17")
        self.assertTrue(all(g["index"] is None for g in result["groups"]))
        self.assertTrue(all(g["today"]["missing_videos"] == 1 for g in result["groups"]))

    def test_growth_detected_even_without_new_uploads(self):
        self.fixture()
        with db.session() as c:
            c.execute("UPDATE bd_videos SET day='2026-10-01'")
        result = growth.build(self.day)
        self.assertEqual(result["selected_count"], 3)
        self.assertTrue(all(t["today"]["new_videos"] == 0 for t in result["tags"]))
        self.assertTrue(all(t["index"] > 1 for t in result["tags"]))

    def test_initial_observation_does_not_create_ten_days_of_zero_baseline(self):
        self.add(1, ["NewName"], snapshots=False)
        with db.session() as c:
            growth.record_sample(c, "BV1000000001", 100, stamp("2026-10-17")+3600)
        result = growth.build("2026-10-17")
        self.assertTrue(all(g["index"] is None for g in result["groups"]))

    def test_pause_stops_refresh_and_request_is_anonymous(self):
        bvid = self.add(1, ["Example"])
        with db.session() as c:
            c.execute("UPDATE bg_refresh SET active=1,next_due=0")
        with patch.object(bili, "paused", return_value=True), patch.object(bili, "_plain") as request:
            self.assertFalse(growth.sample_once())
            request.assert_not_called()
        with patch.object(bili, "paused", return_value=False), \
                patch.object(bili, "_plain", return_value={"code": 0, "data": {
                    "bvid": bvid, "stat": {"view": 500}}}) as request:
            self.assertTrue(growth.sample_once())
            self.assertFalse(request.call_args.kwargs["authenticated"])
            self.assertTrue(request.call_args.kwargs["persistent"])
        with db.session() as c:
            self.assertEqual(c.execute("SELECT views FROM bd_videos").fetchone()[0], 500)

    def test_refresh_error_backoff_and_wrong_bv_not_saved(self):
        self.add(1, ["Example"])
        with db.session() as c:
            c.execute("UPDATE bg_refresh SET active=1,next_due=0")
            before = c.execute("SELECT COUNT(*) FROM bg_samples").fetchone()[0]
        with patch.object(bili, "paused", return_value=False), patch.object(bili, "pause_left", return_value=0), \
                patch.object(bili, "_plain", return_value={"code": 0, "data": {
                    "bvid": "wrong", "stat": {"view": 999}}}):
            self.assertFalse(growth.sample_once())
        with db.session() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM bg_samples").fetchone()[0], before)
            self.assertEqual(c.execute("SELECT attempts FROM bg_refresh").fetchone()[0], 1)

    def test_route_and_invalid_parameters(self):
        class Response:
            def _json(self, body, code=200):
                self.result, self.code = body, code
        response = Response()
        response.path = "/api/bili-growth"
        server.Handler.do_GET(response)
        self.assertEqual(response.code, 200)
        self.assertEqual(response.result["revision"], growth.REVISION)
        with self.assertRaises(ValueError):
            growth.build("2099-01-01")
        with self.assertRaises(ValueError):
            growth.edge_decision("missing", "other")

    def test_http_rebuild_persists_c_and_rejects_bad_body_before_writing(self):
        self.fixture()
        class Response:
            def __init__(self, body):
                raw = json.dumps(body).encode()
                self.path = "/api/bili-growth/rebuild"
                self.headers = {"Content-Length": str(len(raw))}
                self.rfile = io.BytesIO(raw)
            def _json(self, body, code=200):
                self.result, self.code = body, code
        response = Response({"day": self.day, "smoothing": 200})
        server.Handler.do_POST(response)
        self.assertEqual(response.code, 200)
        self.assertEqual(response.result["smoothing"], 200)
        self.assertEqual(db.kv_get("bili_growth_smoothing"), 200)
        invalid = Response({"day": "2099-01-01", "smoothing": 999})
        server.Handler.do_POST(invalid)
        self.assertEqual(invalid.code, 400)
        self.assertEqual(db.kv_get("bili_growth_smoothing"), 200)
        invalid = Response([])
        server.Handler.do_POST(invalid)
        self.assertEqual(invalid.code, 400)

    def test_historical_cache_survives_a_later_default_rebuild(self):
        self.fixture()
        growth.build("2026-10-25")
        growth.build("2026-10-26")
        self.assertEqual(growth.payload("2026-10-25")["day"], "2026-10-25")


if __name__ == "__main__":
    unittest.main()
