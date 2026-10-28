import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from radar import config, db, revenue, trend


class RevenueTests(unittest.TestCase):
    now = 20 * trend.DAY

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(config, "DB_PATH", str(Path(self.tmp.name) / "test.db"))
        self.dir_patch = patch.object(config, "DATA_DIR", self.tmp.name)
        self.db_patch.start()
        self.dir_patch.start()
        db.init()
        self.model = json.loads(revenue.MODEL_PATH.read_text(encoding="utf-8"))
        self.model["usd_cny"] = 7
        self.ent = {"name": "Game", "extra": {"price_usd": 10, "is_free": False}}
        self.series = {("steam", "1", "reviews"): [(self.now - trend.DAY, 100), (self.now, 110)]}

    def tearDown(self):
        self.db_patch.stop()
        self.dir_patch.stop()
        self.tmp.cleanup()

    def estimate(self, member="steam:1", ent=None, ranks=None):
        return revenue.estimate_member(member, ent or self.ent, self.model, self.series, ranks or {}, self.now)

    def test_reviews_to_users_then_net_not_heat(self):
        row = self.estimate()
        self.assertEqual(row["daily_users"], 350)
        self.assertAlmostEqual(row["net_per_user_cny"], 49)
        self.assertAlmostEqual(row["daily_cny"], 17150)
        self.assertEqual(row["status"], "scenario")
        self.assertEqual(row["source"], "steam")
        self.assertEqual(row["horizon_days"], 90)
        self.assertTrue(row["assumptions"])

    def test_free_steam_is_not_zero_revenue(self):
        row = self.estimate(ent={"extra": {"is_free": True, "price_usd": 0}})
        self.assertIsNone(row["daily_cny"])
        self.assertIsNone(row["daily_users"])

    def test_single_sample_does_not_become_growth(self):
        self.series[("steam", "1", "reviews")] = [(self.now, 1000)]
        self.assertIsNone(self.estimate()["daily_cny"])

    def test_zero_observed_flow_is_valid(self):
        self.series[("steam", "1", "reviews")][-1] = (self.now, 100)
        self.assertEqual(self.estimate()["daily_cny"], 0)

    def test_tap_heat_is_not_downloads(self):
        self.series[("taptap", "1", "downloads")] = [(self.now - trend.DAY, 100), (self.now, 300)]
        row = self.estimate("taptap:1")
        self.assertIsNone(row["daily_cny"])
        self.assertIn("校准", row["reason"])

    def test_douyin_rank_needs_anchor_and_exponent(self):
        row = self.estimate("dymini:1", ranks={("dymini", "1", "rank_new"): (self.now, 60)})
        self.assertIsNone(row["daily_users"])
        self.assertIn("锚点", row["reason"])

    def test_douyin_only_51_to_200(self):
        self.model["conversions"]["dymini"].update(anchor_daily_users=10000, exponent=1)
        for rank, expected in [(50, None), (51, 10000 / 51), (200, 50), (201, None)]:
            with self.subTest(rank=rank):
                row = self.estimate("dymini:1", ranks={("dymini", "1", "rank_new"): (self.now, rank)})
                if expected is None:
                    self.assertIsNone(row["daily_users"])
                else:
                    self.assertAlmostEqual(row["daily_users"], expected)

    def test_settlement_purchase_tax_share_refund_then_withholding(self):
        rule = dict(iap_share=.7, ad_share=.6, refund_rate=.1, indirect_tax_rate=.2,
                    withholding_rate=.05, settlement_fee_rate=.01, payment_channel_rate=0,
                    invoice_adjustment_factor=1)
        self.assertAlmostEqual(revenue.settle(120, 10, rule), (120 * .9 / 1.2 * .7 + 6) * .95 * .99)

    def test_developer_net_is_not_deducted_twice(self):
        self.assertEqual(revenue.settle(10, 2, {}, "developer_net"), 12)
        self.assertIsNone(revenue.settle(10, 2, {}, "reported_revenue"))

    def test_unknown_settlement_is_missing(self):
        self.assertIsNone(revenue.settle(10, 0, {"iap_share": .7}))

    def test_no_cost_fields_are_applied(self):
        baseline = self.estimate()["daily_cny"]
        self.model["steam"].update(development_cost=1000000, cpi=1000, creator_commission=.9)
        self.assertEqual(self.estimate()["daily_cny"], baseline)

    def test_no_momentum_multiplier(self):
        self.series[("steam", "1", "reviews")] = [(self.now - trend.DAY * i, 1000 - 10 * i)
                                                   for i in range(10, -1, -1)]
        self.assertEqual(self.estimate()["daily_users"], 350)

    def test_country_mixture_and_genre_matching(self):
        self.model["profiles"] = [
            dict(country="CN", genre="puzzle", monetization="iaa", horizon_days=90,
                 iaa_cny=2, basis="developer_net", source="developer cohort"),
            dict(country="US", genre="puzzle", monetization="iaa", horizon_days=90,
                 iaa_cny=10, basis="developer_net", source="developer cohort")]
        attrs = dict(country_mix={"CN": .75, "US": .25}, genre="puzzle", monetization="iaa")
        value, error = revenue._mobile_value("taptap", attrs, self.model)
        self.assertIsNone(error)
        self.assertEqual(value["net_per_user_cny"], 4)
        attrs["genre"] = "slg"
        self.assertIsNone(revenue._mobile_value("taptap", attrs, self.model)[0])

    def test_global_benchmark_is_not_silently_a_china_profile(self):
        attrs = dict(country_mix={"CN": 1}, genre="casual", monetization="iap")
        self.assertIsNone(revenue._mobile_value("taptap", attrs, self.model)[0])

    def test_country_weights_must_not_be_normalized_silently(self):
        attrs = dict(country_mix={"CN": .2}, genre="casual", monetization="iap")
        self.assertIn("合计", revenue._mobile_value("taptap", attrs, self.model)[1])

    def test_different_horizons_are_not_mixed(self):
        self.model["profiles"] = [
            dict(country="CN", genre="puzzle", monetization="iaa", horizon_days=30,
                 iaa_cny=2, basis="developer_net", source="developer cohort")]
        attrs = dict(country_mix={"CN": 1}, genre="puzzle", monetization="iaa")
        self.assertIsNone(revenue._mobile_value("taptap", attrs, self.model)[0])

    def test_hybrid_cannot_sum_different_samples(self):
        self.model["profiles"] = [
            dict(country="CN", genre="puzzle", monetization="hybrid", horizon_days=90,
                 iaa_cny=2, iap_cny=5, basis="developer_net", source="different samples")]
        attrs = dict(country_mix={"CN": 1}, genre="puzzle", monetization="hybrid")
        self.assertIn("同批", revenue._mobile_value("taptap", attrs, self.model)[1])

    def test_complete_mobile_scenario_produces_revenue(self):
        self.model["conversions"]["dymini"].update(anchor_daily_users=12000, exponent=1,
                                                   source="synthetic calibration")
        self.model["profiles"] = [
            dict(country="CN", genre="puzzle", monetization="iaa", horizon_days=90,
                 iaa_cny=2, basis="developer_net", source="synthetic cohort")]
        ent = {"extra": {"revenue": {"country_mix": {"CN": 1}, "genre": "puzzle",
                                     "monetization": "iaa"}}}
        row = self.estimate("dymini:1", ent, {("dymini", "1", "rank_new"): (self.now, 60)})
        self.assertEqual(row["daily_users"], 200)
        self.assertEqual(row["net_per_user_cny"], 2)
        self.assertEqual(row["daily_cny"], 400)
        self.assertEqual(row["status"], "scenario")

    def test_contract_does_not_leak_to_another_platform(self):
        self.model["profiles"] = [
            dict(country="CN", genre="puzzle", monetization="iaa", horizon_days=90,
                 iaa_cny=2, basis="gross", source="synthetic cohort")]
        attrs = dict(country_mix={"CN": 1}, genre="puzzle", monetization="iaa",
                     contract="apple_small_business")
        self.assertIn("不匹配", revenue._mobile_value("dymini", attrs, self.model)[1])

    def test_ad_invoice_adjustment_not_a_second_network_commission(self):
        rule = dict(ad_share=.6, withholding_rate=0, settlement_fee_rate=0,
                    invoice_adjustment_factor=1.01 / 1.06)
        self.assertAlmostEqual(revenue.settle(0, 100, rule), 60 * 1.01 / 1.06)

    def test_new_chart_is_not_a_fake_launch_date(self):
        from radar import analyze
        ent = {"extra": {"new_candidate": True}, "last_seen": self.now, "name": "Game"}
        with patch.object(db, "source_first_runs", return_value={}), \
                patch.object(analyze, "_game_age", return_value=(None, None)), \
                patch.object(analyze, "_display", return_value=("Game", "", "")), \
                patch.object(analyze, "_big_game", return_value=False):
            rows = analyze._new_games({"a": ["dymini:1"]}, {("dymini", "1"): ent}, self.now)
            self.assertEqual(rows["a"], {"days": None, "how": "new_chart"})
            ent["extra"]["new_candidate"] = False
            self.assertEqual(analyze._new_games({"a": ["dymini:1"]},
                                                {("dymini", "1"): ent}, self.now), {})

    def test_overlapping_channels_are_not_summed(self):
        row = dict(member="taptap:1", daily_users=100, net_per_user_cny=2, daily_cny=200)
        other = dict(row, member="tapmaker:1", daily_cny=300)
        out = revenue.aggregate([row, other])
        self.assertEqual(out["daily_cny"], 300)
        self.assertNotEqual(out["daily_cny"], 500)

    def test_revenue_selection_is_independent_of_heat(self):
        rows = {str(i): {"daily_cny": i} for i in range(401)}
        rows["high_revenue_no_heat"] = {"daily_cny": 1e6}
        picked = revenue.visible_groups(list(rows), rows)
        self.assertEqual(picked[0], "high_revenue_no_heat")
        self.assertEqual(len(picked), 400)

    def test_invalid_share_does_not_emit_amount(self):
        self.model["steam"]["developer_share"] = 70
        self.assertIsNone(self.estimate()["daily_cny"])

    def test_stale_rank_absent_from_latest_chart_is_not_kept(self):
        with db.session() as c:
            db.put_metrics(c, self.now - 3600, "dymini", [("1", "rank_new", 60)])
            db.put_metrics(c, self.now, "dymini", [("2", "rank_new", 61)])
        entities = {("dymini", "1"): {"extra": {}}, ("dymini", "2"): {"extra": {}}}
        with patch.object(config, "source_on", return_value=True):
            rows, _ = revenue.score_groups({"a": ["dymini:1"]}, entities, self.now, self.model)
        self.assertEqual(rows["a"]["channels"][0]["reason"], "缺当前同榜单名次")

    def test_tap_same_id_keeps_one_channel(self):
        with db.session() as c:
            for src in ("tapmaker", "taptap"):
                db.put_metrics(c, self.now - trend.DAY, src, [("1", "downloads", 10)])
                db.put_metrics(c, self.now, src, [("1", "downloads", 20)])
        with patch.object(config, "source_on", return_value=True):
            rows, _ = revenue.score_groups({"a": ["taptap:1", "tapmaker:1"]}, {}, self.now, self.model)
        self.assertEqual(len(rows["a"]["channels"]), 1)

    def test_kv_overrides_do_not_mutate_file(self):
        before = copy.deepcopy(self.model["conversions"]["dymini"])
        db.kv_set("revenue_model", {"conversions": {"dymini": {"anchor_daily_users": 999}}})
        self.assertEqual(revenue.load_model()["conversions"]["dymini"]["anchor_daily_users"], 999)
        self.assertIsNone(before["anchor_daily_users"])


if __name__ == "__main__":
    unittest.main()
