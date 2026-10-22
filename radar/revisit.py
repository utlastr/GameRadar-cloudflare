"""抓取调度：一款游戏要不要逐个深查、多久查一次。

"深查" = 一款一款地发请求：打开 TapTap 详情页、查 Steam 在线人数和评论数、跟踪 B站 视频播放。
榜单、直播列表这类一次拿一整页的数据照常抓（不花额外请求），不受这里影响。

三层（业界做法）：
1. 永久不深查（客观事实，不打分）：
   - 所在游戏档案里已经有官方手机版 / 小游戏版（同开发商或发行商的手机端成员），而它自己不是手机端；
   - 名字里有大作名（KW_BIG_GAMES，和打分里"老牌大游戏"用的同一份名单）。
   名单在游戏档案重算时生成（archive.rebuild → kv revisit_skip）。
2. 自适应重访（指数退避，爬虫重访 / RSS 轮询的常用做法）：每次深查后，看主指标每天的增速在不在它自己过去的
   正常波动里。前 REVISIT_LEARN 次每轮都查；之后正常 → 间隔翻倍（1、2、4……小时，最多 REVISIT_MAX_HOURS）；
   增速比自己过去高出 REVISIT_Z 个稳健标准差 → 回到每轮都查。
3. 突变触发：便宜信号（榜单名次、B站 当天新视频数）按天记，今天比自己过去的日常高出 REVISIT_Z 个稳健标准差
   → 立刻回到每轮都查。稳健标准差 = 1.4826 × MAD（中位数绝对偏差），3 倍是统计上的惯例。
"""
import json
import math
import time
from collections import Counter

from . import config, db

MOBILE = {"taptap", "tapmaker", "appcn", "appus", "haoyou", "mistore", "wxmini", "dymini"}
_MAIN = ("players", "downloads", "fans", "reviews")


def _p(k, d):
    return getattr(config, k, d)


def median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def robust_z(x, hist):
    """x 比 hist 的中位数高出几个稳健标准差。历史少于 3 个点返回 None。"""
    if x is None or len(hist) < 3:
        return None
    med = median(hist)
    s = 1.4826 * median([abs(h - med) for h in hist])
    if s <= 0:
        return math.inf if x > med else (0.0 if x == med else -math.inf)
    return (x - med) / s


def _blank(member):
    return {"member": member, "last_ts": 0, "interval_h": 0.0, "n": 0, "last_v": None,
            "hist": "[]", "sig": "{}", "reason": ""}


def step(row, metrics, ts, learn=5, z_cut=3.0, max_h=168.0, keep=30):
    """一次深查之后更新调度状态（纯计算，tests 直接测）。返回新的 row。"""
    r = dict(row)
    v = next((metrics.get(k) for k in _MAIN if metrics.get(k) is not None), None)
    hist = json.loads(r.get("hist") or "[]")
    ivl = float(r.get("interval_h") or 0)
    n = int(r.get("n") or 0) + 1
    if v is not None and r.get("last_v") is not None and r.get("last_ts"):
        days = max((ts - r["last_ts"]) / 86400.0, 1 / 24.0)
        g = (math.log1p(max(v, 0)) - math.log1p(max(r["last_v"], 0))) / days
        z = robust_z(g, hist)
        hist = (hist + [g])[-keep:]
        if n < learn:
            ivl, reason = 0.0, "学习中"
        elif z is not None and z > z_cut:
            ivl, reason = 0.0, "增速异常"
        else:
            ivl, reason = min(max_h, max(1.0, ivl * 2)), "平稳"
    else:
        ivl, reason = 0.0, "学习中"
    r.update(last_ts=ts, interval_h=ivl, n=n, last_v=v if v is not None else r.get("last_v"),
             hist=json.dumps(hist), reason=reason)
    return r


def poke(row, value, day, z_cut=3.0, keep=30):
    """记一个便宜信号（每天取最大值）。返回 (新 row, 是否触发)。"""
    r = dict(row)
    sig = json.loads(r.get("sig") or "{}")
    hist = sig.get("hist") or []
    if sig.get("day") != day:
        if sig.get("day") is not None and sig.get("v") is not None:
            hist = (hist + [sig["v"]])[-keep:]
        sig = {"day": day, "v": value, "hist": hist}
    else:
        sig["v"] = max(sig.get("v") if sig.get("v") is not None else value, value)
    z = robust_z(sig["v"], hist)
    fired = z is not None and z > z_cut and float(r.get("interval_h") or 0) > 0
    if fired:
        r.update(interval_h=0.0, reason="便宜信号突增")
    r["sig"] = json.dumps(sig)
    return r, fired


class Plan:
    """一轮抓取用一个：先 due() 决定查不查，查完 done()，便宜信号 signal()，最后 flush()。"""

    def __init__(self, now=None):
        self.now = int(now or time.time())
        self.on = bool(_p("REVISIT_ENABLED", True))
        self.skip = db.kv_get("revisit_skip") or {}
        with db.session() as c:
            self.rows = {r["member"]: dict(r) for r in c.execute("SELECT * FROM revisit")}
        self.dirty = set()
        self.stat = Counter()

    def skipped(self, member):
        return self.on and member in self.skip

    def due(self, member):
        if not self.on:
            return True
        if member in self.skip:
            self.stat["skip"] += 1
            return False
        r = self.rows.get(member)
        if not r or not r.get("last_ts"):
            return True
        if self.now - r["last_ts"] >= float(r.get("interval_h") or 0) * 3600 - 600:
            return True
        self.stat["wait"] += 1
        return False

    def done(self, member, metrics, ts=None):
        r = self.rows.get(member) or _blank(member)
        self.rows[member] = step(r, metrics or {}, int(ts or time.time()), int(_p("REVISIT_LEARN", 5)),
                                 float(_p("REVISIT_Z", 3.0)), float(_p("REVISIT_MAX_HOURS", 168)))
        self.dirty.add(member)
        self.stat["checked"] += 1

    def signal(self, member, value):
        if value is None:
            return
        r = self.rows.get(member) or _blank(member)
        r, fired = poke(r, float(value), time.strftime("%Y-%m-%d", time.localtime(self.now)),
                        float(_p("REVISIT_Z", 3.0)))
        self.rows[member] = r
        self.dirty.add(member)
        if fired:
            self.stat["fired"] += 1

    def flush(self):
        if not self.dirty:
            return
        cols = ("member", "last_ts", "interval_h", "n", "last_v", "hist", "sig", "reason")
        with db.session() as c:
            c.executemany("INSERT OR REPLACE INTO revisit(%s) VALUES (%s)" % (", ".join(cols), ",".join("?" * 8)),
                          [tuple(self.rows[m].get(k) for k in cols) for m in self.dirty])
        self.dirty.clear()

    def summary(self):
        s = self.stat
        return "深查 %d 款，按间隔跳过 %d 款，已有手机版 / 大作不查 %d 款，突增触发 %d 款" % (
            s["checked"], s["wait"], s["skip"], s["fired"])


def rank_value(metrics):
    """榜单名次当便宜信号：名次越前值越大（-log 名次）。没有名次返回 None。"""
    ranks = [v for k, v in (metrics or {}).items() if k.startswith("rank_") and v]
    return -math.log(min(ranks)) if ranks else None


def bili_signals(plan):
    """B站：每份档案当天新发的视频数，当便宜信号喂给档案里每个成员。"""
    since = plan.now - 86400
    try:
        with db.session() as c:
            rows = c.execute(
                "SELECT g.gid, COUNT(DISTINCT v.bvid) FROM opp_vmap m JOIN opp_videos v ON v.bvid = m.bvid "
                "JOIN game_members g ON g.member = m.gid WHERE v.pubdate >= ? GROUP BY g.gid", (since,)).fetchall()
            gids = {r[0] for r in rows}
            members = {}
            for r in c.execute("SELECT gid, member FROM game_members"):
                if r[0] in gids:
                    members.setdefault(r[0], []).append(r[1])
    except Exception:                             # noqa: BLE001
        return 0
    n = 0
    for gid, cnt in rows:
        for m in members.get(gid, []):
            if not m.startswith("btopic:"):
                plan.signal(m, cnt)
                n += 1
    return n


def skip_list(nodes, gid_map, big_words, dev_level):
    """生成永久不深查名单 {成员: 原因}（archive.rebuild 调用）。"""
    by_g = {}
    for n, info in nodes.items():
        for m in info["members"]:
            g = gid_map.get(m)
            if g:
                by_g.setdefault(g, []).append((m, n))
    out = {}
    for lst in by_g.values():
        mob = sorted({n for m, n in lst if m.split(":", 1)[0] in MOBILE})
        mdevs = [d for n in mob for d in nodes[n].get("devs") or []]
        if not mdevs:
            continue
        for m, n in lst:
            if m.split(":", 1)[0] in MOBILE or m.startswith("btopic:"):
                continue
            if dev_level(nodes[n].get("devs"), mdevs) == "same":
                out[m] = "已有官方手机版"
    for n, info in nodes.items():
        for m in info["members"]:
            src = m.split(":", 1)[0]
            for k in info.get("names") or []:
                hit = next((w for w in big_words if w and w in k and
                            not (src == "fortnite" and w in ("fortnite", "堡垒之夜"))), None)
                if hit:
                    out[m] = "大作（%s）" % hit
                    break
    return out


def report():
    skip = db.kv_get("revisit_skip") or {}
    with db.session() as c:
        rows = [dict(r) for r in c.execute("SELECT interval_h, reason FROM revisit")]
    by = Counter()
    for r in rows:
        h = float(r["interval_h"] or 0)
        by["每轮都查" if h <= 0 else "≤4 小时" if h <= 4 else "≤1 天" if h <= 24 else "≤1 周"] += 1
    return {"skip": len(skip), "skip_by": dict(Counter(v.split("（")[0] for v in skip.values())),
            "tracked": len(rows), "interval": dict(by), "reason": dict(Counter(r["reason"] or "" for r in rows)),
            "last": db.kv_get("revisit_last") or {}}
