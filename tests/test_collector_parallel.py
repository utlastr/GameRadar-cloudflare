import threading
import unittest
from unittest.mock import patch

from radar import collect
from radar.social import extract, jobs


class ParallelTests(unittest.TestCase):
    def test_ten_platforms(self):
        barrier = threading.Barrier(10, timeout=5)
        sources = [str(i) for i in range(10)]
        with patch.object(collect, "_run", side_effect=lambda *args: barrier.wait()):
            collect._parallel(sources, dict.fromkeys(sources), 0)

    def test_same_platform_drains_in_order(self):
        observed = []
        with patch.object(collect, "_run", side_effect=lambda source, *args: observed.append(source)):
            collect._parallel(["appcn", "appus"], dict.fromkeys(["appcn", "appus"]), 0)
        self.assertEqual(observed, ["appcn", "appus"])
        self.assertIs(collect.platform_lock("tapmaker"), collect.platform_lock("taptap"))

    def test_runtime_timing_is_not_hot_ranking(self):
        self.assertTrue(extract.junk_text("middleware_injectCtx_before"))
        found = {"lists": [{"src": "parse", "path": "$", "items": [
            {"text": "runtime_%d" % i, "nums": {"duration": 1000}} for i in range(8)]}]}
        self.assertEqual(jobs._short_lists(found, 4), [])

    def test_search_does_not_open_browser(self):
        with patch.object(jobs.page, "open_capture") as capture, patch.object(jobs.store, "status"):
            self.assertFalse(jobs.run_kw("douyin_kw", "anything"))
            capture.assert_not_called()
