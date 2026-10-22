"""B站：分区新视频发现、视频明细跟踪、UP 主平时水平、评论和弹幕抽样。

请求全部走 sources/bili.py（v72 起程序里只有这一个 B站 客户端，风控暂停全程序共用）。
接口实测结果见 docs/OPP_API_PROBE.md：dynamic/region 已下线，用 newlist?rid=4；
空间视频接口必须带 dm_img_* 参数，否则 -352。
"""
import random
import time

from .. import activity, config, db
from ..sources import bili
from . import budget_left, budget_use, ensure, status
from . import gamemap

TIDS = tuple(sorted(bili.GAME_TIDS - {4}))


def _sleep(lo=0.8, hi=1.8):
    time.sleep(random.uniform(lo, hi))


def _budget_ok():
    if budget_left() <= 0:
        return False
    budget_use()
    return True


# ---------------- 发现新视频 ----------------
def discover_once(pages=20, window=None):
    """Scan the newest feed to the persisted overlap boundary, with explicit gaps."""
    from ..social import incremental
    ensure()
    if budget_left() < 20 or bili.paused():
        return None
    bili.warmup()
    started = int(time.time())
    progress = db.kv_get("publication_bili_discovery", {}) or {}
    boundary = int(progress.get("watermark") or started - 86400) - 3600
    if window:
        boundary = window[0]
        if progress.get("window") != list(window):
            progress = {}
    pass_started = int(progress.get("pass_started") or started)
    resume = max(1, int(progress.get("next_page") or 1) - 1)
    page_numbers = list(dict.fromkeys([1] + list(range(resume, resume + pages))))[:pages]
    seen, new, err = {}, 0, ""
    reached = False
    continuation = False
    next_page = resume
    for pn in page_numbers:
        if not _budget_ok():
            err = "请求预算不足，本轮时间区间未扫完"
            break
        try:
            arr = bili.newlist(pn)
        except bili.RiskError as e:
            err = str(e)
            break
        except Exception as e:                   # noqa: BLE001
            err = str(e)
            next_page = pn
            break
        if not arr:
            reached = True
            break
        for a in arr:
            seen[a.get("bvid")] = a
        next_page = pn + 1
        dates = [int(a.get("pubdate") or 0) for a in arr]
        if dates and all(0 < t <= boundary for t in dates):
            reached = True
            break
        _sleep()
    for bvid, a in seen.items():
        if bvid and _upsert_video(a):
            new += 1
    total = _count_videos()
    if not reached and not err:
        continuation = True
        err = "达到每轮页数上限，存在未覆盖区间"
    items = [{"vid": b, "text": bili.video_record(a)["title"], "ts": a.get("pubdate"),
              "author_id": str((a.get("owner") or {}).get("mid") or ""),
              "url": "https://www.bilibili.com/video/" + b} for b, a in seen.items() if b]
    added = incremental.ingest("bilibili", items)
    with db.session() as c:
        for it in items:
            a = seen[it["vid"]]
            for member, name, method in gamemap.match(it["text"], a.get("desc") or ""):
                c.execute("INSERT OR IGNORE INTO pub_topics VALUES (?,?,?,?)",
                          ("bilibili", it["vid"], name, method))
    incremental.record_run("bilibili_newlist", started, items, added,
                           "feed_overlap" if reached and not err else "feed_gap", err)
    # A capped or failed scan must not advance past content we have not read.
    if reached and not err:
        db.kv_set("publication_bili_discovery", {"watermark": pass_started, "completed": int(time.time())})
    else:
        db.kv_set("publication_bili_discovery", dict(progress, next_page=next_page,
                                                     pass_started=pass_started, continuation=continuation,
                                                     window=list(window) if window else None))
    if err and not seen:
        status("bili_track", False, total, err)
    else:
        status("bili_track", True, total, "", "本轮新增 %d 条 / 抓到 %d 条；%s" %
               (new, len(seen), err or "已到达上次采集的重叠边界（非全站覆盖保证）"))
    if new:
        activity.say("B站：分区新增 %d 条视频（库里共 %d 条）" % (new, total))
    return None if window and err else new


def _upsert_video(a):
    bvid = a.get("bvid")
    if not bvid:
        return False
    owner = a.get("owner") or {}
    stat = a.get("stat") or {}
    title = bili.video_record(a)["title"]
    now = int(time.time())
    with db.session() as c:
        if c.execute("SELECT 1 FROM opp_videos WHERE bvid = ?", (bvid,)).fetchone():
            return False
        c.execute("INSERT INTO opp_videos(bvid, aid, cid, tid, mid, title, tags, pubdate, duration, "
                  "first_seen, track_until, descr, owner) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (bvid, a.get("aid"), a.get("cid"), a.get("tid"), str(owner.get("mid") or ""),
                   title, None, int(a.get("pubdate") or 0), a.get("duration"), now, 0,
                   a.get("desc") or "", owner.get("name") or ""))
        _sample(c, bvid, now, stat)
    return True


def _sample(c, bvid, ts, s):
    c.execute("INSERT OR REPLACE INTO opp_vsamples(bvid, ts, view, lk, coin, fav, share, reply, dm) "
              "VALUES (?,?,?,?,?,?,?,?,?)",
              (bvid, ts, int(s.get("view") or 0), int(s.get("like") or 0), int(s.get("coin") or 0),
               int(s.get("favorite") or 0), int(s.get("share") or 0), int(s.get("reply") or 0),
               int(s.get("danmaku") or 0)))


def _count_videos():
    with db.session() as c:
        return c.execute("SELECT COUNT(*) FROM opp_videos").fetchone()[0]


# ---------------- 视频明细跟踪 ----------------
def _sample_gap(age_sec):
    """0–72 小时每 2 小时，之后每天，21 天停（工程参数）。"""
    return 2 * 3600 if age_sec <= 72 * 3600 else 24 * 3600


def due_videos(limit=200):
    """该采样的视频，按每小时播放快的优先。"""
    now = int(time.time())
    with db.session() as c:
        rows = c.execute(
            "SELECT v.bvid, v.aid, v.cid, v.pubdate, v.mid, s.last_ts, s.last_view FROM opp_videos v "
            "LEFT JOIN (SELECT bvid, MAX(ts) AS last_ts, MAX(view) AS last_view FROM opp_vsamples GROUP BY bvid) s "
            "ON s.bvid = v.bvid WHERE v.pubdate > ? ORDER BY v.pubdate DESC LIMIT 4000",
            (now - 21 * 86400,)).fetchall()
        # v82：按名字搜到的新游戏视频（评分要用）不受上面 4000 条的限制，排最前
        found = {r[0] for r in c.execute("SELECT DISTINCT bvid FROM opp_vmap WHERE method = 'search'")}
        have = {r["bvid"] for r in rows}
        rows = list(rows) + [r for r in c.execute(
            "SELECT v.bvid, v.aid, v.cid, v.pubdate, v.mid, s.last_ts, s.last_view FROM opp_videos v "
            "LEFT JOIN (SELECT bvid, MAX(ts) AS last_ts, MAX(view) AS last_view FROM opp_vsamples GROUP BY bvid) s "
            "ON s.bvid = v.bvid WHERE v.pubdate > ? AND v.bvid IN (SELECT bvid FROM opp_vmap WHERE method = 'search')",
            (now - 21 * 86400,)) if r["bvid"] not in have]
    skip = set((db.kv_get("revisit_skip") or {}).keys()) if getattr(config, "REVISIT_ENABLED", True) else set()
    gone = set()
    if skip:
        with db.session() as c:
            maps = {}
            for b, g in c.execute("SELECT m.bvid, COALESCE(gm.gid, m.gid) FROM opp_vmap m "
                                  "LEFT JOIN game_members gm ON gm.member = m.gid"):
                maps.setdefault(b, set()).add(g)
            arc = {}
            for m, g in c.execute("SELECT member, gid FROM game_members"):
                arc.setdefault(g, set()).add(m)
        for b, gs in maps.items():
            if all(arc.get(g, {g}) & skip for g in gs):
                gone.add(b)              # 只对应到"已有官方手机版 / 大作"的档案：不再逐条跟踪
    out = []
    for r in rows:
        pub = int(r["pubdate"] or now)
        age = now - pub
        if age < 0 or r["bvid"] in gone or now - int(r["last_ts"] or 0) < _sample_gap(age):
            continue
        out.append(((r["bvid"] not in found, -(r["last_view"] or 0) / max(1.0, age / 3600.0)), r))
    out.sort(key=lambda x: x[0])
    return [r for _s, r in out[:limit]]


def track_once(limit=200):
    ensure()
    if budget_left() < 10 or bili.paused():
        return 0
    n, err = 0, ""
    for r in due_videos(limit):
        if budget_left() <= 2 or not _budget_ok():
            break
        try:
            x = bili.view(r["bvid"])
        except bili.RiskError as e:
            err = str(e)
            break
        except Exception as e:                   # noqa: BLE001
            err = str(e)
            continue
        with db.session() as c:
            _sample(c, r["bvid"], int(time.time()), x.get("stat") or {})
            c.execute("UPDATE opp_videos SET aid = COALESCE(?, aid), cid = COALESCE(?, cid), "
                      "descr = CASE WHEN ? != '' THEN ? ELSE descr END WHERE bvid = ?",
                      (x.get("aid"), x.get("cid"), x.get("desc") or "", x.get("desc") or "", r["bvid"]))
        n += 1
        _sleep()
    if n:
        activity.say("B站：采样 %d 条视频" % n)
    status("bili_sample", not err or n > 0, n, err if not n else "", err if n else "")
    uploader_refresh()
    return n


# ---------------- UP 主平时水平 ----------------
UPLOADER_TTL = 7 * 86400


def uploader_refresh(limit=8):
    """每个 UP 主 7 天更新一次：粉丝数 + 最近发布超过 7 天的视频播放中位数。"""
    now = int(time.time())
    with db.session() as c:
        rows = c.execute(
            "SELECT v.mid, COUNT(*) AS n FROM opp_videos v LEFT JOIN opp_uploaders u ON u.mid = v.mid "
            "WHERE v.mid != '' AND (u.ts IS NULL OR u.ts < ?) GROUP BY v.mid ORDER BY n DESC LIMIT ?",
            (now - UPLOADER_TTL, limit)).fetchall()
    n = 0
    for r in rows:
        if budget_left() <= 3 or bili.paused():
            break
        try:
            followers, median, base_n, est = _uploader_stats(r["mid"])
        except bili.RiskError:
            break
        except Exception:                        # noqa: BLE001
            continue
        with db.session() as c:
            c.execute("INSERT OR REPLACE INTO opp_uploaders(mid, name, followers, base_median, base_n, "
                      "base_est, ts) VALUES (?,?,?,?,?,?,?)", (r["mid"], "", followers, median, base_n, est, now))
        n += 1
    return n


def _uploader_stats(mid):
    """返回 (粉丝数, 平时中位数, 样本数, 是否估计)。"""
    _budget_ok()
    followers = 0
    try:
        followers = bili.card(mid)["follower"]
    except bili.RiskError:
        raise
    except Exception:                            # noqa: BLE001
        pass
    _sleep()
    median, base_n, est = 0, 0, 0
    try:
        _budget_ok()
        cutoff = time.time() - 7 * 86400
        plays = [int(v.get("play") or 0) for v in bili.space_videos(mid) if (v.get("created") or 0) < cutoff][:10]
        if plays:
            plays.sort()
            median, base_n = plays[len(plays) // 2], len(plays)
    except bili.RiskError:
        raise
    except Exception:                            # noqa: BLE001
        pass
    if not median:
        with db.session() as c:
            r = c.execute("SELECT s.view FROM opp_vsamples s JOIN opp_videos v ON v.bvid = s.bvid "
                          "WHERE v.mid = ? ORDER BY s.ts DESC LIMIT 20", (mid,)).fetchall()
        plays = sorted(int(x[0] or 0) for x in r if x[0])
        if plays:
            median, base_n, est = plays[len(plays) // 2], len(plays), 1
    return followers, median, base_n, est


# ---------------- 评论和弹幕 ----------------
DEMAND_DEFAULT = {
    "mobile": "手机|手游|安卓|苹果|ios|移动端|手机版|手机上|能在手机",
    "name": "叫什么|什么游戏|游戏名|哪里下|在哪下|怎么下|多少钱|steam上",
    "want": "想玩|求链接|好想玩|买了|已购|入了",
}
DEMAND_AT_HOURS = (12, 24, 72)


def _words():
    w = getattr(config, "OPP_DEMAND_WORDS", None)
    return w if isinstance(w, dict) and w else DEMAND_DEFAULT


def _count_words(texts):
    import re
    pats = {}
    for k, src in _words().items():
        try:
            pats[k] = re.compile(src, re.I)
        except re.error:
            continue
    hit = {k: 0 for k in pats}
    for t in texts:
        for k, p in pats.items():
            if p.search(t or ""):
                hit[k] += 1
    return len(texts), hit.get("mobile", 0), hit.get("name", 0), hit.get("want", 0)


def _need_demand(row, now):
    pub = int(row["pubdate"] or now)
    age = (now - pub) / 3600.0
    with db.session() as c:
        got = {r[0] for r in c.execute("SELECT DISTINCT ts FROM opp_demand WHERE bvid = ?", (row["bvid"],))}
    for h in DEMAND_AT_HOURS:
        if age >= h - 1 and not any(abs(ts - (pub + h * 3600)) < 3 * 3600 for ts in got):
            return True
    return False


def demand_once(limit=10):
    ensure()
    if bili.paused():
        return 0
    now = int(time.time())
    with db.session() as c:
        rows = c.execute("SELECT bvid, aid, cid, pubdate FROM opp_videos WHERE pubdate > ? "
                         "ORDER BY pubdate DESC LIMIT 300", (now - 5 * 86400,)).fetchall()
    n = 0
    for r in rows:
        if n >= limit or budget_left() <= 4:
            break
        if not r["aid"] or not _need_demand(r, now):
            continue
        try:
            texts = bili.replies(r["aid"], between=_budget_ok)
            with db.session() as c:
                c.execute("INSERT OR REPLACE INTO opp_demand(bvid, ts, kind, n_total, n_mobile, n_name, n_want) "
                          "VALUES (?,?,?,?,?,?,?)", (r["bvid"], now, "reply") + _count_words(texts))
            if r["cid"]:
                dtexts = bili.danmaku(r["cid"])
                with db.session() as c:
                    c.execute("INSERT OR REPLACE INTO opp_demand(bvid, ts, kind, n_total, n_mobile, n_name, n_want) "
                              "VALUES (?,?,?,?,?,?,?)", (r["bvid"], now, "dm") + _count_words(dtexts))
            n += 1
        except bili.RiskError:
            break
        except Exception:                        # noqa: BLE001
            pass
        _sleep(1.2, 2.5)
    if n:
        status("bili_reply", True, n)
    return n


# ---------------- 对应关系 ----------------
def map_once():
    n = gamemap.apply_all()
    k = gamemap.update_unknown()
    if n or k:
        activity.say("B站：新增视频对应 %d 条，新登记 B站 话题 %d 个" % (n, k))
    return n


SEARCH_EVERY = 86400            # 工程参数：同一款新游戏一天在 B站 搜一次


def search_games():
    """v82：必看的新游戏（analyze 写的 kv radar_bili_terms）按名字搜 B站 最近 21 天的游戏区视频，
    标题或标签里有这个名字的记下来（opp_vmap 方法 search，对应到这款游戏的档案成员），之后照常逐条采样。"""
    from .. import match
    ensure()
    n_max = int(getattr(config, "BILI_GAME_SEARCH_PER_RUN", 10) or 0)
    if n_max <= 0 or budget_left() < 20 or bili.paused():
        return 0
    terms = db.kv_get("radar_bili_terms", []) or []
    done = db.kv_get("bili_game_search", {}) or {}
    now = int(time.time())
    n, hits, err = 0, 0, ""
    for t in terms:
        if n >= n_max or budget_left() < 10:
            break
        if now - int(done.get(t["gid"]) or 0) < SEARCH_EVERY:
            continue
        keys = [k for k in (bili.normalize(x) for x in t.get("names") or []) if match.linkable(k)]
        if not keys:
            continue
        if not _budget_ok():
            break
        try:
            res = bili.search(t["names"][0], beg=now - 21 * 86400, order="pubdate")
        except bili.RiskError as e:
            err = str(e)
            break
        except Exception as e:                   # noqa: BLE001
            err = str(e)
            res = {"list": []}
        done[t["gid"]] = now
        n += 1
        for v in res["list"]:
            text = bili.normalize((v.get("title") or "") + (v.get("tag") or ""))
            if not any(k in text for k in keys):
                continue
            with db.session() as c:
                if not c.execute("SELECT 1 FROM opp_videos WHERE bvid = ?", (v["bvid"],)).fetchone():
                    c.execute("INSERT INTO opp_videos(bvid, aid, cid, tid, mid, title, tags, pubdate, duration, "
                              "first_seen, track_until, descr, owner) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (v["bvid"], None, None, v.get("tid"), v.get("mid") or "", v.get("title") or "", None,
                               int(v.get("pubdate") or 0), None, now, 0, v.get("desc") or "", v.get("up") or ""))
                c.execute("INSERT OR IGNORE INTO opp_vmap(bvid, gid, method, conf) VALUES (?, ?, 'search', NULL)",
                          (v["bvid"], t["member"]))
            hits += 1
        _sleep()
    db.kv_set("bili_game_search", {g: ts for g, ts in done.items() if now - int(ts or 0) < 7 * 86400})
    if n:
        activity.say("B站：按名字搜了 %d 款新游戏，对上 %d 条视频" % (n, hits))
    if err and not n:
        activity.say("B站按名字搜失败：%s" % err[:100])
    return n


def search_followers(term, pages=1):
    """按游戏名搜最新视频，找没见过的跟风视频。"""
    out = []
    for pn in range(1, pages + 1):
        if not _budget_ok():
            break
        try:
            r = bili.search(term, page=pn)
        except Exception:                        # noqa: BLE001
            break
        out.extend(r["list"])
        _sleep()
    return out
