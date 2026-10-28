"""Focused regression and read-only checks against the running local dashboard."""
import io
import json
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8765" + path, timeout=60) as response:
        return json.load(response)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    output = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        "test_bili_growth", "test_bili_latest", "test_bili_pipeline", "test_web_routes",
    ])
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    dashboard = get("/api/bili-growth")
    terms = get("/api/bili-terms?page_size=1")
    assert dashboard["revision"] == "2026-10-27-tag-growth-v1"
    assert dashboard["selected_count"] <= dashboard["candidate_limit"]
    assert all(g["index"] is None for g in dashboard["groups"]
               if g["state"] == "baseline_pending")
    try:
        get("/api/bili-growth?day=not-a-date")
        raise AssertionError("Invalid date was accepted by the running server")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    report = {
        "checked_at": time.time(), "focused_tests": result.testsRun,
        "focused_failures": len(result.failures), "focused_errors": len(result.errors),
        "runtime_revision": dashboard["revision"], "day": dashboard["day"],
        "selected_tags": dashboard["selected_count"], "indexed_raw_tags": dashboard["tag_count"],
        "ready_groups": dashboard["coverage"]["ready_groups"],
        "pending_groups": dashboard["coverage"]["baseline_pending_groups"],
        "deferred_connections": dashboard["deferred_edges"],
        "keyword_catalogue_saved": terms["saved_total"],
        "sampling": dashboard["sampling"],
        "browser_visual_check": "Not completed: the existing CDP browser timed out during Playwright attachment.",
        "full_suite_attempt": {
            "tests": 318, "errors": 17, "failures": 1,
            "notes": [
                "Unrelated component update tests reference missing config.UPDATE_SCOPE.",
                "Legacy social tests import removed douyin/incremental/growth/schedule modules.",
                "Existing daily migration test expects in_progress while the summary exposes the effective risk pause.",
                "No unrelated modules or pause-status behavior were restored or changed to satisfy these tests.",
            ],
        },
    }
    (ROOT / "output/bili-growth-regression.log").write_text(output.getvalue(), encoding="utf-8")
    (ROOT / "output/bili-growth-verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    main()
