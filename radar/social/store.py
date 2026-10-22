# -*- coding: utf-8 -*-
"""社交平台数据的表和读写。表名都以 soc_ 开头，不碰别的功能的表。"""
import json
import os
import threading
import time

from .. import config, db

SCHEMA = """
CREATE TABLE IF NOT EXISTS soc_hot(platform TEXT, job TEXT, ts INTEGER, rank INTEGER, text TEXT, value REAL,
  nums TEXT, url TEXT, list_path TEXT);
CREATE INDEX IF NOT EXISTS idx_soc_hot_job_ts ON soc_hot(job, ts);
CREATE TABLE IF NOT EXISTS soc_series(platform TEXT, term TEXT, metric TEXT, day TEXT, value REAL, ts INTEGER,
  PRIMARY KEY(platform, term, metric, day));
CREATE TABLE IF NOT EXISTS soc_lookup(platform TEXT, term TEXT, ts INTEGER, ok INTEGER, summary TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS idx_soc_lookup ON soc_lookup(platform, term, ts);
CREATE TABLE IF NOT EXISTS soc_terms(norm TEXT PRIMARY KEY, term TEXT, first_ts INTEGER, last_ts INTEGER,
  seen INTEGER, sources TEXT, best_rank INTEGER, best_value REAL, cls TEXT, cls_why TEXT, age TEXT, age_why TEXT,
  label TEXT, ai TEXT, ai_ts INTEGER, updated INTEGER);
CREATE TABLE IF NOT EXISTS soc_videos(platform TEXT, vid TEXT, term TEXT, ts INTEGER, pub_ts INTEGER, plays REAL,
  comments REAL, likes REAL, shares REAL, favs REAL, PRIMARY KEY(platform, vid, ts));
CREATE INDEX IF NOT EXISTS idx_soc_videos ON soc_videos(platform, ts);
"""

JOB_NAMES = {
    "douyin_hot": "抖音热榜",
    "douyin_kw": "抖音搜索（网页）",
    "douyin_open": "抖音开放平台·视频搜索（官方）",
    "douyin_index_hot": "抖音指数·热点榜",
    "douyin_index_kw": "抖音指数·关键词",
    "bili_index_home": "哔哩指数·首页榜单",
    "bili_index_kw": "哔哩指数·关键词",
    "kuaishou_hot": "快手热榜",
    "kuaishou_kw": "快手搜索",
    "xiaohongshu_kw": "小红书搜索",
    "tiktok_hashtag": "TikTok 创意中心·热门话题",
    "tiktok_ads": "TikTok 广告库（官方，欧盟）",
}

_ready = {"done": False}
_lock = threading.Lock()
_status_lock = threading.Lock()


def ensure():
    with _lock:
        if _ready["done"]:
            return
        with db.session() as c:
            c.executescript(SCHEMA)
        _ready["done"] = True


# ---------------- 运行状态 ----------------

def status(job, ok, count=0, err="", note="", url=None):
    with _status_lock:
        _status_update(job, ok, count, err, note, url)


def _status_update(job, ok, count=0, err="", note="", url=None):
    st = db.kv_get("social_status", {}) or {}
    s = st.get(job, {})
    now = int(time.time())
    s["name"] = JOB_NAMES.get(job, job)
    s["try_ts"] = now
    s["url"] = None if ok else url        # 失败时记下页面网址：要人机验证时面板上一点就打开这个页面
    if ok:
        s.update({"ok_ts": now, "count": count, "error": "", "note": (note or "")[:300]})
    else:
        s.update({"err_ts": now, "error": (err or "")[:300], "note": (note or "")[:300]})
    st[job] = s
    db.kv_set("social_status", st)


def status_all():
    return db.kv_get("social_status", {}) or {}


def last_try(job):
    return (status_all().get(job) or {}).get("try_ts") or 0


# ---------------- 调试文件 ----------------

def debug_dump(job, digest):
    try:
        folder = os.path.join(config.DATA_DIR, "social_debug")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, job + ".json"), "w", encoding="utf-8") as f:
            json.dump(digest, f, ensure_ascii=False, indent=1)
    except (OSError, TypeError, ValueError):
        pass


# ---------------- 写 ----------------

_VALUE_HINT = ("hot", "heat", "index", "value", "score", "view", "count", "num", "pv", "uv", "热")


def main_value(nums):
    if not nums:
        return None
    for h in _VALUE_HINT:
        for k, v in nums.items():
            last = k.rsplit(".", 1)[-1].lower()
            if h in last and "rank" not in last and "#" not in last:
                return v
    vals = [v for k, v in nums.items() if "rank" not in k.lower() and "#" not in k]
    return max(vals) if vals else None


def save_hot(platform, job, lists, ts=None):
    ts = int(ts or time.time())
    rows = []
    for L in lists:
        for it in L["items"]:
            rows.append((platform, job, ts, it["rank"], it["text"][:200], main_value(it["nums"]),
                         json.dumps(dict(list(it["nums"].items())[:24]), ensure_ascii=False), it.get("url"),
                         (L.get("path") or "")[:200]))
    if rows:
        with db.session() as c:
            c.executemany("INSERT INTO soc_hot VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def save_series(platform, term, series, ts=None):
    ts = int(ts or time.time())
    rows = []
    for s in series:
        for day, v in s["points"]:
            rows.append((platform, term, s["metric"][:120], day, v, ts))
    if rows:
        with db.session() as c:
            c.executemany("INSERT OR REPLACE INTO soc_series VALUES (?,?,?,?,?,?)", rows)
    return len(rows)


def save_videos(platform, term, items, ts=None):
    """搜索结果里每条视频的数字（v81 评分用：同一条视频两次的差 = 增量；7 天内发的从发布时算）。"""
    from .extract import video_stats, stable_video_id
    ts = int(ts or time.time())
    rows = []
    for it in items:
        if not stable_video_id(platform, it):
            continue
        vid = (it.get("url") or "").split("?")[0]
        st = video_stats(it)
        if all(st[k] is None for k in ("plays", "comments", "likes", "shares", "favs")):
            continue
        rows.append((platform, vid, term, ts, it.get("ts"), st["plays"], st["comments"], st["likes"],
                     st["shares"], st["favs"]))
    if rows:
        with db.session() as c:
            c.executemany("INSERT OR REPLACE INTO soc_videos VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def save_lookup(platform, term, ok, summary=None, note=""):
    with db.session() as c:
        c.execute("INSERT INTO soc_lookup VALUES (?,?,?,?,?,?)",
                  (platform, term, int(time.time()), 1 if ok else 0,
                   json.dumps(summary or {}, ensure_ascii=False), (note or "")[:300]))


# ---------------- 读 ----------------

def latest_hot(job, limit=60):
    with db.session() as c:
        r = c.execute("SELECT MAX(ts) FROM soc_hot WHERE job = ?", (job,)).fetchone()
        if not r or not r[0]:
            return {"ts": None, "items": []}
        rows = c.execute("SELECT rank, text, value, url, list_path FROM soc_hot WHERE job = ? AND ts = ? "
                         "ORDER BY list_path, rank LIMIT ?", (job, r[0], limit)).fetchall()
    from .extract import junk_text
    return {"ts": r[0], "items": [{"rank": a, "text": b, "value": v, "url": u, "list": p}
                                 for a, b, v, u, p in rows if not junk_text(b)]}


def lookups_today(platform):
    since = time.time() - 86400
    with db.session() as c:
        r = c.execute("SELECT COUNT(*) FROM soc_lookup WHERE platform = ? AND ts >= ?", (platform, since)).fetchone()
    return r[0] if r else 0


def terms_today(platform):
    with db.session() as c:
        return {r[0] for r in c.execute("SELECT DISTINCT term FROM soc_lookup WHERE platform=? AND ts>=?",
                                       (platform, time.time() - 86400))}


def last_lookup(platform, term=None):
    with db.session() as c:
        if term is None:
            r = c.execute("SELECT MAX(ts) FROM soc_lookup WHERE platform = ?", (platform,)).fetchone()
        else:
            r = c.execute("SELECT MAX(ts) FROM soc_lookup WHERE platform = ? AND term = ?",
                          (platform, term)).fetchone()
    return (r[0] if r else 0) or 0


def lookups_for(term, limit=30):
    with db.session() as c:
        rows = c.execute("SELECT platform, ts, ok, summary, note FROM soc_lookup WHERE term = ? "
                         "ORDER BY ts DESC LIMIT ?", (term, limit)).fetchall()
    out = []
    for p, ts, ok, s, note in rows:
        try:
            s = json.loads(s or "{}")
        except ValueError:
            s = {}
        out.append({"platform": p, "name": JOB_NAMES.get(p, p), "ts": ts, "ok": bool(ok), "summary": s,
                    "note": note})
    return out


def latest_lookups(limit=80):
    with db.session() as c:
        rows = c.execute("SELECT platform, term, ts, ok, summary, note FROM soc_lookup "
                         "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for p, t, ts, ok, s, note in rows:
        try:
            s = json.loads(s or "{}")
        except ValueError:
            s = {}
        s.pop("top", None)
        out.append({"platform": p, "name": JOB_NAMES.get(p, p), "term": t, "ts": ts, "ok": bool(ok),
                    "summary": s, "note": note})
    return out


def series_for(term):
    with db.session() as c:
        rows = c.execute("SELECT platform, metric, day, value FROM soc_series WHERE term = ? ORDER BY day",
                         (term,)).fetchall()
    out = {}
    for p, m, d, v in rows:
        out.setdefault(p + " · " + m, []).append([d, v])
    return out
