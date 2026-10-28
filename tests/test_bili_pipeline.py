"""Exercise real HTTP transport and production workers against a local fixture."""
import json
import tempfile
import threading
import time
import unittest
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from radar import bili_daily as daily, bili_latest as latest, bili_stream as stream, config, db
from radar.sources import bili


def bv(number):
    return f"BV1{number:09d}"


def capacity_fixture(gap=0.01, latency=0.024):
    """Local-only HTTP load: 50 full details, 40 head reads, 10 dated directories."""
    lock, arrivals, errors, details = threading.Lock(), [], [], []
    active, peak = [0], [0]
    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query)
            with lock:
                arrivals.append({"path": parsed.path, "cookie": self.headers.get("Cookie")})
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                time.sleep(latency)
                number = int(params.get("bvid", [bv(1)])[0][3:])
                row = {"bvid": bv(number), "tid": 17, "tid_v2": 2079,
                       "pubdate": int(time.time()), "title": "[Example]",
                       "owner": {"mid": 1, "name": "Author"},
                       "stat": dict(view=100, like=10, coin=5, favorite=4, share=3, reply=2)}
                data = ({"View": row, "Tags": [{"tag_name": "Example"},
                                              {"tag_name": "AllTags"}]}
                        if parsed.path.endswith("/view/detail") else {"archives": [row]})
                body = json.dumps({"code": 0, "data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                with lock:
                    active[0] -= 1
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    original = bili._anonymous_json
    def local_transport(url, params, headers, timeout):
        if urlsplit(url).hostname != "api.bilibili.com":
            raise AssertionError("Unexpected fixture host")
        return original(f"http://127.0.0.1:{server.server_port}{urlsplit(url).path}",
                        params, headers, timeout)
    def run(kind, numbers):
        try:
            for number in numbers:
                if kind == "detail":
                    details.append(bili.detail_with_tags(bv(number), persistent=True, timeout=12))
                elif kind == "latest":
                    bili.latest_game_posts()
                else:
                    bili._plain("https://api.bilibili.com/x/web-interface/newlist_rank",
                                {"rid": 17}, authenticated=False, persistent=True)
        except Exception as exc:
            errors.append(str(exc))
        finally:
            bili.close_anonymous_session()
    values = {}
    try:
        with patch.object(bili, "_anonymous_local", threading.local()), \
                patch.object(bili, "_anonymous_json", side_effect=local_transport), \
                patch.object(bili, "REQUEST_GAP", gap), \
                patch.object(bili, "MAX_INFLIGHT", 4), \
                patch.object(bili, "_last_request", 0), \
                patch.object(bili, "_request_events", deque(maxlen=20000)), \
                patch.object(bili, "_request_results", deque(maxlen=20000)), \
                patch.object(bili.db, "kv_get", side_effect=lambda key, default=None: values.get(key, default)), \
                patch.object(bili.db, "kv_set", side_effect=lambda key, value: values.__setitem__(key, value)), \
                patch.object(config, "PROXY", ""), \
                patch("urllib.request.getproxies", return_value={}), \
                patch.object(bili, "cookie") as cookie:
            threads = [threading.Thread(target=run, args=("detail", range(index, 51, 4)))
                       for index in range(1, 5)]
            threads.extend(threading.Thread(target=run, args=("latest", range(20))) for _ in range(2))
            threads.append(threading.Thread(target=run, args=("directory", range(10))))
            started = time.monotonic()
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(120)
            elapsed = time.monotonic() - started
            if any(thread.is_alive() for thread in threads):
                raise AssertionError("Capacity fixture worker did not finish")
            cookie.assert_not_called()
            return {"target": "localhost_fixture", "requests": len(arrivals),
                    "details": len(details), "lists": sum(r["path"].endswith("/newlist") for r in arrivals),
                    "directories": sum(r["path"].endswith("/newlist_rank") for r in arrivals),
                    "complete_tags_and_stats": sum(
                        r["tags"] == ["Example", "AllTags"] and
                        all(r[k] is not None for k in ("views", "likes", "coins", "favorites", "shares", "replies"))
                        for r in details),
                    "seconds": round(elapsed, 3), "max_concurrent_http": peak[0],
                    "gap_seconds": gap, "fixture_wire_seconds": latency,
                    "requests_per_minute": round(len(arrivals) * 60 / elapsed, 2),
                    "details_per_minute": round(len(details) * 60 / elapsed, 2),
                    "cookies_sent": sum(bool(r["cookie"]) for r in arrivals),
                    "errors": errors, "network": bili.request_metrics()}
    finally:
        server.shutdown()
        server.server_close()
        serving.join(5)


class PipelineTests(unittest.TestCase):
    def test_capacity_includes_forty_list_requests_as_well_as_full_details(self):
        result = capacity_fixture()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["requests"], 100)
        self.assertEqual(result["details"], 50)
        self.assertEqual(result["complete_tags_and_stats"], 50)
        self.assertEqual(result["lists"], 40)
        self.assertEqual(result["directories"], 10)
        self.assertGreater(result["max_concurrent_http"], 1)
        self.assertLessEqual(result["max_concurrent_http"], 4)
        self.assertEqual(result["cookies_sent"], 0)

    def test_http_polling_details_daily_retry_and_restart_share_saved_progress(self):
        now = int(time.time())
        yesterday = (datetime.now(bili.BEIJING).date() - timedelta(days=1)).isoformat()
        requests, heads = [], [0]

        def raw(number, day=None):
            published = (int(datetime.fromisoformat(day + "T23:59:00").replace(
                tzinfo=bili.BEIJING).timestamp()) if day else now - 12 + number)
            return {"bvid": bv(number), "pubdate": published, "title": "[Example]",
                    "owner": {"mid": number % 2 + 1, "name": "Author"},
                    "tid": 21 if day else 17, "tid_v2": 2079,
                    "stat": dict(view=100, like=10, coin=5, favorite=4, share=3, reply=2)}

        class Fixture(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                params = parse_qs(parsed.query)
                requests.append({"path": parsed.path, "params": params,
                                 "cookie": self.headers.get("Cookie"), "time": time.time()})
                code, message = 0, "OK"
                if parsed.path.endswith("/newlist"):
                    heads[0] += 1
                    top = 12 if heads[0] == 1 else 13
                    data = {"archives": [raw(n) for n in range(top, top - 12, -1)]}
                elif parsed.path.endswith("/newlist_rank"):
                    data = {"numResults": 2, "numPages": 1, "result": [
                        raw(101, yesterday), raw(102, yesterday)]}
                elif parsed.path.endswith("/view/detail"):
                    number = int(params["bvid"][0][3:])
                    if number == 1:
                        code, message, data = 62012, "62012", {}
                    else:
                        data = {"View": raw(number, yesterday if number > 100 else None),
                                "Tags": [{"tag_name": "Example"}, {"tag_name": "AllTags"}]}
                else:
                    raise AssertionError("Unexpected endpoint: " + parsed.path)
                body = json.dumps({"code": code, "message": message, "data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        original_transport = bili._anonymous_json

        def transport(url, params, headers, timeout):
            self.assertEqual(urlsplit(url).hostname, "api.bilibili.com")
            return original_transport(
                f"http://127.0.0.1:{server.server_port}{urlsplit(url).path}",
                params, headers, timeout)

        try:
            with tempfile.TemporaryDirectory() as directory, \
                    patch.multiple(config, DATA_DIR=directory,
                        DB_PATH=str(Path(directory) / "pipeline.db"),
                        PROXY="",
                        BVIDEO_STREAM_ENABLED=True, BVIDEO_LATEST_SECONDS=3,
                        BVIDEO_DAILY_TIDS=[21]), \
                    patch.object(config, "source_on", return_value=True), \
                    patch.object(bili, "REQUEST_GAP", 0.01), \
                    patch.object(bili, "_anonymous_local", threading.local()), \
                    patch("urllib.request.getproxies", return_value={}), \
                    patch.object(bili, "_anonymous_json", side_effect=transport), \
                    patch.object(bili, "cookie") as cookie, \
                    patch.object(stream, "_threads", {}), \
                    patch.object(stream, "_started", False), \
                    patch("radar.activity.say"):
                db.init()
                daily_result, failures = [], []

                def run_daily():
                    try:
                        daily_result.append(daily.run_once(yesterday, 10, 10))
                    except Exception as exc:
                        failures.append(exc)
                    finally:
                        bili.close_anonymous_session()

                stop = threading.Event()
                stream.start(stop)
                threads = list(stream._threads.values())
                worker = threading.Thread(target=run_daily)
                worker.start()
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        result = stream.summary()
                        if heads[0] >= 2 and result["tagged"] == 14 and not worker.is_alive():
                            break
                        time.sleep(0.02)
                finally:
                    stop.set()
                    for thread in threads:
                        thread.join(15)
                    worker.join(15)
                self.assertFalse(any(t.is_alive() for t in threads + [worker]))
                self.assertEqual(failures, [])
                # Shared workers can own the final daily BV while this slice yields.
                self.assertIn(daily_result[0]["status"], ("complete", "waiting_details"))
                with db.session() as c:
                    c.execute("UPDATE bd_days SET retry_after=0 WHERE day=?", (yesterday,))
                refreshed = daily.run_once(yesterday, 10, 10)
                self.assertEqual(refreshed["status"], "complete", refreshed)
                self.assertEqual(refreshed["tagged"], 2)
                result = stream.summary()
                self.assertEqual(result["videos"], 15)
                self.assertEqual(result["tagged"], 14)
                self.assertEqual(result["realtime"]["videos"], 13)
                self.assertEqual(result["realtime"]["tagged"], 12)
                self.assertEqual(result["pending_tags"], 1)
                self.assertEqual(result["unavailable"], 0)
                self.assertEqual(result["gaps"], 0)
                self.assertEqual(result["latest"]["continuity"], "overlap")
                cookie.assert_not_called()
                self.assertFalse(any(r["cookie"] for r in requests))
                self.assertEqual(sum(r["params"].get("bvid") == [bv(1)] for r in requests), 1)
                self.assertEqual({r["params"]["rid"][0] for r in requests
                                  if r["path"].endswith("/newlist")}, {"4"})
                with db.session() as c:
                    saved = c.execute("SELECT * FROM bd_videos WHERE bvid=?", (bv(2),)).fetchone()
                    self.assertEqual(json.loads(saved["tags"]), ["Example", "AllTags"])
                    self.assertEqual((saved["likes"], saved["coins"], saved["shares"]), (10, 5, 3))
                before = len(requests)
                stream._ready.discard(config.DB_PATH)
                stream.init()
                self.assertFalse(stream.enrich_once())
                self.assertEqual(len(requests), before)
                bili.close_anonymous_session()
        finally:
            server.shutdown()
            server.server_close()
            serving.join(5)


if __name__ == "__main__":
    unittest.main()
