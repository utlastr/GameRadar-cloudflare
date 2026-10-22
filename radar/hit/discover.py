# -*- coding: utf-8 -*-
"""候选发现：不靠人报游戏名，找出"哪款游戏在 B站 上刚开始起势"。

## 候选从哪来：各平台游戏名 + B站 自己冒出来的名字（v72）

`entities` 表里躺着 14870 个游戏名，来自 Steam、TapTap、好游快爆、虎牙、斗鱼、
App Store、B站 等二十多个源，每天自动更新。这就是现成的候选池——不需要我们再
发明什么方法去找游戏名。

验证过：铸剑大师在 entities 里有（tapmaker / huya_t / douyu_t / bili_t 四个源），
竞拍之王也有（huya / douyu / bili）。v72 起再加三路 B站 自己的名字：没对上商店的 B站 话题、
B站 话题档案、每周必看游戏视频的书名号——不要求别的平台先收录。

## 怎么验：B站 上的加速比

一个名字有了，不代表它在 B站 起势。要查两件事：
  最近 7 天有多少条视频（now）
  过去 90~180 天有多少条（past）
加速比 = (now/7) ÷ (past/90)

铸剑大师：现在 200+ 条/月，过去 90~180 天几乎为 0 → 加速比几百倍。
跑了三年的老游戏：两个窗口差不多 → 加速比 1 倍左右。

为什么不用"以前有没有"：铸剑大师 2025-04 就有零星视频，用有无判断会把它当老词丢掉。

## 走过的弯路（别再走）

- 泛词搜索结果里根本搜不到刚起势的游戏（搜"游戏"三天 336 条，铸剑大师 0 条）。
- 标签接口每条 4.6 秒，且实测头部视频标签里没有游戏名。
- 排行榜 ranking/v2 全是官方 PV，跟"游戏火不火"无关。
- 游戏区最新列表 250 条样本太小，一个游戏名只出现一两次，凑不出频次。
"""
import datetime
import re
import time

from .. import config, db
from ..sources import bili
from . import bsearch

RECENT_DAYS = 7
PAST_FROM, PAST_TO = 90, 180
MAX_CHECK = 60             # 工程参数：每轮最多核 60 个名字，控制请求量
SKIP_SOURCES = {"roblox", "fortnite", "poki", "crazygames", "kick"}   # 这几个源不是国内手游/端游
CN_SOURCES = {"tapmaker", "haoyou", "mistore", "appcn", "huya", "douyu", "bili",
              "bili_t", "dymini", "wxmini", "huya_t", "douyu_t", "opp"}
CJK = re.compile(r"[\u4e00-\u9fff]")
LIMIT = 1000               # B站搜索接口单次查询的上限（接口事实，不是判断标准）


def rule():
    """起势判断用的数字，全部是临时默认值，待校准；在 settings.json 的 HIT_DISCOVER_RULE 里改。"""
    d = {"min_recent": 8, "min_speedup": 5.0, "min_past": 20, "zero_past": 5, "zero_min_now": 20,
         "min_title_hit": 0.40, "min_title_n": 5}
    d.update(getattr(config, "HIT_DISCOVER_RULE", None) or {})
    return d


def _ts(days_ago):
    d = datetime.date.today() - datetime.timedelta(days=days_ago)
    return int(datetime.datetime(d.year, d.month, d.day).timestamp())


def _bili_names(days):
    """B站 自己冒出来的名字：没对上商店的 B站 话题、B站 话题档案、每周必看游戏视频的书名号。"""
    since = int(time.time()) - days * 86400
    out = []
    with db.session() as c:
        for sql, args, tag in (
                ("SELECT name FROM opp_unknown WHERE status = 'pending' ORDER BY n_up DESC LIMIT 300", (), "B站未对上"),
                ("SELECT name_cn FROM opp_games WHERE gid LIKE 'btopic:%' AND state_ts > ? LIMIT 300", (since,), "B站话题"),
                ("SELECT title, tid, tname FROM hit_weekly WHERE pubdate > ?", (since,), "每周必看")):
            try:
                rows = c.execute(sql, args).fetchall()
            except Exception:                     # noqa: BLE001
                continue
            for r in rows:
                if tag == "每周必看":
                    if bili.tid_of(dict(r)) not in bili.GAME_TIDS:
                        continue
                    for n in bili.bracket_names(r["title"] or ""):
                        out.append((n, tag))
                elif r[0]:
                    out.append((r[0], tag))
    return out


def pool(days=30, limit=400):
    """候选池：国内平台里最近还在活跃的游戏名 + B站 自己冒出来的名字（不要求别的平台先有）。"""
    bsearch.ensure_cfg()
    now = int(time.time())
    known = set()
    for g in bsearch.games():
        known.add(bili.normalize(g["name"]))
        known.update(bili.normalize(t) for t in g.get("title_terms") or [])
    out, seen = [], set()

    def add(name, srcs, last_seen=None, first_seen=None):
        name = (name or "").strip()
        k = bili.normalize(name)
        if not k or k in seen or k in known or not CJK.search(name) or not 2 <= len(name) <= 20:
            return
        seen.add(k)
        out.append({"name": name, "sources": srcs, "last_seen": last_seen, "first_seen": first_seen})

    for name, tag in _bili_names(days):
        add(name, [tag])
    with db.session() as c:
        rows = c.execute(
            "SELECT name, GROUP_CONCAT(DISTINCT source) AS srcs, MAX(last_seen) AS last_seen,"
            " MIN(first_seen) AS first_seen FROM entities"
            " WHERE last_seen > ? GROUP BY name"
            " ORDER BY COUNT(DISTINCT source) DESC, MAX(last_seen) DESC LIMIT ?",
            (now - days * 86400, limit * 8)).fetchall()
    for r in rows:
        srcs = set((r["srcs"] or "").split(","))
        if srcs and srcs <= SKIP_SOURCES:
            continue
        if not (srcs & CN_SOURCES):
            continue
        add(r["name"], sorted(srcs & CN_SOURCES), r["last_seen"], r["first_seen"])
    # B站 名字和各平台名字交替排，谁都不会被另一路挤出池子
    a = [x for x in out if x["last_seen"] is None]
    b = [x for x in out if x["last_seen"] is not None]
    mixed = []
    for i in range(max(len(a), len(b))):
        mixed += a[i:i + 1] + b[i:i + 1]
    return mixed[:limit]


def _count(s, name, beg, end, tries=3):
    """查一个时间窗口里的条数，抗接口抖动。

    B站 这个搜索接口会**随机返回 0**：同一个窗口同一秒内连查两次，一次 1000、
    一次 0（命运方舟、猛兽派对都实测到了）。所以最多查 3 次取最大值，并且只要任何一次
    拿到了结果列表，条数至少是列表长度。被风控直接抛出去，整轮停下。
    """
    best, got_list = 0, []
    for i in range(tries):
        try:
            r = s.search(name, beg, end, 1)
        except bili.RiskError:
            raise
        except Exception as e:                     # noqa: BLE001
            print("   窗口查询失败 %s：%s" % (name, e))
            time.sleep(0.8)
            continue
        total = int(r.get("total") or 0)
        lst = r.get("list") or []
        best = max(best, total, len(lst))
        if lst:
            got_list = lst
            break
        if i < tries - 1:
            time.sleep(0.8)
    return best, got_list


def check(s, name, recent=RECENT_DAYS):
    """查一个名字在 B站 上的加速比。返回 None 表示没起势或查不到。

    标题命中率挡分词误匹配：搜"火拼坦克大战"会把所有"坦克大战"的视频都算进 total。
    v72：接口顶到上限、分母太小这几种情况不再悄悄丢掉，而是留下并写明原因（flags），
    这类候选不自动回溯，等人看（state = 待人看）。
    """
    R = rule()
    now_n, lst = _count(s, name, _ts(recent), _ts(-1))
    if now_n < R["min_recent"]:
        return None
    flags = []
    if lst:
        hit = sum(1 for v in lst if name in (v.get("title") or ""))
        if hit < R["min_title_n"] or hit / float(len(lst)) < R["min_title_hit"]:
            return None
    if now_n >= LIMIT * 0.9:
        flags.append("近7天已接近接口上限 %d 条，量级大" % LIMIT)
    past_n, past_list = _count(s, name, _ts(PAST_TO), _ts(PAST_FROM))
    past_n = max(past_n, len(past_list))
    now_per_day = now_n / float(recent)
    past_per_day = past_n / float(PAST_TO - PAST_FROM)
    speedup = None
    if past_n >= LIMIT * 0.9:
        flags.append("过去也接近接口上限，一直很大，加速比算不准")
    elif past_n <= R["zero_past"]:
        if now_n < R["zero_min_now"]:
            return None
        speedup = 999.0
    elif past_n < R["min_past"]:
        flags.append("过去只有 %d 条，分母小，加速比不可信" % past_n)
        speedup = round(now_per_day / past_per_day, 1)
    else:
        speedup = now_per_day / past_per_day
        if speedup < R["min_speedup"]:
            return None
    score = None if (flags or speedup is None) else round(min(speedup, 999) * now_n / 100.0, 2)
    return {"now": now_n, "past": past_n, "speedup": round(speedup, 1) if speedup is not None else None,
            "score": score, "flags": flags}


def _cursor():
    with db.session() as c:
        c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
        r = c.execute("SELECT v FROM kv WHERE k='hit_disc_cursor'").fetchone()
    return int(r[0]) if r and r[0] else 0


def _set_cursor(v):
    with db.session() as c:
        c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
        c.execute("INSERT OR REPLACE INTO kv(k, v) VALUES('hit_disc_cursor', ?)", (str(v),))


def discover_once(limit_check=MAX_CHECK, quiet=False, days=30):
    """池子里轮转挑名字 → B站 验起势 → 写 hit_candidate。

    池子有几百个名字，一轮全查要半小时以上，会长时间压着接口。所以用 cursor
    轮转：每轮只查 limit_check 个，下一轮接着往后，跑满一圈再从头来。
    """
    all_names = pool(days=days, limit=400)
    if not all_names:
        print("  池子是空的")
        return 0
    start = _cursor() % max(len(all_names), 1)
    batch = all_names[start:start + limit_check]
    if len(batch) < limit_check:                  # 走到尾巴就绕回开头
        batch += all_names[:limit_check - len(batch)]
    if not quiet:
        print("  池子 %d 个名字，本轮从第 %d 个起查 %d 个"
              % (len(all_names), start, len(batch)))
    s = bsearch.Searcher(9222)
    kept = 0
    try:
        for c_ in batch:
            if bili.paused():
                break
            try:
                got = check(s, c_["name"])
            except bili.RiskError as e:
                bili.note_risk(e)
                break
            if not got:
                time.sleep(0.4)
                continue
            if not quiet:
                print("   起势 %s  近7天=%d 过去90天=%d 加速比=%s 分=%s  [%s] %s"
                      % (c_["name"], got["now"], got["past"], got["speedup"],
                         got["score"], "/".join(c_["sources"]), "；".join(got["flags"])))
            with db.session() as c:
                c.execute("INSERT OR REPLACE INTO hit_candidate"
                          "(tag, n_up, n_video, play_sum, play_median, first_day, last_day,"
                          " novelty, score, state, ts, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (c_["name"], 0, got["now"], 0, 0,
                           datetime.date.today().strftime("%Y-%m-%d"),
                           datetime.date.today().strftime("%Y-%m-%d"),
                           got["speedup"], got["score"], "待人看" if got["flags"] else "待回溯",
                           int(time.time()),
                           "近7天%d条/过去90天%d条/来源%s%s"
                           % (got["now"], got["past"], "/".join(c_["sources"]),
                              ("/" + "；".join(got["flags"])) if got["flags"] else "")))
            kept += 1
            time.sleep(0.4)
    finally:
        s.close()
    _set_cursor(start + len(batch))
    if not quiet:
        print("  本轮写入候选 %d 个，游标推到 %d" % (kept, start + len(batch)))
    return kept


def backfill(tag, y0=None, quiet=False):
    """把候选加进清单并补齐完整历史。只做一次。

    不给 y0 时从 2018 起二分找这款游戏第一条视频的月份，再往前多取几个月（见 bsearch.lifecycle_start）。"""
    gid = bsearch.add_game(tag, kws=[tag], tags=[tag], title_terms=[tag],
                           source="auto", note="候选发现自动加入")
    g = [x for x in bsearch.games() if x["gid"] == gid][0]
    if not quiet:
        print("回溯 %s（%s）从 %s 年" % (tag, gid, y0 or "自动"))
    r = bsearch.collect(g, y0=y0)
    with db.session() as c:
        c.execute("UPDATE hit_candidate SET state='已回溯', note=? WHERE tag=?",
                  ("%d 条" % r["n"], tag))
    return r


def top_candidates(limit=50):
    bsearch.ensure_cfg()
    with db.session() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM hit_candidate ORDER BY score DESC LIMIT ?", (limit,))]
