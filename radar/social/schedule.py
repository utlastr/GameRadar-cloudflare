"""Persistent half-day batches in UTC+8, with restart catch-up and bounded retries."""
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import db
from . import funnel, jobs, settings

HALF_DAY = 12 * 3600
OFFSET = 8 * 3600
KEY = "social_halfday_v1"
_lock = threading.Lock()


def platform_key(platform):
    return {"douyin_index": "douyin", "bili_index": "bilibili"}.get(platform, platform)


def latest_boundary(now):
    return (int(now) + OFFSET) // HALF_DAY * HALF_DAY - OFFSET


def state(now=None):
    now = int(time.time() if now is None else now)
    saved = db.kv_get(KEY, {}) or {}
    return {**saved, "timezone": "Asia/Shanghai", "hours": [0, 12],
            "next_end": latest_boundary(now) + HALF_DAY}


def _tasks():
    out = [{"job": "bilibili_newlist", "platform": "bilibili"}]
    if settings.platform_on("douyin"):
        out.append({"job": "douyin_game_feed", "platform": "douyin"})
    for job, platform, url, limit in jobs.HOT_JOBS:
        if settings.platform_on(platform):
            out.append({"job": job, "platform": platform, "url": url, "limit": limit})
    return out


def tick(now=None):
    """One coordinator owns persistence; workers never rewrite batch snapshots."""
    if not _lock.acquire(blocking=False):
        return "采集执行中"
    try:
        return _tick(now)
    finally:
        _lock.release()


def _execute(task, window):
    from ..collect import platform_lock
    source = {"bilibili": "bili"}.get(platform_key(task["platform"]), platform_key(task["platform"]))
    with platform_lock(source):
        return _execute_locked(task, window)


def _execute_locked(task, window):
    if task["job"] == "douyin_game_feed":
        from . import douyin
        return "sampled" if douyin.run(window) else "retry"
    if task["job"] == "bilibili_newlist":
        from ..opp import bili_track
        result = bili_track.discover_once(window=window)
        progress = db.kv_get("publication_bili_discovery", {}) or {}
        if result is None and progress.get("continuation"):
            return "pending"
        return "sampled" if result is not None else "retry"
    return "sampled" if jobs.run_hot(task["job"], task["platform"], task["url"], task["limit"]) else "retry"


def _tick(now=None):
    now = int(time.time() if now is None else now)
    saved = db.kv_get(KEY, {}) or {}
    boundary = latest_boundary(now)
    active = saved.get("active")
    if not active:
        # First installation starts with the most recently closed half-day.
        end = int(saved.get("last_end", boundary - HALF_DAY)) + HALF_DAY
        if end > boundary:
            return ""
        active = {"start": end - HALF_DAY, "end": end, "created": now, "tasks": _tasks()}
        saved["active"] = active
        db.kv_set(KEY, saved)
    selected, platforms = [], set()
    workers = max(1, min(20, int(settings.get("SOCIAL_MAX_WORKERS") or 10)))
    for task in active["tasks"]:
        # Cancel persisted searches too, including batches made by older versions.
        if "term" in task or task["job"] in {j for j, _, _ in jobs.KW_JOBS}:
            task.update(state="cancelled", error="已禁止主动关键词搜索")
        if task.get("state") in ("sampled", "failed", "cancelled"):
            continue
        platform = platform_key(task["platform"])
        if platform in platforms or len(selected) >= workers:
            continue
        if now < task.get("retry_after", 0):
            continue
        # Persist the attempt before network I/O. A crash retries at most twice.
        attempts = int(task.get("attempts", 0))
        if attempts >= 3:
            task.update(state="failed", error="重试次数已用完")
            db.kv_set(KEY, saved)
            continue
        task.update(attempts=attempts + 1, state="running", retry_after=now + 15 * 60)
        selected.append(task)
        platforms.add(platform)
    db.kv_set(KEY, saved)
    if selected:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="social-platform") as pool:
            futures = {pool.submit(_execute, task, (active["start"], active["end"])): task for task in selected}
            for future in as_completed(futures):
                task, error = futures[future], ""
                try:
                    outcome = future.result()
                except Exception as exc:
                    outcome, error = "retry", str(exc)[:300]
                if outcome == "pending":
                    task.update(state="pending", attempts=task["attempts"] - 1, retry_after=0, error="")
                else:
                    ok = outcome == "sampled"
                    task.update(state="sampled" if ok else ("failed" if task["attempts"] >= 3 else "retry"),
                                finished=int(time.time()), error=error or ("" if ok else "采集失败，见平台状态"),
                                coverage="sample_only" if ok else "unavailable")
                db.kv_set(KEY, saved)
        return "并发采集 %d 个平台" % len(selected)
    if any(t.get("state") not in ("sampled", "failed", "cancelled") for t in active["tasks"]):
        return "等待失败任务重试"
    active["finished"] = now
    active["coverage"] = "partial"  # Current adapters do not establish exhaustive coverage.
    saved["last_batch"] = active
    saved["last_end"] = active["end"]
    saved.pop("active", None)
    db.kv_set(KEY, saved)
    return "半日轮次结束（覆盖不全）"
