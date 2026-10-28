# -*- coding: utf-8 -*-
"""社交平台数据的表和读写。表名都以 soc_ 开头，不碰别的功能的表。"""
import json
import os
import threading
import time

from .. import config, db
from ..sources.bili import normalize
from .validation import VERSION

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
CREATE TABLE IF NOT EXISTS soc_verified(
 kind TEXT,record_id INTEGER,version INTEGER,evidence TEXT,
 PRIMARY KEY(kind,record_id));
"""

JOB_NAMES = {
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

_ready = set()
_lock = threading.Lock()


def ensure():
    with _lock:
        if config.DB_PATH in _ready:
            return
        with db.session() as c:
            c.executescript(SCHEMA)
            _remove_douyin_hot(c)
        _ready.add(config.DB_PATH)


def _remove_douyin_hot(c):
    """Remove only the retired general hot-list source and its derived evidence."""
    marker = "social:removed_douyin_hot:v1"
    if c.execute("SELECT 1 FROM kv WHERE k=?", (marker,)).fetchone():
        return
    c.execute("DELETE FROM soc_verified WHERE kind='hot' AND record_id IN "
              "(SELECT rowid FROM soc_hot WHERE job='douyin_hot')")
    c.execute("DELETE FROM soc_hot WHERE job='douyin_hot'")
    affected = []
    for row in c.execute("SELECT norm,sources,label FROM soc_terms").fetchall():
        sources = json.loads(row["sources"] or "[]")
        if "douyin_hot" not in sources:
            continue
        sources = [s for s in sources if s != "douyin_hot"]
        if not sources and not row["label"]:
            c.execute("DELETE FROM soc_terms WHERE norm=?", (row["norm"],))
        else:
            affected.append(row["norm"])
            c.execute("UPDATE soc_terms SET sources=?,best_rank=NULL,best_value=NULL WHERE norm=?",
                      (json.dumps(sources), row["norm"]))
    if affected:
        metrics = {}
        for row in c.execute(f"SELECT text,rank,value FROM soc_hot h WHERE {trusted_hot_sql()}"):
            key = normalize((row["text"] or "").strip().lstrip("#").strip())
            rank, value = metrics.get(key, (None, None))
            metrics[key] = (min(rank, row["rank"]) if rank is not None else row["rank"],
                            max(value, row["value"]) if value is not None else row["value"])
        for key in affected:
            rank, value = metrics.get(key, (None, None))
            c.execute("UPDATE soc_terms SET best_rank=?,best_value=? WHERE norm=?", (rank, value, key))
    saved = c.execute("SELECT v FROM kv WHERE k='social_status'").fetchone()
    if saved:
        status_data = json.loads(saved[0] or "{}")
        status_data.pop("douyin_hot", None)
        c.execute("UPDATE kv SET v=? WHERE k='social_status'", (json.dumps(status_data),))
    c.execute("DELETE FROM kv WHERE k='sampling:social:douyin_hot'")
    c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (marker, "true"))


def _verify(c, kind, record_id, evidence):
    c.execute("INSERT OR REPLACE INTO soc_verified VALUES(?,?,?,?)",
              (kind, record_id, VERSION, json.dumps(evidence, ensure_ascii=False)))


def trusted_hot_sql(alias="h"):
    return (f"({alias}.job!='douyin_hot' AND EXISTS(SELECT 1 FROM soc_verified e WHERE e.kind='hot' "
            f"AND e.record_id={alias}.rowid AND e.version={VERSION} "
            f"AND json_extract(e.evidence,'$.job')={alias}.job "
            f"AND json_extract(e.evidence,'$.ts')={alias}.ts "
            f"AND json_extract(e.evidence,'$.text')={alias}.text "
            f"AND json_extract(e.evidence,'$.rank')={alias}.rank "
            f"AND json_extract(e.evidence,'$.value')={alias}.value "
            f"AND json_extract(e.evidence,'$.list_path')={alias}.list_path))")


# ---------------- 运行状态 ----------------

def status(job, ok, count=0, err="", note="", url=None, validation_version=0):
    st = db.kv_get("social_status", {}) or {}
    s = st.get(job, {})
    now = int(time.time())
    s["name"] = JOB_NAMES.get(job, job)
    s["try_ts"] = now
    s["last_ok"] = bool(ok)
    s["validation_version"] = validation_version
    s["url"] = None if ok else url        # 失败时记下页面网址：要人机验证时面板上一点就打开这个页面
    if ok:
        s.update({"ok_ts": now, "count": count, "error": "", "note": (note or "")[:300]})
    else:
        s.update({"err_ts": now, "error": (err or "")[:300], "note": (note or "")[:300]})
    st[job] = s
    db.kv_set("social_status", st)


def status_all():
    return {k: v for k, v in (db.kv_get("social_status", {}) or {}).items() if k != "douyin_hot"}


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
    if job == "douyin_hot":
        raise ValueError("Removed ranking: douyin_hot")
    ensure()
    ts = int(ts or time.time())
    rows = []
    for L in lists:
        if L.get("validation_version") != VERSION or not L.get("contract") or not L.get("source_url"):
            raise ValueError("Unverified ranking rejected")
        for it in L["items"]:
            if it.get("rank") is None or it.get("value") is None:
                raise ValueError("Ranking omitted its official rank or metric")
            rows.append(((platform, job, ts, it["rank"], it["text"][:200], it["value"],
                         json.dumps(dict(list(it["nums"].items())[:24]), ensure_ascii=False), it.get("url"),
                         (L.get("path") or "")[:200]),
                         {k: L[k] for k in ("contract", "source_url", "name")}))
    if rows:
        with db.session() as c:
            for values, evidence in rows:
                record = c.execute("INSERT INTO soc_hot VALUES (?,?,?,?,?,?,?,?,?)", values)
                _verify(c, "hot", record.lastrowid, dict(
                    evidence, job=job, ts=ts, text=values[4], rank=values[3],
                    value=values[5], list_path=values[8]))
    return len(rows)


def save_series(platform, term, series, ts=None):
    ensure()
    if any(s.get("validation_version") != VERSION or s.get("term") != term for s in series):
        raise ValueError("Unverified keyword series rejected")
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
    from .extract import video_stats
    ensure()
    if any(it.get("validation_version") != VERSION or it.get("term") != term
           or not it.get("vid") for it in items):
        raise ValueError("Unverified search attribution or video identity rejected")
    ts = int(ts or time.time())
    rows = []
    for it in items:
        vid = it["vid"]
        st = video_stats(it)
        if all(st[k] is None for k in ("plays", "comments", "likes", "shares", "favs")):
            continue
        rows.append((platform, vid, term, ts, it.get("ts"), st["plays"], st["comments"], st["likes"],
                     st["shares"], st["favs"]))
    if rows:
        with db.session() as c:
            for row in rows:
                saved = c.execute("INSERT OR REPLACE INTO soc_videos VALUES (?,?,?,?,?,?,?,?,?,?)", row)
                _verify(c, "video", saved.lastrowid, {"term": term, "platform": platform})
    return len(rows)


def save_lookup(platform, term, ok, summary=None, note=""):
    ensure()
    if ok and (summary or {}).get("validation_version") != VERSION:
        ok, summary, note = False, {}, "来源及查询词归属未验证，结果未采用"
    with db.session() as c:
        row = c.execute("INSERT INTO soc_lookup VALUES (?,?,?,?,?,?)",
                        (platform, term, int(time.time()), 1 if ok else 0,
                         json.dumps(summary or {}, ensure_ascii=False), (note or "")[:300]))
        _verify(c, "lookup", row.lastrowid, {"validated_result": bool(ok)})


# ---------------- 读 ----------------

def latest_hot(job, limit=100000):
    ensure()
    if job == "douyin_hot":
        return {"ts": None, "items": [], "validation_version": VERSION}
    with db.session() as c:
        r = c.execute(f"SELECT MAX(ts) FROM soc_hot h WHERE job=? AND {trusted_hot_sql()}", (job,)).fetchone()
        if not r or not r[0]:
            return {"ts": None, "items": [], "validation_version": VERSION}
        rows = c.execute(f"""SELECT h.rank,h.text,h.value,h.url,h.list_path,e.evidence
            FROM soc_hot h JOIN soc_verified e ON e.kind='hot' AND e.record_id=h.rowid
            WHERE h.job=? AND h.ts=? AND e.version=? AND {trusted_hot_sql()}
            ORDER BY h.list_path,h.rank LIMIT ?""", (job, r[0], VERSION, limit)).fetchall()
        previous = {}
        for text, path, value, rank, ts in c.execute(f"""
            SELECT text,list_path,value,rank,ts FROM (
                SELECT h.*,ROW_NUMBER() OVER(PARTITION BY text,list_path
                    ORDER BY ABS(?-ts-86400),ts DESC) rn
                FROM soc_hot h WHERE job=? AND ts BETWEEN ? AND ?
                AND {trusted_hot_sql()}
            ) WHERE rn=1""", (r[0], job, r[0] - 259200, r[0] - 3600)):
            previous[(text, path)] = (value, rank, ts)
        terms = {t: (age, cls) for t, age, cls in c.execute("SELECT term,age,cls FROM soc_terms")}
    items = []
    for a, b, v, u, p, evidence in rows:
        prev = previous.get((b, p))
        delta = v - prev[0] if prev else None
        growth = delta / prev[0] if prev and prev[0] > 0 else None
        age, cls = terms.get(b, ("", ""))
        items.append({"rank": a, "text": b, "value": v, "url": u, "list": p,
                      **json.loads(evidence), "delta": delta, "growth": growth,
                      "previous_ts": prev[2] if prev else None,
                      "old": age == "old", "classification": cls})
    return {"ts": r[0], "validation_version": VERSION,
            "items": items}


def lookups_today(platform):
    since = time.time() - 86400
    with db.session() as c:
        r = c.execute("SELECT COUNT(*) FROM soc_lookup WHERE platform = ? AND ts >= ?", (platform, since)).fetchone()
    return r[0] if r else 0


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
        rows = c.execute("SELECT platform, ts, ok, summary, note FROM soc_lookup l WHERE term = ? "
                         "AND EXISTS(SELECT 1 FROM soc_verified e WHERE e.kind='lookup' "
                         "AND e.record_id=l.rowid AND e.version=1) "
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
        rows = c.execute("SELECT platform, term, ts, ok, summary, note FROM soc_lookup l "
                         "WHERE EXISTS(SELECT 1 FROM soc_verified e WHERE e.kind='lookup' "
                         "AND e.record_id=l.rowid AND e.version=1) "
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
    # No keyword-series adapter has passed source and query attribution checks.
    return {}


def legacy_counts():
    ensure()
    with db.session() as c:
        counts = {kind: c.execute(
            f"SELECT COUNT(*) FROM {table} t WHERE NOT EXISTS(SELECT 1 FROM soc_verified e "
            "WHERE e.kind=? AND e.record_id=t.rowid AND e.version=?)", (kind, VERSION)).fetchone()[0]
                for kind, table in (("hot", "soc_hot"), ("lookup", "soc_lookup"),
                                    ("series", "soc_series"), ("video", "soc_videos"))}
        counts["hot"] = c.execute(
            f"SELECT COUNT(*) FROM soc_hot t WHERE NOT {trusted_hot_sql('t')}").fetchone()[0]
        return counts


def _unverified_series_for(term):
    with db.session() as c:
        rows = c.execute("SELECT platform, metric, day, value FROM soc_series WHERE term = ? ORDER BY day",
                         (term,)).fetchall()
    out = {}
    for p, m, d, v in rows:
        out.setdefault(p + " · " + m, []).append([d, v])
    return out
