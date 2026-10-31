"""刚起势（浮萍榜）：现在还小、但已经连着几天按比例往上涨的游戏。

思路（浮萍原理）：浮萍每天翻一倍，铺满池塘前一天才盖住一半。榜首那种已经铺满了，没意义；
要找的是"还只盖住 1/8、1/16，但每天都在翻"的。所以只看两件事：
  1. 连着几天，每天都比前一天涨了至少 MIN_DAILY 倍（不是某一天突然跳一下）
  2. 现在还不在本平台前 TOP_SKIP 名（已经铺满的不要）

每个平台按最近 24 小时、24–48 小时……这样切成 7 段，每段取一个数：
  在线人数 / 主播数 / 观看人数：这一段里的最高值（去掉白天晚上的起伏）
  累计数（评论、下载、预约）：这一段里新增了多少（看的是"每天新增"有没有越来越多）
  榜单名次：这一段里最好的名次，换算成"大小" = 1 / 名次（第 40 名到第 20 名 = 大了一倍）；
           那一段没上榜，按"刚好在榜外"算
某一段程序没怎么在跑（抓取次数不到最多那段的 40%），这一段不算，连涨就断在这里。
"""
import math
import time
from collections import defaultdict

from . import config, db

DAY = 86400
WINDOWS = 7
MIN_DAILY = 1.15        # 每天至少涨 15% 才算"涨了一天"
MIN_STREAK = 2          # 至少连涨 2 天（3 个点一直往上）
TOP_SKIP = 10           # 已经在本平台前 10 名的不算（已经铺满了）
KEEP = 60               # 每个平台最多给面板多少个

# (来源, 指标): (类型, 最低门槛, 说明)。门槛：最近一段至少要有这么多，太小的来回跳是噪音
LEVEL = {
    ("steam", "players"): (30, "Steam 每天最高在线"),
    ("roblox", "players"): (300, "Roblox 每天最高在线"),
    ("fortnite", "players"): (100, "Fortnite 每天最高在线"),
    ("kick", "viewers"): (100, "Kick 每天最高观看"),
    ("huya", "rooms"): (3, "虎牙每天最多主播数"),
    ("douyu", "rooms"): (3, "斗鱼每天最多主播数"),
    ("bili", "rooms"): (3, "B站直播每天最多主播数"),
    ("twitch", "rooms"): (3, "Twitch 每天最多主播数"),
}
CUM = {
    ("steam", "reviews"): (5, "Steam 每天新增评论"),
    ("taptap", "downloads"): (300, "TapTap 每天新增热度"),
    ("taptap", "reserves"): (300, "TapTap 每天新增预约"),
    ("tapmaker", "downloads"): (100, "TapTap 制造每天新增热度"),
}
LIVE = {"huya", "douyu", "bili", "twitch"}       # 直播分区某一段没抓到 = 那段没人播（算 0）


def _coverage(c, source, now):
    """每一段的抓取次数够不够。返回 {段号: True/False}。"""
    cnt = dict(c.execute("SELECT CAST((? - ts) / 86400 AS INTEGER) AS d, COUNT(DISTINCT ts) FROM metrics "
                         "WHERE source = ? AND ts > ? GROUP BY d", (now, source, now - (WINDOWS + 1) * DAY)))
    top = max(cnt.values()) if cnt else 0
    return {d: cnt.get(d, 0) >= max(1, 0.4 * top) for d in range(WINDOWS + 1)}


def _streak(sizes, ok, floor):
    """sizes[d]：第 d 段（0 = 最近 24 小时）的大小。返回 (连涨几天, 总共涨了几倍)。"""
    n = 0
    while n + 1 < WINDOWS and ok.get(n) and ok.get(n + 1) and sizes.get(n) is not None:
        prev = sizes.get(n + 1)
        if prev is None:
            break
        if sizes[n] / max(prev, floor) < MIN_DAILY:
            break
        n += 1
    if n == 0:
        return 0, 1.0
    return n, sizes[0] / max(sizes[n], floor)


def _rows(c, source, metric, now):
    """{实体: {段号: (最大, 最小)}}"""
    out = defaultdict(dict)
    for e, d, vmax, vmin in c.execute(
            "SELECT entity, CAST((? - ts) / 86400 AS INTEGER) AS d, MAX(value), MIN(value) FROM metrics "
            "WHERE source = ? AND metric = ? AND ts > ? AND entity != '__index__' GROUP BY entity, d",
            (now, source, metric, now - (WINDOWS + 1) * DAY)):
        out[e][d] = (vmax, vmin)
    return out


def _one(c, source, metric, kind, minv, label, now, ok):
    """一个平台的一个指标。返回 [{entity, ...}]。"""
    data = _rows(c, source, metric, now)
    if not data:
        return []
    sizes, shown = {}, {}
    if kind == "rank":
        board_n = {}
        for e, w in data.items():
            for d, (vmax, vmin) in w.items():
                board_n[d] = max(board_n.get(d, 0), vmax)
        for e, w in data.items():
            if 0 not in w:
                continue
            s, v = {}, {}
            for d in range(WINDOWS):
                if d in w:
                    s[d], v[d] = 1.0 / w[d][1], w[d][1]
                elif ok.get(d) and board_n.get(d):
                    s[d], v[d] = 1.0 / (board_n[d] + 1), None       # 那一段没上榜
            sizes[e], shown[e] = s, v
        floor = 1.0 / (max(board_n.values()) + 1)
    elif kind == "cum":
        for e, w in data.items():
            s, v = {}, {}
            for d in range(WINDOWS):
                if d in w and d + 1 in w:
                    s[d] = v[d] = max(0.0, w[d][0] - w[d + 1][0])
            if 0 in s:
                sizes[e], shown[e] = s, v
        floor = max(1.0, minv / 2)
    else:
        for e, w in data.items():
            if 0 not in w:
                continue
            s = {}
            for d in range(WINDOWS):
                if d in w:
                    s[d] = w[d][0]
                elif source in LIVE and ok.get(d):
                    s[d] = 0.0
            sizes[e], shown[e] = s, dict(s)
        floor = max(1.0, minv / 2)

    # 现在在本平台排第几（名次榜直接用名次）
    if kind == "rank":
        pos = {e: int(1 / s[0] + 0.5) for e, s in sizes.items()}
        v10 = None
    else:
        order = sorted(sizes, key=lambda e: -sizes[e][0])
        pos = {e: i + 1 for i, e in enumerate(order)}
        v10 = sizes[order[TOP_SKIP - 1]][0] if len(order) >= TOP_SKIP else None
    out = []
    for e, s in sizes.items():
        if kind != "rank" and s[0] < minv:
            continue
        if pos[e] <= TOP_SKIP:
            continue
        n, total = _streak(s, ok, floor)
        if n < MIN_STREAK:
            continue
        g = total ** (1.0 / n)
        # 照这个速度几天进前 10
        if kind == "rank":
            gap = pos[e] / TOP_SKIP
        else:
            gap = (v10 / s[0]) if v10 else None
        days = math.log(gap) / math.log(g) if gap and gap > 1 and g > 1 else None
        series = [shown[e].get(d) for d in range(WINDOWS - 1, -1, -1)]
        out.append({"entity": e, "metric": metric, "kind": kind, "label": label, "streak": n,
                    "daily": round(g, 2), "total": round(total, 2), "pos": pos[e],
                    "now": shown[e].get(0), "then": shown[e].get(n),
                    "gap": round(gap, 1) if gap else None, "days": round(days, 1) if days else None,
                    "series": series})
    return out


def text(r):
    """必看卡片上的一句话（不带括号）。"""
    if r["kind"] == "rank":
        then = f"第 {int(r['then'])} 名" if r.get("then") else "榜外"
        return f"刚起势：{r['label']}连升 {r['streak']} 天，{then}→第 {int(r['now'])} 名"
    return f"刚起势：{r['label']}连涨 {r['streak']} 天，每天 ×{r['daily']:.1f}，现在第 {r['pos']} 名"


def hot_of(r):
    """进必看用的热度：连涨期间一共涨 2 倍 = 0.5，4 倍 = 0.7，8 倍及以上 = 0.9。每天涨得太慢的不给。"""
    if r["daily"] < 1.25:
        return 0.0
    return round(min(0.9, 0.3 + 0.2 * math.log2(max(r["total"], 1.0))), 3)


def compute(entities):
    """返回 {来源: [行]}，每个游戏只留最猛的一个指标，按"连涨期间一共涨了几倍"排。"""
    now = int(time.time())
    specs = []
    for (s, m), (minv, label) in LEVEL.items():
        specs.append((s, m, "level", minv, label))
    for (s, m), (minv, label) in CUM.items():
        specs.append((s, m, "cum", minv, label))
    for (s, m), (kind, label) in config.RANK_BOARDS.items():
        if kind != "info":
            specs.append((s, m, "rank", 0, label))
    by_src = defaultdict(dict)
    with db.session() as c:
        cov = {}
        for s, m, kind, minv, label in specs:
            if not config.source_on(s):
                continue
            if s not in cov:
                cov[s] = _coverage(c, s, now)
            if not cov[s].get(0):
                continue          # 最近 24 小时基本没抓到
            for r in _one(c, s, m, kind, minv, label, now, cov[s]):
                old = by_src[s].get(r["entity"])
                if not old or r["total"] > old["total"]:
                    by_src[s][r["entity"]] = r
    out = {}
    for s, rows in by_src.items():
        lst = []
        for e, r in rows.items():
            ent = entities.get((s, e)) or {}
            x = ent.get("extra") or {}
            if x.get("generic"):
                continue
            r = dict(r, key=f"{s}:{e}", id=e, name=ent.get("name") or e, url=x.get("url") or "")
            r["text"] = text(r)
            r["hot"] = hot_of(r)
            lst.append(r)
        lst.sort(key=lambda r: (-r["total"], -r["streak"]))
        out[s] = lst
    return out
