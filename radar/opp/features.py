"""指标（方案第 5 节）。

一条铁律：算"第 d 天"的指标时，只能用 t0 + d 天以前的数据。回溯和实时都一样，
否则模型会学到未来信息，AUC 好看但没用。
"""
import json
import statistics
import time

from .. import db

VIEW_AT_HOURS = (2, 6, 12, 24, 48, 72)


def _samples(bvid, until=None):
    q = "SELECT ts, view, lk, coin, fav, share, reply, dm FROM opp_vsamples WHERE bvid = ?"
    args = [bvid]
    if until:
        q += " AND ts <= ?"
        args.append(until)
    q += " ORDER BY ts"
    with db.session() as c:
        return [dict(r) for r in c.execute(q, args)]


def _interp(samples, pubdate, hours):
    """发布后 h 小时的播放，用相邻两次采样线性插值。插不出来返回 None。"""
    target = pubdate + hours * 3600
    if not samples:
        return None
    if target <= samples[0]["ts"]:
        return None                       # 第一次采样已经晚于目标时间，不能倒推
    if target >= samples[-1]["ts"]:
        return None                       # 目标时刻还没采样到
    for a, b in zip(samples, samples[1:]):
        if a["ts"] <= target <= b["ts"]:
            span = b["ts"] - a["ts"]
            if span <= 0:
                return a["view"]
            k = (target - a["ts"]) / span
            return int(a["view"] + (b["view"] - a["view"]) * k)
    return None


def video_features(bvid, until=None):
    """单条视频的指标。"""
    with db.session() as c:
        row = c.execute("SELECT bvid, pubdate, mid, tid, tags FROM opp_videos WHERE bvid = ?",
                        (bvid,)).fetchone()
        if not row:
            return {}
        up = c.execute("SELECT followers, base_median, base_est FROM opp_uploaders WHERE mid = ?",
                       (row["mid"],)).fetchone()
    pub = int(row["pubdate"] or 0)
    s = _samples(bvid, until)
    if not s:
        return {}
    cur = s[-1]
    out = {"bvid": bvid, "pubdate": pub, "mid": row["mid"], "tid": row["tid"],
           "view": cur["view"], "age_h": round((cur["ts"] - pub) / 3600.0, 2) if pub else None}

    for h in VIEW_AT_HOURS:
        v = _interp(s, pub, h)
        if v is not None:
            out["views_at_%dh" % h] = v

    # 最近一段的增速，以及和上一段的比值
    if len(s) >= 2:
        a, b = s[-2], s[-1]
        dt = max(1.0, (b["ts"] - a["ts"]) / 3600.0)
        out["rate_h"] = round((b["view"] - a["view"]) / dt, 2)
        if len(s) >= 3:
            c0 = s[-3]
            dt0 = max(1.0, (a["ts"] - c0["ts"]) / 3600.0)
            prev = (a["view"] - c0["view"]) / dt0
            if prev > 0:
                out["accel"] = round(((b["view"] - a["view"]) / dt) / prev, 3)

    view = max(1, cur["view"] or 0)
    for k, col in (("coin", "coin"), ("fav", "fav"), ("share", "share"),
                   ("reply", "reply"), ("dm", "dm"), ("like", "lk")):
        out["r_" + k] = round((cur[col] or 0) / view, 5)

    if up:
        followers = up["followers"] or 0
        median = up["base_median"] or 0
        out["followers"] = followers
        out["base_est"] = bool(up["base_est"])
        if median:
            out["mult"] = round(cur["view"] / median, 3)
        if followers:
            out["fan_ratio"] = round(cur["view"] / followers, 4)

    # 评论/弹幕里的需求词
    with db.session() as c:
        d = c.execute("SELECT SUM(n_total), SUM(n_mobile), SUM(n_name), SUM(n_want) FROM opp_demand "
                      "WHERE bvid = ?" + (" AND ts <= ?" if until else ""),
                      ((bvid, until) if until else (bvid,))).fetchone()
    if d and (d[0] or 0) > 0:
        tot = float(d[0])
        out["demand_total"] = int(tot)
        out["demand_mobile"] = round((d[1] or 0) / tot, 4)
        out["demand_name"] = round((d[2] or 0) / tot, 4)
        out["demand_want"] = round((d[3] or 0) / tot, 4)

    out["age_pct"] = age_percentile(out)
    return out


_REF_CACHE = {"ts": 0, "rows": []}


def _ref_rows():
    """参照集合：近 30 天进跟踪池的全部视频（第一次采样 + 发布 6 小时的播放）。"""
    if time.time() - _REF_CACHE["ts"] < 3600 and _REF_CACHE["rows"]:
        return _REF_CACHE["rows"]
    since = int(time.time()) - 30 * 86400
    with db.session() as c:
        rows = c.execute("SELECT bvid, pubdate FROM opp_videos WHERE pubdate >= ?", (since,)).fetchall()
    out = []
    for r in rows:
        s = _samples(r["bvid"])
        if not s:
            continue
        v6 = _interp(s, int(r["pubdate"] or 0), 6)
        if v6 is None:
            continue
        out.append((r["bvid"], int(r["pubdate"] or 0), v6))
    _REF_CACHE.update(ts=time.time(), rows=out)
    return out


def age_percentile(feat):
    """同分区、同样发布时长的视频里，这条的播放排百分之几。样本 < 200 返回 None。"""
    ref = _ref_rows()
    if len(ref) < 200:
        return None
    mine = feat.get("views_at_6h")
    if not mine:
        return None
    tid = feat.get("tid")
    same = [v for b, p, v in ref if b != feat.get("bvid")]
    if tid is not None:
        with db.session() as c:
            ids = [r[0] for r in c.execute("SELECT bvid FROM opp_videos WHERE tid = ?", (tid,))]
        only = [v for b, p, v in ref if b in set(ids) and b != feat.get("bvid")]
        if len(only) >= 200:
            same = only
    if len(same) < 200:
        return None
    below = sum(1 for v in same if v < mine)
    return round(below / float(len(same)), 4)


# ---------------- 单个游戏 ----------------

def game_videos(gid, until=None):
    """这个游戏的相关视频，带对应方式和可信度。"""
    q = ("SELECT v.bvid, v.pubdate, v.mid, m.method FROM opp_vmap m "
         "JOIN opp_videos v ON v.bvid = m.bvid WHERE m.gid = ?")
    args = [gid]
    if until:
        q += " AND v.pubdate <= ?"
        args.append(until)
    q += " ORDER BY v.pubdate"
    with db.session() as c:
        return [dict(r) for r in c.execute(q, args)]


def game_features(gid, d=None, now=None):
    """第 d 天的游戏指标。d=None 表示实时（用全部已有数据）。"""
    now = int(now or time.time())
    with db.session() as c:
        g = c.execute("SELECT * FROM opp_games WHERE gid = ?", (gid,)).fetchone()
    if not g:
        return {}
    t0 = int(g["t0"] or 0)
    until = None
    if d is not None and t0:
        until = t0 + int(d * 86400)
        if until > now:
            return {}
    vids = game_videos(gid, until)
    if not vids:
        return {}
    if not t0:
        t0 = int(vids[0]["pubdate"] or now)
    out = {"gid": gid, "name": g["name_cn"], "t0": t0,
           "d": d if d is not None else round((now - t0) / 86400.0, 2)}

    ups = {}
    views = []
    feats = []
    for v in vids:
        f = video_features(v["bvid"], until)
        if not f:
            continue
        feats.append(f)
        views.append(f.get("view") or 0)
        ups.setdefault(str(v["mid"]), []).append(f)

    out["n_up"] = len(ups)
    day = 86400
    out["n_up_new_24h"] = sum(1 for k, fs in ups.items()
                              if min(f.get("pubdate") or 0 for f in fs) >= (until or now) - day)
    out["n_up_new_48h"] = sum(1 for k, fs in ups.items()
                              if min(f.get("pubdate") or 0 for f in fs) >= (until or now) - 2 * day)
    out["views_sum"] = sum(views)
    if views:
        out["views_top1_share"] = round(max(views) / float(sum(views) or 1), 4)
    pcts = [f.get("age_pct") for f in feats if f.get("age_pct") is not None]
    if pcts:
        out["best_age_pct"] = max(pcts)

    medians = [f.get("followers") or 0 for k, fs in ups.items() for f in fs[:1]]
    medians = [m for m in medians if m]
    if medians:
        cut = statistics.median(medians)
        out["n_up_small"] = sum(1 for k, fs in ups.items()
                                if (fs[0].get("followers") or 0) < cut
                                and (fs[0].get("mult") or 0) >= _mult_min())

    # 需求率按播放加权
    for key in ("demand_mobile", "demand_name", "demand_want"):
        tot = sum((f.get("view") or 0) for f in feats if f.get(key) is not None)
        if tot:
            out[key] = round(sum((f.get(key) or 0) * (f.get("view") or 0)
                                 for f in feats if f.get(key) is not None) / tot, 4)

    out["by_method"] = {}
    for v in vids:
        out["by_method"][v.get("method") or ""] = out["by_method"].get(v.get("method") or "", 0) + 1
    out["videos"] = len(vids)
    out["n_live"] = _live_streamers(gid, until or now)
    return out


def _live_streamers(member, ts):
    """同一个游戏档案里的直播分区（虎牙 / 斗鱼 / B站），当天最多几个主播在播。没并进档案就没有。"""
    from .. import archive
    lives = [m for m in archive.members_of(archive.gid_of(member) or "")
             if m.split(":", 1)[0] in ("huya", "douyu", "bili")]
    if not lives:
        return None
    day = int(ts // 86400 * 86400)
    best = None
    with db.session() as c:
        for m in lives:
            s, e = m.split(":", 1)
            r = c.execute("SELECT MAX(value) FROM metrics WHERE source = ? AND entity = ? AND metric = 'rooms' "
                          "AND ts >= ? AND ts <= ?", (s, e, day, int(ts))).fetchone()
            if r and r[0] is not None:
                best = max(best or 0, int(r[0]))
    return best


def _mult_min():
    from .. import config
    return float(getattr(config, "OPP_TRACK_MIN_MULT", 3) or 3)


def save_features(gid, d, feat):
    with db.session() as c:
        c.execute("INSERT OR REPLACE INTO opp_features(gid, d, ts, data) VALUES (?,?,?,?)",
                  (gid, float(d), int(time.time()), json.dumps(feat, ensure_ascii=False)))


def t0_for(gid):
    """t0 = 第一条进跟踪池的相关视频的发布时间。"""
    with db.session() as c:
        r = c.execute("SELECT MIN(v.pubdate) FROM opp_vmap m JOIN opp_videos v ON v.bvid = m.bvid "
                      "WHERE m.gid = ?", (gid,)).fetchone()
    return int(r[0]) if r and r[0] else 0


def refresh_games():
    """把所有游戏重算一遍实时指标并存档。"""
    with db.session() as c:
        gids = [r[0] for r in c.execute("SELECT gid FROM opp_games")]
    n = 0
    for gid in gids:
        t0 = t0_for(gid)
        if t0:
            with db.session() as c:
                c.execute("UPDATE opp_games SET t0 = ? WHERE gid = ? AND (t0 = 0 OR t0 IS NULL)",
                          (t0, gid))
        feat = game_features(gid)
        if feat:
            save_features(gid, feat.get("d") or 0, feat)
            n += 1
    return n
