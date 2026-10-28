"""Independent legacy game sampling workers; samples are never called a census."""
import os
import threading
import time
import uuid
from datetime import datetime

from . import activity, bili_daily, bili_queue, bili_terms, config, db
from .sources import bili

SCHEMA = """
CREATE TABLE IF NOT EXISTS bs_partitions(
 tid INTEGER PRIMARY KEY,status TEXT DEFAULT 'pending',polls INTEGER DEFAULT 0,
 rows INTEGER DEFAULT 0,new_videos INTEGER DEFAULT 0,last_attempt REAL DEFAULT 0,
 last_success REAL DEFAULT 0,latest TEXT,error TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS bs_items(
 tid INTEGER,bvid TEXT,PRIMARY KEY(tid,bvid));
CREATE INDEX IF NOT EXISTS bs_item_bvid ON bs_items(bvid);
CREATE TABLE IF NOT EXISTS bs_pending(
 bvid TEXT PRIMARY KEY,retry_after REAL DEFAULT 0,attempts INTEGER DEFAULT 0);
"""
_ready = set()
_init_lock = threading.Lock()
_lock = threading.Lock()
_partition_locks = {tid: threading.Lock() for tid in bili.LEGACY_GAME_SECTIONS}
_enrich_lock = threading.Lock()
_enrich_active = set()
_enrich_turn = 0
_threads = {}
_started = False


def init():
    with _init_lock:
        bili_daily.init()
        if config.DB_PATH not in _ready:
            with db.session() as c:
                c.executescript(SCHEMA)
                columns = {r["name"] for r in c.execute("PRAGMA table_info(bs_partitions)")}
                for name, kind in (("next_poll", "REAL DEFAULT 0"),
                                   ("last_seconds", "REAL DEFAULT 0"),
                                   ("runtime_pid", "INTEGER DEFAULT 0")):
                    if name not in columns:
                        c.execute(f"ALTER TABLE bs_partitions ADD COLUMN {name} {kind}")
                c.executemany("INSERT OR IGNORE INTO bs_partitions(tid) VALUES(?)",
                              [(tid,) for tid in bili.LEGACY_GAME_SECTIONS])
            _ready.add(config.DB_PATH)


def interval():
    return max(60, int(config.BVIDEO_STREAM_SECONDS))


def _claim(owner, tid=None):
    name = "stream" if tid is None else f"stream:{tid}"
    now = time.time()
    with db.session() as c:
        c.execute("BEGIN IMMEDIATE")
        if tid is None:
            conflict = c.execute("""SELECT 1 FROM bd_lease
                WHERE name LIKE 'stream:%' AND expires>? LIMIT 1""", (now,)).fetchone()
        else:
            conflict = c.execute("""SELECT 1 FROM bd_lease
                WHERE name='stream' AND expires>?""", (now,)).fetchone()
        if conflict:
            return False
        c.execute("""INSERT INTO bd_lease VALUES(?,?,?)
            ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires=excluded.expires
            WHERE bd_lease.expires<?""", (name, owner, now + 240, now))
        return c.execute("SELECT owner FROM bd_lease WHERE name=?", (name,)).fetchone()[0] == owner


def _next_poll(tid=None):
    if db.kv_get("bili_stream_mode", "") == "latest_window":
        return float((db.kv_get("bili_latest_state", {}) or {}).get("next_poll") or 0)
    legacy = float(db.kv_get("bili_stream_next", 0) or 0)
    with db.session() as c:
        if tid is None:
            value = c.execute("SELECT MIN(next_poll) FROM bs_partitions").fetchone()[0]
        else:
            value = c.execute("SELECT next_poll FROM bs_partitions WHERE tid=?", (tid,)).fetchone()[0]
    return float(value or legacy)


def _save_sample(tid, result):
    with db.session() as c:
        added, queued = 0, 0
        for record in result["list"]:
            bvid = record["bvid"]
            added += c.execute("INSERT OR IGNORE INTO bs_items VALUES(?,?)", (tid, bvid)).rowcount
            inserted = c.execute("""INSERT INTO bd_videos(
                bvid,day,title,owner_id,tid,views,likes,favorites,replies)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(bvid) DO NOTHING""",
                (bvid, record["date"], record["title"], record["owner_id"], tid,
                 *[record.get(k) for k in ("views", "likes", "favorites", "replies")])).rowcount
            if inserted:
                bili_terms.seed(c, dict(record, tags=[]))
            state = c.execute("SELECT detail_state FROM bd_videos WHERE bvid=?", (bvid,)).fetchone()[0]
            if state in ("pending", "error"):
                queued += bili_queue.enqueue(c, bvid, recent=True)
        c.execute("""UPDATE bs_partitions SET status=?,rows=?,new_videos=new_videos+?,
            last_success=?,latest=?,error='' WHERE tid=?""",
            ("sampled" if result["list"] else "empty", len(result["list"]), added,
             time.time(), result.get("latest"), tid))
    return added


def _mark(tid, status, error):
    with db.session() as c:
        c.execute("UPDATE bs_partitions SET status=?,error=? WHERE tid=?",
                  (status, str(error)[:500], tid))


def run_once(force=False, stop_event=None, tid=None):
    """Poll one independent child, or perform a manual sweep for diagnostics."""
    init()
    if tid is not None:
        tid = int(tid)
        if tid not in bili.LEGACY_GAME_SECTIONS:
            raise ValueError("Unknown legacy game child")
    if not config.BVIDEO_STREAM_ENABLED or not config.source_on("bvideo"):
        return dict(summary(), status="disabled")
    worker_lock = _lock if tid is None else _partition_locks[tid]
    if not worker_lock.acquire(blocking=False):
        return dict(summary(), busy=True)
    sections = list(bili.LEGACY_GAME_SECTIONS) if tid is None else [tid]
    lease = "stream" if tid is None else f"stream:{tid}"
    owner, claimed = uuid.uuid4().hex, False
    started, successful, added, errors = time.time(), 0, 0, []
    status = "sampling"
    try:
        if not force and started < _next_poll(tid):
            return dict(summary(), skipped=True)
        claimed = _claim(owner, tid)
        if not claimed:
            return dict(summary(), busy=True)
        bili._guard(bili.REGION_FEED_SCOPE)
        for section in sections:
            with bili.request_slot(bili.REGION_FEED_SCOPE):
                bili._guard(bili.REGION_FEED_SCOPE)
                if stop_event and stop_event.is_set():
                    status = "interrupted"
                    break
                with db.session() as c:
                    c.execute("UPDATE bd_lease SET expires=? WHERE name=? AND owner=?",
                              (time.time() + 240, lease, owner))
                    c.execute("""UPDATE bs_partitions SET status='polling',
                        last_attempt=?,polls=polls+1 WHERE tid=?""", (time.time(), section))
                attempt = time.time()
                try:
                    result = bili.legacy_region_feed(section, config.BVIDEO_STREAM_PAGE_SIZE)
                    added += _save_sample(section, result)
                    successful += 1
                except bili.RiskError as exc:
                    _mark(section, "paused", exc)
                    raise
                except Exception as exc:
                    _mark(section, "error", exc)
                    errors.append(f"{section}: {exc}")
                finally:
                    with db.session() as c:
                        c.execute("""UPDATE bs_partitions SET last_seconds=?,runtime_pid=?
                            WHERE tid=?""", (round(time.time() - attempt, 3), os.getpid(), section))
        if errors:
            status = "partial" if successful else "error"
    except bili.RiskError as exc:
        status = "paused"
        errors.append(str(exc))
    except Exception as exc:
        status = "error"
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            if claimed:
                finished = time.time()
                target = started + interval()
                # Slow requests resume once, without replaying missed timer ticks.
                target = max(target, finished + 1)
                target = max(target, finished + bili.pause_left(),
                             finished + bili.pause_left(bili.REGION_FEED_SCOPE))
                with db.session() as c:
                    c.executemany("UPDATE bs_partitions SET next_poll=? WHERE tid=?",
                                  [(target, section) for section in sections])
                    c.execute("DELETE FROM bd_lease WHERE name=? AND owner=?", (lease, owner))
                if tid is None:
                    db.kv_set("bili_stream_next", target)
                db.kv_set("bili_stream_round" if tid is None else f"bili_stream_round:{tid}", {
                    "status": status, "started": started, "finished": finished,
                    "seconds": round(finished - started, 2), "polled": successful,
                    "new_sightings": added, "error": "；".join(errors)[:1200],
                    "pid": os.getpid(),
                })
        finally:
            worker_lock.release()
    return summary()


def enrich_once():
    """One durable Tag job; live polling cannot indefinitely postpone details."""
    global _enrich_turn
    init()
    if not config.BVIDEO_STREAM_ENABLED or not config.source_on("bvideo"):
        return False
    latest_mode = db.kv_get("bili_stream_mode", "") == "latest_window"
    if bili.paused() or bili.paused("视频详情及标签") or (
            not latest_mode and bili.paused(bili.REGION_FEED_SCOPE)):
        return False
    with _enrich_lock:
        remaining = _next_poll() - time.time()
        if not latest_mode and remaining < 8:
            return False
        with db.session() as c:
            now = time.time()
            limit = len(_enrich_active) + 1
            jobs = []
            urgent = latest_mode and bili_queue.recent_pressure(c, now) > 120
            cycle = 10 if urgent else 5
            preferred = ("recent", "backlog") if _enrich_turn % cycle != cycle - 1 else (
                "backlog", "recent")
            for lane in preferred:
                if lane == "recent":
                    jobs = bili_queue.ready_jobs(c, "recent", now, limit)
                else:
                    # Separate indexed ranges avoid sorting the entire historical debt per BV.
                    jobs = bili_queue.ready_jobs(c, "backlog", now, limit)
                    jobs.extend(bili_queue.ready_jobs(c, "expired", now, limit))
                    jobs.sort(key=lambda row: row["queued"])
                jobs = [row for row in jobs if row["bvid"] not in _enrich_active]
                if jobs:
                    break
        job = next((row for row in jobs if row["bvid"] not in _enrich_active), None)
        if job is None:
            return False
        _enrich_turn += 1
        _enrich_active.add(job["bvid"])
    try:
        try:
            if not bili_daily.enrich_video(job["bvid"], persistent=True, timeout=12):
                return False
        except bili.RiskError:
            raise
        except Exception:
            # enrich_video persists one shared retry deadline; no second backoff clock.
            return False
        return True
    finally:
        with _enrich_lock:
            _enrich_active.discard(job["bvid"])


def summary():
    init()
    today = datetime.now(bili.BEIJING).date().isoformat()
    with db.session() as c:
        parts = [dict(row) for row in c.execute("SELECT * FROM bs_partitions")]
        realtime = dict(c.execute("""SELECT COUNT(*) videos,
            COALESCE(SUM(v.detail_state='ok'),0) tagged,
            COALESCE(SUM(v.detail_state='unavailable'),0) unavailable,
            COALESCE(SUM(v.day=?),0) today_videos
            FROM bd_videos v WHERE v.detail_state!='archived'
            AND EXISTS(SELECT 1 FROM bs_items s WHERE s.bvid=v.bvid)""",
                                (today,)).fetchone())
        counts = dict(c.execute("""SELECT COUNT(*) videos,
            COALESCE(SUM(detail_state='ok'),0) tagged,
            COALESCE(SUM(detail_state='unavailable'),0) unavailable,
            COALESCE(SUM(day=?),0) today_videos,
            COALESCE(SUM(detail_state IN ('pending','error')),0) pending_tags
            FROM bd_videos WHERE detail_state!='archived'""", (today,)).fetchone())
        queue = bili_queue.metrics(c)
        totals = {r["tid"]: dict(r) for r in c.execute("""SELECT tid,COUNT(*) videos,
            SUM(detail_state IN ('pending','error')) pending FROM bd_videos
            WHERE detail_state!='archived' GROUP BY tid""")}
        policy = bili_queue.collection_policy(c)
        baseline = dict(c.execute("""SELECT COUNT(*) videos,
            COALESCE(SUM(v.detail_state='ok'),0) tagged,
            COALESCE(SUM(v.detail_state IN ('pending','error')),0) pending_tags,
            COALESCE(SUM(v.detail_state='unavailable'),0) unavailable
            FROM bs_latest_seen s JOIN bd_videos v USING(bvid)
            WHERE s.first_seen>=? AND s.published>=? AND v.detail_state!='archived'""",
            (policy["started"], policy["started"])).fetchone()) if policy.get("started") else {}
    order = list(bili.LEGACY_GAME_SECTIONS)
    workers = {name: thread.is_alive() for name, thread in list(_threads.items())}
    for part in parts:
        part["name"] = bili.LEGACY_GAME_SECTIONS[part["tid"]]
        part["worker_started"] = bool(workers.get(f"bili-stream-{part['tid']}"))
        part["new_videos"] = totals.get(part["tid"], {}).get("videos", 0)
        part["pending_tags"] = totals.get(part["tid"], {}).get("pending", 0)
    parts.sort(key=lambda part: order.index(part["tid"]))
    state = db.kv_get("bili_stream_round", {}) or {}
    enabled = bool(config.BVIDEO_STREAM_ENABLED and config.source_on("bvideo"))
    status = state.get("status", "pending") if enabled else "disabled"
    parallel = db.kv_get("bili_stream_mode", "") == "partition_threads"
    if enabled and parallel:
        state = {}
        failed = [p for p in parts if p["status"] == "error"]
        status = ("error" if len(failed) == len(parts) else "partial") if failed else (
            "sampling" if any(p["polls"] for p in parts) else "pending")
    pause = max(bili.pause_left(), bili.pause_left(bili.REGION_FEED_SCOPE))
    if enabled and pause:
        status = "paused"
    result = {**counts, "realtime": realtime, "queue": queue,
            "collection_policy": policy, "baseline": baseline,
            "statistics_scope": "all_saved_videos", "today": today, "status": status,
            "enabled": enabled, "worker_started": _started, "runtime_pid": os.getpid(),
            "mode": "partition_threads" if parallel else "serial_round",
            "workers": workers,
            "partition_workers_running": sum(p["worker_started"] for p in parts),
            "tag_worker_running": any(alive for name, alive in workers.items()
                                      if name == "bili-stream-tags" or
                                      name.startswith("bili-stream-tags-")),
            "tag_workers_running": sum(alive for name, alive in workers.items()
                                      if name == "bili-stream-tags" or
                                      name.startswith("bili-stream-tags-")),
            "interval_seconds": interval(), "requested_page_size": config.BVIDEO_STREAM_PAGE_SIZE,
            "partitions": parts, "round": state,
            "next_poll": _next_poll(), "pause_seconds": pause,
            "coverage": "Legacy game feed samples; not chronological or exhaustive",
            "counters": "Cumulative counters at detail retrieval, not daily increases"}
    if db.kv_get("bili_stream_mode", "") == "latest_window":
        from .bili_latest import summary_fields
        return summary_fields(result)
    return result


def start(stop_event=None):
    from .bili_latest import start as start_latest
    start_latest(stop_event)
