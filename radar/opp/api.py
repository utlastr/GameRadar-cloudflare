"""机会雷达的接口数据拼装（server.py 只负责转发）。

第一期没有校准，所以**不给概率**——只给原始指标和百分位，界面上标"未校准"。
这是方案第 0 节的硬要求：成功门槛由用户的案例决定，程序不许自己定。
"""
import json
import time

from .. import config, db
from . import features

CALIBRATED = False          # 第三期接上校准以后由 opp_models 决定


def _feat(gid):
    with db.session() as c:
        r = c.execute("SELECT d, ts, data FROM opp_features WHERE gid = ? ORDER BY ts DESC LIMIT 1",
                      (gid,)).fetchone()
    if not r:
        return {}
    try:
        return json.loads(r[2])
    except ValueError:
        return {}


def api_list(state=""):
    """活着的游戏列表。按状态分组，组内按播放排序（没有概率可排）。"""
    with db.session() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT gid, name_cn, t0, state, state_ts, source FROM opp_games ORDER BY state, t0 DESC")]
    now = int(time.time())
    out = []
    for g in rows:
        if state and g["state"] != state:
            continue
        f = _feat(g["gid"])
        t0 = int(g.get("t0") or 0)
        item = {
            "id": g["gid"], "name": g["name_cn"], "state": g["state"] or "观察",
            "t0": t0, "age_h": round((now - t0) / 3600.0, 1) if t0 else None,
            "prob": None, "calibrated": CALIBRATED,
            "n_up": f.get("n_up"), "views_sum": f.get("views_sum"),
            "best_age_pct": f.get("best_age_pct"), "views_top1_share": f.get("views_top1_share"),
            "demand_mobile": f.get("demand_mobile"), "by_method": f.get("by_method"),
            "videos": f.get("videos"), "n_live": f.get("n_live"),
        }
        out.append(item)
    out.sort(key=lambda x: (-(x["views_sum"] or 0)))
    return {"items": out, "calibrated": CALIBRATED, "updated": now,
            "count": len(out)}


def api_game(gid):
    """单个 B站 话题 / Steam 成员：指标、相关视频（带对应方式）、曲线。"""
    if not gid:
        return {"error": "缺少 id"}
    with db.session() as c:
        g = c.execute("SELECT * FROM opp_games WHERE gid = ?", (gid,)).fetchone()
    if not g:
        return {"error": "没有这个游戏：%s" % gid}
    feat = features.game_features(gid)
    vids = features.game_videos(gid)
    out_videos = []
    for v in vids[-50:]:
        with db.session() as c:
            row = c.execute("SELECT title, pubdate, owner FROM opp_videos WHERE bvid = ?",
                            (v["bvid"],)).fetchone()
        out_videos.append({
            "bvid": v["bvid"], "title": (row["title"] if row else "")[:60],
            "owner": row["owner"] if row else "", "pubdate": v.get("pubdate"),
            "method": v.get("method"),
            "url": "https://www.bilibili.com/video/" + v["bvid"],
        })
    with db.session() as c:
        samples = [dict(r) for r in c.execute(
            "SELECT ts, view FROM opp_vsamples WHERE bvid IN (SELECT bvid FROM opp_vmap WHERE gid = ?) "
            "ORDER BY ts", (gid,))]
    curve = {}
    for s in samples:
        curve.setdefault(s["ts"], 0)
        curve[s["ts"]] += s["view"] or 0
    return {"id": gid, "name": g["name_cn"], "state": g["state"], "t0": g["t0"],
            "features": feat, "videos": out_videos,
            "curve": sorted([[k, v] for k, v in curve.items()]),
            "calibrated": CALIBRATED,
            "note": "未校准：只显示原始指标和百分位，不给概率"}
