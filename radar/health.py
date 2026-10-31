"""Operational failures are independent of ranking and evaluated on every poll."""
import importlib
import threading
import time

from . import config, db

_started = 0
_startup_errors = {}
_worker_lock = threading.Lock()


def worker_result(name, error=""):
    with _worker_lock:
        state = db.kv_get("bili_worker_health", {}) or {}
        previous = state.get(name) or {}
        if (not error and not previous.get("error") and
                time.time() - previous.get("ts", 0) < 5):
            return
        state[name] = {"ts": time.time(), "error": str(error)[:400]}
        db.kv_set("bili_worker_health", state)


WORKERS = {
    "bvideo": ("B站最新投稿", "bili-stream-latest", 90),
    "bvideo_tags": ("B站标签补齐", "bili-stream-tags", None),
    "bvideo_storage": ("B站视频入库", "bili-stream-inbox", None),
    "bvideo_analysis": ("B站本地分析", "bili-stream-analysis", None),
    "youtube": ("YouTube热榜及频道", "youtube-sweeper", 5400),
    "youtube_search": ("YouTube时间窗", "youtube-game-videos", None),
}


def start_collectors(stop_event=None):
    global _started
    _started = time.time()
    for name in ("bili_latest", "bili_growth", "youtube", "ytfeed"):
        try:
            importlib.import_module("." + name, __package__).start(stop_event)
            _startup_errors.pop(name, None)
        except Exception as exc:
            _startup_errors[name] = str(exc)[:400]
            from . import activity
            activity.say("紧急：%s 启动失败：%s" % (name, exc))


def snapshot(now=None):
    now = time.time() if now is None else now
    alerts = []

    def add(key, title, detail, ts=None):
        alerts.append({"id": key, "severity": "critical", "title": title,
                       "detail": detail, "ts": ts or now})

    for name, error in _startup_errors.items():
        add("startup:" + name, name + " 启动失败", error)
    live = {t.name for t in threading.enumerate() if t.is_alive()}
    latest = db.kv_get("bili_latest_state", {}) or {}
    status = db.kv_get("source_status", {}) or {}
    for key, (name, thread, limit) in WORKERS.items():
        if thread not in live:
            add("worker:" + key, name + "未运行", "采集线程未启动或已经退出；历史成功记录不代表正在运行。")
            continue
        if key == "bvideo":
            success = latest.get("received") or 0
            if latest.get("status") in ("error", "paused", "gap", "stale"):
                add("latest", name + "异常", latest.get("error") or
                    "实时列表发生断档，不能保证采集连续。", latest.get("last_attempt"))
        elif key == "youtube":
            success = (status.get("youtube") or {}).get("ok_ts", 0)
        else:
            continue
        if limit and now - (success or _started) > limit:
            add("stale:" + key, name + "超时未成功",
                "最近成功：" + (time.strftime("%m-%d %H:%M:%S", time.localtime(success))
                                if success else "尚未成功") + "；预期最长间隔 %s 秒。" % limit, success)
    for source, value in status.items():
        if value.get("error") and value.get("err_ts", 0) >= value.get("ok_ts", 0):
            add("source:" + source, source + "采集失败", value["error"], value.get("err_ts"))
        if value.get("warn") and not value["warn"].startswith("本轮新增"):
            add("partial:" + source, source + "部分采集异常", value["warn"], value.get("ok_ts"))
    from .collect import source_status
    for source, value in source_status().items():
        if value.get("stale") and source not in ("bvideo", "youtube"):
            add("stale_source:" + source, source + "超时未成功", "最近成功已超过预期采集间隔。",
                value.get("ok_ts"))
    denial = db.kv_get("bili_realtime_denial", {}) or {}
    if denial and (not denial.get("retry_at") or denial["retry_at"] > now):
        add("bili_denial", "B站实时接口被限制", denial["error"], denial.get("ts"))
    youtube_error = db.kv_get("youtube_last_error")
    if youtube_error:
        add("youtube_window_error", "YouTube时间窗采集失败", str(youtube_error))
    growth_error = db.kv_get("bili_growth_worker", {}) or {}
    if growth_error.get("error"):
        add("bili_growth_error", "B站增长采样失败", growth_error["error"],
            growth_error.get("last_attempt"))
    for worker, value in (db.kv_get("bili_worker_health", {}) or {}).items():
        if value.get("error"):
            add("pipeline:" + worker, "B站" + worker + "失败", value["error"], value.get("ts"))
    progress = db.kv_get("youtube_feed_progress", {}) or {}
    if progress.get("error"):
        add("youtube_feed_error", "YouTube采集部分失败", progress["error"], progress.get("ts"))
    if progress.get("running") and now - progress.get("ts", _started) > 180:
        add("youtube_feed_stalled", "YouTube采集进度停滞", "正在采集，但超过180秒没有取得进展。")
    if latest.get("received"):
        with db.session() as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='bs_gaps'").fetchone():
                gaps = c.execute("SELECT COUNT(*) FROM bs_gaps WHERE resolved=0").fetchone()[0]
                if gaps:
                    add("bili_gaps", "B站实时采集存在断档", "%s 处未解决断档，不能视为完整覆盖。" % gaps)
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if {"bs_pending", "bs_events"} <= tables:
                pending = c.execute("SELECT MIN(queued) FROM bs_pending").fetchone()[0]
                completed = c.execute("""SELECT SUM(n) FROM bs_events
                    WHERE kind IN ('tag_completed','tag_unavailable') AND ts>=?""",
                                      (max(_started, now - 120),)).fetchone()[0]
                if pending and now - max(pending, _started) > 120 and not completed:
                    add("bili_tags_stalled", "B站标签补齐停滞",
                        "有待补任务，但120秒内没有完成补齐。")
                if pending and now - pending > 300:
                    add("bili_tags_backlog", "B站标签补齐积压",
                        "存在等待超过5分钟的标签任务，发现视频不等于完成分析。")
            if "bq_analysis" in tables:
                failed = c.execute("""SELECT error FROM bq_analysis
                    WHERE attempts>0 ORDER BY queued LIMIT 1""").fetchone()
                if failed:
                    add("bili_analysis_job_error", "B站视频分析任务失败", failed[0])
    if config.STEAM_ONLY:
        add("paused_platforms", "其他平台新增采集仍暂停",
            "当前为 Steam 优先模式；B站实时视频和YouTube独立运行，其他平台只展示历史数据。")
    return {"ts": now, "ok": not alerts, "alerts": alerts,
            "workers": {k: t in live for k, (_, t, _) in WORKERS.items()}}
