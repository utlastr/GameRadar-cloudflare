# -*- coding: utf-8 -*-
"""按游戏名在 B站 搜索，把一款游戏的全部视频捞出来，按天统计。

## 多个别名混着叫怎么办
"赌石"这款游戏一会儿叫「翡翠刮刮乐」，一会儿叫「赌石刮刮乐」。直接用"赌石"搜
会混进原神赌石、赌石大师之类的无关内容。所以分两层：

- **搜索词**（kw）：决定去搜什么，可以多给几个别名；
- **归属判定**（tags / title_terms）：决定一条视频算不算这游戏的。只看两件事，
  视频在游戏分区，且 标签命中专属标签 或 标题命中专属短语。

"赌石"这种泛词只用来搜，**绝不用来判定归属**，否则脏数据会灌进来。

## 搜索上限 1000 条
接口单次查询最多给 1000 条。所以按月切；某个月顶到 1000 就再切成天。

请求、签名、Cookie、风控暂停全部走 radar/sources/bili.py（全程序唯一的 B站 客户端）。
这里的每个 tag / 标题短语在游戏档案里是一个 btopic 成员，和 Steam / TapTap 等合并成同一个游戏。
"""
import concurrent.futures
import datetime
import random
import sys
import threading
import time

from .. import config, db
from ..sources import bili
from . import ensure

GAME_TIDS = bili.GAME_TIDS

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


def topics(game):
    """这个案例对应的 B站 话题成员（游戏档案里的 btopic:xxx）。"""
    out = []
    for t in list(game.get("tags") or []) + list(game.get("title_terms") or []):
        n = bili.normalize(t)
        if n and "btopic:" + n not in out:
            out.append("btopic:" + n)
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
    """旧调用方式的薄壳，实际全部转给 bili 客户端。"""

    def __init__(self, port=9222, cookie_file=None):
        self.n = 0

    def search(self, kw, beg, end, page=1, order="pubdate"):
        """查一页。失败必须抛出来，不能返回 0。"""
        r = bili.search(kw, beg, end, page, order)
        self.n += 1
        return r

    def card(self, mid):
        r = bili.card(mid)
        return {"code": 0, "follower": r["follower"], "name": r["name"]}

    def space_videos(self, mid, n=30):
        return bili.space_videos(mid, n)

    def close(self):
        pass


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
    return bili.tid_of(v)


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
        except bili.RiskError as e:
            bili.note_risk(e)
            raise
        except Exception as e:                    # noqa: BLE001
            last = e
            if i < tries - 1:
                wait = backoff[min(i, len(backoff) - 1)]
                print("     检索失败（%s），等 %ds 重试 %d/%d" % (e, wait, i + 1, tries - 1))
                time.sleep(wait)
    raise last


def _total_until(s, game, end, beg=None):
    """从 beg（默认统计窗口起点）到 end 之前，这款游戏一共有多少条（各别名相加）。

    注意方向：必须是"从头到这个月"，不能是"从这个月到现在"。后者对任何较早的月份
    都成立（今天的数据也在区间里），二分会被带偏到 2018 年——踩过。
    """
    beg = beg or _ts(_start_year(), 1, 1)
    tot = 0
    for kw in game["kws"]:
        r = _retry(lambda k=kw: s.search(k, beg, end, 1, order="pubdate"))
        tot += int(r.get("total") or 0)
        time.sleep(random.uniform(0.15, 0.3))
    return tot


def _start_year():
    return int(getattr(config, "HIT_WINDOW_START_YEAR", 2018) or 2018)


def _lead_months():
    return int(getattr(config, "HIT_LEAD_MONTHS", 3) or 0)


def window_spans(y0=None):
    """统计窗口内的月份列表（老 -> 新）。默认从 HIT_WINDOW_START_YEAR（2018）开始。"""
    return month_ranges(y0 or _start_year())


def lifecycle_start(s, game, spans=None):
    """二分找这款游戏第一条视频出现在哪个月，再往前多取 HIT_LEAD_MONTHS 个月。

    "从 2018 到这个月一共几条"随月份只增不减，所以可以二分，十来次请求就够，
    不用逐月翻一百多个月。泛词在早年的零星视频也会让起点变早，这只多花请求，不会漏数据。
    """
    spans = spans or window_spans()
    lo, hi = 0, len(spans) - 1

    def has(i):
        # 搜索接口会随机返回 0（见 discover._count），0 要再查一次才信
        if _total_until(s, game, spans[i][2], spans[0][1]) > 0:
            return True
        time.sleep(1.0)
        return _total_until(s, game, spans[i][2], spans[0][1]) > 0

    if not has(hi):
        return hi
    while lo < hi:
        mid = (lo + hi) // 2
        if has(mid):
            hi = mid
        else:
            lo = mid + 1
    return max(0, lo - _lead_months())


def collect(game, y0=None, y1=None, quiet=False):
    """把一款游戏在统计窗口内的数据捞下来。返回统计。

    y0/y1 是可选的年份上下界，只用来补历史（比如只补 2020~2024），
    不传就从 HIT_WINDOW_START_YEAR 起二分找起点。存库是 INSERT OR REPLACE，补历史不会动已有行。
    """
    ensure()
    s = Searcher(9222)
    t0 = time.time()
    allv = {}
    try:
        active = window_spans(y0)
        if not y0:
            active = active[lifecycle_start(s, game, active):]
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
                    except bili.RiskError as e:
                        bili.note_risk(e)
                        raise
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
    lst = s.space_videos(mid, n)
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
        if bili.paused():
            return None
        try:
            b = up_baseline(s, r["mid"])
        except bili.RiskError as e:
            bili.note_risk(e)
            return None
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
