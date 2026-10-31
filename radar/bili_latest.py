"""Overlap-checked rolling head, durable per-child queues, and explicit gaps."""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

from . import activity, bili_queue, bili_stream as stream, bili_terms, config, db
from . import bili_realtime_client as bili

SOURCE = "legacy_game_latest_web"
REVISION = bili_queue.REVISION
SCHEMA = """
CREATE TABLE IF NOT EXISTS bs_latest_seen(
 bvid TEXT PRIMARY KEY,tid INTEGER,published REAL,first_seen REAL);
CREATE INDEX IF NOT EXISTS bs_latest_seen_time ON bs_latest_seen(first_seen);
CREATE TABLE IF NOT EXISTS bs_inbox(
 bvid TEXT PRIMARY KEY,tid INTEGER,record TEXT,queued REAL);
CREATE INDEX IF NOT EXISTS bs_inbox_tid ON bs_inbox(tid,queued);
CREATE TABLE IF NOT EXISTS bs_gaps(
 id TEXT PRIMARY KEY,detected REAL,previous_latest REAL,current_earliest REAL,
 reason TEXT,resolved INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS bs_events(ts REAL,kind TEXT,n INTEGER);
CREATE INDEX IF NOT EXISTS bs_events_time ON bs_events(ts);
"""
_ready = set()
_init_lock = threading.Lock()
_poll_lock = threading.Lock()
_runtime_started = 0


def init():
    stream.init()
    with _init_lock:
        if config.DB_PATH not in _ready:
            with db.session() as c:
                c.executescript(SCHEMA)
            _ready.add(config.DB_PATH)


def state():
    return db.kv_get("bili_latest_state", {}) or {}


def _write_state(c, value):
    c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('bili_latest_state',?)",
              (json.dumps(value, ensure_ascii=False),))


def _interval(window):
    target = max(3.0, float(config.BVIDEO_LATEST_SECONDS))
    return round(max(3.0, min(target, max(1, window) * 0.4)), 2)


def _claim(owner):
    now = time.time()
    with db.session() as c:
        c.execute("""INSERT INTO bd_lease VALUES('stream_latest',?,?)
            ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires=excluded.expires
            WHERE bd_lease.expires<?""", (owner, now + 90, now))
        return c.execute("SELECT owner FROM bd_lease WHERE name='stream_latest'").fetchone()[0] == owner


def poll_once(force=False, stop_event=None):
    init()
    if not config.BVIDEO_STREAM_ENABLED or not config.source_on("bvideo"):
        return {"status": "disabled"}
    if not _poll_lock.acquire(blocking=False):
        return {"busy": True}
    owner, claimed = uuid.uuid4().hex, False
    try:
        previous = state()
        if not force and time.time() < float(previous.get("next_poll") or 0):
            return dict(previous, skipped=True)
        if stop_event and stop_event.is_set():
            return {"status": "interrupted"}
        claimed = _claim(owner)
        if not claimed:
            return {"busy": True}
        started = time.time()
        result = bili.latest_game_posts(config.BVIDEO_STREAM_PAGE_SIZE)
        received = time.time()
        rows = result["list"]
        old_ids, ids = set(previous.get("bvids", [])), {r["bvid"] for r in rows}
        overlap = len(old_ids & ids)
        if old_ids and result["latest"] < float(previous["latest"]):
            raise RuntimeError("Latest publication watermark moved backwards; checkpoint retained")
        gap = bool(old_ids and not overlap)
        counts = {str(tid): 0 for tid in bili.LEGACY_GAME_SECTIONS}
        child_latest = dict(previous.get("child_latest", {}))
        window = result["latest"] - result["earliest"]
        interval = _interval(window)
        current = {
            "source": SOURCE, "status": "gap" if gap else "latest",
            "started": started, "received": received, "seconds": round(received - started, 3),
            "earliest": result["earliest"], "latest": result["latest"],
            "window_seconds": window, "returned": len(rows), "overlap": overlap,
            "continuity": "bootstrap" if not old_ids else "gap" if gap else "overlap",
            "last_poll_seconds": round(received - float(previous["received"]), 3)
            if previous.get("received") else None,
            "tracking_started": previous.get("tracking_started", received),
            "interval_seconds": interval, "next_poll": max(started + interval, received + 0.5),
            "bvids": [r["bvid"] for r in rows], "pid": os.getpid(), "error": "",
        }
        # The queue and head checkpoint commit together; a crash cannot skip unsaved jobs.
        with db.session() as c:
            cutoff = bili_queue.collection_policy(c).get("started", 0)
            for row in reversed(rows):
                if row["published"] < cutoff:
                    continue
                tid = str(row["tid"])
                child_latest[tid] = max(child_latest.get(tid, 0), row["published"])
                added = c.execute("INSERT OR IGNORE INTO bs_latest_seen VALUES(?,?,?,?)",
                                  (row["bvid"], row["tid"], row["published"], received)).rowcount
                if added:
                    c.execute("INSERT OR IGNORE INTO bs_inbox VALUES(?,?,?,?)",
                              (row["bvid"], row["tid"], json.dumps(row, ensure_ascii=False), received))
                    counts[tid] += 1
            if gap:
                c.execute("INSERT INTO bs_gaps VALUES(?,?,?,?,?,0)",
                          (uuid.uuid4().hex, received, previous["latest"], result["earliest"],
                           "No BV overlap with the previous saved head"))
            current.update(new=sum(counts.values()), per_child_new=counts, child_latest=child_latest)
            _write_state(c, current)
            c.execute("INSERT INTO bs_events VALUES(?,'discovered',?)", (received, current["new"]))
            c.execute("DELETE FROM bs_events WHERE ts<?", (received - 86400,))
        from .collect import _status
        _status("bvideo", True, len(rows), warn="实时列表出现断档" if gap else "")
        return current
    except Exception as exc:
        failed = state()
        now = time.time()
        pause = max(bili.pause_left(), bili.pause_left(bili.LATEST_GAME_SCOPE))
        failed.update(source=SOURCE, status="paused" if isinstance(exc, bili.RiskError) else "error",
                      error=str(exc)[:500], last_attempt=now,
                      next_poll=now + max(10, pause), pid=os.getpid())
        with db.session() as c:
            _write_state(c, failed)
        from .collect import _status
        _status("bvideo", False, err=failed["error"])
        return failed
    finally:
        try:
            if claimed:
                with db.session() as c:
                    c.execute("DELETE FROM bd_lease WHERE name='stream_latest' AND owner=?", (owner,))
        finally:
            _poll_lock.release()


def drain_partition(tid, limit=100):
    """Continue the saved FIFO inbox; no Bilibili request is issued here."""
    init()
    tid = int(tid)
    if tid not in bili.LEGACY_GAME_SECTIONS:
        raise ValueError("Unknown legacy game child")
    lock = stream._partition_locks[tid]
    if not lock.acquire(blocking=False):
        return 0
    try:
        with db.session() as c:
            pending = c.execute("""SELECT * FROM bs_inbox WHERE tid=?
                ORDER BY queued,rowid LIMIT ?""", (tid, max(1, min(100, int(limit))))).fetchall()
        if not pending:
            return 0
        records = [json.loads(row["record"]) for row in pending]
        latest = datetime.fromtimestamp(max(r["published"] for r in records), bili.BEIJING).isoformat()
        stream._save_sample(tid, {"list": records, "latest": latest})
        with db.session() as c:
            c.executemany("DELETE FROM bs_inbox WHERE bvid=?", [(row["bvid"],) for row in pending])
        return len(records)
    finally:
        lock.release()


def summary_fields(base):
    init()
    current, now = state(), time.time()
    with db.session() as c:
        queued = {r["tid"]: r["n"] for r in c.execute(
            "SELECT tid,COUNT(*) n FROM bs_inbox GROUP BY tid")}
        cutoff = bili_queue.collection_policy(c).get("started", 0)
        gaps = c.execute("SELECT COUNT(*) FROM bs_gaps WHERE resolved=0 AND detected>=?",
                         (cutoff,)).fetchone()[0]
        minute = {r["kind"]: r["n"] for r in c.execute("""SELECT kind,SUM(n) n FROM bs_events
            WHERE ts>=? GROUP BY kind""", (max(now - 60, cutoff),))}
        since = max(now - 600, _runtime_started, current.get("tracking_started", now))
        ten_minutes = {r["kind"]: r["n"] for r in c.execute("""SELECT kind,SUM(n) n FROM bs_events
            WHERE ts>=? GROUP BY kind""", (since,))}
        oldest = base["queue"]["oldest_pending_day"]
        seen = c.execute("SELECT COUNT(*) FROM bs_latest_seen").fetchone()[0]
    for part in base["partitions"]:
        tid = str(part["tid"])
        published = current.get("child_latest", {}).get(tid)
        part.update(rows=current.get("per_child_new", {}).get(tid, 0),
                    latest=datetime.fromtimestamp(published, bili.BEIJING).isoformat()
                    if published else None,
                    last_success=current.get("received", 0),
                    pending_list=queued.get(part["tid"], 0), status="saved" if published else "waiting",
                    network_worker=False, worker_started=False, polls=None,
                    last_attempt=None, last_seconds=None, runtime_pid=None)
    pause = max(bili.pause_left(), bili.pause_left(bili.LATEST_GAME_SCOPE))
    tag_pause = bili.pause_left("视频详情及标签")
    latest = current.get("latest")
    status = ("disabled" if not base["enabled"] else
              "paused" if pause else current.get("status", "pending"))
    if latest and now - latest > 180 and status not in ("disabled", "paused", "error"):
        status = "stale"
    elapsed = max(1, now - since)
    arrivals = ten_minutes.get("discovered", 0) * 60 / elapsed
    enqueued_rate = ten_minutes.get("tag_queued", 0) * 60 / elapsed
    completed = ten_minutes.get("tag_completed", 0) * 60 / elapsed
    unavailable = ten_minutes.get("tag_unavailable", 0) * 60 / elapsed
    delta = (ten_minutes.get("tag_queued", 0) - ten_minutes.get("tag_completed", 0)
             - ten_minutes.get("tag_unavailable", 0))
    growth = delta * 60 / elapsed
    throughput = {
        "window_seconds": round(elapsed), "discovered_per_minute": round(arrivals, 2),
        "runtime_started": _runtime_started or None,
        "queued_tags_per_minute": round(enqueued_rate, 2),
        "tagged_per_minute": round(completed, 2), "oldest_pending_day": oldest,
        "status": "paused" if tag_pause else "empty" if not base["pending_tags"] else "warming_up" if elapsed < 60 else
                  "growing" if delta > 0 else "draining" if delta < 0 else
                  "balanced" if completed + unavailable > 0 else "stalled",
        "net_growth_per_minute": round(growth, 2),
        # A short quiet window cannot predict the next daytime peak or deferred retries.
        "catch_up_seconds": None,
        "scope": "all_saved_videos",
        "observed_drain_seconds": round(base["pending_tags"] * 60 / -growth)
            if not tag_pause and elapsed >= 60 and growth < 0 else None,
    }
    base.update(
        source=SOURCE, mode="latest_window", status=status, round={},
        collector_revision=REVISION, throughput=throughput, network=bili.request_metrics(),
        full_coverage=False, independent_partition_fetching=False,
        partition_workers_running=0,
        storage_worker_running=bool(base["workers"].get("bili-stream-inbox")),
        interval_seconds=current.get("interval_seconds", config.BVIDEO_LATEST_SECONDS),
        next_poll=current.get("next_poll", 0), pause_seconds=pause, latest=current,
        latest_age_seconds=max(0, int(now - latest)) if latest else None,
        list_worker_running=bool(base["workers"].get("bili-stream-latest")),
        pending_list=sum(queued.values()), source_seen=seen,
        gaps=gaps, new_last_minute=minute.get("discovered", 0),
        tags_last_minute=minute.get("tag_completed", 0),
        pause=bili.pause_info(bili.LATEST_GAME_SCOPE),
        tag_pause_seconds=tag_pause,
        tag_priority={"recent_seconds": bili_queue.RECENT_SECONDS,
                      "recent_share": 0.9 if base["queue"]["recent_oldest_seconds"] > 120 else 0.8,
                      "backlog_share": 0.1 if base["queue"]["recent_oldest_seconds"] > 120 else 0.2,
                      "borrow_unused": True},
        analysis_worker_running=bool(base["workers"].get("bili-stream-analysis")),
        worker_states={
            "list": "stopped" if not base["workers"].get("bili-stream-latest") else
                    "disabled" if not base["enabled"] else "paused" if pause else "polling",
            "storage": "stopped" if not base["workers"].get("bili-stream-inbox") else
                       "disabled" if not base["enabled"] else "draining" if sum(queued.values()) else "idle",
            "tags": "stopped" if not base["tag_worker_running"] else
                    "disabled" if not base["enabled"] else "paused" if tag_pause else
                    "enriching" if base["pending_tags"] else "idle",
        },
        coverage="Rolling legacy game-parent head, overlap checked; no historical cursor or census",
    )
    return base


def log_health():
    result, now = stream.summary(), time.time()
    health = result["throughput"]["status"]
    signature = (result["status"], health, bool(result["gaps"]))
    previous = db.kv_get("bili_stream_health_log", {}) or {}
    if previous.get("signature") == list(signature) and now - previous.get("ts", 0) < 60:
        return
    labels = {"empty": "无待补任务", "warming_up": "统计采样中", "growing": "积压增长",
              "draining": "积压减少", "balanced": "积压持平", "stalled": "补齐未推进",
              "paused": "风控暂停，未发起请求"}
    activity.say(f"B站采集：近60秒发现 {result['new_last_minute']} 条，"
                 f"补齐Tag及统计 {result['tags_last_minute']} 条；"
                 f"待补 {result['pending_tags']:,} 条，{labels[health]}；"
                 f"未衔接记录 {result['gaps']} 处")
    db.kv_set("bili_stream_health_log", {"signature": list(signature), "ts": now})


def register_gap_days():
    """Closed days with observed gaps join the existing durable daily backfill."""
    if not config.BVIDEO_DAILY_ENABLED:
        return []
    from . import bili_daily
    init()
    with db.session() as c:
        if bili_queue.collection_policy(c).get("mode") == "realtime_only":
            return []
    today = datetime.now(bili.BEIJING).date()
    with db.session() as c:
        gaps = c.execute("""SELECT previous_latest,current_earliest FROM bs_gaps
            WHERE resolved=0""").fetchall()
    days = set()
    for gap in gaps:
        first = datetime.fromtimestamp(gap["previous_latest"], bili.BEIJING).date()
        last = datetime.fromtimestamp(gap["current_earliest"], bili.BEIJING).date()
        while first <= last and first < today:
            days.add(first.isoformat())
            first += timedelta(days=1)
    for day in sorted(days):
        bili_daily.ensure_day(day)
    return sorted(days)


def start(stop_event=None):
    global _runtime_started
    from .health import worker_result
    init()
    with stream._lock:
        if stream._started:
            return
        stream._started = True
        stream._threads.clear()
        _runtime_started = time.time()
    stop = stop_event or threading.Event()
    db.kv_set("bili_stream_mode", "latest_window")
    bili.preserve_cooldown_budget()
    register_gap_days()

    def latest_loop():
        last_log = 0
        while not stop.is_set():
            try:
                if config.BVIDEO_STREAM_ENABLED and config.source_on("bvideo"):
                    result = poll_once(stop_event=stop)
                    if time.time() - last_log >= 60:
                        if result.get("busy"):
                            activity.say("B站最新投稿：等待上次请求租约释放，尚未发起新请求")
                        else:
                            activity.say("B站最新投稿：状态 %s，本次返回 %s 条，新增 %s 条；%s" % (
                                result.get("status"), result.get("returned", 0),
                                result.get("new", 0), result.get("error") or
                                ("列表断档" if result.get("status") == "gap" else "实时轮询中")))
                        last_log = time.time()
            except Exception as exc:
                activity.say("B站最新投稿：" + str(exc)[:200])
            stop.wait(0.5)

    def inbox_loop():
        while not stop.is_set():
            try:
                if config.BVIDEO_STREAM_ENABLED and config.source_on("bvideo"):
                    for tid in bili.LEGACY_GAME_SECTIONS:
                        if stop.is_set():
                            break
                        drain_partition(tid)
                    worker_result("入库")
            except Exception as exc:
                worker_result("入库", exc)
                activity.say("B站视频入库：" + str(exc)[:200])
            stop.wait(0.2)

    def tag_loop():
        while not stop.is_set():
            progressed = False
            try:
                if config.BVIDEO_STREAM_ENABLED and config.source_on("bvideo"):
                    progressed = stream.enrich_once()
            except bili.RiskError:
                pass
            except Exception as exc:
                activity.say("B站 Tag 补齐：" + str(exc)[:200])
            stop.wait(0.01 if progressed else 0.2)

    def maintenance_loop():
        last_rescan = 0
        while not stop.is_set():
            try:
                if config.BVIDEO_STREAM_ENABLED and config.source_on("bvideo"):
                    if time.time() - last_rescan >= 300:
                        with db.session() as c:
                            bili_terms.rescan(c, datetime.now(bili.BEIJING).date().isoformat(),
                                              game_only=True, title_limit=200, refresh_terms=False)
                        last_rescan = time.time()
                    register_gap_days()
                    log_health()
                    worker_result("维护")
            except Exception as exc:
                worker_result("维护", exc)
                activity.say("B站采集维护：" + str(exc)[:200])
            stop.wait(60)

    def analysis_loop():
        last_refresh = 0
        while not stop.is_set():
            completed = 0
            try:
                completed = bili_queue.analyze_once()
                if time.time() - last_refresh >= 5:
                    bili_terms.refresh_dirty(sweep=256)
                    last_refresh = time.time()
                worker_result("本地分析")
            except Exception as exc:
                worker_result("本地分析", exc)
                activity.say("B站本地分析：" + str(exc)[:200])
            stop.wait(0.05 if completed else 1)

    def worker(name, target, *args):
        def run():
            try:
                target(*args)
            finally:
                bili.close_anonymous_session()
                with stream._lock:
                    stream._threads.pop(name, None)
                    if not stream._threads:
                        stream._started = False
        thread = threading.Thread(target=run, name=name, daemon=True)
        stream._threads[name] = thread
        return thread

    threads = [worker("bili-stream-latest", latest_loop)]
    threads.append(worker("bili-stream-inbox", inbox_loop))
    threads.append(worker("bili-stream-tags", tag_loop))
    for index in range(1, max(1, min(4, int(config.BVIDEO_TAG_WORKERS)))):
        threads.append(worker(f"bili-stream-tags-{index + 1}", tag_loop))
    threads.append(worker("bili-stream-maintenance", maintenance_loop))
    threads.append(worker("bili-stream-analysis", analysis_loop))
    for thread in threads:
        thread.start()
    activity.say(f"B站：实时列表、分类入库、{len(threads) - 4} 个Tag线程及独立分析线程已启动；"
                 f"统一发起间隔 {bili.REQUEST_GAP:g} 秒，最多 {bili.MAX_INFLIGHT} 个在途请求；"
                 "风控暂停继续生效")
