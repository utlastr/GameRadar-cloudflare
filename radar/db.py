"""本地数据库（SQLite，一个文件，不用装数据库软件）。

历史数据存储格式：一行一条记录 = 时间、平台、游戏或关键词、指标名、数值（metrics 表）。
加新平台、新指标不用改结构。
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    source TEXT, entity TEXT, name TEXT, first_seen INTEGER, last_seen INTEGER, extra TEXT,
    PRIMARY KEY (source, entity)
);
CREATE TABLE IF NOT EXISTS metrics (
    ts INTEGER, source TEXT, entity TEXT, metric TEXT, value REAL,
    PRIMARY KEY (source, entity, metric, ts)
);
CREATE INDEX IF NOT EXISTS idx_metrics_sm_ts ON metrics(source, metric, ts);
CREATE TABLE IF NOT EXISTS metrics_hourly (
    hour_ts INTEGER, source TEXT, entity TEXT, metric TEXT, vmin REAL, vavg REAL, vmax REAL,
    PRIMARY KEY (source, entity, metric, hour_ts)
);
CREATE TABLE IF NOT EXISTS anomaly (
    source TEXT, entity TEXT, metric TEXT, cusum REAL, last_ts INTEGER,
    PRIMARY KEY (source, entity, metric)
);
CREATE TABLE IF NOT EXISTS labels (
    member TEXT PRIMARY KEY, stars INTEGER, blocked INTEGER DEFAULT 0, note TEXT,
    aliases TEXT, keywords TEXT, updated INTEGER
);
CREATE TABLE IF NOT EXISTS links (
    a TEXT, b TEXT, relation TEXT, updated INTEGER, PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS supply (
    target TEXT, term TEXT, checked_at INTEGER, result TEXT, PRIMARY KEY (target, term)
);
CREATE TABLE IF NOT EXISTS alerts (akey TEXT, ts INTEGER, score REAL, level TEXT);
CREATE TABLE IF NOT EXISTS scores (
    ts INTEGER, akey TEXT, name TEXT, final REAL, hot REAL, imit REAL, win REAL,
    PRIMARY KEY (ts, akey)
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS runs (ts INTEGER, job TEXT, seconds REAL, ok INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS changelog (ts INTEGER, source TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS games (gid TEXT PRIMARY KEY, name TEXT, n INTEGER, created INTEGER, updated INTEGER);
CREATE TABLE IF NOT EXISTS game_members (
    member TEXT PRIMARY KEY, gid TEXT, name TEXT, method TEXT, evidence TEXT, ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_game_members_gid ON game_members(gid);
CREATE TABLE IF NOT EXISTS archive_decisions (
    a TEXT, b TEXT, relation TEXT, pattern TEXT, origin TEXT, ts INTEGER, PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS archive_queue (
    a TEXT, b TEXT, pattern TEXT, evidence TEXT, rank REAL, ts INTEGER, PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS archive_log (ts INTEGER, action TEXT, a TEXT, b TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS revisit (
    member TEXT PRIMARY KEY, last_ts INTEGER, interval_h REAL, n INTEGER, last_v REAL,
    hist TEXT, sig TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS steam_summary (
    day TEXT, scope TEXT, item_key TEXT, name TEXT, rank INTEGER, value REAL, delta REAL, extra TEXT,
    PRIMARY KEY (day, scope, item_key)
);
"""


_ready = set()


def _connect():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fresh = not os.path.exists(config.DB_PATH)
    conn = sqlite3.connect(config.DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    if os.environ.get("RADAR_SQLITE_JOURNAL_MODE", "").upper() == "WAL":
        conn.execute("PRAGMA journal_mode=WAL")
    if fresh or config.DB_PATH not in _ready:
        conn.executescript(SCHEMA)       # 表不在就建上（只加不删，已有数据不动）
        _ready.add(config.DB_PATH)
    return conn


def _retry_missing(fn):
    """碰到 no such table：重新建表再试一次，不让整个程序停下来。"""
    try:
        return fn()
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e):
            raise
        _ready.discard(config.DB_PATH)
        init()
        return fn()


@contextmanager
def session():
    """用法：with db.session() as c: c.execute(...)  结束时自动保存并关闭。"""
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init():
    with session() as c:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(labels)")}
        if "dismissed" not in cols:
            # 你点"先不看"时的热度；热度涨到它的 DISMISS_BACK 倍会重新出现。0 = 没点过
            c.execute("ALTER TABLE labels ADD COLUMN dismissed REAL DEFAULT 0")


def upsert_entities(c, source, ts, items):
    for it in items:
        extra = it.get("extra")
        if source in ("haoyou", "taptap", "tapmaker") and extra is not None:
            from .mobile_details import preserve
            old = c.execute("SELECT extra FROM entities WHERE source=? AND entity=?",
                            (source, str(it["entity"]))).fetchone()
            if old:
                extra = preserve(extra, json.loads(old[0] or "{}"))
        c.execute(
            """INSERT INTO entities(source, entity, name, first_seen, last_seen, extra)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(source, entity) DO UPDATE SET
                 name = excluded.name, last_seen = excluded.last_seen,
                 extra = COALESCE(excluded.extra, entities.extra)""",
            (source, str(it["entity"]), it.get("name") or str(it["entity"]), ts, ts,
             json.dumps(extra, ensure_ascii=False) if extra is not None else None))


def source_first_runs():
    """每个平台第一次抓到数据的时间。v30 起新接的平台比程序晚很多天才开始抓，
    "开始运行时就有""新出现"要按这个平台自己开始抓的时间算，不能按程序第一次运行的时间。"""
    base = kv_get("first_run") or 0
    with session() as c:
        rows = c.execute("SELECT source, MIN(first_seen) FROM entities GROUP BY source").fetchall()
    return {s: max(base, v or 0) for s, v in rows}


def put_metrics(c, ts, source, rows):
    c.executemany(
        "INSERT OR REPLACE INTO metrics(ts, source, entity, metric, value) VALUES (?, ?, ?, ?, ?)",
        [(ts, source, str(e), m, float(v)) for e, m, v in rows if v is not None])


def kv_get(k, default=None):
    def go():
        with session() as c:
            return c.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
    r = _retry_missing(go)
    return json.loads(r[0]) if r else default


def kv_set(k, v):
    def go():
        with session() as c:
            c.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)",
                      (k, json.dumps(v, ensure_ascii=False)))
    _retry_missing(go)


def log_run(job, seconds, ok, detail=""):
    with session() as c:
        c.execute("INSERT INTO runs(ts, job, seconds, ok, detail) VALUES (?, ?, ?, ?, ?)",
                  (int(time.time()), job, seconds, 1 if ok else 0, detail[:2000]))


def load_entities():
    """返回 {(source, entity): {"name", "first_seen", "last_seen", "extra"}}"""
    out = {}
    with session() as c:
        for r in c.execute("SELECT * FROM entities"):
            try:
                extra = json.loads(r["extra"]) if r["extra"] else {}
            except ValueError:
                extra = {}
            out[(r["source"], r["entity"])] = {
                "name": r["name"], "first_seen": r["first_seen"],
                "last_seen": r["last_seen"], "extra": extra}
    return out


def load_labels():
    out = {}
    with session() as c:
        for r in c.execute("SELECT * FROM labels"):
            out[r["member"]] = {
                "stars": r["stars"], "blocked": bool(r["blocked"]), "note": r["note"] or "",
                "aliases": json.loads(r["aliases"]) if r["aliases"] else [],
                "keywords": json.loads(r["keywords"]) if r["keywords"] else [],
                "dismissed": (r["dismissed"] if "dismissed" in r.keys() else 0) or 0,
                "updated": r["updated"] or 0}
    return out


def save_label(member, **fields):
    cur = load_labels().get(member, {"stars": None, "blocked": False, "note": "",
                                     "aliases": [], "keywords": [], "dismissed": 0})
    cur.update({k: v for k, v in fields.items() if v is not None})
    with session() as c:
        c.execute(
            """INSERT OR REPLACE INTO labels(member, stars, blocked, note, aliases, keywords, updated, dismissed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (member, cur.get("stars"), 1 if cur.get("blocked") else 0, cur.get("note") or "",
             json.dumps(cur.get("aliases") or [], ensure_ascii=False),
             json.dumps(cur.get("keywords") or [], ensure_ascii=False), int(time.time()),
             float(cur.get("dismissed") or 0)))


def load_links():
    with session() as c:
        return [dict(r) for r in c.execute("SELECT * FROM links")]


def save_link(a, b, relation):
    a, b = sorted([a, b])
    with session() as c:
        c.execute("INSERT OR REPLACE INTO links(a, b, relation, updated) VALUES (?, ?, ?, ?)",
                  (a, b, relation, int(time.time())))
