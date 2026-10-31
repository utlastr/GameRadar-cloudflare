"""v81 评分：分数 = 异常值，只跟别的游戏比。

- 地区：SCORE_REGION（默认 cn）。算哪些视频平台看地图里这个地区的视频平台（radar/platforms.py），
  其中有数据的：B站（opp_* 表，按档案）、抖音 / 快手 / 小红书（soc_videos，搜索结果按名字对到档案）。
- 每条视频：最近 7 天的增量折成每天。不满 7 天按实际时长取平均（新发的视频从发布时算，发布时 = 0）；
  两个点至少隔 MIN_SPAN。
- 每个平台：综合量 = 播放 + Σ 权重 × 评论 / 点赞 / 转发 / 收藏，权重 = 全平台播放增量 ÷ 这种动作的增量
  （平台没有播放数时用点赞当单位）。一款游戏 = 它所有视频的每天综合量相加。
- 正常值：这个平台上满 7 天的游戏（不到 MIN_FIT 款用全部）的 ln(1 + 综合量) 的中位数，
  离散程度 = 1.4826 × 中位数绝对偏差。异常值 z = (ln(1 + 综合量) − 正常值) ÷ 离散程度。
- 多个平台：Σz ÷ √平台数（Stouffer）。
- 降分开：PENALTY_WORDS 的倍数乘到综合量上再算 z。
- v85 大厂 / 大作（big_reason）：照常算分，单独标出来；必看页"大厂游戏：隐藏"开关（默认开）把它们藏起来，不删除。
  判断：名字在 KW_BIG_GAMES / BIG_GAME_NAMES；开发或发行在 BIG_COMPANIES（+ BIG_COMPANIES_EXTRA）；
  标签 3A / AAA；Steam 价格 ≥ BIG_PRICE_USD；安装包 ≥ BIG_SIZE_GB（电脑）/ BIG_SIZE_GB_MOBILE（手机）。
  缺数据 = 不算大厂，照常显示。
- 下载类倍数（entity_multiples）只做参考显示，不进分数。
- v96：平台不再按地区挑：B站 + soc_videos 里有数据的所有平台（video_platforms，以后 YouTube 有数据自动加入）
  + 直播平台（live_scores：config.LIVE_SOURCES，最近 7 天每天最高观看人数 / 直播热度的平均，没抓到的天不算）。
  每个平台照样只和自己比（z）。合成改成 combine：max(最好的平台, Σz ÷ √平台数)——几个平台都火加分，
  多一个不火的平台不扣分。只有一个平台时和以前完全一样。
"""
import datetime
import math
import re
import time
import unicodedata
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
SOCIAL = {"douyin", "kuaishou", "xiaohongshu", "tiktok"}   # 搜索结果存在 soc_videos 的平台
VIDEO_NAMES = {"bilibili": "B站", "douyin": "抖音", "kuaishou": "快手", "xiaohongshu": "小红书",
               "youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}
LIVE_CN = {"viewers": "观看人数", "heat": "直播热度"}
LIVE_CACHE = 1800         # 工程参数：直播 7 天数据半小时重算一次（表很大）
VIDS_KEEP = 80            # 工程参数：每款游戏每个平台记下综合量最大的 80 条视频，详情图表用


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
    """社交视频搜索结果按所有命中的搜索词对到游戏档案。"""
    since = now - 2 * WINDOW
    try:
        with db.session() as c:
            rows = c.execute("SELECT vid, ts, plays, comments, likes, shares, favs FROM soc_videos "
                             "WHERE platform = ? AND ts >= ? ORDER BY ts", (platform, since)).fetchall()
            try:
                evidence = (" AND EXISTS (SELECT 1 FROM soc_search_matches e WHERE e.platform=t.platform "
                            "AND e.vid=t.vid AND e.term=t.term AND e.provenance_version>=5)") if platform == "douyin" else ""
                meta = c.execute(
                    "SELECT v.vid, MAX(v.pub_ts), t.term "
                    "FROM soc_videos v JOIN soc_video_terms t "
                    "ON t.platform = v.platform AND t.vid = v.vid "
                    "WHERE v.platform = ? AND v.ts >= ? " + evidence + " GROUP BY v.vid, t.term",
                    (platform, since)).fetchall()
            except Exception:
                if platform == "douyin":
                    # Fail closed if search-card evidence has not been created yet.
                    return {}, {}
                meta = c.execute("SELECT vid, MAX(pub_ts), term FROM soc_videos "
                                 "WHERE platform = ? AND ts >= ? GROUP BY vid, term", (platform, since)).fetchall()
    except Exception:                                   # noqa: BLE001
        return {}, {}
    gids = defaultdict(set)
    pub = {}
    for vid, p, term in meta:
        pub[vid] = p
        gids[vid] |= term_gids.get(match.normalize(term or ""), set())
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
    per = defaultdict(lambda: {"value": 0.0, "t0": now, "n": 0, "parts": {}, "vids": []})
    for v, x in comp.items():
        r = rates[v]
        for g in gids[v]:
            d = per[g]
            d["value"] += x
            d["n"] += 1
            d["t0"] = min(d["t0"], start[v])
            d["vids"].append((x, v))
            for i, y in enumerate(r):          # v93：真实的每天播放 / 评论 / 点赞……（页面显示用，不进分数）；没这项的视频不算 0
                if y is not None:
                    d["parts"][FIELDS[i]] = d["parts"].get(FIELDS[i], 0.0) + y
    for d in per.values():
        d["full"] = now - d.pop("t0") >= WINDOW
        d["vids"] = [v for _, v in sorted(d["vids"], reverse=True)[:VIDS_KEEP]]
    return dict(per), {"unit": FIELD_CN[unit], "weights": {FIELD_CN[k]: x for k, x in w.items()}}


def region_platforms():
    try:
        from . import platforms
        keys = platforms.video_keys(getattr(config, "SCORE_REGION", "cn"))
    except Exception:                                   # noqa: BLE001
        keys = []
    return [k for k in keys if k == "bilibili" or k in SOCIAL]


def video_platforms(now):
    """v96：B站 + soc_videos 里最近 14 天出现过的所有平台。以后 YouTube / TikTok 一有数据（soc_videos 里
    platform = youtube）就自动算进排名，不用改代码。"""
    out = ["bilibili"]
    try:
        with db.session() as c:
            for (p,) in c.execute("SELECT DISTINCT platform FROM soc_videos WHERE ts >= ?", (now - 2 * WINDOW,)):
                if p and p.lower() not in ("bilibili", "bili", "b站") and p not in out:
                    out.append(p)
    except Exception:                                   # noqa: BLE001
        pass
    return out


_LIVE = {"ts": 0, "data": None}


def live_daily(now):
    """v96 直播：{成员: (平台, 指标, {第几天前: 当天最高})}，最近 7 天。
    平台 = 来源去掉 _t / _kw（虎牙分区和虎牙标题里提到这款游戏的算同一个平台）；每个平台有观看人数就用观看人数，
    没有就用直播热度。config.LIVE_SOURCES 里以后加的直播平台自动算进来。结果缓存半小时。"""
    if _LIVE["data"] is not None and now - _LIVE["ts"] < LIVE_CACHE:
        return _LIVE["data"]
    srcs = sorted(getattr(config, "LIVE_SOURCES", set()) or set())
    per = defaultdict(dict)
    if srcs:
        q = ",".join("?" * len(srcs))
        try:
            with db.session() as c:
                rows = c.execute(
                    f"SELECT source, entity, metric, CAST((? - ts) / 86400 AS INTEGER) AS d, MAX(value) FROM metrics "
                    f"WHERE source IN ({q}) AND metric IN ('viewers', 'heat') AND ts >= ? "
                    "GROUP BY source, entity, metric, d", (now, *srcs, now - WINDOW)).fetchall()
        except Exception:                               # noqa: BLE001
            rows = []
        for s, e, m, d, v in rows:
            if v and v > 0:
                per[(s, m)].setdefault(e, {})[int(d)] = v
    base = lambda s: s.split("_", 1)[0]                 # noqa: E731
    has_viewers = {base(s) for s, m in per if m == "viewers"}
    out = {}
    for (s, m), ents in per.items():
        want = "viewers" if base(s) in has_viewers else "heat"
        if m != want:
            continue
        for e, days in ents.items():
            out[f"{s}:{e}"] = (base(s), m, days)
    _LIVE.update(ts=now, data=out)
    return out


def live_scores(groups, now):
    """直播平台：{平台: ({档案号: {value, n, full, parts}}, 说明)}。
    value = 最近 7 天每天最高观看人数（或热度）的平均；没抓到的天不算（不当 0）；同一个平台几个成员当天相加。"""
    links = {m: gid for gid, ms in groups.items() for m in ms}
    per = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    met = {}
    for m, (p, metric, days) in live_daily(now).items():
        g = links.get(m)
        if not g:
            continue
        met[p] = metric
        for d, v in days.items():
            per[p][g][d] += v
    out = {}
    for p, gs in per.items():
        cn = LIVE_CN.get(met[p], met[p])
        sc = {}
        for g, days in gs.items():
            vals = [v for v in days.values() if v > 0]
            if not vals:
                continue
            val = sum(vals) / len(vals)
            sc[g] = {"value": val, "n": len(vals), "full": max(days) >= 6, "parts": {cn: round(val)}}
        if sc:
            out["live:" + p] = (sc, {"unit": cn, "weights": {}, "kind": "live"})
    return out


def name_index(groups, entities):
    out = defaultdict(set)
    labels = db.load_labels()
    for gid, ms in groups.items():
        for m in ms:
            e = entities.get(tuple(m.split(":", 1))) or {}
            x = e.get("extra") or {}
            label = labels.get(m) or {}
            for n in ((e.get("name"), x.get("name_cn"), x.get("name_en")) +
                      tuple(x.get("aliases") or []) + tuple(label.get("aliases") or [])):
                k = match.normalize(n or "")
                if match.linkable(k):
                    out[k].add(gid)
    # The batch is an explicit archive association, including short aliases.
    batch = db.kv_get("radar_social_batch", {}) or {}
    for query in batch.get("queries", []):
        for gid in query.get("gids", []):
            if gid in groups:
                out[match.normalize(query["term"])].add(gid)
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


_TOK = re.compile(r"[a-z0-9]+")


def company_hit(dev, companies):
    """开发 / 发行名里有没有大厂名。中文按包含（"深圳市腾讯计算机系统有限公司"含"腾讯"），
    英文按整词（"EA" 不会对上 "Idea Factory"）。对上返回名单里的那个名字。"""
    low = unicodedata.normalize("NFKC", str(dev or "")).lower()
    if not low.strip():
        return None
    toks = " " + " ".join(_TOK.findall(low)) + " "
    norm = match.normalize(low)
    for c in companies:
        cl = unicodedata.normalize("NFKC", str(c or "")).lower()
        if match.has_cjk(cl):
            cn = match.normalize(cl)
            if len(cn) >= 2 and cn in norm:
                return c
        else:
            ct = " ".join(_TOK.findall(cl))
            if ct and " " + ct + " " in toks:
                return c
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def big_reason(members, entities, name_norms, big_words):
    """大厂 / 大作的原因（一句话），不是返回 None。缺数据的那一项不算。"""
    names = big_words + [match.normalize(x) for x in getattr(config, "BIG_GAME_NAMES", []) or []
                         if match.normalize(x)]
    for n in name_norms:
        hit = next((w for w in names if w and n and w in n), None)
        if hit:
            return "大作：名字带「" + hit + "」"
    comps = list(getattr(config, "BIG_COMPANIES", []) or []) + list(getattr(config, "BIG_COMPANIES_EXTRA", []) or [])
    size_pc = _num(getattr(config, "BIG_SIZE_GB", None))
    size_mob = _num(getattr(config, "BIG_SIZE_GB_MOBILE", None))
    price_max = _num(getattr(config, "BIG_PRICE_USD", None))
    for m in members:
        s, e = m.split(":", 1)
        x = (entities.get((s, e)) or {}).get("extra") or {}
        devs = list(x.get("developers") or []) + list(x.get("publishers") or []) + \
            [x[k] for k in ("dev", "publisher") if x.get(k)]
        for d in devs:
            if isinstance(d, str) and d and company_hit(d, comps):
                return "大厂：" + d
    for m in members:
        s, e = m.split(":", 1)
        x = (entities.get((s, e)) or {}).get("extra") or {}
        gb = _num(x.get("size_gb"))
        lim = size_mob if s in MOBILE else size_pc
        if gb is not None and lim and gb >= lim:
            return f"大作：安装包 {gb:.1f} GB（≥ {lim:g} GB）"
        if s == "steam":
            p = _num(x.get("price_usd"))
            if p is not None and price_max and p >= price_max:
                return f"大作：Steam 售价 ${p:.0f}（≥ ${price_max:g}）"
    tags = set(_tags(members, entities))
    if tags & {"3a", "aaa"}:
        return "大作：标签 3A"
    return None


def score_groups(groups, entities):
    """每个档案：raw（不降分的异常值）、final（降分后）、各平台明细、下载参考倍数。没有视频数据的 raw = None。"""
    now = int(time.time())
    em, meta = entity_multiples(now)
    pen = penalty_factors(groups, entities)
    big_words = [match.normalize(x) for x in getattr(config, "KW_BIG_GAMES", []) if match.normalize(x)]
    words = getattr(config, "PENALTY_WORDS", {}) or {}
    # v96：不再只看一个地区：所有有数据的视频平台 + 直播平台都算（以后有数据的平台自动加入）
    plats = video_platforms(now)
    names = {p: VIDEO_NAMES.get(p, p) for p in plats}
    terms = name_index(groups, entities) if len(plats) > 1 else {}
    links = video_links(groups, entities)
    scored = []
    for p in plats:
        vids, gids = bili_videos(now, links) if p == "bilibili" else social_videos(p, now, terms)
        scored.append((p, *platform_scores(vids, gids, now)))
    try:
        for p, (sc, info) in live_scores(groups, now).items():
            b = p.split(":", 1)[1]
            nm = (getattr(config, "LIVE_PLATFORMS", {}).get(b) or (b,))[0]
            names[p] = nm if nm.endswith("直播") else nm + " 直播"
            scored.append((p, sc, info))
    except Exception:                                   # noqa: BLE001
        import traceback
        traceback.print_exc()
    per_plat, plat_meta = {}, {}
    for p, sc, info in scored:
        if not sc:
            plat_meta[p] = {"name": names[p], "games": 0}
            continue
        full = [math.log1p(d["value"]) for d in sc.values() if d["full"]]
        pool = full if len(full) >= MIN_FIT else [math.log1p(d["value"]) for d in sc.values()]
        mid, s = robust_center(pool)
        per_plat[p] = (sc, mid, s)
        plat_meta[p] = {**info, "name": names[p], "games": len(sc), "full": len(full),
                        "pool": "满 7 天的游戏" if pool is full else "全部游戏", "pool_n": len(pool),
                        "normal": round(math.expm1(mid), 1), "s": round(s, 3)}
    out = {}
    for gid, ms in groups.items():
        items = [it for m in ms for it in em.get(m, [])]
        down = max((it for it in items if it["kind"] == "down"), key=lambda it: it["x"], default=None)
        nm = [match.normalize((entities.get(tuple(m.split(":", 1))) or {}).get("name") or "") for m in ms]
        big = big_reason(ms, entities, nm, big_words)
        tags = _tags(ms, entities)
        cuts = [(k, pen[k]["factor"]) for k in words if k in pen and _has(tags, words[k]) and pen[k]["factor"] < 1]
        f = math.prod(x for _, x in cuts) if cuts else 1.0
        vrows, vids = [], {}
        for p, (sc, mid, s) in per_plat.items():
            d = sc.get(gid)
            if not d:
                continue
            z = (math.log1p(d["value"]) - mid) / s if s > 0 else 0.0
            zf = (math.log1p(d["value"] * f) - mid) / s if s > 0 else 0.0
            vrows.append({"p": p, "name": names[p], "value": round(d["value"], 1), "normal": plat_meta[p]["normal"],
                          "z": round(z, 2), "zf": round(zf, 2), "n": d["n"], "full": d["full"],
                          "parts": {FIELD_CN.get(k, k): round(x) for k, x in d.get("parts", {}).items()},
                          "kind": "live" if p.startswith("live:") else "video"})
            if d.get("vids"):
                vids[p] = d["vids"]
        if not vrows and not down:
            continue
        out[gid] = {"raw": combine([r["z"] for r in vrows]), "final": combine([r["zf"] for r in vrows]),
                    "videos": sorted(vrows, key=lambda r: -r["z"]), "down": down, "big": big, "cut": cuts,
                    "_vids": vids}
    return out, {**meta, "platforms": plat_meta, "region": getattr(config, "SCORE_REGION", "cn"), "penalty": pen}


def combine(zs):
    """v96 几个平台合成一个分数：取"最好的那个平台"和"所有平台合起来（Σz ÷ √平台数）"里大的那个。
    几个平台都火 → 合起来更高（加分）；多了一个不火的平台 → 不扣分（还是最好的那个）；没数据的平台不算。"""
    if not zs:
        return None
    return round(max(max(zs), sum(zs) / math.sqrt(len(zs))), 2)


def _day(ts):
    return datetime.date.fromtimestamp(ts).toordinal()


def game_series(gid, days=90):
    """v96 详情图表用：这款游戏在每个视频平台每天新增多少播放（只算排名时对上的那些视频；不进分数）。
    每条视频取每天最后一次的播放数，两次之间的增量平均摊到中间每一天；视频在范围内发布的从发布那天的 0 算起。
    返回 {平台: {"name", "days": [[日期, 播放], ...]}}，日期按本机时间。"""
    vids = (db.kv_get("heat_vids") or {}).get(gid) or {}
    now = int(time.time())
    since = now - days * DAY
    out = {}
    for p, ids in vids.items():
        ids = [i for i in ids if i][:VIDS_KEEP]
        if not ids or p.startswith("live:"):
            continue
        q = ",".join("?" * len(ids))
        try:
            with db.session() as c:
                if p == "bilibili":
                    rows = c.execute(f"SELECT bvid, ts, view FROM opp_vsamples WHERE bvid IN ({q}) AND ts >= ? "
                                     "ORDER BY ts", (*ids, since)).fetchall()
                    pub = dict(c.execute(f"SELECT bvid, pubdate FROM opp_videos WHERE bvid IN ({q})", ids).fetchall())
                else:
                    rows = c.execute(f"SELECT vid, ts, plays FROM soc_videos WHERE platform = ? AND vid IN ({q}) "
                                     "AND ts >= ? ORDER BY ts", (p, *ids, since)).fetchall()
                    pub = dict(c.execute(f"SELECT vid, MAX(pub_ts) FROM soc_videos WHERE platform = ? "
                                         f"AND vid IN ({q}) GROUP BY vid", (p, *ids)).fetchall())
        except Exception:                               # noqa: BLE001
            continue
        last = defaultdict(dict)
        for v, ts, x in rows:
            if x is not None and ts:
                last[v][_day(ts)] = x
        tot = defaultdict(float)
        for v, byd in last.items():
            ds = sorted(byd)
            pts = [(d, byd[d]) for d in ds]
            p0 = pub.get(v)
            if p0 and p0 >= since and _day(p0) <= ds[0]:
                pts.insert(0, (_day(p0) - 1, 0.0))
            for (d0, x0), (d1, x1) in zip(pts, pts[1:]):
                inc = max(0.0, x1 - x0) / (d1 - d0)
                for d in range(d0 + 1, d1 + 1):
                    tot[d] += inc
        if tot:
            out[p] = {"name": VIDEO_NAMES.get(p, p),
                      "days": [[datetime.date.fromordinal(d).isoformat(), round(v)] for d, v in sorted(tot.items())]}
    return out


def rank_key(h, which):
    """排序：有分数的按分数，没有的排后面（再按下载参考倍数）。"""
    v = (h or {}).get(which)
    d = ((h or {}).get("down") or {}).get("x") or 0.0
    return (0, -v, 0.0) if v is not None else (1, 0.0, -d)
