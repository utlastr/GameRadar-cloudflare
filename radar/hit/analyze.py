"""历史游戏的过程曲线和相似匹配（方案第三、四步）。

能算什么、不能算什么，先说清楚：
- **能**：每个游戏每天新增多少视频、多少个新 UP 主。每周必看给了每条视频的
  发布时间和最终播放，所以"按天新增"是精确的。
- **不能**：历史上"当天累计播放"。接口只给最终播放，没有当时的快照。
  所以曲线用"当天新增视频的最终播放之和"，不等于当时的热度，会高估早期。
  这一条不许含糊，界面上要写明。
- 真实累计播放要靠第二期的每日快照（雷达自己的采集本来就在做这件事）。
"""
import json
import math
import time

from .. import db


def _videos(gid):
    """这个游戏所有视频（用首次进必看的那一周兜底，必看没有就按名字再算）。"""
    with db.session() as c:
        rows = c.execute(
            "SELECT v.bvid, v.title, v.tid, v.play, v.mid, v.pubdate, v.first_week "
            "FROM hit_games g JOIN hit_videos v ON 1 = 1 WHERE g.gid = ?", (gid,)).fetchall()
        g = c.execute("SELECT * FROM hit_games WHERE gid = ?", (gid,)).fetchone()
    if not g:
        return [], None
    # 只保留标题里确实出现这个名字的视频
    name = g["name"]
    out = [dict(r) for r in rows if name and name in (r["title"] or "")]
    return out, dict(g)


def curve(gid, days=45):
    """按起爆日对齐的每日曲线。

    返回 [{d, n_video, n_up, play}]，d=0 是起爆日。
    起爆日 = 这个游戏最早那条视频的发布时间（没有发布时间就用第一周估算）。
    """
    vids, g = _videos(gid)
    if not vids:
        return []
    stamps = [int(v["pubdate"] or 0) for v in vids if v["pubdate"]]
    if not stamps:
        # 没发布时间：用第一周那期的记录时间粗略对齐（误差以周计，只在兜底时用）
        with db.session() as c:
            r = c.execute("SELECT MIN(ts) FROM hit_weekly WHERE bvid = ?", (vids[0]["bvid"],)).fetchone()
        t0 = int(r[0] or 0) if r else 0
        if not t0:
            return []
    else:
        t0 = min(stamps)
    day0 = int(t0 // 86400 * 86400)

    buckets = {}
    seen_up = {}
    for v in vids:
        p = int(v["pubdate"] or 0)
        if not p:
            continue
        d = int((p - day0) // 86400)
        if d < 0 or d > days:
            continue
        b = buckets.setdefault(d, {"n_video": 0, "play": 0, "ups": set()})
        b["n_video"] += 1
        b["play"] += int(v["play"] or 0)
        b["ups"].add(v["mid"])
    out, cum_up, cum_v = [], 0, 0
    for d in sorted(buckets):
        cum_up += len(buckets[d]["ups"])
        cum_v += buckets[d]["n_video"]
        out.append({"d": d, "n_video": buckets[d]["n_video"], "n_up": len(buckets[d]["ups"]),
                    "play": buckets[d]["play"], "cum_video": cum_v, "cum_up": cum_up})
    return out


def burst_day(gid):
    """起爆日：视频第一次进必看的那一周对应的最早视频日（d=0 的定义）。"""
    c = curve(gid, 3)
    return c[0]["d"] if c else None


def first_week(gid):
    with db.session() as c:
        r = c.execute("SELECT first_week, first_ts, n_video, n_up, play_sum, name, state "
                      "FROM hit_games WHERE gid = ?", (gid,)).fetchone()
    return dict(r) if r else {}


def shape(c, n_days=7):
    """取前 n 天的形状，归一化成向量，用来算相似度。"""
    vec = []
    for d in range(n_days):
        row = next((x for x in c if x["d"] == d), None)
        vec.append(float(row["n_video"]) if row else 0.0)
    s = sum(vec)
    if s <= 0:
        return None
    return [x / s for x in vec]


def _dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


_CURVES = {"ts": 0, "data": {}}


def _all_shapes(n_days=7, min_video=2):
    if time.time() - _CURVES["ts"] < 600 and _CURVES["data"]:
        return _CURVES["data"]
    with db.session() as c:
        gids = [r[0] for r in c.execute(
            "SELECT gid FROM hit_games WHERE n_video >= ? ORDER BY n_video DESC LIMIT 800", (min_video,))]
    out = {}
    for gid in gids:
        c = curve(gid, min(n_days, 45))
        if not c:
            continue
        sh = shape(c, n_days)
        if sh:
            out[gid] = sh
    _CURVES.update(ts=time.time(), data=out)
    return out


def similar(gid, n_days=7, k=15):
    """找出起势方式最像的历史游戏（第四步的核心）。"""
    mine = _all_shapes(n_days).get(gid)
    if not mine:
        return []
    pool = _all_shapes(n_days)
    scored = []
    for other, sh in pool.items():
        if other == gid:
            continue
        scored.append((_dist(mine, sh), other))
    scored.sort()
    out = []
    for dist, other in scored[:k]:
        fw = first_week(other)
        out.append({"gid": other, "name": fw.get("name"), "dist": round(dist, 4),
                    "first_week": fw.get("first_week"), "n_video": fw.get("n_video"),
                    "n_up": fw.get("n_up"), "play_sum": fw.get("play_sum")})
    return out


def distribution(bins=(1, 3, 5, 10, 20, 50, 100)):
    """所有历史游戏的规模分布，给用户划"成功"用（方案第二步结尾）。"""
    with db.session() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT gid, name, first_week, n_video, n_up, play_sum, state FROM hit_games "
            "ORDER BY n_video DESC")]
    out = []
    for lo in bins:
        n = sum(1 for r in rows if (r["n_video"] or 0) >= lo)
        out.append({"至少视频数": lo, "游戏数": n})
    return {"rows": rows, "bins": out, "total": len(rows)}
