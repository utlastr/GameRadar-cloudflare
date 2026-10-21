# -*- coding: utf-8 -*-
"""按游戏名在 B站 搜索，把一款游戏的全部视频捞出来，按天统计。

## 多个别名混着叫怎么办
"赌石"这款游戏一会儿叫「翡翠刮刮乐」，一会儿叫「赌石刮刮乐」。直接用"赌石"搜
会混进原神赌石、赌石大师之类的无关内容。所以分两层：

- **搜索词**（kw）：决定去搜什么，可以多给几个别名；
- **归属判定**（tags / title_terms）：决定一条视频算不算这游戏的。只看两件事，
  视频在游戏分区，且 标签命中专属标签 或 标题命中专属短语。

"赌石"这种泛词只用来搜，**绝不用来判定归属**，否则脏数据会灌进来。
判定靠 tag 比靠标题稳：标题里没写游戏名的视频不会漏，无关内容也不会混进来。

## 搜索上限 1000 条
接口单次查询最多给 1000 条。所以按月切；某个月顶到 1000 就再切成天。

## 为什么用 wbi 签名而不是浏览器 fetch
不带签名的 `x/web-interface/search/type` 现在对谁都是 412（你的浏览器里也一样），
带 wbi 签名的 `x/web-interface/wbi/search/type` 才返回 code=0。所以这里直接用
Python 发签名请求，比开浏览器快得多，也不用在你窗口里留标签页。
Cookie 从你已登录的浏览器取一次，缓存 6 小时，过期再取。
"""
import concurrent.futures
import datetime
import json
import os
import random
import sys
import threading
import time

_here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _here not in sys.path:
    sys.path.insert(0, _here)
# 托盘版 app.run() 把 sys.stdout 换成了 Tee，没有 reconfigure()，别在这里炸。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from radar import cdp, db, net                        # noqa: E402
from radar.hit import ensure                          # noqa: E402
from radar.opp import wbi                             # noqa: E402

SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
CARD_URL = "https://api.bilibili.com/x/web-interface/card"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0")

GAME_TIDS = {4, 17, 65, 171, 172, 173, 121, 136, 19}

# 三款"成功"游戏（用户定的体量）。搜索词和归属判定分开写。
# 这三款是人指定的种子。之后自动发现的游戏不进这里，进 hit_game_cfg 表，
# 由 games() 合并进来——写死的清单跟不上"每天都有新游戏起势"这件事。
SEED_GAMES = [
    {
        "gid": "hit:jade",
        "name": "赌石 / 翡翠刮刮乐",
        "kws": ["翡翠刮刮乐", "赌石刮刮乐", "赌石模拟器", "翡翠精英模拟器"],
        "tags": ["赌石刮刮乐", "翡翠刮刮乐"],
        "title_terms": ["翡翠刮刮乐", "赌石刮刮乐", "赌石模拟器", "翡翠精英模拟器"],
    },
    {
        "gid": "hit:auction",
        "name": "竞拍之王",
        "kws": ["竞拍之王"],
        "tags": ["竞拍之王"],
        "title_terms": ["竞拍之王"],
    },
    {
        "gid": "hit:sword",
        "name": "铸剑大师",
        "kws": ["铸剑大师"],
        "tags": ["铸剑大师"],
        "title_terms": ["铸剑大师"],
    },
]

CFG_SCHEMA = """
CREATE TABLE IF NOT EXISTS hit_game_cfg(
  gid TEXT PRIMARY KEY, name TEXT, kws TEXT, tags TEXT, title_terms TEXT,
  source TEXT, added_ts INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS hit_candidate(
  tag TEXT PRIMARY KEY, n_up INTEGER, n_video INTEGER, play_sum INTEGER,
  play_median INTEGER, first_day TEXT, last_day TEXT, novelty REAL,
  score REAL, state TEXT, ts INTEGER, note TEXT);
"""

_cfg_ready = {"done": False}


def _split(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def ensure_cfg():
    if _cfg_ready["done"]:
        return
    with db.session() as c:
        c.executescript(CFG_SCHEMA)
    _cfg_ready["done"] = True


def games():
    """种子 + 自动发现的游戏，合成一份清单。"""
    out = [dict(g, source="seed") for g in SEED_GAMES]
    try:
        ensure_cfg()
        with db.session() as c:
            for r in c.execute("SELECT * FROM hit_game_cfg ORDER BY added_ts"):
                out.append({"gid": r["gid"], "name": r["name"],
                            "kws": _split(r["kws"]), "tags": _split(r["tags"]),
                            "title_terms": _split(r["title_terms"]),
                            "source": r["source"] or "auto"})
    except Exception:                             # noqa: BLE001
        pass
    return out


def add_game(name, kws=None, tags=None, title_terms=None, source="auto", note=""):
    """把一款游戏加进自动清单。返回它的 gid。"""
    ensure_cfg()
    name = (name or "").strip()
    if not name:
        raise ValueError("游戏名不能为空")
    gid = "auto:" + name
    kws = kws or [name]
    tags = tags or [name]
    title_terms = title_terms or [name]
    with db.session() as c:
        c.execute("INSERT OR REPLACE INTO hit_game_cfg"
                  "(gid, name, kws, tags, title_terms, source, added_ts, note)"
                  " VALUES (?,?,?,?,?,?,?,?)",
                  (gid, name, ",".join(kws), ",".join(tags), ",".join(title_terms),
                   source, int(time.time()), note))
    return gid

JS_SEARCH = """
(async (kw, beg, end, page, order) => {
  const u = new URL('https://api.bilibili.com/x/web-interface/search/type');
  u.searchParams.set('search_type','video');
  u.searchParams.set('keyword', kw);
  u.searchParams.set('order', order);
  u.searchParams.set('page', String(page));
  u.searchParams.set('page_size','30');
  u.searchParams.set('tids','4');
  if (beg) u.searchParams.set('pubtime_begin_s', String(beg));
  if (end) u.searchParams.set('pubtime_end_s', String(end));
  const r = await fetch(u.toString(), {credentials:'include'});
  const t = await r.text();
  if (!t.trim().startsWith('{')) {
    // B站被挑战/被拦时会返回一段 HTML 验证页。这绝不能被当成"没有数据"。
    return {code:-1, message:'HTTP '+r.status+' 返回的不是 JSON（疑似反爬验证页）',
            total:0, pages:0, list:[]};
  }
  const j = JSON.parse(t);
  const d = j.data || {};
  return {code:j.code, message:j.message||'', total:d.numResults||0, pages:d.numPages||0,
    list:(d.result||[]).map(v=>({
      bvid:v.bvid, play:v.play||0, like:v.like||0, reply:v.video_review||0,
      mid:String(v.mid||''), up:v.author||'', tid:v.typeid, tname:v.typename||'',
      pubdate:v.pubdate||0, title:(v.title||'').replace(/<[^>]+>/g,''),
      tag:v.tag||''
    }))};
})(%s, %s, %s, %s, %s)
"""

JS_CARD = """
(async (mid) => {
  const r = await fetch('https://api.bilibili.com/x/web-interface/card?mid=' + mid, {credentials:'include'});
  const j = await r.json();
  const d = j.data || {};
  return {code:j.code, follower:(d.follower||0), name:(d.card||{}).name || ''};
})(%s)
"""


def _ts(y, m, d=1):
    return int(datetime.datetime(y, m, d).timestamp())


def month_ranges(y0=2018, y1=None):
    now = datetime.datetime.now()
    y1 = y1 or now.year
    out = []
    for y in range(y0, y1 + 1):
        for m in range(1, 13):
            if y == now.year and m > now.month:
                break
            beg = _ts(y, m)
            end = _ts(y + 1, 1) if m == 12 else _ts(y, m + 1)
            out.append(("%04d-%02d" % (y, m), beg, end))
    return out


class Searcher:
    """用 wbi 签名的搜索接口，Python 直发。

    Cookie 从已登录的浏览器取一次，存到 radar_data/bili_cookie.json，6 小时内复用，
    这样不会为了每次查询都在你窗口里开标签页。
    """

    CACHE_HOURS = 6

    def __init__(self, port=9222, cookie_file=None):
        self.port = port
        self.cookie_file = cookie_file or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "radar_data", "bili_cookie.json")
        self._ck = None
        self.n = 0

    def _load_cached(self):
        try:
            with open(self.cookie_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            if time.time() - float(d.get("ts") or 0) < self.CACHE_HOURS * 3600 and d.get("cookie"):
                return d["cookie"]
        except Exception:                         # noqa: BLE001
            pass
        return None

    def _fetch_cookies(self):
        """从已登录的浏览器读一次 cookie，读完把标签页关掉。"""
        tab = cdp.open_tab("https://www.bilibili.com/", self.port, wait=4.0, background=True)
        try:
            ck = tab.cookies(["https://www.bilibili.com"])
        finally:
            cdp.close_tab(tab)
        if "SESSDATA" not in ck:
            raise RuntimeError("浏览器里没有 B站 登录态，先登录一下")
        val = "; ".join("%s=%s" % (k, v) for k, v in ck.items())
        try:
            os.makedirs(os.path.dirname(self.cookie_file), exist_ok=True)
            with open(self.cookie_file, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "cookie": val}, f)
        except Exception:                         # noqa: BLE001
            pass
        return val

    def cookie(self, force=False):
        if force:
            self._ck = None
        if not self._ck:
            self._ck = self._load_cached() or self._fetch_cookies()
        return self._ck

    def _headers(self):
        return {"Cookie": self.cookie(), "Referer": "https://search.bilibili.com/",
                "Origin": "https://search.bilibili.com", "User-Agent": UA}

    def search(self, kw, beg, end, page=1, order="pubdate"):
        """查一页。

        失败必须抛出来，不能返回 0。返回 0 会让"被拦住/调用失败"和"这款游戏这段
        时间没有视频"看起来一模一样，生命周期起点会算错——上一版就是这么错的。
        """
        params = {"search_type": "video", "keyword": kw, "order": order,
                  "page": page, "page_size": 30, "tids": 4}
        if beg:
            params["pubtime_begin_s"] = beg
        if end:
            params["pubtime_end_s"] = end
        d = wbi.get_signed(SEARCH_URL, params, headers=self._headers(), retries=2)
        if d.get("code") != 0:
            raise RuntimeError("B站搜索 code=%s %s" % (d.get("code"), d.get("message") or ""))
        data = d.get("data") or {}
        self.n += 1
        return {"total": int(data.get("numResults") or 0),
                "pages": int(data.get("numPages") or 0),
                "list": [_clean(v) for v in (data.get("result") or [])]}

    def card(self, mid):
        """UP 主现在有多少粉丝。"""
        d = net.get_json_strict(CARD_URL, {"mid": mid}, headers=self._headers(), retries=1, timeout=12)
        data = d.get("data") or {}
        return {"code": d.get("code"), "follower": int(data.get("follower") or 0),
                "name": (data.get("card") or {}).get("name") or ""}

    def close(self):
        pass


def _clean(v):
    """把接口返回的一条整理成我们要的形状。"""
    title = (v.get("title") or "")
    for a, b in (('<em class="keyword">', ""), ("</em>", "")):
        title = title.replace(a, b)
    return {"bvid": v.get("bvid"), "play": v.get("play") or 0, "like": v.get("like") or 0,
            "reply": v.get("video_review") or 0, "mid": str(v.get("mid") or ""),
            "up": v.get("author") or "", "tid": v.get("typeid"), "tname": v.get("typename") or "",
            "pubdate": v.get("pubdate") or 0, "title": title, "tag": v.get("tag") or ""}


def _fetch_span(s, kw, beg, end):
    """取一段区间里全部视频。

    区间总数为 0 就不用翻了；不到 1000 就翻完所有页；
    顶到 1000（接口上限）就按天切开，一天一天取——一天不可能有 1000 条。
    """
    first = s.search(kw, beg, end, 1)
    total = int(first.get("total") or 0)
    if total == 0:
        return []

    if total >= 1000:
        out = {}
        day = beg
        while day < end:
            nxt = day + 86400
            for p in range(1, 35):
                r = s.search(kw, day, min(nxt, end), p)
                got = r.get("list") or []
                if not got:
                    break
                for v in got:
                    out[v["bvid"]] = v
                if len(got) < 30:
                    break
                time.sleep(random.uniform(0.5, 0.9))
            day = nxt
            time.sleep(random.uniform(0.4, 0.8))
        return list(out.values())

    out = list(first.get("list") or [])
    pages = min(int(first.get("pages") or 1), 34)
    for p in range(2, pages + 1):
        time.sleep(random.uniform(0.6, 1.1))
        r = s.search(kw, beg, end, p)
        got = r.get("list") or []
        if not got:
            break
        out.extend(got)
    return out


def _tid(v):
    """分区 id 统一成整数。

    接口给的 typeid 是字符串（'17'），集合里是整数（17），直接比会永远不相等，
    结果就是"一条都不属于这款游戏"——踩过一次，别再踩。
    """
    try:
        return int(v.get("tid"))
    except (TypeError, ValueError):
        return None


def belongs(game, v):
    """归属判定：游戏分区 + （标签命中 或 标题命中专属短语）。"""
    if _tid(v) not in GAME_TIDS:
        return False
    tags = (v.get("tag") or "").replace("，", ",").split(",")
    tags = [t.strip() for t in tags if t.strip()]
    for t in game["tags"]:
        if any(t in x for x in tags):
            return True
    title = v.get("title") or ""
    for t in game["title_terms"]:
        if t in title:
            return True
    return False


def save(gid, vids, kw):
    n = 0
    with db.session() as c:
        for v in vids:
            c.execute("INSERT OR REPLACE INTO hit_topic(gid, bvid, pubdate, play, like_, reply, mid, up, "
                      "tid, tname, title, tags, kw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (gid, v["bvid"], int(v.get("pubdate") or 0), int(v.get("play") or 0),
                       int(v.get("like") or 0), int(v.get("reply") or 0), str(v.get("mid") or ""),
                       v.get("up") or "", v.get("tid"), v.get("tname") or "",
                       v.get("title") or "", v.get("tag") or "", kw))
            n += 1
    return n


def _retry(fn, tries=4, backoff=(20, 60, 120)):
    """失败就退避重试。重试完还失败就抛出去，绝不悄悄当 0。"""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:                    # noqa: BLE001
            last = e
            if i < tries - 1:
                wait = backoff[min(i, len(backoff) - 1)]
                print("     检索失败（%s），等 %ds 重试 %d/%d" % (e, wait, i + 1, tries - 1))
                time.sleep(wait)
    raise last


def _total_until(s, game, end, beg=None):
    """从 beg（默认 2018-01）到 end 之前，这款游戏一共有多少条（各别名相加）。

    注意方向：必须是"从头到这个月"，不能是"从这个月到现在"。后者对任何较早的月份
    都成立（今天的数据也在区间里），二分会被带偏到 2018 年——踩过。
    """
    beg = beg or _ts(2018, 1, 1)
    tot = 0
    for kw in game["kws"]:
        r = _retry(lambda k=kw: s.search(k, beg, end, 1, order="pubdate"))
        tot += int(r.get("total") or 0)
        time.sleep(random.uniform(0.15, 0.3))
    return tot


# 统计窗口起点。为什么不是 2018：
# "竞拍之王"这种词在 2018 年也有零散视频（泛指"拍卖之王"），一条也算起点的话，
# 要逐月取一百多个月、跑一个多小时，而且那条根本不是这款游戏。这两款都是近一两年
# 起来的，从 2025-01 开始取，前面正好是一段贴着 0 的平线，"从零起势"看得清楚。
WINDOW_START = (2025, 1)


def window_spans(y0=None):
    """统计窗口内的月份列表（老 -> 新）。"""
    y0 = y0 or WINDOW_START[0]
    return month_ranges(y0)


def collect(game, y0=None, y1=None, quiet=False):
    """把一款游戏在统计窗口内的数据捞下来。返回统计。

    y0/y1 是可选的年份上下界，只用来补历史（比如只补 2020~2024），
    不传就按 WINDOW_START 走。存库是 INSERT OR REPLACE，补历史不会动已有行。
    """
    ensure()
    s = Searcher(9222)
    t0 = time.time()
    allv = {}
    try:
        active = window_spans(y0)
        if y1:
            active = [m for m in active if int(m[0][:4]) <= int(y1)]
        if not quiet:
            print("  统计区间 %s ~ %s，共 %d 个月"
                  % (active[0][0], active[-1][0], len(active)))

        # 逐月把全部视频拉下来（从早到晚，进度看得见）
        for label, beg, end in active:
            for kw in game["kws"]:
                for v in _fetch_span(s, kw, beg, end):
                    if belongs(game, v):
                        allv[v["bvid"]] = v
                time.sleep(random.uniform(0.2, 0.4))
            if not quiet:
                print("    %s  累计 %d 条  %ds" % (label, len(allv), int(time.time() - t0)))
            if len(allv) and (time.time() - t0) > 600:
                if not quiet:
                    print("    （已跑 10 分钟，先把已有部分存下来）")
                save(game["gid"], list(allv.values()), ",".join(game["kws"]))
        save(game["gid"], list(allv.values()), ",".join(game["kws"]))
        return {"gid": game["gid"], "n": len(allv), "months": len(active),
                "sec": int(time.time() - t0)}
    finally:
        s.close()


SPACE_URL = "https://api.bilibili.com/x/space/wbi/arc/search"


def ensure_stat_table(c):
    """每条视频每天的播放快照。

    为什么必须有这张表：B站只给"现在的播放"，没有历史时间轴。活着的游戏如果不
    每天记一次，当天涨了多少就永久丢了。已经死了的游戏补不回来，只能从今天开始。
    """
    c.execute("CREATE TABLE IF NOT EXISTS hit_stat("
              "gid TEXT, bvid TEXT, day TEXT, ts INTEGER,"
              " play INTEGER, like_ INTEGER, reply INTEGER,"
              " PRIMARY KEY(gid, bvid, day))")


def snapshot(gid=None, days=21, quiet=False):
    """给还活着的游戏记当天快照。

    做法不是逐条视频去调详情接口（5228 条要命），而是按天重搜最近 days 天：
    搜索结果自带"现在的播放"，一次请求能拿回几十上百条。顺带把 hit_topic 里的
    现值刷新一遍。
    """
    ensure()
    s = Searcher(9222)
    today = datetime.date.today()
    day_key = today.strftime("%Y-%m-%d")
    now = int(time.time())
    total = 0
    games_sel = [g for g in games() if (gid is None or g["gid"] == gid)]
    try:
        with db.session() as c:
            ensure_stat_table(c)
        for g in games_sel:
            got = {}
            for back in range(days):
                d = today - datetime.timedelta(days=back)
                beg = int(datetime.datetime(d.year, d.month, d.day).timestamp())
                end = beg + 86400
                for kw in g["kws"]:
                    try:
                        for v in _fetch_span(s, kw, beg, end):
                            if belongs(g, v):
                                got[v["bvid"]] = v
                    except Exception as e:        # noqa: BLE001
                        print("   快照 %s %s 失败：%s" % (g["name"], d, e))
                    time.sleep(random.uniform(0.2, 0.4))
                if not quiet:
                    print("   %s %s 当天累计 %d 条" % (g["name"], d, len(got)))
            if not got:
                continue
            with db.session() as c:
                for v in got.values():
                    # 新视频要进主表，否则"复活"的视频只在快照表里，主表看不见
                    c.execute("INSERT OR REPLACE INTO hit_topic(gid, bvid, pubdate, play, like_, reply,"
                              " mid, up, tid, tname, title, tags, kw)"
                              " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (g["gid"], v["bvid"], int(v.get("pubdate") or 0),
                               int(v.get("play") or 0), int(v.get("like") or 0),
                               int(v.get("reply") or 0), str(v.get("mid") or ""),
                               v.get("up") or "", v.get("tid"), v.get("tname") or "",
                               v.get("title") or "", v.get("tag") or "",
                               ",".join(g["kws"])))
                    c.execute("INSERT OR REPLACE INTO hit_stat"
                              "(gid, bvid, day, ts, play, like_, reply) VALUES (?,?,?,?,?,?,?)",
                              (g["gid"], v["bvid"], day_key, now,
                               int(v.get("play") or 0), int(v.get("like") or 0),
                               int(v.get("reply") or 0)))
            total += len(got)
            if not quiet:
                print("   %s 快照写入 %d 条" % (g["name"], len(got)))
    finally:
        s.close()
    return total


def _game_terms():
    """所有游戏名/别名，用来把"这款游戏自己的视频"从 UP 的平时水平里剔掉。"""
    out = set()
    for g in games():
        out.add(g["name"])
        for t in g.get("title_terms") or []:
            out.add(t)
    return {t.lower() for t in out if t}


def up_baseline(s, mid, n=30):
    """一个 UP 最近 n 条视频的播放中位数 = 他平时的播放水平。

    必须剔掉本项目的视频：某个小 UP 因为一条游戏爆款冲上 200 万，如果不剔掉，
    他的"平时水平"会被自己抬高，爆款倍数就被算小了。
    """
    params = {"mid": mid, "ps": n, "pn": 1, "order": "pubdate", "index": 0,
              "tid": 0, "keyword": ""}
    h = s._headers()
    h["Referer"] = "https://space.bilibili.com/%s/video" % mid
    d = wbi.get_signed(SPACE_URL, params, headers=h, retries=2)
    if d.get("code") != 0:
        raise RuntimeError("空间接口 code=%s %s" % (d.get("code"), d.get("message") or ""))
    lst = (((d.get("data") or {}).get("list") or {}).get("vlist")) or []
    terms = _game_terms()
    plays, days = [], []
    for v in lst:
        t = (v.get("title") or "").lower()
        if any(k in t for k in terms):
            continue
        plays.append(int(v.get("play") or 0))
        if v.get("created"):
            days.append(int(v["created"]))
    if not plays:
        return None
    plays.sort()
    mid_i = len(plays) // 2
    med = plays[mid_i] if len(plays) % 2 else (plays[mid_i - 1] + plays[mid_i]) // 2
    return {"median": med, "n": len(plays),
            "start": min(days) if days else 0, "end": max(days) if days else 0}


def baselines(gid=None, limit=3000, min_followers=10000, top=30, workers=8):
    """补"平时播放水平"（爆款倍数要用）。

    不抓全量，只抓有意义的：
      - 各游戏播放前 top 条视频背后的 UP（爆款倍数就要这些）；
      - 粉丝数 >= min_followers 的 UP（万粉以下的小号，用"播放÷粉丝数"就够，
        细抠他们的历史播放是浪费请求——用户明确说过不必管这么细）。
    并发跑，别一个接一个等——单次请求本身就要 5 秒，串行是纯浪费。
    """
    ensure()
    s = Searcher(9222)
    with db.session() as c:
        c.execute("CREATE TABLE IF NOT EXISTS hit_up_base("
                  "mid TEXT PRIMARY KEY, median_play INTEGER, n_video INTEGER,"
                  " span_start INTEGER, span_end INTEGER, ts INTEGER)")

    q = ("SELECT t.mid, MAX(t.up) AS up, MAX(COALESCE(u.followers,0)) AS followers, "
         "MAX(t.play) AS best_play, COUNT(*) AS n "
         "FROM hit_topic t "
         "LEFT JOIN hit_up u ON u.mid = t.mid "
         "LEFT JOIN hit_up_base b ON b.mid = t.mid "
         "WHERE t.mid != '' AND b.mid IS NULL")
    args = []
    if gid:
        q += " AND t.gid = ?"
        args.append(gid)
    q += " GROUP BY t.mid"
    with db.session() as c:
        rows = [dict(r) for r in c.execute(q, args)]

    # 每个游戏各自播放前 top 条视频的 UP
    keep = set()
    by_gid = {}
    with db.session() as c:
        for r in c.execute("SELECT gid, mid FROM hit_topic WHERE mid != '' AND pubdate > 0"):
            by_gid.setdefault(r["gid"], []).append(r["mid"])
        for g, mids in by_gid.items():
            for r in c.execute(
                    "SELECT DISTINCT mid FROM hit_topic WHERE gid = ? AND mid != '' "
                    "ORDER BY play DESC LIMIT ?", (g, top)):
                keep.add(r["mid"])

    todo = [r for r in rows
            if r["mid"] in keep or int(r["followers"] or 0) >= int(min_followers)]
    small = len(rows) - len(todo)
    print("候选 %d 个，要抓 %d 个（头部+万粉以上），跳过 %d 个万粉以下小号"
          % (len(rows), len(todo), small))

    done = [0]
    lock = threading.Lock()

    def work(r):
        try:
            b = up_baseline(s, r["mid"])
        except Exception as e:                    # noqa: BLE001
            with lock:
                print("   失败 %s：%s" % (r["up"] or r["mid"], str(e)[:60]))
            return None
        if not b:
            return None
        with db.session() as c:
            c.execute("INSERT OR REPLACE INTO hit_up_base"
                      "(mid, median_play, n_video, span_start, span_end, ts)"
                      " VALUES (?,?,?,?,?,?)",
                      (r["mid"], b["median"], b["n"], b["start"], b["end"],
                       int(time.time())))
        with lock:
            done[0] += 1
            if done[0] % 20 == 0:
                print("   已补 %d / %d" % (done[0], len(todo)))
        time.sleep(random.uniform(0.2, 0.5))
        return b

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, todo))
    finally:
        s.close()
    print("基线补完：成功 %d / %d" % (done[0], len(todo)))
    return done[0]


def followers(gid=None, limit=2000):
    ensure()
    s = Searcher(9222)
    n = 0
    try:
        q = ("SELECT DISTINCT t.mid, t.up FROM hit_topic t LEFT JOIN hit_up u ON u.mid = t.mid "
             "WHERE t.mid != '' AND u.ts IS NULL")
        args = []
        if gid:
            q += " AND t.gid = ?"
            args.append(gid)
        q += " LIMIT ?"
        args.append(limit)
        with db.session() as c:
            rows = c.execute(q, args).fetchall()
        print("待补粉丝数:", len(rows))
        for r in rows:
            r2 = _retry(lambda m=r["mid"]: s.card(m), tries=2, backoff=(15,))
            if r2 and r2.get("code") == 0:
                with db.session() as c:
                    c.execute("INSERT OR REPLACE INTO hit_up(mid, name, followers, ts) VALUES (?,?,?,?)",
                              (r["mid"], r2.get("name") or r["up"], int(r2.get("follower") or 0),
                               int(time.time())))
                n += 1
            time.sleep(random.uniform(0.5, 1.0))
            if n % 25 == 0:
                print("   已补 %d / %d" % (n, len(rows)))
    finally:
        s.close()
    return n


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    y0 = int(sys.argv[2]) if len(sys.argv) > 2 else None
    y1 = int(sys.argv[3]) if len(sys.argv) > 3 else None
    for g in games():
        if which not in ("all", g["gid"], "seed" if g.get("source") == "seed" else ""):
            continue
        print("\n=== %s ===" % g["name"])
        r = collect(g, y0=y0, y1=y1)
        print("  完成：%s 条视频，%s 个活跃月，%ss" % (r["n"], r["months"], r["sec"]))
