"""B站：分区新视频发现、视频明细跟踪、UP 主平时水平、评论和弹幕抽样。

接口实测结果见 docs/OPP_API_PROBE.md。几个要点（都是实测出来的，不是文档抄的）：
- `x/web-interface/dynamic/region` 已经下线（任何 rid 都返回 -404），改用 `x/web-interface/newlist?rid=4`，
  rid=4 是游戏大类，每条自带 tid，够用。
- `x/space/wbi/arc/search` 必须带 dm_img_list / dm_img_str / dm_cover_img_str / dm_img_inter，
  少一个就 -352 风控。
- newlist 每条已经带 aid / cid / stat / pubdate / tid / desc，发现时就能落库，不用再调一次 view，省预算。
"""
import json
import random
import time
import urllib.request
import zlib
from html import unescape

from .. import activity, config, db
from ..net import get_json, get_json_strict, get_text
from ..sources import bili
from . import budget_left, budget_use, ensure, note_risk, reset_risk
from . import gamemap
from . import wbi

TIDS = (17, 171, 172, 65, 173, 121, 136, 19)
_HEAD = {"Referer": "https://www.bilibili.com/", "Origin": "https://www.bilibili.com"}
_SEARCH_HEAD = {"Referer": "https://search.bilibili.com/"}
_DM = {"dm_img_list": "[]",
       "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
       "dm_cover_img_str": "QU5HTEUgKEludGVsLCBJbnRlbChSKSBVSEQgR3JhcGhpY3MgNjMwICgweDAwMDAzRTlCKSBEaXJlY3QzRDExIHZzXzVfMCBwc181XzApLCBvciBzaW1pbGFy",
       "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}'}


def _sleep(lo=0.8, hi=1.8):
    time.sleep(random.uniform(lo, hi))


def _check(d, what):
    if not isinstance(d, dict):
        raise RuntimeError("%s：返回的不是数据" % what)
    if d.get("code") not in (0, None):
        raise RuntimeError("%s：B站返回 code=%s %s" % (what, d.get("code"),
                                                    d.get("message") or d.get("msg") or ""))
    return d.get("data") or {}


def _budget_ok():
    if budget_left() <= 0:
        return False
    budget_use()
    return True


# ---------------- 3.1 发现新视频 ----------------

def discover_once(pages=3):
    """每个分区取前 150 条。rid=4 是游戏大类，pn 1..3 每页 50。"""
    ensure()
    if budget_left() < 20:
        return 0
    bili._warmup()
    seen, new = {}, 0
    err = ""
    for pn in range(1, pages + 1):
        if not _budget_ok():
            break
        try:
            d = get_json("https://api.bilibili.com/x/web-interface/newlist",
                         {"rid": 4, "ps": 50, "pn": pn, "type": 0},
                         headers=_HEAD, retries=1, timeout=15)
            data = _check(d, "B站分区最新")
        except Exception as e:                   # noqa: BLE001
            err = str(e)
            if note_risk(e):
                break
            continue
        arr = data.get("archives") or []
        if not arr:
            break
        for a in arr:
            seen[a.get("bvid")] = a
        _sleep()
    if seen:
        reset_risk()
    for bvid, a in seen.items():
        if not bvid:
            continue
        if _upsert_video(a):
            new += 1
    total = _count_videos()
    if err and not seen:
        __import__("radar.opp", fromlist=["x"]).status("bili_track", False, total, err)
    else:
        __import__("radar.opp", fromlist=["x"]).status(
            "bili_track", True, total, "",
            "本轮新增 %d 条 / 抓到 %d 条" % (new, len(seen)))
    if new:
        activity.say("机会雷达：B站新增 %d 条视频（库里共 %d 条）" % (new, total))
    return new


def _upsert_video(a):
    bvid = a.get("bvid")
    if not bvid:
        return False
    owner = a.get("owner") or {}
    stat = a.get("stat") or {}
    title = unescape(str(a.get("title") or "").replace("<em class=\"keyword\">", "").replace("</em>", ""))
    now = int(time.time())
    pubdate = int(a.get("pubdate") or 0)
    with db.session() as c:
        row = c.execute("SELECT bvid FROM opp_videos WHERE bvid = ?", (bvid,)).fetchone()
        if row:
            return False
        c.execute("INSERT INTO opp_videos(bvid, aid, cid, tid, mid, title, tags, pubdate, duration, "
                  "first_seen, track_until, descr, owner) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (bvid, a.get("aid"), a.get("cid"), a.get("tid"), str(owner.get("mid") or ""),
                   title, None, pubdate, a.get("duration"), now, 0,
                   a.get("desc") or "", owner.get("name") or ""))
        c.execute("INSERT OR REPLACE INTO opp_vsamples(bvid, ts, view, lk, coin, fav, share, reply, dm) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (bvid, now, int(stat.get("view") or 0), int(stat.get("like") or 0),
                   int(stat.get("coin") or 0), int(stat.get("favorite") or 0),
                   int(stat.get("share") or 0), int(stat.get("reply") or 0),
                   int(stat.get("danmaku") or 0)))
    return True


def _count_videos():
    with db.session() as c:
        return c.execute("SELECT COUNT(*) FROM opp_videos").fetchone()[0]


# ---------------- 3.2 视频明细跟踪 ----------------

def _views(row):
    with db.session() as c:
        r = c.execute("SELECT view FROM opp_vsamples WHERE bvid = ? ORDER BY ts DESC LIMIT 1",
                      (row["bvid"],)).fetchone()
    return int(r[0]) if r else 0


def _sample_gap(age_sec):
    """多久采一次：0–72 小时每 2 小时，之后每天，21 天停。"""
    if age_sec <= 72 * 3600:
        return 2 * 3600
    return 24 * 3600


def due_videos(limit=200):
    """该采样的视频，按"离发布时间近、播放增速高"优先。"""
    now = int(time.time())
    with db.session() as c:
        rows = c.execute(
            "SELECT v.bvid, v.aid, v.cid, v.pubdate, v.mid FROM opp_videos v "
            "LEFT JOIN (SELECT bvid, MAX(ts) AS last_ts, MAX(view) AS last_view FROM opp_vsamples GROUP BY bvid) s "
            "ON s.bvid = v.bvid WHERE v.pubdate > ? ORDER BY v.pubdate DESC LIMIT 4000",
            (now - 30 * 86400,)).fetchall()
    out = []
    for r in rows:
        age = now - int(r["pubdate"] or now)
        if age < 0 or age > 21 * 86400:
            continue
        with db.session() as c:
            s = c.execute("SELECT MAX(ts), MAX(view) FROM opp_vsamples WHERE bvid = ?",
                          (r["bvid"],)).fetchone()
        last_ts, last_view = (s[0] or 0), (s[1] or 0)
        gap = _sample_gap(age)
        if now - last_ts < gap:
            continue
        # 增速：每次采样涨多少
        speed = (last_view or 0) / max(1.0, (now - int(r["pubdate"] or now)) / 3600.0)
        out.append((speed, r))
    out.sort(key=lambda x: -x[0])
    return [r for _s, r in out[:limit]]


def track_once(limit=None):
    """采样 + 补标签 + 更新 UP 主水平。"""
    ensure()
    limit = limit or 200
    if budget_left() < 10:
        return 0
    bili._warmup()
    n, err = 0, ""
    for r in due_videos(limit):
        if budget_left() <= 2:
            break
        if not _budget_ok():
            break
        try:
            d = get_json_strict("https://api.bilibili.com/x/web-interface/view", {"bvid": r["bvid"]},
                                headers=_HEAD, retries=0, timeout=12)
            x = _check(d, "B站视频详情")
        except Exception as e:                   # noqa: BLE001
            err = str(e)
            if note_risk(e):
                break
            continue
        s = x.get("stat") or {}
        with db.session() as c:
            c.execute("INSERT OR REPLACE INTO opp_vsamples(bvid, ts, view, lk, coin, fav, share, reply, dm) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (r["bvid"], int(time.time()), int(s.get("view") or 0), int(s.get("like") or 0),
                       int(s.get("coin") or 0), int(s.get("favorite") or 0), int(s.get("share") or 0),
                       int(s.get("reply") or 0), int(s.get("danmaku") or 0)))
            c.execute("UPDATE opp_videos SET aid = COALESCE(?, aid), cid = COALESCE(?, cid) WHERE bvid = ?",
                      (x.get("aid"), x.get("cid"), r["bvid"]))
        n += 1
        _sleep()
    if n:
        activity.say("机会雷达：采样 %d 条视频" % n)
    __import__("radar.opp", fromlist=["x"]).status("bili_sample", not err, n, err)
    uploader_refresh()
    return n


# ---------------- 3.3 UP 主平时水平 ----------------

UPLOADER_TTL = 7 * 86400


def uploader_refresh(limit=8):
    """每个 UP 主 7 天更新一次：粉丝数 + 最近 10 条发布超过 7 天的视频播放中位数。"""
    now = int(time.time())
    with db.session() as c:
        rows = c.execute(
            "SELECT v.mid, COUNT(*) AS n FROM opp_videos v LEFT JOIN opp_uploaders u ON u.mid = v.mid "
            "WHERE v.mid != '' AND (u.ts IS NULL OR u.ts < ?) GROUP BY v.mid ORDER BY n DESC LIMIT ?",
            (now - UPLOADER_TTL, limit)).fetchall()
    n = 0
    for r in rows:
        if budget_left() <= 3:
            break
        mid = r["mid"]
        try:
            followers, median, base_n, est = _uploader_stats(mid)
        except Exception as e:                   # noqa: BLE001
            if note_risk(e):
                break
            continue
        with db.session() as c:
            c.execute("INSERT OR REPLACE INTO opp_uploaders(mid, name, followers, base_median, base_n, "
                      "base_est, ts) VALUES (?,?,?,?,?,?,?)",
                      (mid, "", followers, median, base_n, est, now))
        n += 1
    return n


def _uploader_stats(mid):
    """返回 (粉丝数, 平时中位数, 样本数, 是否估计)。"""
    _budget_ok()
    followers = 0
    try:
        d = get_json_strict("https://api.bilibili.com/x/web-interface/card", {"mid": mid},
                            headers=_HEAD, retries=0, timeout=12)
        followers = int((_check(d, "B站用户名片").get("follower") or 0))
    except Exception:                            # noqa: BLE001
        pass
    _sleep()
    median, base_n, est = 0, 0, 0
    try:
        _budget_ok()
        p = {"mid": mid, "ps": 30, "pn": 1, "order": "pubdate", "platform": "web",
             "web_location": 1550101, "order_avoided": "true"}
        p.update(_DM)
        d = wbi.get_signed("https://api.bilibili.com/x/space/wbi/arc/search", p, headers=_HEAD)
        vl = ((d.get("data") or {}).get("list") or {}).get("vlist") or []
        cutoff = time.time() - 7 * 86400
        plays = [int(v.get("play") or 0) for v in vl if (v.get("created") or 0) < cutoff][:10]
        if plays:
            plays.sort()
            median = plays[len(plays) // 2]
            base_n = len(plays)
    except Exception as e:                       # noqa: BLE001
        note_risk(e)
    if not median:
        # 拿不到就用库里这个 UP 主已有视频的中位数顶上，并标记"估计"
        with db.session() as c:
            r = c.execute("SELECT view FROM opp_vsamples s JOIN opp_videos v ON v.bvid = s.bvid "
                          "WHERE v.mid = ? ORDER BY s.ts DESC LIMIT 20", (mid,)).fetchall()
        plays = sorted(int(x[0] or 0) for x in r if x[0])
        if plays:
            median, base_n, est = plays[len(plays) // 2], len(plays), 1
    return followers, median, base_n, est


# ---------------- 3.4 评论和弹幕 ----------------

DEMAND_DEFAULT = {
    "mobile": "手机|手游|安卓|苹果|ios|移动端|手机版|手机上|能在手机",
    "name": "叫什么|什么游戏|游戏名|哪里下|在哪下|怎么下|多少钱|steam上",
    "want": "想玩|求链接|好想玩|买了|已购|入了",
}


def _words():
    w = getattr(config, "OPP_DEMAND_WORDS", None)
    return w if isinstance(w, dict) and w else DEMAND_DEFAULT


def _count_words(texts):
    """返回 (总数, 命中手机, 命中问名字, 命中想玩)。"""
    import re
    w = _words()
    pats = {}
    for k, src in w.items():
        try:
            pats[k] = re.compile(src, re.I)
        except re.error:
            continue
    tot = len(texts)
    hit = {k: 0 for k in pats}
    for t in texts:
        for k, p in pats.items():
            if p.search(t or ""):
                hit[k] += 1
    return tot, hit.get("mobile", 0), hit.get("name", 0), hit.get("want", 0)


def _need_demand(row, now):
    """发布后约 12 / 24 / 72 小时各抓一次，抓过就跳过。"""
    age = (now - int(row["pubdate"] or now)) / 3600.0
    with db.session() as c:
        got = {r[0] for r in c.execute("SELECT DISTINCT ts FROM opp_demand WHERE bvid = ?",
                                       (row["bvid"],))}
    if not got:
        return age >= 12 - 1
    for h in DEMAND_AT_HOURS:
        if age >= h - 1 and not any(abs(ts - int(row["pubdate"] + h * 3600)) < 3 * 3600 for ts in got):
            return True
    return False


def demand_once(limit=10):
    """给到点的视频抓一次评论 + 弹幕。"""
    ensure()
    now = int(time.time())
    with db.session() as c:
        rows = c.execute("SELECT bvid, aid, cid, pubdate FROM opp_videos WHERE pubdate > ? "
                         "ORDER BY pubdate DESC LIMIT 300", (now - 5 * 86400,)).fetchall()
    n = 0
    for r in rows:
        if n >= limit or budget_left() <= 4:
            break
        if not _need_demand(r, now):
            continue
        try:
            texts = _replies(r["aid"])
            tot, mob, name, want = _count_words(texts)
            with db.session() as c:
                c.execute("INSERT OR REPLACE INTO opp_demand(bvid, ts, kind, n_total, n_mobile, n_name, n_want) "
                          "VALUES (?,?,?,?,?,?,?)",
                          (r["bvid"], now, "reply", tot, mob, name, want))
            if r["cid"]:
                dtexts = _danmaku(r["cid"])
                dt, dm, dn, dw = _count_words(dtexts)
                with db.session() as c:
                    c.execute("INSERT OR REPLACE INTO opp_demand(bvid, ts, kind, n_total, n_mobile, n_name, n_want) "
                              "VALUES (?,?,?,?,?,?,?)",
                              (r["bvid"], now, "dm", dt, dm, dn, dw))
            n += 1
        except Exception as e:                   # noqa: BLE001
            if note_risk(e):
                break
        _sleep(1.2, 2.5)
    if n:
        __import__("radar.opp", fromlist=["x"]).status("bili_reply", True, n)
    return n


def _replies(aid, pages=5):
    """热度排序前 5 页评论。"""
    out = []
    if not aid:
        return out
    nxt = 0
    for _ in range(pages):
        if budget_left() <= 1:
            break
        _budget_ok()
        d = wbi.get_signed("https://api.bilibili.com/x/v2/reply/wbi/main",
                           {"oid": aid, "type": 1, "mode": 3, "next": nxt, "ps": 20}, headers=_HEAD)
        data = _check(d, "B站评论")
        for rep in data.get("replies") or []:
            out.append(((rep.get("content") or {}).get("message") or ""))
        cur = data.get("cursor") or {}
        nxt = cur.get("next") or 0
        if cur.get("is_end") or not nxt:
            break
        _sleep(1.0, 2.0)
    return out


def _danmaku(cid, limit=3000):
    """弹幕 XML 是 deflate 压缩的，用 zlib.decompress(data, -15) 解开。"""
    url = "https://api.bilibili.com/x/v1/dm/list.so?oid=%s" % cid
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
    try:
        xml = zlib.decompress(raw, -15).decode("utf-8", "replace")
    except zlib.error:
        xml = raw.decode("utf-8", "replace")
    import re
    out = re.findall(r"<d p=\"[^\"]*\">([^<]*)</d>", xml)
    return out[:limit]


# ---------------- 对应关系 ----------------

def map_once():
    n = gamemap.apply_all()
    k = gamemap.update_unknown()
    if n or k:
        activity.say("机会雷达：新增对应 %d 条，待确认游戏名 %d 个" % (n, k))
    return n


# ---------------- 按游戏名搜跟风视频（第一期先用搜索接口）----------------

def search_followers(term, pages=1):
    """按游戏名搜最新视频，找没见过的跟风视频。"""
    out = []
    for pn in range(1, pages + 1):
        if budget_left() <= 1:
            break
        _budget_ok()
        try:
            d = get_json_strict("https://api.bilibili.com/x/web-interface/search/type",
                                {"search_type": "video", "keyword": term, "order": "pubdate",
                                 "page": pn, "page_size": 30},
                                headers=_SEARCH_HEAD, retries=0, timeout=15)
        except Exception as e:                   # noqa: BLE001
            note_risk(e)
            break
        for v in (_check(d, "B站搜索").get("result") or []):
            rec = bili.video_record(v)
            rec["stat"] = {"view": rec.get("views") or 0}
            out.append(rec)
        _sleep()
    return out
