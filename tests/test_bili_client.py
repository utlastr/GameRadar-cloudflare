import unittest
import threading
import time
from unittest.mock import MagicMock, patch

from radar.sources import bili


class BiliClientTests(unittest.TestCase):
    def setUp(self):
        self.values = {}
        self.patches = [
            patch.object(bili.db, "kv_get", side_effect=lambda k, default=None: self.values.get(k, default)),
            patch.object(bili.db, "kv_set", side_effect=lambda k, v: self.values.__setitem__(k, v)),
            patch.object(bili, "REQUEST_GAP", 0),
            patch.object(bili, "_anonymous_local", threading.local()),
            patch("radar.activity.say"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def test_352_only_pauses_the_failing_endpoint(self):
        with patch.object(bili, "get_json_strict", return_value={"code": -352}), \
                patch.object(bili, "cookie", return_value=""):
            with self.assertRaises(bili.RiskError):
                bili._plain("https://api.bilibili.com/x/web-interface/ranking/v2",
                            {"rid": 4}, authenticated=False)
        self.assertFalse(bili.paused())
        self.assertTrue(bili.paused("排行榜"))
        self.assertFalse(bili.paused("全站热门"))
        self.assertFalse(bili.paused("分区最新"))
        self.assertNotIn("bili_pause_until", self.values)

    def test_412_still_pauses_every_endpoint(self):
        with patch.object(bili, "get_json_strict", return_value={"code": -412}):
            with self.assertRaises(bili.RiskError):
                bili.newlist(1)
        self.assertTrue(bili.paused())
        with patch.object(bili, "get_json_strict") as request:
            with self.assertRaises(bili.RiskError):
                bili.view("BV19gpw6XE95")
        request.assert_not_called()

    def test_risk_is_not_counted_twice_by_callers(self):
        bili.note_risk("code=-352", "搜索")
        bili.note_risk(bili.RiskError("code=-352", "搜索"))
        self.assertEqual(self.values["bili_risk_hits:搜索"], 1)
        self.assertNotIn("bili_pause_until", self.values)

    def test_concurrent_failures_share_one_incident_and_keep_first_deadline(self):
        barrier = threading.Barrier(4)
        def report():
            barrier.wait(5)
            bili.note_risk("HTTP Error 412", "视频详情及标签")
        threads = [threading.Thread(target=report) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        deadline = self.values["bili_pause_until"]
        bili.note_risk("HTTP Error 412", "日期目录")
        self.assertEqual(self.values["bili_risk_hits"], 1)
        self.assertEqual(self.values["bili_pause_until"], deadline)
        self.assertEqual(self.values["bili_last_risk"]["origin"], "视频详情及标签")
        self.assertEqual(self.values["bili_adaptive_requests_per_minute"], 60)

    def test_new_incident_after_expiry_escalates_but_existing_pause_is_preserved(self):
        with patch.object(bili.time, "time", return_value=10000):
            bili.note_risk("HTTP Error 412", "搜索")
        with patch.object(bili.time, "time", return_value=12000):
            bili.note_risk("HTTP Error 412", "日期目录")
        with patch.object(bili.time, "time", return_value=14000):
            bili.note_risk("HTTP Error 412", "视频详情及标签")
            self.assertEqual(self.values["bili_risk_hits"], 3)
            self.assertEqual(self.values["bili_pause_until"], 14000 + 7200)
            self.assertEqual(bili.pause_info()["seconds"], 7200)
        self.assertEqual(self.values["bili_adaptive_requests_per_minute"], 15)

    def test_upgrade_of_existing_pause_never_resets_or_shortens_it(self):
        deadline = time.time() + 5000
        self.values.update(bili_pause_until=deadline, bili_risk_hits=3)
        with patch.object(bili, "REQUEST_GAP", 0.5):
            bili.preserve_cooldown_budget()
            bili.preserve_cooldown_budget()
            self.assertEqual(bili.effective_request_gap(), 1)
        self.assertEqual(self.values["bili_pause_until"], deadline)
        self.assertEqual(self.values["bili_risk_hits"], 3)
        self.assertEqual(self.values["bili_adaptive_requests_per_minute"], 60)

    def test_adaptive_budget_applies_to_all_requests_and_survives_success(self):
        self.values["bili_adaptive_requests_per_minute"] = 30
        with patch.object(bili, "_last_request", 100), \
                patch.object(bili.time, "monotonic", return_value=100.5), \
                patch.object(bili.time, "sleep") as sleep:
            bili._http(lambda: {"code": 0}, "分区最新")
        sleep.assert_called_once_with(1.5)
        self.assertEqual(bili.request_metrics()["latency_free_ceiling_per_minute"], 30)
        self.assertEqual(self.values["bili_adaptive_requests_per_minute"], 30)

    def test_latest_uses_archives_even_when_page_count_is_zero(self):
        record = {"bvid": "BV19gpw6XE95"}
        data = {"code": 0, "data": {"page": {"count": 0}, "archives": [record]}}
        with patch.object(bili, "get_json_strict", return_value=data) as request, \
                patch.object(bili, "cookie") as cookie:
            self.assertEqual(bili.newlist(1), [record])
        cookie.assert_not_called()
        self.assertEqual(request.call_args.kwargs["retries"], 0)

    def test_http_412_is_global_and_not_retried(self):
        with patch.object(bili, "get_json_strict", side_effect=RuntimeError("HTTP Error 412")) as request:
            with self.assertRaises(bili.RiskError):
                bili.newlist(1)
        self.assertEqual(request.call_count, 1)
        self.assertTrue(bili.paused())

    def test_success_on_another_endpoint_does_not_reset_risk_history(self):
        bili.note_risk("code=-352", "排行榜")
        with patch.object(bili, "get_json_strict", return_value={"code": 0, "data": {"archives": []}}):
            bili.newlist(1)
        self.assertEqual(self.values["bili_risk_hits:排行榜"], 1)

    def test_paused_search_does_not_read_cookies_or_request_a_signature(self):
        bili.note_risk("code=-352", "搜索")
        with patch.object(bili, "cookie") as cookie, \
                patch.object(bili, "wbi_sign") as sign, \
                patch.object(bili, "get_json_strict") as request:
            with self.assertRaises(bili.RiskError):
                bili.search("game")
        for operation in (cookie, sign, request):
            operation.assert_not_called()

    def test_requests_share_a_minimum_gap(self):
        with patch.object(bili, "REQUEST_GAP", 1.5), \
                patch.object(bili, "_last_request", 100), \
                patch.object(bili.time, "monotonic", return_value=100.5), \
                patch.object(bili.time, "sleep") as sleep:
            bili._http(lambda: {"code": 0}, "分区最新")
        sleep.assert_called_once_with(1.0)

    def test_anonymous_polling_reuses_connection_without_login_or_redirects(self):
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.status_code = 200
        response.json.return_value = {"code": 0, "data": {"new": []}}
        with patch("requests.Session", return_value=session) as create, \
                patch.object(bili, "cookie") as cookie:
            for _ in range(2):
                bili._plain("https://app.bilibili.com/x/v2/region/dynamic/list",
                            {"rid": 17}, authenticated=False, persistent=True)
        create.assert_called_once()
        cookie.assert_not_called()
        self.assertFalse(session.trust_env)
        self.assertEqual(session.cookies.clear.call_count, 2)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertNotIn("Cookie", session.get.call_args.kwargs["headers"])

    def test_anonymous_http_412_stops_globally_without_retry(self):
        session = MagicMock()
        session.get.return_value.__enter__.return_value.status_code = 412
        with patch("requests.Session", return_value=session):
            with self.assertRaises(bili.RiskError):
                bili._plain("https://app.bilibili.com/x/v2/region/dynamic/list",
                            {"rid": 17}, authenticated=False, persistent=True)
        self.assertTrue(bili.paused())
        self.assertEqual(session.get.call_count, 1)

    def test_anonymous_challenge_pauses_without_solving_or_retrying(self):
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.status_code = 200
        response.json.side_effect = ValueError("not JSON")
        response.text = "captcha verification"
        with patch("requests.Session", return_value=session):
            with self.assertRaises(bili.RiskError):
                bili._plain("https://app.bilibili.com/x/v2/region/dynamic/list",
                            {"rid": 17}, authenticated=False, persistent=True)
        self.assertTrue(bili.paused(bili.REGION_FEED_SCOPE))
        self.assertEqual(session.get.call_count, 1)

    def test_numeric_business_message_is_not_duplicated(self):
        with self.assertRaisesRegex(RuntimeError, r"code=62012$"):
            bili.check({"code": 62012, "message": "62012"}, "detail")

    def test_fair_admission_keeps_details_and_directory_moving_under_head_traffic(self):
        order, errors, threads = [], [], []
        active, maximum = [0], [0]
        lock = threading.Lock()

        def run(scope, count):
            try:
                for _ in range(count):
                    def request():
                        with lock:
                            active[0] += 1
                            maximum[0] = max(maximum[0], active[0])
                            order.append(scope)
                        time.sleep(0.001)
                        with lock:
                            active[0] -= 1
                        return {"code": 0}
                    bili._http(request, scope)
            except Exception as exc:
                errors.append(exc)

        with patch.object(bili, "MAX_INFLIGHT", 1):
            with bili.request_slot("holder"):
                for scope, count in ((bili.LATEST_GAME_SCOPE, 15), ("\u89c6\u9891\u8be6\u60c5\u53ca\u6807\u7b7e", 5),
                                     ("\u65e5\u671f\u76ee\u5f55", 5), ("other", 5)):
                    thread = threading.Thread(target=run, args=(scope, count))
                    threads.append(thread)
                    thread.start()
                deadline = time.monotonic() + 5
                while sum(bili.request_metrics()["waiting"].values()) < 4 and time.monotonic() < deadline:
                    time.sleep(0.001)
                self.assertEqual(sum(bili.request_metrics()["waiting"].values()), 4)
            for thread in threads:
                thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(maximum[0], 1)
        self.assertEqual(len(order), 30)
        self.assertLess(order.index("\u89c6\u9891\u8be6\u60c5\u53ca\u6807\u7b7e"), 3)
        self.assertEqual(order.count("\u65e5\u671f\u76ee\u5f55"), 5)

    def test_nested_request_slot_is_reentrant(self):
        with bili.request_slot("outer"):
            self.assertEqual(bili._http(lambda: {"code": 0}, "inner"), {"code": 0})
        self.assertFalse(bili._request_active)
        self.assertFalse(any(bili.request_metrics()["waiting"].values()))

    def test_network_latency_overlaps_but_start_spacing_and_pool_remain_bounded(self):
        active, maximum, starts, errors = [0], [0], [], []
        lock = threading.Lock()
        def request():
            with lock:
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
                starts.append(time.monotonic())
            time.sleep(0.08)
            with lock:
                active[0] -= 1
            return {"code": 0}
        def run():
            try:
                bili._http(request, "视频详情及标签")
            except Exception as exc:
                errors.append(exc)
        with patch.object(bili, "REQUEST_GAP", 0.02), patch.object(bili, "MAX_INFLIGHT", 3):
            threads = [threading.Thread(target=run) for _ in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(maximum[0], 3)
        self.assertTrue(all(b - a >= 0.018 for a, b in zip(starts, starts[1:])), starts)
        self.assertLess(starts[-1] - starts[0], 0.8)
        metrics = bili.request_metrics()
        self.assertEqual(metrics["spacing"], "request_start")
        self.assertEqual(metrics["limit_source"], "application_policy")
        self.assertGreater(metrics["mean_wire_seconds"], 0)
        self.assertEqual(metrics["inflight"], 0)
        self.assertEqual(metrics["admitted"], 0)
        self.assertFalse(bili._request_active)

    def test_queued_calls_stop_on_risk_without_clearing_existing_pause(self):
        called, errors = [], []
        def request():
            called.append(1)
            return {"code": -412}
        def run():
            try:
                bili._http(request, "视频详情及标签")
            except bili.RiskError:
                pass
            except Exception as exc:
                errors.append(exc)
        with patch.object(bili, "REQUEST_GAP", 0.01), patch.object(bili, "MAX_INFLIGHT", 4):
            threads = [threading.Thread(target=run) for _ in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(called), 1)
        self.assertTrue(bili.paused())

    def test_inflight_success_does_not_clear_another_calls_risk_history(self):
        def request():
            bili.note_risk("HTTP Error 412")
            return {"code": 0}
        bili._http(request)
        self.assertEqual(self.values["bili_risk_hits"], 1)
        self.assertTrue(bili.paused())

    def test_anonymous_connections_belong_to_individual_threads(self):
        sessions, errors = [], []
        def create():
            session = MagicMock()
            response = session.get.return_value.__enter__.return_value
            response.status_code = 200
            response.json.return_value = {"code": 0}
            sessions.append(session)
            return session
        def run():
            try:
                for _ in range(2):
                    bili._anonymous_json("https://api.bilibili.com/test", {}, {}, 2)
            except Exception as exc:
                errors.append(exc)
            finally:
                bili.close_anonymous_session()
        with patch("requests.Session", side_effect=create):
            threads = [threading.Thread(target=run) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(len(sessions), 4)
        for session in sessions:
            self.assertEqual(session.get.call_count, 2)
            session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
