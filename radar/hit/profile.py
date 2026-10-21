# -*- coding: utf-8 -*-
"""一款游戏的全部数据：逐日、UP 主结构、头部视频、生命周期。

数据来源 hit_topic（bsearch.py 捞的）+ hit_up（UP 主粉丝数）。
"""
import datetime
import statistics
import time

from .. import db

TIERS = [(0, 10000, "1万以下"), (10000, 100000, "1-10万"),
         (100000, 1000000, "10-100万"), (1000000, 10 ** 12, "100万以上")]


def _day(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _rows(gid):
    with db.session() as c:
        return [dict(r) for r in c.execute(
            "SELECT t.*, u.followers FROM hit_topic t LEFT JOIN hit_up u ON u.mid = t.mid "
            "WHERE t.gid = ? AND t.pubdate > 0 ORDER BY t.pubdate", (gid,))]


def daily(gid):
    """逐日表：每天新增视频、新增 UP、当天发布视频的总播放、各粉丝档视频数。"""
    rows = _rows(gid)
    if not rows:
        return []
    days = {}
    seen_up = set()
    for r in rows:
        d = _day(r["pubdate"])
        b = days.setdefault(d, {"n_video": 0, "n_up_new": 0, "play": 0, "ups": set(), "tiers": {}})
        b["n_video"] += 1
        b["play"] += int(r["play"] or 0)
        b["ups"].add(r["mid"])
        if r["mid"] not in seen_up:
            b["n_up_new"] += 1
            seen_up.add(r["mid"])
        tier = _tier(r.get("followers"))
        b["tiers"][tier] = b["tiers"].get(tier, 0) + 1
    out = []
    for d in sorted(days):
        b = days[d]
        out.append({"day": d, "n_video": b["n_video"], "n_up": len(b["ups"]),
                    "n_up_new": b["n_up_new"], "play": b["play"], "tiers": b["tiers"]})
    return out


def _tier(f):
    if f is None:
        return "未知"
    for lo, hi, name in TIERS:
        if lo <= f < hi:
            return name
    return "未知"


def up_structure(gid):
    """UP 主结构：每个粉丝档有多少 UP、多少视频、占总播放多少。"""
    rows = _rows(gid)
    total_play = sum(int(r["play"] or 0) for r in rows) or 1
    buckets = {}
    for r in rows:
        t = _tier(r.get("followers"))
        b = buckets.setdefault(t, {"n_up": set(), "n_video": 0, "play": 0})
        b["n_up"].add(r["mid"])
        b["n_video"] += 1
        b["play"] += int(r["play"] or 0)
    order = [x[2] for x in TIERS] + ["未知"]
    out = []
    for t in order:
        if t not in buckets:
            continue
        b = buckets[t]
        out.append({"tier": t, "n_up": len(b["n_up"]), "n_video": b["n_video"],
                    "play": b["play"], "play_pct": round(b["play"] / total_play, 4)})
    return out


def top_videos(gid, k=20):
    rows = sorted(_rows(gid), key=lambda r: -(r["play"] or 0))[:k]
    return [{"bvid": r["bvid"], "title": (r["title"] or "")[:60], "up": r["up"] or "",
             "followers": r.get("followers"), "tier": _tier(r.get("followers")),
             "play": r["play"], "day": _day(r["pubdate"]),
             "url": "https://www.bilibili.com/video/" + r["bvid"]} for r in rows]


def lifecycle(gid):
    """起点、起爆日、峰值、回落一半、总量。"""
    rows = _rows(gid)
    if not rows:
        return {}
    s = _series(rows)
    # 起爆日：第一次满足"当天 ≥3 条，且是此前 30 天日均的 3 倍以上"。
    # 用补零后的日历序列 + 30 天回溯，是因为"此前"必须按日历算：赌石 2024-07 有
    # 一条、之后 21 个月一条都没有，它的起势日是从零到 3 条的那天，不是那条孤片。
    burst, best = None, 0
    for i, x in enumerate(s):
        if x["n"] < 3:
            continue
        lo = max(0, i - 30)
        base = sum(y["n"] for y in s[lo:i]) / float(max(i - lo, 1))
        if x["n"] >= max(3, base * 3):
            burst, best = x["day"], i
            break
    peak = max(s, key=lambda x: x["n"])
    # 从峰值回落一半：按 7 天滚动
    half_day, rolled = None, []
    for x in s:
        rolled.append(x["n"])
        rolled = rolled[-7:]
        if x["day"] > peak["day"] and sum(rolled) / len(rolled) <= peak["n"] / 2.0:
            half_day = x["day"]
            break
    total_play = sum(int(r["play"] or 0) for r in rows)
    ups = {r["mid"] for r in rows}
    return {"start": s[0]["day"], "end": s[-1]["day"], "days": len(s),
            "burst": burst, "peak_day": peak["day"], "peak_video": peak["n"],
            "half_day": half_day, "n_video": len(rows), "n_up": len(ups),
            "play": total_play,
            "avg_video_per_day": round(len(rows) / float(len(s)), 2)}


def summary(gid):
    rows = _rows(gid)
    if not rows:
        return {}
    lc = lifecycle(gid)
    plays = [int(r["play"] or 0) for r in rows]
    plays.sort()
    up = up_structure(gid)
    return {"gid": gid, "n_video": len(rows), "n_up": len({r["mid"] for r in rows}),
            "play_sum": sum(plays), "play_median": plays[len(plays) // 2],
            "play_top1": plays[-1] if plays else 0,
            "lifecycle": lc, "up_structure": up,
            "first_day": lc.get("start"), "last_day": lc.get("end")}


# ---------- 结案判定 ----------
# 为什么不用"视频数下降"结案：存量 UP 会惯性产出。竞拍之王 2026-10 还有 42 个新 UP、
# 97 条视频，看着没死，但单条播放中位从 2249 掉到 559、头部从 31.8 万掉到 1.4 万。
# 人在做，没人看——市场已经判它死了。所以三条一起卡，缺一条都会被惯性骗。
# 结案判定用"日总播放"而不是"单条播放中位"：一条视频只剩几只的时候，中位数
# 剧烈抖动，会把已经死掉的游戏卡在门线上（竞拍之王就卡在 17.5% vs 15%）。
# 日总播放 = 条数 × 单条播放，体量掉了就是掉了，不会被少数几条视频带偏。
CLOSE_RULE = {"play_total": 0.03, "new_up": 0.10, "days": 14}
REOPEN_RULE = {"new_up": 0.20, "play_total": 0.10, "days": 3}
ROLL = 7                                     # 滚动窗口，单日数字太抖


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return 0
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) // 2


def _series(rows):
    """逐日的 视频数 / 新UP / 单条播放中位 / 头部播放 / 总播放。

    中间没有视频的日子必须补 0：不补的话，5 月最后一有数据的天会和 10 月第一天
    直接挨在一起，滚动窗口把两段完全不同的时期混在一起算，结案判定就废了。
    """
    days, seen = {}, set()
    for r in rows:
        d = _day(r["pubdate"])
        b = days.setdefault(d, {"n": 0, "new": 0, "plays": []})
        b["n"] += 1
        b["plays"].append(int(r["play"] or 0))
        if r["mid"] not in seen:
            b["new"] += 1
            seen.add(r["mid"])
    if not days:
        return []
    first = datetime.datetime.strptime(min(days), "%Y-%m-%d")
    last = datetime.datetime.strptime(max(days), "%Y-%m-%d")
    out, cur = [], first
    while cur <= last:
        d = cur.strftime("%Y-%m-%d")
        b = days.get(d)
        if b:
            out.append({"day": d, "n": b["n"], "new": b["new"],
                        "med": _median(b["plays"]), "top": max(b["plays"]),
                        "total": sum(b["plays"])})
        else:
            out.append({"day": d, "n": 0, "new": 0, "med": 0, "top": 0, "total": 0})
        cur += datetime.timedelta(days=1)
    return out


def _roll(series, key):
    """滚动 ROLL 天的均值（含当天）。"""
    out, buf = [], []
    for x in series:
        buf.append(x[key])
        buf = buf[-ROLL:]
        out.append(sum(buf) / float(len(buf)))
    return out


def case_state(gid):
    """这款游戏现在是什么状态：活体 / 尾巴期 / 已结案 / 二次起爆。

    峰值基准取"起爆日到峰值日"这段窗口里的真实数字（不是整段历史的最高单日，
    那样会被一条偶然爆款带偏）。
    """
    rows = _rows(gid)
    if not rows:
        return {}
    series = _series(rows)
    lc = lifecycle(gid)
    # 没有起爆日就是"没起爆"，不是"起爆了又死"。赌石是缓慢起势（每天 1~5 条），
    # 永远触发不了"≥3 条且超此前均值 3 倍"，硬拿第一天当起爆点会得出
    # "2024-07 结案、2026-05 二次起爆"这种荒唐结论——踩过第二次。
    if not lc.get("burst"):
        return {"state": "未识别起势", "closed_day": None, "reopen_day": None,
                "peak": {}, "now": {}, "ratio": {},
                "last_day": series[-1]["day"], "days_since_last": None,
                "n_video": len(rows),
                "note": "增长太平缓，没有满足起爆定义（≥3 条且超此前日均 3 倍）的那一天，"
                        "不做结案判定",
                "rule": CLOSE_RULE}
    burst = lc["burst"]
    peak_day = lc.get("peak_day") or series[-1]["day"]

    win = [x for x in series if burst <= x["day"] <= peak_day] or series
    peak_n = max(x["n"] for x in win)
    peak_new = max(x["new"] for x in win)
    peak_med = _median([x["med"] for x in win if x["med"] > 0])
    peak_top = max(x["top"] for x in win)
    peak_total = max(x["total"] for x in win)

    # 样本太薄就别下结论。峰值中位播放都取不到，说明这款游戏没有足够多的
    # 正常视频来建立基线，任何"结案/活体"判断都是噪声。
    if not peak_med or not peak_total or len(rows) < 30 or max(x["n"] for x in series) < 3:
        return {"state": "样本不足", "closed_day": None, "reopen_day": None,
                "peak": {"n": peak_n, "new_up": peak_new, "median_play": peak_med,
                         "top_play": peak_top, "total_play": peak_total},
                "now": {}, "ratio": {}, "last_day": series[-1]["day"],
                "days_since_last": None, "n_video": len(rows),
                "note": "视频太少（%d 条），峰值基线建立不起来，不做结案判定" % len(rows),
                "rule": CLOSE_RULE}

    roll_n, roll_new = _roll(series, "n"), _roll(series, "new")
    roll_med, roll_top = _roll(series, "med"), _roll(series, "top")
    roll_total = _roll(series, "total")

    # 结案只在"起爆之后"才有意义。起爆前那段零星视频（铸剑大师 2025-04~2026-09
    # 一年半只有 19 条）不是"死了"，是"还没起来"。混在一起判会得出"结案一年半后
    # 二次起爆"这种荒唐结论——踩过。
    # 结案从"峰值日之后"才开始判，不是从起爆日。起爆后还在爬升的那几周，绝对量
    # 本来就远低于将来的峰值——从起爆日就判会得出"3 月起爆、3 月结案、5 月二次起爆"
    # 这种自相矛盾的结论（竞拍之王踩过）。
    peak_i = next((i for i, x in enumerate(series) if x["day"] >= peak_day), len(series) - 1)

    def closed_at(i):
        return (roll_total[i] < CLOSE_RULE["play_total"] * peak_total
                and roll_new[i] < CLOSE_RULE["new_up"] * peak_new)

    closed_day, run, closed_i = None, 0, None
    for i in range(peak_i, len(series)):
        run = run + 1 if closed_at(i) else 0
        if run >= CLOSE_RULE["days"]:
            closed_day = series[i - CLOSE_RULE["days"] + 1]["day"]
            closed_i = i
            break

    reopen_day = None
    if closed_i is not None:
        run = 0
        for i in range(closed_i + 1, len(series)):
            ok = (roll_new[i] >= REOPEN_RULE["new_up"] * peak_new
                  and roll_total[i] >= REOPEN_RULE["play_total"] * peak_total)
            run = run + 1 if ok else 0
            if run >= REOPEN_RULE["days"]:
                reopen_day = series[i - REOPEN_RULE["days"] + 1]["day"]
                break

    now_med, now_top, now_new = roll_med[-1], roll_top[-1], roll_new[-1]
    now_total = roll_total[-1]
    if len(series) - 1 - peak_i < 3:
        state = "活体"                          # 刚到峰值，还没有后续可判
    elif closed_day and reopen_day:
        state = "二次起爆"
    elif closed_day:
        state = "已结案"
    elif now_total < CLOSE_RULE["play_total"] * peak_total and now_new < CLOSE_RULE["new_up"] * peak_new:
        state = "尾巴期"
    else:
        state = "活体"

    try:
        since = (datetime.date.today()
                 - datetime.datetime.strptime(series[-1]["day"], "%Y-%m-%d").date()).days
    except Exception:                         # noqa: BLE001
        since = None

    return {
        "state": state,
        "closed_day": closed_day,
        "reopen_day": reopen_day,
        "peak": {"n": peak_n, "new_up": peak_new, "median_play": peak_med,
                 "top_play": peak_top, "total_play": peak_total},
        "now": {"n": round(roll_n[-1], 1), "new_up": round(now_new, 2),
                "median_play": now_med, "top_play": now_top,
                "total_play": int(now_total)},
        "ratio": {"n": round(roll_n[-1] / peak_n, 3) if peak_n else 0,
                  "new_up": round(now_new / peak_new, 3) if peak_new else 0,
                  "median_play": round(now_med / peak_med, 3) if peak_med else 0,
                  "top_play": round(now_top / peak_top, 3) if peak_top else 0,
                  "total_play": round(now_total / peak_total, 3) if peak_total else 0},
        "last_day": series[-1]["day"],
        "days_since_last": since,
        "rule": CLOSE_RULE,
    }


def all_games():
    with db.session() as c:
        gids = [r[0] for r in c.execute("SELECT DISTINCT gid FROM hit_topic")]
    out = []
    for g in gids:
        s = summary(g)
        if s:
            out.append(s)
    out.sort(key=lambda x: -(x.get("play_sum") or 0))
    return out
