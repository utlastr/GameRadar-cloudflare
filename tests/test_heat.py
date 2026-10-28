import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from radar import config, db, heat, trend


class TrendTests(unittest.TestCase):
    now = 20 * trend.DAY

    def points(self, increments):
        value = 1000
        pts = [(self.now - len(increments) * trend.DAY, value)]
        for delta in increments:
            value += delta
            pts.append((pts[-1][0] + trend.DAY, value))
        return pts

    def test_trend_is_symmetric_and_bounded(self):
        up = trend.profile([50] * 3 + [10] * 7)
        down = trend.profile([10] * 3 + [50] * 7)
        self.assertAlmostEqual(up["trend"], 2 / 3)
        self.assertAlmostEqual(down["trend"], -2 / 3)
        self.assertAlmostEqual(up["trend_factor"], 1 + .2 * 2 / 3)
        self.assertAlmostEqual(down["trend_factor"], 1 - .2 * 2 / 3)
        self.assertEqual(trend.profile([100] * 3 + [0] * 7)["trend_factor"], 1.2)
        self.assertEqual(trend.profile([0] * 3 + [100] * 7)["trend_factor"], .8)

    def test_weighted_increments_not_lifetime_total(self):
        p = trend.counter_profile(self.points([20, 40, 100]), self.now)
        self.assertAlmostEqual(p["value"], 66)
        self.assertEqual(p["days"][:3], [100, 40, 20])
        self.assertFalse(p["trend_known"])

    def test_single_day_decline_does_not_destroy_base(self):
        p = trend.counter_profile(self.points([200] * 9 + [100]), self.now)
        self.assertEqual(p["value"], 150)
        self.assertAlmostEqual(p["recent"], 500 / 3)
        self.assertGreater(p["trend_factor"], .8)
        self.assertGreater(p["adjusted"], 120)
        self.assertLess(p["adjusted"], 150)

    def test_accelerating_and_fading_launch(self):
        up = trend.counter_profile(self.points([41, 110]), self.now)
        down = trend.counter_profile(self.points([41, 20]), self.now)
        self.assertGreater(up["adjusted"], down["adjusted"])
        self.assertEqual(up["trend_factor"], 1)
        self.assertEqual(down["trend_factor"], 1)

    def test_first_snapshot_is_not_growth(self):
        self.assertIsNone(trend.counter_profile([(self.now, 10000)], self.now))

    def test_missing_baseline_is_not_zero(self):
        p = trend.profile([100, 100, 100] + [None] * 7)
        self.assertIsNone(p["baseline"])
        self.assertFalse(p["trend_known"])
        self.assertEqual(p["trend_factor"], 1)

    def test_zero_observed_flow_is_valid(self):
        p = trend.counter_profile(self.points([0] * 10), self.now)
        self.assertEqual(p["value"], 0)
        self.assertEqual(p["trend"], 0)
        self.assertTrue(p["trend_known"])

    def test_counter_reset_is_unknown_not_zero(self):
        pts = [(self.now - trend.DAY, 100), (self.now, 50)]
        self.assertIsNone(trend.counter_profile(pts, self.now))

    def test_two_day_sampling_gap_is_not_spread_into_daily_growth(self):
        pts = [(self.now - 2 * trend.DAY, 100), (self.now, 500)]
        self.assertIsNone(trend.counter_profile(pts, self.now))

    def test_stale_snapshot_cannot_keep_ranking(self):
        self.assertIsNone(trend.counter_profile(
            self.points([100] * 10), self.now + trend.FRESH + 1))

    def test_irregular_samples_use_elapsed_time(self):
        pts = [(self.now - 48 * 3600, 0), (self.now - 18 * 3600, 300),
               (self.now, 480)]
        p = trend.counter_profile(pts, self.now)
        self.assertEqual(p["days"][:2], [240, 240])

    def test_short_observed_interval_does_not_create_past_days(self):
        p = trend.counter_profile([(self.now - 2 * 3600, 40), (self.now, 50)], self.now)
        self.assertEqual(p["value"], 120)
        self.assertTrue(all(x is None for x in p["days"][1:]))
        self.assertFalse(p["trend_known"])

    def test_online_levels_are_averaged_not_differenced(self):
        p = trend.level_profile([(self.now - trend.DAY, 200), (self.now, 100)], self.now)
        self.assertEqual(p["value"], 150)
        self.assertEqual(p["days"][0], 150)

    def test_online_decrease_is_not_a_counter_reset(self):
        points = [(self.now - i * trend.DAY, 200 + i * 10) for i in range(10, -1, -1)]
        p = trend.level_profile(points, self.now)
        self.assertIsNotNone(p)
        self.assertLess(p["trend"], 0)


class PureTrendTests(unittest.TestCase):
    def row(self, recent, baseline, p="steam/reviews"):
        return {**trend.profile([recent] * 3 + [baseline] * 7), "p": p, "name": p}

    def test_absolute_scale_does_not_change_trend_order(self):
        tiny = heat.pure_trend([self.row(50, 10)])
        huge = heat.pure_trend([self.row(500000, 100000)])
        self.assertEqual(tiny["ratio"], 5)
        self.assertEqual(heat.rank_key({"trend": tiny}, "trend"),
                         heat.rank_key({"trend": huge}, "trend"))

    def test_fast_small_game_outranks_large_declining_game(self):
        fast = {"trend": heat.pure_trend([self.row(50, 10)]), "raw": -100}
        fading = {"trend": heat.pure_trend([self.row(100, 200)]), "raw": 100}
        self.assertLess(heat.rank_key(fast, "trend"), heat.rank_key(fading, "trend"))

    def test_zero_baseline_is_not_infinite_growth(self):
        t = heat.pure_trend([self.row(100, 0)])
        self.assertEqual(t["state"], "zero_baseline")
        self.assertIsNone(t["ratio"])
        self.assertIsNone(t["growth_pct"])

    def test_missing_history_is_not_flat_trend(self):
        row = {**trend.profile([100, None, None] + [None] * 7), "p": "steam/reviews",
               "name": "Steam"}
        self.assertEqual(heat.pure_trend([row])["state"], "insufficient")

    def test_largest_ratio_is_selected_without_platform_count_bonus(self):
        best = self.row(50, 10)
        t = heat.pure_trend([best, self.row(1000, 1000, "taptap/downloads")])
        self.assertEqual(t["ratio"], heat.pure_trend([best])["ratio"])
        self.assertEqual(t["p"], "steam/reviews")

    def test_trend_winner_survives_composite_score_truncation(self):
        scores = {
            "large": {"raw": 100, "final": 100, "trend": heat.pure_trend([self.row(100, 100)])},
            "small": {"raw": -100, "final": -100, "trend": heat.pure_trend([self.row(50, 10)])},
        }
        self.assertEqual(set(heat.visible_groups(list(scores), scores, limit=1)), {"large", "small"})


class HeatScoreTests(unittest.TestCase):
    def score(self, values, platforms=("bilibili",), factor=1.0, multiples=None):
        groups = {gid: ["steam:" + gid] for gid in values}
        entities = {("steam", gid): {"name": gid} for gid in values}
        scores = {gid: {"value": value, "adjusted": value, "full": True, "n": 2}
                  for gid, value in values.items() if value is not None}
        with patch.multiple(heat,
                            entity_multiples=lambda now: (multiples or {}, {}),
                            penalty_factors=lambda *a: {"tag": {"factor": factor}},
                            region_platforms=lambda: platforms,
                            name_index=lambda *a: {}, video_links=lambda *a: {},
                            bili_videos=lambda *a: ([], {}),
                            social_videos=lambda *a: ([], {}),
                            platform_scores=lambda *a: (scores, {}),
                            big_reason=lambda *a: None,
                            _tags=lambda *a: [], _has=lambda *a: True), \
                patch.object(heat.config, "PENALTY_WORDS", {"tag": []}):
            return heat.score_groups(groups, entities)

    def counter(self, values, metric="reviews"):
        return {f"steam:{gid}": [
            {**trend.profile([value] * 10), "src": "steam", "metric": metric,
             "kind": "down", "normal": 10, "x": value / 10, "cumulative": True}]
                for gid, value in values.items()}

    def test_preserves_existing_peer_normalization(self):
        rows, meta = self.score({"low": 3, "normal": 10, "high": 40})
        self.assertLess(rows["low"]["raw"], 0)
        self.assertEqual(rows["normal"]["raw"], 0)
        self.assertGreater(rows["high"]["raw"], 0)
        self.assertEqual(meta["trend_weight"], .2)

    def test_penalty_only_changes_final(self):
        rows, _ = self.score({"low": 3, "normal": 10, "high": 40}, factor=.5)
        self.assertEqual(rows["normal"]["raw"], 0)
        self.assertLess(rows["normal"]["final"], rows["normal"]["raw"])

    def test_counter_only_game_has_primary_score(self):
        rows, _ = self.score({"low": None, "high": None},
                             multiples=self.counter({"low": 10, "high": 50}))
        self.assertGreater(rows["high"]["raw"], rows["low"]["raw"])
        self.assertEqual(rows["high"]["videos"], [])
        self.assertEqual(rows["high"]["counters"][0]["value"], 50)

    def test_rank_only_remains_reference_not_fake_downloads(self):
        ranks = {"steam:a": [{"src": "steam", "metric": "rank_played", "kind": "down",
                              "value": 1, "normal": 10, "x": 10}]}
        rows, _ = self.score({"a": None}, multiples=ranks)
        self.assertIsNone(rows["a"]["raw"])
        self.assertEqual(rows["a"]["counters"], [])

    def test_second_counter_does_not_inflate_platform_count(self):
        counters = self.counter({"low": 10, "high": 50})
        rows, _ = self.score({"low": None, "high": None}, multiples=counters)
        original = rows["high"]["raw"]
        for items in counters.values():
            items.append({**items[0], "metric": "downloads"})
        rows, _ = self.score({"low": None, "high": None}, multiples=counters)
        self.assertEqual(rows["high"]["raw"], original)

    def test_duplicate_store_members_are_not_summed(self):
        a = self.counter({"a": 50})["steam:a"]
        rows = heat.counter_platforms({"g": ["steam:a", "steam:b"]},
                                     {"steam:a": a, "steam:b": a})
        self.assertEqual(rows["steam/reviews"]["g"]["value"], 50)

    def test_momentum_adjusts_positive_activity_before_z(self):
        profiles = {"fall": trend.profile([100] * 3 + [200] * 7),
                    "rise": trend.profile([100] * 3 + [50] * 7)}
        multiples = {f"steam:{gid}": [
            {**p, "src": "steam", "metric": "reviews", "kind": "down",
             "normal": 100, "x": p["adjusted"] / 100, "cumulative": True}]
                     for gid, p in profiles.items()}
        rows, _ = self.score({"fall": None, "rise": None}, multiples=multiples)
        self.assertGreater(rows["rise"]["raw"], rows["fall"]["raw"])


class CounterDatabaseTests(unittest.TestCase):
    def test_all_collected_counters_share_increment_logic_but_players_do_not(self):
        now = 20 * trend.DAY
        with tempfile.TemporaryDirectory() as tmp, \
                patch.multiple(config, DATA_DIR=tmp, DB_PATH=str(Path(tmp) / "test.db")), \
                patch.object(config, "ANALYZE", {("steam", "players"): ("level", 500)}), \
                patch.object(config, "source_on", return_value=True):
            db.init()
            with db.session() as c:
                for src, metric in [("steam", "reviews"), ("tapmaker", "downloads"),
                                    ("roblox", "visits"), ("taptap", "fans")]:
                    for entity, delta in [("a", 40), ("b", 100)]:
                        for i in range(11):
                            db.put_metrics(c, now - (10 - i) * trend.DAY, src,
                                           [(entity, metric, 10000 + i * delta)])
                db.put_metrics(c, now - trend.DAY, "steam", [("a", "players", 200)])
                db.put_metrics(c, now, "steam", [("a", "players", 100)])
            rows, _ = heat.entity_multiples(now)
            for src in ("steam", "tapmaker", "roblox", "taptap"):
                item = next(x for x in rows[f"{src}:a"] if x["cumulative"])
                self.assertEqual(item["value"], 40)
                self.assertEqual(item["trend_factor"], 1)
                self.assertEqual(item["total"], 10400)
                self.assertEqual(item["sampled_at"], now)
            players = next(x for x in rows["steam:a"] if x["metric"] == "players")
            self.assertEqual(players["value"], 100)
            self.assertFalse(players["cumulative"])


class VideoTrendTests(unittest.TestCase):
    def test_recent_publication_does_not_invent_zero_baseline(self):
        now = 20 * trend.DAY
        pts = [(now, [100, None, None, None, None])]
        out, _ = heat.platform_scores({"v": (pts, now - trend.DAY)}, {"v": {"g"}}, now)
        self.assertEqual(out["g"]["value"], 100)
        self.assertFalse(out["g"]["trend_known"])
        self.assertFalse(out["g"]["full"])

    def test_video_uses_same_increment_profile(self):
        now = 20 * trend.DAY
        pts = [(now - (10 - i) * trend.DAY, [100 + 40 * i, None, None, None, None])
               for i in range(11)]
        out, _ = heat.platform_scores({"v": (pts, None)}, {"v": {"g"}}, now)
        self.assertEqual(out["g"]["value"], 40)
        self.assertEqual(out["g"]["trend_factor"], 1)

    def test_missing_video_history_is_not_zero_baseline(self):
        now = 20 * trend.DAY
        pts = [(now - trend.DAY, [100, None, None, None, None]),
               (now, [140, None, None, None, None])]
        out, _ = heat.platform_scores({"v": (pts, None)}, {"v": {"g"}}, now)
        self.assertFalse(out["g"]["trend_known"])
        self.assertEqual(out["g"]["value"], 40)

    def test_stale_video_is_excluded(self):
        now = 20 * trend.DAY
        pts = [(now - 4 * trend.DAY, [0, None, None, None, None]),
               (now - 3 * trend.DAY, [40, None, None, None, None])]
        out, _ = heat.platform_scores({"v": (pts, None)}, {"v": {"g"}}, now)
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main()
