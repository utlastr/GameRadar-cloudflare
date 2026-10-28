"""One durable enrichment queue and a separate, retryable local analysis outbox."""
import json
import threading
import time

from . import config, db

REVISION = "2026-10-28-fresh-baseline-v6"
RECENT_SECONDS = 3600
_analysis_lock = threading.Lock()
READY_QUERY = """SELECT p.* FROM bs_pending p INDEXED BY bq_lane
    CROSS JOIN bd_videos v ON v.bvid=p.bvid
    WHERE p.lane=? AND {window} AND p.retry_after<=? AND v.detail_retry_after<=?
    AND v.detail_state IN ('pending','error')
    AND NOT EXISTS(SELECT 1 FROM bd_lease l
        WHERE l.name='detail:'||p.bvid AND l.expires>?)
    ORDER BY p.queued,p.rowid LIMIT ?"""


def init(c):
    c.executescript("""
        CREATE TABLE IF NOT EXISTS bs_pending(
            bvid TEXT PRIMARY KEY,retry_after REAL DEFAULT 0,attempts INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS bs_events(ts REAL,kind TEXT,n INTEGER);
        CREATE INDEX IF NOT EXISTS bs_events_time ON bs_events(ts);
        CREATE TABLE IF NOT EXISTS bq_analysis(
            bvid TEXT PRIMARY KEY,record TEXT,queued REAL,retry_after REAL DEFAULT 0,
            attempts INTEGER DEFAULT 0,error TEXT DEFAULT '');
        CREATE INDEX IF NOT EXISTS bq_analysis_due ON bq_analysis(retry_after,queued);
        CREATE TABLE IF NOT EXISTS bq_archived(
            bvid TEXT PRIMARY KEY,previous_state TEXT,archived_at REAL);
        CREATE TABLE IF NOT EXISTS bq_archive_cache(k TEXT PRIMARY KEY,v TEXT);
    """)
    columns = {row["name"] for row in c.execute("PRAGMA table_info(bs_pending)")}
    for name, kind in (("queued", "REAL DEFAULT 0"), ("lane", "TEXT DEFAULT 'backlog'")):
        if name not in columns:
            c.execute(f"ALTER TABLE bs_pending ADD COLUMN {name} {kind}")
    c.execute("CREATE INDEX IF NOT EXISTS bq_lane ON bs_pending(lane,queued)")
    c.execute("CREATE INDEX IF NOT EXISTS bq_due ON bs_pending(retry_after,queued)")
    if c.execute("SELECT 1 FROM kv WHERE k='bili_tag_queue_revision'").fetchone() is None:
        # Import existing debt once, preserving both copies of legacy retry deadlines.
        c.execute("""DELETE FROM bs_pending WHERE bvid IN
            (SELECT bvid FROM bd_videos WHERE detail_state IN ('ok','unavailable','archived'))""")
        c.execute("""INSERT OR IGNORE INTO bs_pending(bvid,retry_after,attempts)
            SELECT bvid,detail_retry_after,detail_attempts FROM bd_videos
            WHERE detail_state IN ('pending','error') ORDER BY day,bvid""")
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='bs_latest_seen'").fetchone():
            c.execute("""UPDATE bs_pending SET lane='recent',
                queued=(SELECT first_seen FROM bs_latest_seen s WHERE s.bvid=bs_pending.bvid)
                WHERE bvid IN (SELECT bvid FROM bs_latest_seen WHERE first_seen>=?)""",
                      (time.time() - RECENT_SECONDS,))
        c.execute("INSERT INTO kv VALUES('bili_tag_queue_revision',?)", (json.dumps(REVISION),))


def collection_policy(c):
    row = c.execute("SELECT v FROM kv WHERE k='bili_collection_reset'").fetchone()
    return json.loads(row["v"]) if row else {}


def archive_pending(now=None):
    """Offline, one-time cutover; raw records and completed observations remain intact."""
    from . import bili_latest, bili_growth
    bili_latest.init()
    bili_growth.init()
    now = time.time() if now is None else now
    with db.session() as c:
        previous = collection_policy(c)
        if previous:
            return previous
        retained = c.execute("SELECT COUNT(*) FROM bd_videos WHERE detail_state='ok'").fetchone()[0]
        c.execute("""INSERT INTO bq_archived
            SELECT bvid,detail_state,? FROM bd_videos WHERE detail_state IN ('pending','error')""",
                  (now,))
        archived = c.execute("SELECT COUNT(*) FROM bq_archived").fetchone()[0]
        c.execute("""UPDATE bd_videos SET detail_state='archived'
            WHERE bvid IN (SELECT bvid FROM bq_archived)""")
        c.execute("""INSERT OR IGNORE INTO bd_term_dirty
            SELECT DISTINCT h.norm FROM bd_word_hits h JOIN bq_archived a USING(bvid)""")
        c.execute("DELETE FROM bs_pending")
        c.execute("DELETE FROM bs_inbox")
        c.execute("DELETE FROM bd_lease WHERE name='daily' OR name='stream_latest' OR name LIKE 'detail:%'")
        c.execute("""UPDATE bd_days SET status='archived',retry_after=0,
            error='历史任务已归档，自动历史扩张已关闭'""")
        c.execute("UPDATE bd_partitions SET state='archived',retry_after=0")
        c.execute("UPDATE bg_refresh SET active=0,tracked_until=0")
        c.execute("""INSERT OR REPLACE INTO bq_archive_cache
            SELECT k,v FROM kv WHERE k='bili_growth_dashboard' OR k LIKE 'bili_growth_dashboard:%'""")
        c.execute("DELETE FROM kv WHERE k='bili_growth_dashboard' OR k LIKE 'bili_growth_dashboard:%'")
        policy = {"started": now, "mode": "realtime_only", "archived": archived,
                  "completed_retained": retained}
        c.execute("INSERT OR REPLACE INTO kv VALUES('bili_collection_reset',?)",
                  (json.dumps(policy),))
        c.execute("INSERT OR REPLACE INTO kv VALUES('bili_directory_backpressure','false')")
        c.execute("INSERT OR REPLACE INTO kv VALUES('bili_latest_state',?)",
                  (json.dumps({"tracking_started": now, "status": "pending",
                               "bvids": [], "child_latest": {}, "next_poll": 0}),))
    return policy


def enqueue(c, bvid, recent=False, now=None):
    now = time.time() if now is None else now
    added = c.execute("""INSERT OR IGNORE INTO bs_pending(bvid,queued,lane,retry_after,attempts)
        SELECT bvid,?,?,detail_retry_after,detail_attempts FROM bd_videos
        WHERE bvid=? AND detail_state IN ('pending','error')""",
                      (now, "recent" if recent else "backlog", bvid)).rowcount
    if recent:
        c.execute("""UPDATE bs_pending SET lane='recent',queued=?
            WHERE bvid=? AND lane!='recent'""", (now, bvid))
    if added:
        c.execute("INSERT INTO bs_events VALUES(?,'tag_queued',1)", (now,))
    return added


def finish(c, bvid, unavailable=False):
    removed = c.execute("DELETE FROM bs_pending WHERE bvid=?", (bvid,)).rowcount
    if removed:
        kind = "tag_unavailable" if unavailable else "tag_completed"
        c.execute("INSERT INTO bs_events VALUES(?,?,1)", (time.time(), kind))


def defer_analysis(c, record):
    c.execute("""INSERT INTO bq_analysis(bvid,record,queued) VALUES(?,?,?)
        ON CONFLICT(bvid) DO UPDATE SET record=excluded.record,queued=excluded.queued,
        retry_after=0,attempts=0,error=''""",
              (record["bvid"], json.dumps(record, ensure_ascii=False), time.time()))


def analyze_once(limit=8):
    from . import bili_daily, bili_growth, bili_terms
    bili_daily.init()
    bili_growth.init()
    if not _analysis_lock.acquire(blocking=False):
        return 0
    try:
        with db.session() as c:
            rows = c.execute("""SELECT * FROM bq_analysis WHERE retry_after<=?
                ORDER BY queued LIMIT ?""", (time.time(), limit)).fetchall()
        completed = 0
        for row in rows:
            try:
                record = json.loads(row["record"])
                with db.session() as c:
                    bili_terms.seed(c, record)
                    # The original counter timestamp is saved by the network worker.
                    bili_growth.record(c, {**record, "views": None})
                    c.execute("DELETE FROM bq_analysis WHERE bvid=? AND record=?",
                              (row["bvid"], row["record"]))
                completed += 1
            except Exception as exc:
                with db.session() as c:
                    c.execute("""UPDATE bq_analysis SET attempts=attempts+1,
                        retry_after=?,error=? WHERE bvid=? AND record=?""",
                              (time.time() + min(3600, 30 * 2 ** min(row["attempts"], 6)),
                               str(exc)[:300], row["bvid"], row["record"]))
        return completed
    finally:
        _analysis_lock.release()


def recent_pressure(c, now=None):
    now = time.time() if now is None else now
    row = c.execute("""SELECT queued FROM bs_pending
        WHERE lane='recent' AND queued>=? AND retry_after<=?
        ORDER BY queued LIMIT 1""", (now - RECENT_SECONDS, now)).fetchone()
    return max(0, now - row["queued"]) if row else 0


def ready_jobs(c, lane, now, limit):
    window, args = "1", ("backlog",)
    if lane in ("recent", "expired"):
        window = "p.queued>=?" if lane == "recent" else "p.queued<?"
        args = ("recent", now - RECENT_SECONDS)
    return c.execute(READY_QUERY.format(window=window),
                     args + (now, now, now, limit)).fetchall()


def directory_pressure(c):
    if collection_policy(c).get("mode") == "realtime_only":
        return True
    high = max(100, int(config.BVIDEO_BACKLOG_HIGH_WATER))
    low = max(0, min(high - 1, int(config.BVIDEO_BACKLOG_LOW_WATER)))
    row = c.execute("SELECT v FROM kv WHERE k='bili_directory_backpressure'").fetchone()
    previous = json.loads(row["v"]) if row else False
    threshold = low if previous else high
    # Indexed/bounded probe: do not count the entire debt before every directory page.
    blocked = c.execute("SELECT 1 FROM bs_pending LIMIT 1 OFFSET ?", (threshold,)).fetchone() is not None
    if blocked != previous:
        c.execute("INSERT OR REPLACE INTO kv VALUES('bili_directory_backpressure',?)",
                  (json.dumps(blocked),))
    return blocked


def metrics(c, now=None):
    now = time.time() if now is None else now
    row = c.execute("""SELECT COUNT(*) total,
        COALESCE(SUM(p.lane='recent' AND p.queued>=?),0) recent,
        COALESCE(SUM(MAX(p.retry_after,v.detail_retry_after)>?),0) deferred,
        MIN(v.day) oldest_pending_day FROM bs_pending p JOIN bd_videos v ON v.bvid=p.bvid
        WHERE v.detail_state IN ('pending','error')""",
                    (now - RECENT_SECONDS, now)).fetchone()
    out = dict(row)
    policy = collection_policy(c)
    out.update(backlog=out["total"] - out["recent"],
               recent_oldest_seconds=round(recent_pressure(c, now), 1),
               analysis_pending=c.execute("SELECT COUNT(*) FROM bq_analysis").fetchone()[0],
               analysis_errors=c.execute("SELECT COUNT(*) FROM bq_analysis WHERE attempts>0").fetchone()[0],
               term_refresh_pending=c.execute("SELECT COUNT(*) FROM bd_term_dirty").fetchone()[0],
               directory_paused=directory_pressure(c),
               high_water=config.BVIDEO_BACKLOG_HIGH_WATER,
               low_water=config.BVIDEO_BACKLOG_LOW_WATER,
               historical_disabled=policy.get("mode") == "realtime_only",
               revision=REVISION)
    return out
