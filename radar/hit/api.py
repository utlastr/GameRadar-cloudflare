"""成功案例页的接口数据。"""
import time

from . import profile


def api_list():
    games = profile.all_games()
    out = []
    for g in games:
        st = profile.case_state(g["gid"]) or {}
        out.append({
            "id": g["gid"],
            "name": _name(g["gid"]),
            "n_video": g["n_video"], "n_up": g["n_up"],
            "play_sum": g["play_sum"], "play_median": g["play_median"],
            "play_top1": g["play_top1"],
            "start": g.get("first_day"), "end": g.get("last_day"),
            "burst": (g.get("lifecycle") or {}).get("burst"),
            "peak_day": (g.get("lifecycle") or {}).get("peak_day"),
            "peak_video": (g.get("lifecycle") or {}).get("peak_video"),
            "half_day": (g.get("lifecycle") or {}).get("half_day"),
            "state": st.get("state"),
            "closed_day": st.get("closed_day"),
            "reopen_day": st.get("reopen_day"),
            "ratio": st.get("ratio"),
            "days_since_last": st.get("days_since_last"),
        })
    return {"items": out, "count": len(out), "updated": int(time.time()),
            "rule": profile.CLOSE_RULE}


def _name(gid):
    from .bsearch import games
    for g in games():
        if g["gid"] == gid:
            return g["name"]
    return gid


def api_game(gid):
    if not gid:
        return {"error": "缺少 id"}
    s = profile.summary(gid)
    if not s:
        return {"error": "这个游戏还没有数据：%s" % gid}
    d = profile.daily(gid)
    return {
        "id": gid, "name": _name(gid),
        "summary": s,
        "state": profile.case_state(gid),
        "daily": d,
        "up_structure": profile.up_structure(gid),
        "top": profile.top_videos(gid, 20),
        "note": "播放量是视频现在的总播放，不是当天涨的；逐日表的播放=当天发布的视频今天有多少播放。",
    }
