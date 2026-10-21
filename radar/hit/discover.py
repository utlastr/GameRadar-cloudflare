# -*- coding: utf-8 -*-
"""候选发现：不靠人报游戏名，找出"哪款游戏在 B站 上刚开始起势"。

## 候选从哪来：雷达已经抓的各平台游戏名

`entities` 表里躺着 14870 个游戏名，来自 Steam、TapTap、好游快爆、虎牙、斗鱼、
App Store、B站 等二十多个源，每天自动更新。这就是现成的候选池——不需要我们再
发明什么方法去找游戏名。

验证过：铸剑大师在 entities 里有（tapmaker / huya_t / douyu_t / bili_t 四个源），
竞拍之王也有（huya / douyu / bili）。所以这条路是真能覆盖到我们要找的那类游戏。

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
import time

from .. import db
from . import bsearch

RECENT_DAYS = 7
PAST_FROM, PAST_TO = 90, 180
MIN_SPEEDUP = 5.0          # 最近是以前的 5 倍以上才算起势
MIN_RECENT = 8             # 最近 7 天至少 8 条视频，否则样本太薄
MIN_PAST = 20              # 过去 90~180 天至少 20 条；低于这个数，分母太小，加速比不可信
MIN_TITLE_HIT = 0.40       # 至少 40% 的结果标题里写着这个名字（挡分词误匹配）
MAX_CHECK = 60             # 每轮最多核 60 个名字，控制请求量
SKIP_SOURCES = {"roblox", "fortnite", "poki", "crazygames", "kick"}   # 这几个源不是国内手游/端游
# B站 用户会做视频的游戏，基本来自这几个源。Steam 新品里大量英文独立游戏，
# 中文 B站 几乎没人做，混进来只会浪费查询预算。
CN_SOURCES = {"tapmaker", "haoyou", "mistore", "appcn", "huya", "douyu", "bili",
              "bili_t", "dymini", "wxmini", "huya_t", "douyu_t", "opp"}
CJK = __import__("re").compile(r"[\u4e00-\u9fff]")


def _ts(days_ago):
    d = datetime.date.today() - datetime.timedelta(days=days_ago)
    return int(datetime.datetime(d.year, d.month, d.day).timestamp())


def pool(days=30, limit=400):
    """候选池：国内平台里最近还在活跃、中文名、且我们还没追的游戏。"""
    bsearch.ensure_cfg()
    now = int(time.time())
    known = set()
    for g in bsearch.games():
        known.add(g["name"])
        known.update(g.get("title_terms") or [])
    with db.session() as c:
        tracked = {r[0].split(":", 1)[-1] for r in c.execute("SELECT DISTINCT gid FROM hit_topic")}
        rows = c.execute(
            "SELECT name, GROUP_CONCAT(DISTINCT source) AS srcs, MAX(last_seen) AS last_seen,"
            " MIN(first_seen) AS first_seen FROM entities"
            " WHERE last_seen > ? GROUP BY name"
            " ORDER BY COUNT(DISTINCT source) DESC, MAX(last_seen) DESC LIMIT ?",
            (now - days * 86400, limit * 8)).fetchall()
    out, seen = [], set()
    for r in rows:
        name = (r["name"] or "").strip()
        if not name or name in seen:
            continue
        srcs = set((r["srcs"] or "").split(","))
        if srcs and srcs <= SKIP_SOURCES:
            continue
        if not (srcs & CN_SOURCES):               # 只从国内平台来的才考虑
            continue
        if not CJK.search(name):                  # 只要中文名
            continue
        if name in known or name in tracked:
            continue
        if len(name) < 2 or len(name) > 20:
            continue
        seen.add(name)
        out.append({"name": name, "sources": sorted(srcs & CN_SOURCES),
                    "last_seen": r["last_seen"], "first_seen": r["first_seen"]})
        if len(out) >= limit:
            break
    return out


def _count(s, name, beg, end, tries=3):
    """查一个时间窗口里的条数，抗接口抖动。

    B站 这个搜索接口会**随机返回 0**：同一个窗口同一秒内连查两次，一次 1000、
    一次 0（命运方舟、猛兽派对都实测到了）。这个 0 会把老游戏判成"从零起势"，
    是最危险的假阳性来源。所以：最多查 3 次取最大值，并且只要任何一次拿到了
    结果列表，就说明这个窗口里确实有东西，条数至少是列表长度。
    """
    best, got_list = 0, []
    for i in range(tries):
        try:
            r = s.search(name, beg, end, 1)
        except Exception as e:                     # noqa: BLE001
            print("   窗口查询失败 %s：%s" % (name, e))
            time.sleep(0.8)
            continue
        total = int(r.get("total") or 0)
        lst = r.get("list") or []
        if total > best:
            best = total
        if lst:
            got_list = lst
            if best < len(lst):
                best = len(lst)
            break                                  # 有结果就不用再打了
        if i < tries - 1:
            time.sleep(0.8)
    return best, got_list


def check(s, name, recent=RECENT_DAYS):
    """查一个名字在 B站 上的加速比。返回 None 表示没起势或查不到。

    关键的一步是标题命中率：B站 搜索是分词匹配的，搜"火拼坦克大战"会把所有
    "坦克大战"的视频都算进 total——30 条结果里没有一条标题写着"火拼坦克大战"。
    不过这道检查，短名字和通用词根会被大量误判成起势。
    """
    now_n, lst = _count(s, name, _ts(recent), _ts(-1))
    if now_n < MIN_RECENT:
        return None
    if now_n >= 900:
        # 最近一周就顶到接口上限，说明这游戏已经很大了，不是"早期起势"，
        # 而且饱和值让加速比算不准。三角洲行动、王者荣耀这种就该在这一步被挡掉。
        return None
    if lst:
        hit = sum(1 for v in lst if name in (v.get("title") or ""))
        ratio = hit / float(len(lst))
        if hit < 5 or ratio < MIN_TITLE_HIT:
            return None
    past_n, past_list = _count(s, name, _ts(PAST_TO), _ts(PAST_FROM))
    if past_list and past_n < len(past_list):
        past_n = len(past_list)
    now_per_day = now_n / float(recent)
    past_per_day = past_n / float(PAST_TO - PAST_FROM)
    # 饱和陷阱：过去窗口顶到 1000（接口上限）时，加速比恒等于 (1000/7)÷(1000/90)=12.9，
    # 王者荣耀、三角洲行动这种大游戏于是"通过"了——这是假象。过去就饱和 = 一直很热，
    # 不是起势，直接拒。
    if past_n >= 900:
        return None
    # 三种情况要分开：
    #   过去 <=5 条、现在很多   → 从零起势（铸剑大师就是这种：过去 4 条，现在 60+）
    #   过去 >=20 条、现在翻几倍 → 加速（老游戏被带起来）
    #   过去 6~19 条            → 模糊区，分母太小，加速比不可信，直接拒
    if past_n <= 5:
        if now_n < 20:
            return None
        speedup = 999.0
    elif past_n < 20:
        return None
    else:
        speedup = now_per_day / past_per_day
        if speedup < MIN_SPEEDUP:
            return None
    return {"now": now_n, "past": past_n, "speedup": round(speedup, 1),
            "score": round(min(speedup, 999) * now_n / 100.0, 2)}


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
            got = check(s, c_["name"])
            if not got:
                time.sleep(0.4)
                continue
            if not quiet:
                print("   起势 %s  近7天=%d 过去90天=%d 加速比=%.1f 分=%.1f  [%s]"
                      % (c_["name"], got["now"], got["past"], got["speedup"],
                         got["score"], "/".join(c_["sources"])))
            with db.session() as c:
                c.execute("INSERT OR REPLACE INTO hit_candidate"
                          "(tag, n_up, n_video, play_sum, play_median, first_day, last_day,"
                          " novelty, score, state, ts, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (c_["name"], 0, got["now"], 0, 0,
                           datetime.date.today().strftime("%Y-%m-%d"),
                           datetime.date.today().strftime("%Y-%m-%d"),
                           got["speedup"], got["score"], "待回溯", int(time.time()),
                           "近7天%d条/过去90天%d条/来源%s"
                           % (got["now"], got["past"], "/".join(c_["sources"]))))
            kept += 1
            time.sleep(0.4)
    finally:
        s.close()
    _set_cursor(start + len(batch))
    if not quiet:
        print("  本轮写入候选 %d 个，游标推到 %d" % (kept, start + len(batch)))
    return kept


def backfill(tag, y0=2020, quiet=False):
    """把候选加进清单并补齐完整历史。只做一次。"""
    gid = bsearch.add_game(tag, kws=[tag], tags=[tag], title_terms=[tag],
                           source="auto", note="候选发现自动加入")
    g = [x for x in bsearch.games() if x["gid"] == gid][0]
    if not quiet:
        print("回溯 %s（%s）从 %d 年" % (tag, gid, y0))
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
