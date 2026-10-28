import gzip
import io
import unittest
from unittest.mock import patch

from radar import config, server, video


class DashboardLoadingTests(unittest.TestCase):
    def test_live_summary_skips_unused_legacy_video_queries(self):
        with patch.multiple(config, BVIDEO_STREAM_ENABLED=True, BVIDEO_DAILY_ENABLED=True), \
                patch.object(video, "init") as init, \
                patch.object(video.db, "session", side_effect=AssertionError("legacy query")), \
                patch.object(video.db, "kv_get", return_value={}), \
                patch("radar.bili_stream.summary", return_value={"enabled": True}), \
                patch("radar.bili_daily.summary", return_value={"day": "2026-10-27"}):
            result = video.summary(include_legacy=False)
        init.assert_not_called()
        self.assertEqual(result["rows"], [])
        self.assertTrue(result["stream"]["enabled"])
        self.assertEqual(result["daily"]["day"], "2026-10-27")

    def test_large_json_supports_browser_gzip_without_changing_content(self):
        body = '{"value":"' + "example" * 2000 + '"}'

        class Response:
            headers = {"Accept-Encoding": "gzip, deflate, br"}
            wfile = io.BytesIO()
            sent = {}

            def send_response(self, code):
                self.code = code

            def send_header(self, name, value):
                self.sent[name] = value

            def end_headers(self):
                pass

        response = Response()
        server.Handler._send(response, body)
        encoded = response.wfile.getvalue()
        self.assertEqual(response.sent["Content-Encoding"], "gzip")
        self.assertEqual(gzip.decompress(encoded).decode(), body)
        self.assertLess(len(encoded), len(body) // 10)
        self.assertEqual(int(response.sent["Content-Length"]), len(encoded))


if __name__ == "__main__":
    unittest.main()
