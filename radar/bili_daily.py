"""Resumable dated-directory collection; directory coverage is never a whole-site census."""
import argparse
import json
import math
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from html import unescape

from . import activity, bili_queue, bili_term_filter, bili_terms, config, db
from .sources import bili

SCHEMA = """
CREATE TABLE IF NOT EXISTS bd_days(
 day TEXT PRIMARY KEY,status TEXT DEFAULT 'pending',error TEXT DEFAULT '',
 requests INTEGER DEFAULT 0,retry_after REAL DEFAULT 0,updated REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS bd_partitions(
 day TEXT,tid INTEGER,next_page INTEGER DEFAULT 1,total INTEGER,pages INTEGER,
 page_size INTEGER,state TEXT DEFAULT 'pending',PRIMARY KEY(day,tid));
CREATE TABLE IF NOT EXISTS bd_partition_items(
 day TEXT,tid INTEGER,bvid TEXT,PRIMARY KEY(day,tid,bvid));
CREATE INDEX IF NOT EXISTS bd_partition_bvid ON bd_partition_items(bvid);
CREATE TABLE IF NOT EXISTS bd_videos(
 bvid TEXT PRIMARY KEY,day TEXT,title TEXT,owner_id TEXT,tid INTEGER,tid_v2 INTEGER DEFAULT 0,
 tags TEXT,views INTEGER,likes INTEGER,coins INTEGER,favorites INTEGER,shares INTEGER,replies INTEGER,
 detail_state TEXT DEFAULT 'pending',detail_error TEXT DEFAULT '',title_scanned INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS bd_video_day ON bd_videos(day);
CREATE INDEX IF NOT EXISTS bd_video_pending ON bd_videos(day,detail_state,bvid);
CREATE TABLE IF NOT EXISTS bd_lease(name TEXT PRIMARY KEY,owner TEXT,expires REAL);
"""
_ready = set()
_init_lock = threading.Lock()
_lock = threading.Lock()
_started = False


class IndexPending(RuntimeError):
    pass


class YieldRun(Exception):
    pass


class DirectoryChanged(RuntimeError):
    pass


def init():
    with _init_lock:
        if config.DB_PATH not in _ready:
            with db.session() as c:
                c.executescript(SCHEMA + bili_terms.SCHEMA)
                bili_term_filter.init(c)
                columns = {r["name"] for r in c.execute("PRAGMA table_info(bd_partitions)")}
                for name, kind in (("earliest", "TEXT"), ("latest", "TEXT"),
                                   ("retry_after", "REAL DEFAULT 0"),
                                   ("error", "TEXT DEFAULT ''")):
                    if name not in columns:
                        c.execute(f"ALTER TABLE bd_partitions ADD COLUMN {name} {kind}")
                columns = {r["name"] for r in c.execute("PRAGMA table_info(bd_videos)")}
                for name, kind in (("detail_attempts", "INTEGER DEFAULT 0"),
                                   ("detail_retry_after", "REAL DEFAULT 0"),
                                   ("detail_updated", "REAL DEFAULT 0")):
                    if name not in columns:
                        c.execute(f"ALTER TABLE bd_videos ADD COLUMN {name} {kind}")
                c.execute("""CREATE INDEX IF NOT EXISTS bd_detail_due
                    ON bd_videos(day,detail_state,detail_retry_after,bvid)""")
                c.execute("""CREATE INDEX IF NOT EXISTS bd_detail_state
                    ON bd_videos(detail_state,bvid)""")
                bili_queue.init(c)
                # Release only the old day-wide backoff caused by a saved video error.
                c.execute("""UPDATE bd_days SET status='in_progress',error='',retry_after=0
                    WHERE status='error' AND error LIKE '%视频详情及标签%'
                    AND EXISTS(SELECT 1 FROM bd_videos v
                        WHERE v.day=bd_days.day AND v.detail_state='error')""")
            _ready.add(config.DB_PATH)


def yesterday(now=None):
    return (datetime.fromtimestamp(time.time() if now is None else now, bili.BEIJING).date() -
            timedelta(days=1)).isoformat()


def ensure_day(day):
    day = date.fromisoformat(day).isoformat()
    if day >= datetime.now(bili.BEIJING).date().isoformat():
        raise ValueError("Only closed Beijing calendar days can be collected")
    init()
    with db.session() as c:
        c.execute("INSERT OR IGNORE INTO bd_days(day) VALUES(?)", (day,))
        c.executemany("INSERT OR IGNORE INTO bd_partitions(day,tid,page_size) VALUES(?,?,?)",
                      [(day, int(tid), max(1, min(100, int(config.BVIDEO_DAILY_PAGE_SIZE))))
                       for tid in dict.fromkeys(config.BVIDEO_DAILY_TIDS)])


def _claim(owner):
    with db.session() as c:
        c.execute("""INSERT INTO bd_lease VALUES('daily',?,?)
                     ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires=excluded.expires
                     WHERE bd_lease.expires<?""", (owner, time.time() + 120, time.time()))
        return c.execute("SELECT owner FROM bd_lease WHERE name='daily'").fetchone()[0] == owner


def _renew(owner):
    with db.session() as c:
        cur = c.execute("UPDATE bd_lease SET expires=? WHERE name='daily' AND owner=?",
                        (time.time() + 120, owner))
        if not cur.rowcount:
            raise YieldRun()


def _index_guard(tid, day, page):
    # High-volume old game sections stopping near 01:00 are not a complete yesterday.
    if tid not in (17, 65):
        return
    if not page["list"]:
        raise IndexPending(f"Old directory has no indexed videos: {day}, tid={tid}")
    times = []
    for row in page["list"]:
        value = row["pubdate"]
        if isinstance(value, (int, float)) or str(value).isdigit():
            value = datetime.fromtimestamp(int(value), bili.BEIJING)
        else:
            value = datetime.fromisoformat(str(value))
            if value.tzinfo is None:
                value = value.replace(tzinfo=bili.BEIJING)
        times.append(value)
    expected_end = datetime.fromisoformat(day).replace(tzinfo=bili.BEIJING) + timedelta(days=1)
    if max(times) < expected_end - timedelta(hours=2):
        raise IndexPending(f"Old directory watermark is too early: {max(times).isoformat()}, tid={tid}")


def _publication_time(value):
    if isinstance(value, (int, float)) or str(value).isdigit():
        return datetime.fromtimestamp(int(value), bili.BEIJING).isoformat()
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=bili.BEIJING)
    return parsed.astimezone(bili.BEIJING).isoformat()


def _reset_due_partitions(day):
    # Retain videos/Tags while rebuilding only an expired directory snapshot.
    with db.session() as c:
        due = c.execute("""SELECT tid FROM bd_partitions WHERE day=?
            AND state='waiting_index' AND retry_after<=?""", (day, time.time())).fetchall()
        for part in due:
            c.execute("DELETE FROM bd_partition_items WHERE day=? AND tid=?", (day, part["tid"]))
            c.execute("""UPDATE bd_partitions SET next_page=1,total=NULL,pages=NULL,
                earliest=NULL,latest=NULL,state='pending',error='',retry_after=0
                WHERE day=? AND tid=?""", (day, part["tid"]))


def _save_page(job, result):
    day, tid, pn = job["day"], job["tid"], job["next_page"]
    total, pages, size = result["total"], result["pages"], result["page_size"]
    if total and pages != math.ceil(total / size):
        raise RuntimeError("Directory page count is capped or inconsistent with total/page size")
    if job["total"] is not None and (total != job["total"] or pages != job["pages"] or size != job["page_size"]):
        raise DirectoryChanged("Directory counts changed during pagination; restarting its page checkpoint")
    rows = result["list"]
    ids = {row["bvid"] for row in rows}
    if len(ids) != len(rows):
        raise RuntimeError("Duplicate BV IDs within a directory page")
    if pn < pages and len(rows) != size:
        raise RuntimeError("Short directory page before reported end")
    if rows and len(rows) > total:
        raise RuntimeError("Directory rows exceed its reported total")
    with db.session() as c:
        seen_count = c.execute("SELECT COUNT(*) FROM bd_partition_items WHERE day=? AND tid=?",
                               (day, tid)).fetchone()[0]
        if ids:
            repeated = c.execute(
                "SELECT 1 FROM bd_partition_items WHERE day=? AND tid=? AND bvid IN (" +
                ",".join("?" for _ in ids) + ") LIMIT 1", [day, tid, *ids]).fetchone()
            if repeated:
                raise DirectoryChanged("Directory pages overlap or repeat; restarting its page checkpoint")
        for row in rows:
            record = {"bvid": row["bvid"], "date": day, "title": unescape(
                re.sub(r"<[^>]+>", "", row.get("title") or "")),
                "owner_id": str(row.get("mid") or (row.get("owner") or {}).get("mid") or ""),
                "owner_name": row.get("author") or (row.get("owner") or {}).get("name") or "",
                "tags": []}
            actual_tid = bili.tid_of(row)
            if actual_tid is not None and actual_tid != tid:
                raise RuntimeError("Date directory ignored its category filter")
            c.execute("""INSERT INTO bd_videos(bvid,day,title,owner_id,tid) VALUES(?,?,?,?,?)
                         ON CONFLICT(bvid) DO NOTHING""",
                      (record["bvid"], day, record["title"], record["owner_id"], tid))
            c.execute("INSERT INTO bd_partition_items VALUES(?,?,?)", (day, tid, record["bvid"]))
            existing = c.execute("SELECT detail_state FROM bd_videos WHERE bvid=?", (record["bvid"],)).fetchone()
            if existing["detail_state"] != "ok":
                bili_terms.seed(c, record)
            bili_queue.enqueue(c, record["bvid"])
        finished = pn >= pages
        if finished and seen_count + len(ids) != total:
            raise RuntimeError("Unique directory BV count does not equal reported total")
        times = [t for t in (job.get("earliest"), job.get("latest")) if t]
        times.extend(_publication_time(row["pubdate"]) for row in rows)
        earliest, latest = (min(times), max(times)) if times else (None, None)
        state, error, retry = "complete" if finished else "listing", "", 0
        if finished:
            try:
                _index_guard(tid, day, {"list": [{"pubdate": latest}] if latest else []})
            except IndexPending as exc:
                state, error = "waiting_index", str(exc)
                retry = time.time() + config.BVIDEO_DAILY_RETRY_SECONDS
        c.execute("""UPDATE bd_partitions SET next_page=?,total=?,pages=?,page_size=?,state=?,
                     earliest=?,latest=?,error=?,retry_after=? WHERE day=? AND tid=?""",
                  (pn + 1, total, pages, size, state, earliest, latest, error, retry, day, tid))


def _save_detail(record):
    from . import bili_growth
    bili_growth.init()
    with db.session() as c:
        row = c.execute("SELECT day,detail_state FROM bd_videos WHERE bvid=?", (record["bvid"],)).fetchone()
        if row is None or row["day"] != record["date"]:
            raise RuntimeError("Detail publication day does not match the saved directory")
        if row["detail_state"] == "archived":
            return
        c.execute("""UPDATE bd_videos SET title=?,owner_id=?,tid=?,tid_v2=?,tags=?,
                     views=?,likes=?,coins=?,favorites=?,shares=?,replies=?,
                     detail_state='ok',detail_error='',title_scanned=0,
                     detail_retry_after=0,detail_updated=? WHERE bvid=?""",
                  (record["title"], record["owner_id"], record["tid"], record["tid_v2"],
                   json.dumps(record["tags"], ensure_ascii=False),
                   *[record[k] for k in ("views", "likes", "coins", "favorites", "shares", "replies")],
                   time.time(), record["bvid"]))
        bili_queue.finish(c, record["bvid"])
        bili_growth.record_sample(c, record["bvid"], record.get("views"))
        bili_queue.defer_analysis(c, record)


@contextmanager
def detail_claim(bvid):
    """Daily and minute workers share a per-BV enrichment lease."""
    owner, name = uuid.uuid4().hex, "detail:" + bvid
    with db.session() as c:
        c.execute("""INSERT INTO bd_lease VALUES(?,?,?)
            ON CONFLICT(name) DO UPDATE SET owner=excluded.owner,expires=excluded.expires
            WHERE bd_lease.expires<?""", (name, owner, time.time() + 120, time.time()))
        claimed = c.execute("SELECT owner FROM bd_lease WHERE name=?", (name,)).fetchone()[0] == owner
    try:
        yield claimed
    finally:
        if claimed:
            with db.session() as c:
                c.execute("DELETE FROM bd_lease WHERE name=? AND owner=?", (name, owner))


def enrich_video(bvid, persistent=False, timeout=None):
    with detail_claim(bvid) as claimed:
        if not claimed:
            return False
        with db.session() as c:
            row = c.execute("""SELECT detail_state,detail_attempts,detail_retry_after
                FROM bd_videos WHERE bvid=?""", (bvid,)).fetchone()
        if row is None:
            raise RuntimeError("Missing saved video for enrichment")
        if row["detail_state"] in ("ok", "unavailable", "archived"):
            return True
        if row["detail_retry_after"] > time.time():
            return False
        try:
            options = {"persistent": True} if persistent else {}
            if timeout is not None:
                options["timeout"] = timeout
            _save_detail(bili.detail_with_tags(bvid, **options))
        except bili.RiskError:
            raise
        except Exception as exc:
            unavailable = bool(re.search(r"code=(?:-404|-620|62002)(?!\d)", str(exc)))
            with db.session() as c:
                delay = min(3600, 60 * 2 ** min(row["detail_attempts"], 6))
                # Repeated business failures remain saved without monopolizing the queue.
                if re.search(r"code=62012(?!\d)", str(exc)):
                    delay = max(delay, 300 if row["detail_attempts"] == 0 else
                                21600 if row["detail_attempts"] < 3 else 86400)
                retry = 0 if unavailable else time.time() + delay
                c.execute("""UPDATE bd_videos SET detail_state=?,detail_error=?,
                    detail_attempts=detail_attempts+1,detail_retry_after=?,detail_updated=?
                    WHERE bvid=?""", ("unavailable" if unavailable else "error",
                                     str(exc)[:500], retry, time.time(), bvid))
                if unavailable:
                    bili_queue.finish(c, bvid, unavailable=True)
                else:
                    c.execute("""UPDATE bs_pending SET retry_after=?,attempts=?
                        WHERE bvid=?""", (retry, row["detail_attempts"] + 1, bvid))
            if not unavailable:
                raise
        return True


def _state(day, status, error="", requests=0, retry=0):
    with db.session() as c:
        c.execute("""UPDATE bd_days SET status=?,error=?,requests=requests+?,retry_after=?,updated=?
                     WHERE day=?""", (status, error[:1000], requests, time.time() + retry if retry else 0,
                                     time.time(), day))


def _counts(day):
    with db.session() as c:
        return dict(c.execute("""SELECT COUNT(*) videos,
            COALESCE(SUM(detail_state='ok'),0) tagged,
            COALESCE(SUM(detail_state='unavailable'),0) unavailable,
            COALESCE(SUM(detail_state='archived'),0) archived,
            COALESCE(SUM(detail_state IN ('pending','error')),0) pending_details,
            COALESCE(SUM(detail_state='error'),0) detail_errors
            FROM bd_videos WHERE day=?""", (day,)).fetchone())


def run_once(day=None, request_limit=None, seconds=None, stop_event=None, enqueue_only=False):
    """One bounded slice. Checkpoints commit per page/video, including before a pause."""
    init()
    with db.session() as c:
        if bili_queue.collection_policy(c).get("mode") == "realtime_only":
            return {"status": "archived", "day": day or yesterday(), "updated": 0,
                    "videos": 0, "error": "自动历史扩张已关闭"}
    target = day or yesterday()
    ensure_day(target)
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    owner, claimed, used, changed = uuid.uuid4().hex, False, 0, False
    started = time.monotonic()
    limit = config.BVIDEO_DAILY_REQUESTS_PER_RUN if request_limit is None else request_limit
    duration = config.BVIDEO_DAILY_SECONDS_PER_RUN if seconds is None else seconds
    selected = None
    try:
        claimed = _claim(owner)
        if not claimed:
            return {"status": "busy"}
        with db.session() as c:
            if day is not None:
                row = c.execute("""SELECT day FROM bd_days WHERE day=? AND
                    status NOT IN ('complete','partial') AND retry_after<=?""",
                                (target, time.time())).fetchone()
            else:
                row = c.execute("""SELECT day FROM bd_days
                WHERE status NOT IN ('complete','partial') AND retry_after<=?
                ORDER BY CASE WHEN day=? AND status IN ('pending','listing') THEN 0 ELSE 1 END,
                    updated,day LIMIT 1""",
                                (time.time(), target)).fetchone()
        if row is None:
            return summary(target)
        selected = row["day"]
        _reset_due_partitions(selected)
        _state(selected, "listing")
        while True:
            if used >= limit or time.monotonic() - started >= duration or (stop_event and stop_event.is_set()):
                raise YieldRun()
            _renew(owner)
            with db.session() as c:
                part = c.execute("""SELECT * FROM bd_partitions WHERE day=?
                    AND state IN ('pending','listing')
                    ORDER BY next_page,CASE WHEN tid=17 THEN 0 WHEN tid=65 THEN 1 ELSE 2 END,tid LIMIT 1""",
                                 (selected,)).fetchone()
                pending = c.execute("""SELECT bvid FROM bd_videos v WHERE day=?
                    AND detail_state IN ('pending','error') AND detail_retry_after<=?
                    AND NOT EXISTS(SELECT 1 FROM bd_lease l
                        WHERE l.name='detail:'||v.bvid AND l.expires>?)
                    ORDER BY detail_attempts,bvid LIMIT 1""",
                                    (selected, time.time(), time.time())).fetchone()
            # Alternate listing and enrichment so a large/stale directory cannot starve Tags.
            if part is not None and (enqueue_only or pending is None or used % 2 == 0):
                part = dict(part)
                if enqueue_only:
                    with db.session() as c:
                        pressure = bili_queue.directory_pressure(c)
                    if pressure:
                        _state(selected, "backpressure", "历史目录暂缓，优先补齐已有 Tag", used, 30)
                        used = 0
                        break
                bili._guard("日期目录")
                used += 1
                try:
                    result = bili.dated_videos(part["tid"], selected, part["next_page"],
                                               part["page_size"], persistent=True)
                    _save_page(part, result)
                except DirectoryChanged as exc:
                    # Only this partition waits for a stable index; other work continues.
                    with db.session() as c:
                        c.execute("DELETE FROM bd_partition_items WHERE day=? AND tid=?",
                                  (selected, part["tid"]))
                        c.execute("""UPDATE bd_partitions SET next_page=1,total=NULL,pages=NULL,
                            state='waiting_index',earliest=NULL,latest=NULL,error=?,retry_after=?
                            WHERE day=? AND tid=?""", (str(exc),
                            time.time() + config.BVIDEO_DAILY_RETRY_SECONDS, selected, part["tid"]))
                changed = True
                continue
            _state(selected, "details")
            if enqueue_only and pending is not None:
                if part is not None:
                    continue
                _state(selected, "waiting_details", "统一 Tag 队列处理中", used, 30)
                used = 0
                break
            if pending is None:
                with db.session() as c:
                    stale = c.execute("""SELECT error,retry_after FROM bd_partitions
                        WHERE day=? AND state='waiting_index' ORDER BY retry_after LIMIT 1""",
                                      (selected,)).fetchone()
                    deferred = c.execute("""SELECT MIN(detail_retry_after) due,COUNT(*) n
                        FROM bd_videos WHERE day=? AND detail_state IN ('pending','error')""",
                                         (selected,)).fetchone()
                if deferred["n"]:
                    retry = max(5, (deferred["due"] or time.time()) - time.time())
                    if stale:
                        retry = min(retry, max(5, stale["retry_after"] - time.time()))
                    _state(selected, "waiting_details",
                           f"{deferred['n']} 条视频等待详情重试；其余视频继续采集", used, retry)
                    used = 0
                    break
                if stale:
                    _state(selected, "waiting_index", stale["error"], used,
                           max(1, stale["retry_after"] - time.time()))
                    used = 0
                    break
                counts = _counts(selected)
                if not counts["videos"]:
                    raise IndexPending("All selected date directories were empty")
                _state(selected, "partial" if counts["unavailable"] else "complete", requests=used)
                used = 0
                break
            bvid = pending["bvid"]
            bili._guard("视频详情及标签")
            used += 1
            try:
                if not enrich_video(bvid, persistent=True, timeout=12):
                    raise YieldRun()
            except (bili.RiskError, YieldRun):
                raise
            except Exception:
                # enrich_video has persisted this BV's error and retry deadline.
                with db.session() as c:
                    failed = c.execute("SELECT detail_state FROM bd_videos WHERE bvid=?",
                                       (bvid,)).fetchone()
                if failed is None or failed["detail_state"] != "error":
                    raise
            changed = True
    except YieldRun:
        if selected:
            _state(selected, "in_progress", requests=used)
    except IndexPending as exc:
        _state(selected, "waiting_index", str(exc), used, config.BVIDEO_DAILY_RETRY_SECONDS)
    except bili.RiskError as exc:
        _state(selected, "paused", str(exc), used, max(60, bili.pause_left(exc.scope)))
    except Exception as exc:
        if selected:
            _state(selected, "error", f"{type(exc).__name__}: {exc}", used, config.BVIDEO_DAILY_RETRY_SECONDS)
        else:
            raise
    finally:
        try:
            if claimed:
                try:
                    if changed and not enqueue_only:
                        bili_queue.analyze_once(limit=25)
                        _renew(owner)
                        with db.session() as c:
                            bili_terms.rescan(c, datetime.now(bili.BEIJING).date().isoformat(),
                                              game_only=True, title_limit=2000)
                finally:
                    with db.session() as c:
                        c.execute("DELETE FROM bd_lease WHERE name='daily' AND owner=?", (owner,))
        finally:
            _lock.release()
    result = summary(selected or target)
    _log_progress(result)
    return result


def _log_progress(result):
    key, now = "bili_daily_log:" + result["day"], time.time()
    previous = db.kv_get(key, {}) or {}
    if previous.get("status") == result["status"] and now - previous.get("ts", 0) < 300:
        return
    names = {"in_progress": "采集中", "listing": "目录采集中", "details": "补齐详情中",
             "waiting_details": "部分详情等待重试", "waiting_index": "等待目录更新",
             "paused": "风控暂停", "error": "采集失败", "partial": "目录完成，有不可用视频",
             "complete": "所选目录及详情采集完成"}
    error = result.get("error") or ""
    watermark = re.search(r"watermark is too early: ([^,]+), tid=(\d+)", error)
    if watermark:
        name = bili.LEGACY_GAME_SECTIONS.get(int(watermark[2]), "所选分区")
        error = f"{name}目录尚未更新到全天，最晚投稿 {watermark[1]}"
    elif error.startswith("Directory "):
        error = "目录数量或分页发生变化，已保留视频并安排该分区重试"
    elif error.startswith(("Old directory has no indexed", "All selected date directories")):
        error = "日期目录尚无已索引投稿"
    else:
        error = re.sub(r"^(?:RuntimeError|IndexPending):\s*", "", error)
    activity.say(f"B站按日 {result['day']}：{names.get(result['status'], result['status'])}；"
                 f"已存 {result['videos']:,} 条，Tag及统计齐全 {result['tagged']:,} 条，"
                 f"待补 {result.get('pending_details', 0):,} 条"
                 + (f"；{error}" if error else ""))
    db.kv_set(key, {"status": result["status"], "ts": now})


def summary(day=None):
    init()
    with db.session() as c:
        if day is None:
            row = c.execute("SELECT * FROM bd_days ORDER BY day DESC LIMIT 1").fetchone()
        else:
            row = c.execute("SELECT * FROM bd_days WHERE day=?", (day,)).fetchone()
        if row is None:
            return {"status": "pending", "day": day or yesterday(), "videos": 0, "tagged": 0,
                    "partitions": [], "topics": [], "error": "", "page_size": 100}
        out = dict(row)
        out.update(_counts(row["day"]))
        out["partitions"] = [dict(r) for r in c.execute(
            "SELECT * FROM bd_partitions WHERE day=? ORDER BY tid", (row["day"],))]
        out["topics"] = bili_terms.topics(c, row["day"], game_only=True)
        out["term_filter"] = bili_term_filter.summary(c)
        out["page_size"] = max(1, min(100, int(config.BVIDEO_DAILY_PAGE_SIZE)))
        out["directory_complete"] = bool(out["partitions"]) and all(
            p["state"] == "complete" for p in out["partitions"])
        out["coverage"] = "Selected dated directories; not a whole-site game census"
        out["counters"] = "Cumulative counters at detail retrieval, not daily increases"
        pause = max((bili.pause_info(scope) for scope in
                     ("日期目录", "视频详情及标签")), key=lambda value: value["seconds"])
        out["pause_seconds"], out["pause_until"] = pause["seconds"], pause["until"]
        if pause["seconds"] and out["status"] not in ("complete", "partial"):
            out["status"] = "paused"
        return out


def start(stop_event=None):
    global _started
    if _started:
        return
    _started = True
    stop = stop_event or threading.Event()

    def loop():
        while not stop.is_set():
            local = datetime.now(bili.BEIJING)
            due = (local.hour, local.minute) >= (config.BVIDEO_DAILY_HOUR, config.BVIDEO_DAILY_MINUTE)
            if due and config.BVIDEO_DAILY_ENABLED and config.source_on("bvideo"):
                try:
                    from . import bili_stream
                    result = run_once(stop_event=stop, enqueue_only=bili_stream._started)
                    updated = result.get("updated", 0)
                    if updated > float(db.kv_get("bili_daily_reported_updated", 0) or 0):
                        from .collect import _status
                        ok = result["status"] in ("complete", "listing", "details", "in_progress")
                        _status("bvideo", ok, result.get("videos", 0),
                                err=result.get("error") or result["status"])
                        db.kv_set("bili_daily_reported_updated", updated)
                except Exception as exc:
                    activity.say("B站按日任务: " + str(exc)[:200])
            stop.wait(30)
    threading.Thread(target=loop, name="bili-daily", daemon=True).start()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--day")
    parser.add_argument("--requests", type=int)
    parser.add_argument("--seconds", type=float)
    args = parser.parse_args()
    print(json.dumps(run_once(args.day, args.requests, args.seconds), ensure_ascii=False, indent=2))
