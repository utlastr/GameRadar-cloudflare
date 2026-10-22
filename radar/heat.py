"""评分只用倍率：正常水平为 1，不封顶，只跟别的游戏比。

- 地区：SCORE_REGION（默认 cn）。算哪些视频平台看地图里这个地区的视频平台（radar/platforms.py），
  其中有数据的：B站（opp_* 表，按档案）、抖音 / 快手 / 小红书（soc_videos，搜索结果按名字对到档案）。
- 每条视频：最近 7 天的增量折成每天。不满 7 天按实际时长取平均（新发的视频从发布时算，发布时 = 0）；
  两个点至少隔 MIN_SPAN。
- 每个平台：综合量 = 播放 + Σ 权重 × 评论 / 点赞 / 转发 / 收藏，权重 = 全平台播放增量 ÷ 这种动作的增量
  （平台没有播放数时用点赞当单位）。一款游戏 = 它所有视频的每天综合量相加。
- 正常值：同平台满 7 天的游戏（不到 MIN_FIT 款用全部）的每日综合量中位数。
  倍率 = 每日综合量 ÷ 正常值；正常值为零时不评分。
- 多个平台：有效平台倍率取算术平均；缺数据不当成零。
- 降分开：大作 / 大厂不排名；PENALTY_WORDS 的倍数乘到倍率上。
- 下载类倍数（entity_multiples）只做参考显示，不进分数。
"""
import math
import time
from collections import defaultdict

from . import config, db, match

DAY = 86400
WINDOW = 7 * DAY          # 你定的：最近 7 天
LOOKBACK = 8 * DAY        # 工程参数：下载类参考倍数往回找 8 天（抓取调度最长一周查一次）
MIN_SPAN = 3600           # 工程参数：两个点至少隔 1 小时（你说一小时也关键）
MIN_GAP = 12 * 3600       # 工程参数：下载类参考倍数的两个点至少隔 12 小时
FRESH = 32 * 3600         # 工程参数：名次只用 32 小时内的榜
MIN_FIT = 30              # 统计惯例：至少 30 个样本
MAD_K = 1.4826            # 统计惯例：中位数绝对偏差换成标准差
MEANAD_K = 1.253314       # 统计惯例：中位数绝对偏差为 0 时用平均绝对偏差
DOWN = {"downloads", "reserves", "reviews", "players", "visits"}
MOBILE = {"taptap", "tapmaker", "appcn", "appus", "haoyou", "mistore", "wxmini", "dymini"}
FIELDS = ("plays", "comments", "likes", "shares", "favs")
FIELD_CN = {"plays": "播放", "comments": "评论", "likes": "点赞", "shares": "转发", "favs": "收藏"}
SOCIAL = {"douyin", "kuaishou", "xiaohongshu"}   # 搜索结果存在 soc_videos 的平台（键和地图里的一样）


def median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def daily_increment(pts):
    """pts = [(ts, value)] 按时间排好。最新一点和离它 24 小时最近、至少隔 MIN_GAP 的点，折成每天。"""
    if len(pts) < 2:
        return None
    ts, v = pts[-1]
    cands = [p for p in pts[:-1] if ts - p[0] >= MIN_GAP]
    if not cands:
        return None
    b = min(cands, key=lambda p: abs(ts - p[0] - DAY))
    if v < b[1]:
        return None
    return (v - b[1]) * DAY / (ts - b[0])


def fit_b(pairs):
    """pairs = [(名次, 量)]，log 量 = a - b·log 名次 的最小二乘斜率。"""
    pts = [(math.log(r), math.log(q)) for r, q in pairs if r >= 1 and q > 0]
    if len(pts) < MIN_FIT:
        return 1.0, len(pts), "Zipf 默认"
    mx = sum(x for x, _ in pts) / len(pts)
    my = sum(y for _, y in pts) / len(pts)
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx <= 0:
        return 1.0, len(pts), "Zipf 默认"
    b = -sum((x - mx) * (y - my) for x, y in pts) / sxx
    if b <= 0:
        return 1.0, len(pts), "Zipf 默认"
    return round(b, 3), len(pts), "TapTap 拟合"


def entity_multiples(now=None):
    """返回 ({成员: [条目]}, 说明)。条目 = {kind, src, metric, value, normal, x}。"""
    now = int(now or time.time())
    kinds = {k: v[0] for k, v in config.ANALYZE.items()}
    out = defaultdict(list)
    vals = {}
    with db.session() as c:
        for (src, metric), kind in kinds.items():
            if not config.source_on(src):
                continue
            rows = c.execute("SELECT entity, ts, value FROM metrics WHERE source = ? AND metric = ? AND ts >= ? "
                             "ORDER BY ts", (src, metric, now - LOOKBACK)).fetchall()
            series = defaultdict(list)
            for e, ts, v in rows:
                if v is not None:
                    series[e].append((ts, v))
            got = {}
            for e, pts in series.items():
                x = daily_increment(pts) if kind == "cumulative" else pts[-1][1]
                if x is not None and x > 0:
                    got[e] = x
            vals[(src, metric)] = got
        ranks = defaultdict(dict)
        latest = {r[0]: r[1] for r in c.execute(
            "SELECT source, MAX(ts) FROM metrics WHERE metric LIKE 'rank_%' AND ts >= ? GROUP BY source",
            (now - FRESH,))}
        for src, ts in latest.items():
            if not config.source_on(src):
                continue
            for e, m, v in c.execute("SELECT entity, metric, value FROM metrics WHERE source = ? AND ts = ? "
                                     "AND metric LIKE 'rank_%'", (src, ts)):
                if v and v >= 1:
                    ranks[(src, m)][e] = v
    tap_dl = vals.get(("taptap", "downloads"), {})
    pairs = [(r, tap_dl[e]) for (s, m), d in ranks.items() if s == "taptap" for e, r in d.items() if e in tap_dl]
    b, n_fit, how = fit_b(pairs)
    for (src, metric), got in vals.items():
        mid = median(list(got.values()))
        if not mid:
            continue
        kind = "down" if metric in DOWN else "expo"
        for e, x in got.items():
            out[f"{src}:{e}"].append({"kind": kind, "src": src, "metric": metric, "value": x,
                                      "normal": mid, "x": x / mid})
    for (src, metric), d in ranks.items():
        mid = median(list(d.values()))
        if not mid:
            continue
        for e, r in d.items():
            out[f"{src}:{e}"].append({"kind": "down", "src": src, "metric": metric, "value": r,
                                      "normal": mid, "x": (mid / r) ** b})
    return out, {"b": b, "fit_n": n_fit, "fit": how}


def video_rate(pts, pub, now):
    """pts = [(ts, [播放, 评论, 点赞, 转发, 收藏])] 按时间排好，None = 没这项。
    返回 (每天增量, 起点时间)：起点 = 7 天前那一刻之前最近的点；视频是 7 天内发的就从发布时算（全 0）；
    都没有就用 7 天内最早的点。"""
    if not pts:
        return None
    t1, v1 = pts[-1]
    cut = now - WINDOW
    before = [p for p in pts[:-1] if p[0] <= cut]
    if before:
        t0, v0 = before[-1]
    elif pub and cut <= pub < t1:
        t0, v0 = pub, [0] * len(v1)
    else:
        inside = [p for p in pts[:-1] if p[0] > cut]
        if not inside:
            return None
        t0, v0 = inside[0]
    if t1 - t0 < MIN_SPAN:
        return None
    rate = [None if a is None or b is None else max(0.0, a - b) * DAY / (t1 - t0) for a, b in zip(v1, v0)]
    return rate, t0


def composite(rates):
    """{视频: 每天增量} → ({视频: 综合量}, 单位, 权重)。权重 = 全平台单位增量 ÷ 这种动作的增量。"""
    def total(i, j):
        return sum(r[i] for r in rates.values() if r[i] is not None and r[j] is not None)
    base = 0 if total(0, 0) > 0 else 2
    w = {}
    for i in range(len(FIELDS)):
        if i == base:
            continue
        t = total(i, base)
        if t > 0:
            w[i] = total(base, i) / t
    out = {}
    for v, r in rates.items():
        x = (r[base] or 0.0) + sum(wi * (r[i] or 0.0) for i, wi in w.items())
        if x > 0 or r[base] is not None:
            out[v] = x
    return out, FIELDS[base], {FIELDS[i]: round(wi, 2) for i, wi in w.items()}


def robust_center(xs):
    """ln 值列表 → (中位数, 离散程度)。"""
    m = median(xs)
    if m is None:
        return None, 0.0
    dev = [abs(x - m) for x in xs]
    s = MAD_K * median(dev)
    if s <= 0:
        s = MEANAD_K * sum(dev) / len(dev)
    return m, s


def _series(rows):
    out = defaultdict(list)
    for vid, ts, *vals in rows:
        out[vid].append((ts, list(vals)))
    return out


def video_links(groups, entities):
    """opp_vmap 里的编号 → 档案号：档案成员（steam:…、按名字搜到时记的成员、已连上的 btopic:…），
    再加同名 B站 话题（名字和档案里某个名字归一化后完全一样，且只对上一份档案）。"""
    from .sources import bili
    out = {m: gid for gid, ms in groups.items() for m in ms}
    hit = defaultdict(set)
    for gid, ms in groups.items():
        for m in ms:
            e = entities.get(tuple(m.split(":", 1))) or {}
            x = e.get("extra") or {}
            for n in (e.get("name"), x.get("name_cn"), x.get("name_en")):
                k = bili.normalize(n or "")
                if len(k) >= 2:
                    hit["btopic:" + k].add(gid)
    for k, v in hit.items():
        if len(v) == 1 and k not in out:
            out[k] = next(iter(v))
    return out


def bili_videos(now, links):
    """{bvid: (样本, 发布时间)}，{bvid: {档案号}}。"""
    since = now - 2 * WINDOW
    try:
        with db.session() as c:
            rows = c.execute("SELECT bvid, ts, view, reply, lk, share, fav FROM opp_vsamples WHERE ts >= ? "
                             "ORDER BY ts", (since,)).fetchall()
            pub = {r[0]: r[1] or 0 for r in c.execute("SELECT bvid, pubdate FROM opp_videos")}
            gids = defaultdict(set)
            for bvid, m in c.execute("SELECT bvid, gid FROM opp_vmap"):
                g = links.get(m)
                if g:
                    gids[bvid].add(g)
    except Exception:                                   # noqa: BLE001
        return {}, {}
    return {v: (pts, pub.get(v)) for v, pts in _series(rows).items()}, gids


def social_videos(platform, now, term_gids):
    """抖音 / 快手 / 小红书 搜索结果里的视频，按搜索词对到档案。"""
    since = now - 2 * WINDOW
    try:
        with db.session() as c:
            rows = c.execute("SELECT vid, ts, plays, comments, likes, shares, favs FROM soc_videos "
                             "WHERE platform = ? AND ts >= ? ORDER BY ts", (platform, since)).fetchall()
            meta = c.execute("SELECT vid, MAX(pub_ts), GROUP_CONCAT(DISTINCT term) FROM soc_videos "
                             "WHERE platform = ? AND ts >= ? GROUP BY vid", (platform, since)).fetchall()
    except Exception:                                   # noqa: BLE001
        return {}, {}
    gids = defaultdict(set)
    pub = {}
    from .social.extract import stable_video_id
    for vid, p, terms in meta:
        if not stable_video_id(platform, {"url": vid}):
            continue
        pub[vid] = p
        for t in (terms or "").split(","):
            gids[vid] |= term_gids.get(match.normalize(t), set())
    return {v: (pts, pub.get(v)) for v, pts in _series(rows).items()}, gids


def platform_scores(videos, gids, now):
    """一个平台：{档案号: {value, full, n}}，说明。"""
    rates, start = {}, {}
    for v, (pts, pub) in videos.items():
        if not gids.get(v):
            continue
        r = video_rate(pts, pub, now)
        if r:
            rates[v], start[v] = r
    comp, unit, w = composite(rates)
    per = defaultdict(lambda: {"value": 0.0, "t0": now, "n": 0})
    for v, x in comp.items():
        for g in gids[v]:
            d = per[g]
            d["value"] += x
            d["n"] += 1
            d["t0"] = min(d["t0"], start[v])
    for d in per.values():
        d["full"] = now - d.pop("t0") >= WINDOW
    return dict(per), {"unit": FIELD_CN[unit], "weights": {FIELD_CN[k]: x for k, x in w.items()}}


def region_platforms():
    try:
        from . import platforms
        keys = platforms.video_keys(getattr(config, "SCORE_REGION", "cn"))
    except Exception:                                   # noqa: BLE001
        keys = []
    return [k for k in keys if k == "bilibili" or k in SOCIAL]


def name_index(groups, entities):
    out = defaultdict(set)
    for gid, ms in groups.items():
        for m in ms:
            e = entities.get(tuple(m.split(":", 1))) or {}
            x = e.get("extra") or {}
            for n in (e.get("name"), x.get("name_cn"), x.get("name_en")):
                k = match.normalize(n or "")
                if match.linkable(k):
                    out[k].add(gid)
    return out


def _tags(members, entities):
    out = []
    for m in members:
        s, e = m.split(":", 1)
        x = (entities.get((s, e)) or {}).get("extra") or {}
        for t in (x.get("tags") or []) + (x.get("genres") or []) + ([x["genre"]] if x.get("genre") else []):
            if isinstance(t, dict):
                t = t.get("name") or t.get("description") or ""
            out.append(str(t).lower())
    return out


def _has(tags, words):
    return any(w.lower() in t for t in tags for w in words)


def penalty_factors(groups, entities):
    """每组降分词：带这个标签的 PC 游戏（档案里有 Steam）有手机版的比例 ÷ 不带的比例，加 1 平滑，最多 1。"""
    words = getattr(config, "PENALTY_WORDS", {}) or {}
    cnt = {k: [0, 0, 0, 0] for k in words}
    for gid, ms in groups.items():
        if not any(m.startswith("steam:") for m in ms):
            continue
        tags = _tags([m for m in ms if m.startswith("steam:")], entities)
        mob = any(m.split(":", 1)[0] in MOBILE for m in ms)
        for k, ws in words.items():
            i = 0 if _has(tags, ws) else 2
            cnt[k][i] += 1 if mob else 0
            cnt[k][i + 1] += 1
    out = {}
    for k, (a, b, c, d) in cnt.items():
        p1, p0 = (a + 1) / (b + 2), (c + 1) / (d + 2)
        out[k] = {"factor": round(min(1.0, p1 / p0), 3), "with": [a, b], "without": [c, d]}
    return out


def big_reason(members, entities, name_norms, big_words):
    for n in name_norms:
        hit = next((w for w in big_words if w and w in n), None)
        if hit:
            return "大作：" + hit
    for m in members:
        s, e = m.split(":", 1)
        x = (entities.get((s, e)) or {}).get("extra") or {}
        devs = list(x.get("developers") or []) + ([x["dev"]] if x.get("dev") else []) + list(x.get("publishers") or [])
        for d in devs:
            if d and match.company_match(d, config.BIG_COMPANIES):
                return "大厂：" + d
    return None


def score_groups(groups, entities):
    """每个档案：raw（不降分的异常值）、final（降分后）、各平台明细、下载参考倍数。没有视频数据的 raw = None。"""
    now = int(time.time())
    em, meta = entity_multiples(now)
    pen = penalty_factors(groups, entities)
    big_words = [match.normalize(x) for x in getattr(config, "KW_BIG_GAMES", []) if match.normalize(x)]
    words = getattr(config, "PENALTY_WORDS", {}) or {}
    plats = region_platforms()
    names = {"bilibili": "B站", "douyin": "抖音", "kuaishou": "快手", "xiaohongshu": "小红书"}
    terms = name_index(groups, entities) if any(p in SOCIAL for p in plats) else {}
    links = video_links(groups, entities) if "bilibili" in plats else {}
    per_plat, plat_meta = {}, {}
    for p in plats:
        vids, gids = bili_videos(now, links) if p == "bilibili" else social_videos(p, now, terms)
        if p == "douyin":
            from .social.growth import archive_videos
            # Do not rank historical keyword samples as verified gaming evidence.
            vids, gids = archive_videos(now, terms)
        sc, info = platform_scores(vids, gids, now)
        if not sc:
            plat_meta[p] = {"name": names[p], "games": 0}
            continue
        full = [d["value"] for d in sc.values() if d["full"]]
        pool = full if len(full) >= MIN_FIT else [d["value"] for d in sc.values()]
        mid = median(pool)
        per_plat[p] = (sc, mid)
        plat_meta[p] = {**info, "name": names[p], "games": len(sc), "full": len(full),
                        "pool": "满 7 天的游戏" if pool is full else "全部游戏", "pool_n": len(pool),
                        "normal": mid, "score_kind": "multiple"}
    out = {}
    for gid, ms in groups.items():
        items = [it for m in ms for it in em.get(m, [])]
        down = max((it for it in items if it["kind"] == "down"), key=lambda it: it["x"], default=None)
        nm = [match.normalize((entities.get(tuple(m.split(":", 1))) or {}).get("name") or "") for m in ms]
        big = big_reason(ms, entities, nm, big_words)
        tags = _tags(ms, entities)
        cuts = [(k, pen[k]["factor"]) for k in words if k in pen and _has(tags, words[k]) and pen[k]["factor"] < 1]
        f = math.prod(x for _, x in cuts) if cuts else 1.0
        vrows = []
        for p, (sc, mid) in per_plat.items():
            d = sc.get(gid)
            if not d or mid is None or mid <= 0:
                continue
            z = d["value"] / mid
            zf = z * f
            vrows.append({"p": p, "name": names[p], "value": round(d["value"], 1), "normal": plat_meta[p]["normal"],
                          "z": round(z, 2), "zf": round(zf, 2), "n": d["n"], "full": d["full"]})
        if not vrows and not down:
            continue
        k = len(vrows)
        raw = round(sum(r["z"] for r in vrows) / k, 2) if k else None
        final = None if big or not k else round(sum(r["zf"] for r in vrows) / k, 2)
        vrows.sort(key=lambda r: -r["z"])
        out[gid] = {"raw": raw, "final": final, "videos": vrows, "down": down, "big": big, "cut": cuts,
                    "score_kind": "multiple"}
    return out, {**meta, "score_kind": "multiple", "platforms": plat_meta,
                 "region": getattr(config, "SCORE_REGION", "cn"), "penalty": pen}


def rank_key(h, which):
    """排序：有分数的按分数，没有的排后面（再按下载参考倍数）。"""
    v = (h or {}).get(which)
    d = ((h or {}).get("down") or {}).get("x") or 0.0
    return (0, -v, 0.0) if v is not None else (1, 0.0, -d)
