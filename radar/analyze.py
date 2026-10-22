"""分析：增长异常 → 合并同一个游戏 → 打分 → 提醒 → 生成看板数据。

增长判断（需求文档第十五、十六节）：
  1. 按比例算涨幅（对数）
  2. 和自己比：和"24 小时前同一时刻"比（去掉每天固定的高峰低谷）；没有昨天数据的用短期涨幅
  3. 和别人比：同一平台、同一量级的所有游戏里排第几（稳健 z 分，用中位数）。
     中位数就是"大盘指数"，节假日大家一起涨会自动抵消
  4. 累计异常分（CUSUM）：一点点不正常但一直持续会越攒越多
  5. 看扩散：直播间/主播数量（rooms）和总热度分别判断
"""
import json
import math
import statistics
import time
from collections import defaultdict

from . import activity, ai, config, db, identity, images, launch, match, notify, scoring, supply

DAY = 86400


# ---------------- 小工具 ----------------
def _fmt_num(v):
    v = float(v or 0)
    if v >= 1e8:
        return f"{v / 1e8:.1f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.1f}万"
    return f"{v:.0f}"


def _med(xs):
    return statistics.median(xs) if xs else 0.0


def _nearest(points, t, tol):
    best, bd = None, None
    for ts, v in points:
        d = abs(ts - t)
        if d <= tol and (bd is None or d < bd):
            best, bd = v, d
    return best


def _period(source):
    return (config.FAST_MINUTES if source in config.LIVE_SOURCES else config.SLOW_MINUTES) * 60


def _robust_z(values, levels):
    """values: {实体: 涨幅}。返回 ({实体: z}, 大盘中位数)。同量级的够多时和同量级比。"""
    xs = list(values.values())
    if len(xs) < 5:
        return {}, (_med(xs) if xs else None)
    m_all = _med(xs)
    s_all = max(1.4826 * _med([abs(x - m_all) for x in xs]), config.Z_MIN_SPREAD)
    buckets = defaultdict(list)
    for e, g in values.items():
        buckets[int(math.log10(levels[e] + 1))].append(g)
    stats = {}
    for b, bx in buckets.items():
        if len(bx) >= 12:
            m = _med(bx)
            stats[b] = (m, max(1.4826 * _med([abs(x - m) for x in bx]), config.Z_MIN_SPREAD))
    z = {}
    for e, g in values.items():
        m, s = stats.get(int(math.log10(levels[e] + 1)), (m_all, s_all))
        z[e] = (g - m) / s
    return z, m_all


# ---------------- 1. 增长信号 ----------------
def compute_signals(entities):
    now = int(time.time())
    # 程序第一次运行的时间。刚开始运行时所有东西都是第一次见，不能都算"新出现"
    first_run = db.kv_get("first_run")
    if not first_run:
        first_run = now
        db.kv_set("first_run", first_run)
    with db.session() as c:
        anomaly = {(r[0], r[1], r[2]): (r[3], r[4])
                   for r in c.execute("SELECT source, entity, metric, cusum, last_ts FROM anomaly")}
    signals, index, updates, index_rows = [], {}, [], []
    src_first = db.source_first_runs()

    for (source, metric), (kind, min_level) in config.ANALYZE.items():
        P = _period(source)
        s_first = src_first.get(source, first_run)
        tol = P * 0.75
        with db.session() as c:
            rows = c.execute(
                "SELECT entity, ts, value FROM metrics WHERE source = ? AND metric = ? AND ts >= ? ORDER BY ts",
                (source, metric, now - 32 * 3600)).fetchall()
        if not rows:
            continue
        series = defaultdict(list)
        for e, ts, v in rows:
            series[e].append((ts, v))
        latest = max(r[1] for r in rows)
        g24, gs, lv, short, observed = {}, {}, {}, {}, {}
        for e, pts in series.items():
            if latest - pts[-1][0] > max(2 * P, 6 * 3600):
                continue          # 这次没抓到 = 缺失，不当成 0
            sampled = pts[-1][0]
            observed[e] = sampled
            v = pts[-1][1]
            lv[e] = v
            if kind == "level":
                # 在线人数、直播热度一天里起起落落（各国睡觉起床），只和"昨天同一时间"比；
                # 用最近 6 小时里每个点和它 24 小时前的点配对，取平均，一个点的抖动不算数
                pairs = []
                for ts_i, v_i in pts:
                    if ts_i < sampled - 6 * 3600:
                        continue
                    q = _nearest(pts, ts_i - DAY, tol)
                    if q is not None:
                        pairs.append((v_i, q))
                if pairs:
                    a = sum(x for x, _ in pairs) / len(pairs)
                    b = sum(y for _, y in pairs) / len(pairs)
                    g24[e] = math.log((a + 1) / (b + 1))
            else:  # 累计数：比较增量
                W = 6 * 3600
                a = _nearest(pts, sampled - W, tol)
                b = _nearest(pts, sampled - DAY, tol)
                b0 = _nearest(pts, sampled - DAY - W, tol)
                if a is not None and b is not None and b0 is not None:
                    g24[e] = math.log((max(v - a, 0) + 1) / (max(b - b0, 0) + 1))
                # Early evidence uses the actual interval, without waiting for yesterday.
                prev = next((p for p in pts if 3600 <= sampled - p[0] <= 12 * 3600), None)
                if prev is not None and v >= prev[1]:
                    hours = (sampled - prev[0]) / 3600
                    delta = v - prev[1]
                    short[e] = {"rate": delta / hours, "hours": hours, "delta": delta}
                    gs[e] = math.log((v + max(1, min_level * .2)) /
                                     (prev[1] + max(1, min_level * .2))) * 24 / hours

        floor = min_level * 0.2   # 大盘只用有一定规模的算
        z24, idx24 = _robust_z({e: g for e, g in g24.items() if lv[e] >= floor}, lv)
        zs, idxs = _robust_z({e: g for e, g in gs.items() if lv[e] >= floor}, lv)
        index[f"{source}/{metric}"] = {"g24": idx24, "gshort": idxs, "n": len(lv)}
        if idx24 is not None:
            index_rows.append((latest, source, "__index__", metric + "_g24", idx24))

        for e, v in lv.items():
            sampled = observed[e]
            z = z24.get(e)
            if z is None:
                z = zs.get(e)
            ent = entities.get((source, e)) or {}
            first = ent.get("first_seen") or latest
            rel = _release(source, ent.get("extra") or {})
            if rel:
                is_new = 0 <= sampled - rel < config.NEW_DAYS * DAY      # 有上线日期就按上线日期
            else:
                is_new = (first - s_first > DAY) and (latest - first < config.NEW_DAYS * DAY)
            level_ok = v >= min_level or (is_new and v >= min_level * config.NEW_LEVEL_FACTOR)
            cus, last_ts = anomaly.get((source, e, metric), (0.0, 0))
            if z is not None and sampled > (last_ts or 0):
                cus = min(50.0, max(0.0, cus + z - config.CUSUM_K))
                updates.append((source, e, metric, cus, sampled))
            hot, fast = 0.0, False
            if z is not None and level_ok:
                a = max((z - config.Z_START) / (config.Z_FULL - config.Z_START), cus / config.CUSUM_H)
                a = max(0.0, min(1.0, a))
                lf = max(0.4, min(1.0, 0.4 + 0.3 * math.log10((v + 1) / (min_level + 1))))
                hot = a * lf
                fast = z >= config.Z_FAST
            flow = short.get(e)
            if flow and g24.get(e) is None and rel and 0 <= sampled - rel <= 30 * DAY \
                    and now - sampled <= 6 * 3600:
                # A recent release with real flow can surface before peer statistics exist.
                flow_hot = min(1.0, flow["rate"] * 24 / max(1, min_level))
                hot = max(hot, flow_hot)
                fast = fast or flow_hot >= .75
            if now - sampled > 6 * 3600:
                fast = False
            signals.append({"source": source, "entity": e, "metric": metric, "value": v,
                            "g24": g24.get(e), "gshort": gs.get(e), "z": z, "cusum": cus,
                            "hot": hot, "fast": fast, "level_ok": level_ok, "is_new": is_new,
                            "ts": sampled, "observed_growth": flow})

    with db.session() as c:
        c.executemany("INSERT OR REPLACE INTO anomaly(source, entity, metric, cusum, last_ts) "
                      "VALUES (?, ?, ?, ?, ?)", updates)
        c.executemany("INSERT OR REPLACE INTO metrics(ts, source, entity, metric, value) "
                      "VALUES (?, ?, ?, ?, ?)", index_rows)
    return signals, index


# ---------------- 上线日期 ----------------
def _release(source, x):
    """上线时间戳；不知道返回 None。"""
    if source == "steam":
        return None if x.get("coming_soon") else (x.get("release_ts") or None)
    if source in ("taptap", "tapmaker"):
        return None if x.get("preorder") else (x.get("released_time") or None)
    if source == "roblox":
        return x.get("created_ts") or None
    if source in ("haoyou", "appcn", "appus"):
        return None if x.get("preorder") else (x.get("release_ts") or None)
    return None


def _game_age(members, entities, src_first, first_run, now):
    """(上线天数, 依据)。launch = 平台给的上线日期（取最早）；soon = 还没上线（预约 / 即将推出）；
    seen = 没有上线日期，按雷达第一次看到算（只在每个发现平台成员都是在雷达开始抓这个平台一天以后才出现时用）；
    天数 None = 判断不了（没有上线日期，雷达开始抓的时候就已经在了）。"""
    rel, soon, seen, early = None, False, [], False
    for m in members:
        s, e = m.split(":", 1)
        ent = entities.get((s, e)) or {}
        x = ent.get("extra") or {}
        r = _release(s, x)
        if r and (rel is None or r < rel):
            rel = r
        if x.get("coming_soon") or x.get("preorder"):
            soon = True
        if s in config.DISCOVERY_SOURCES:
            fs = ent.get("first_seen") or 0
            if fs and fs - src_first.get(s, first_run) > DAY:
                seen.append(fs)
            else:
                early = True
    if rel:
        return max(0.0, (now - rel) / DAY), "launch"
    if soon:
        return 0.0, "soon"
    if seen and not early:
        return max(0.0, (now - min(seen)) / DAY), "seen"
    return None, "unknown"


# ---------------- 只有名次的榜单 ----------------
def _rank_scores(entities):
    """好游快爆、小米商店、App Store、itch.io、Poki、CrazyGames、Fortnite 的榜单名次 → 热度。

    new 榜（新品、24 小时热门）：上榜就按名次给；trend 榜（热门）：新上榜或一天里名次往前冲了才给，霸榜的不给。
    "新上榜"要这个平台 24 小时前也抓到过才判断（不然程序刚开始运行时所有游戏都是"新上榜"）；
    好游快爆榜单自己标了"新""升 N 名"，没有 24 小时前的数据时先用它的。
    返回 {"来源:编号": {"hot", "text"}}，每个游戏取各个榜里最热的一个。
    """
    now = int(time.time())
    by_src = defaultdict(list)
    for (s, m), (kind, label) in config.RANK_BOARDS.items():
        if kind != "info" and config.source_on(s):
            by_src[s].append((m, kind, label))
    out = {}
    with db.session() as c:
        for s, boards in by_src.items():
            tss = [r[0] for r in c.execute("SELECT DISTINCT ts FROM metrics WHERE source = ? AND ts >= ? "
                                           "ORDER BY ts DESC", (s, now - 32 * 3600))]
            if not tss or now - tss[0] > 8 * 3600:
                continue          # 最近没抓到，不算
            cur_ts = tss[0]
            prev_ts = min((t for t in tss if t <= cur_ts - 18 * 3600), key=lambda t: abs(t - (cur_ts - DAY)),
                          default=None)
            cur, prev = defaultdict(dict), defaultdict(dict)
            for e, m, v in c.execute("SELECT entity, metric, value FROM metrics WHERE source = ? AND ts = ? "
                                     "AND metric LIKE 'rank_%'", (s, cur_ts)):
                cur[m][e] = v
            if prev_ts:
                for e, m, v in c.execute("SELECT entity, metric, value FROM metrics WHERE source = ? AND ts = ? "
                                         "AND metric LIKE 'rank_%'", (s, prev_ts)):
                    prev[m][e] = v
            for m, kind, label in boards:
                ranks = cur.get(m) or {}
                if not ranks:
                    continue
                n = max(len(ranks), max(ranks.values()), 10)
                had_prev = bool(prev.get(m))
                for e, r in ranks.items():
                    r = int(r)
                    pos = 1 - math.log(r) / math.log(n + 1)          # 第 1 名 = 1，最后一名接近 0
                    hot, text = 0.0, ""
                    if kind == "new":
                        hot = config.RANK_NEW_TOP * pos
                        text = f"{label}第{r}"
                    else:
                        p = prev[m].get(e) if had_prev else None
                        mark = None
                        if not had_prev:
                            mark = ((((entities.get((s, e)) or {}).get("extra") or {}).get("marks") or {}).get(m))
                            if isinstance(mark, int) and mark > 0:
                                p = r + mark
                        if (had_prev and e not in prev[m]) or mark == "new":
                            if r <= n / 2:
                                hot = 0.3 + 0.5 * pos
                                text = f"{label}新上榜第{r}"
                        elif p and p - r >= config.RANK_RISE_MIN:
                            hot = min(1.0, 1.5 * math.log(p / r) / math.log(n + 1))
                            text = f"{label}第{int(p)}→{r}"
                    if hot <= 0:
                        continue
                    key = f"{s}:{e}"
                    if key not in out or hot > out[key]["hot"]:
                        out[key] = {"hot": round(min(1.0, hot), 3), "text": text}
    return out


# 新游戏"上线以来平均每天涨多少"用哪个累计数，以及最低多少才算（太小的不看）
LAUNCH_METRIC = {"steam": ("reviews", "条评论", 10), "tapmaker": ("downloads", "热度", 300),
                 "taptap": ("downloads", "热度", 2000), "roblox": ("visits", "次访问", 10000)}


def _launch_scores(entities):
    """上线不到 NEW_GAME_DAYS 天的游戏：累计数 ÷ 上线天数，和同平台其他新游戏比排第几。

    新游戏上线后自然是一路回落的，"比昨天涨了多少"对它不公平；这个从第一次抓到就能算，不用等 24 小时。
    返回 {"来源:编号": {"hot", "age", "rate", "unit", "top"}}。
    """
    now = time.time()
    latest = {}
    with db.session() as c:
        for src, (m, _, _) in LAUNCH_METRIC.items():
            for e, v, _ in c.execute("SELECT entity, value, MAX(ts) FROM metrics WHERE source = ? AND metric = ? "
                                     "AND ts >= ? GROUP BY entity", (src, m, now - 3 * DAY)):
                latest[(src, e)] = v
    peers = defaultdict(list)
    for (s, e), v in latest.items():
        rel = _release(s, (entities.get((s, e)) or {}).get("extra") or {})
        if not rel:
            continue
        age = (now - rel) / DAY
        if age < 0 or age > config.NEW_GAME_DAYS:
            continue
        peers[s].append((e, v / max(age, 0.5), age))
    out = {}
    for s, lst in peers.items():
        if len(lst) < 5:
            continue
        rates = sorted(r for _, r, _ in lst)
        n = len(rates)
        floor, unit = LAUNCH_METRIC[s][2], LAUNCH_METRIC[s][1]
        for e, r, age in lst:
            below = sum(1 for x in rates if x < r)
            pct = below / max(1, n - 1)          # 0 = 最慢，1 = 最快
            hot = max(0.0, (pct - 0.7) / 0.3) if r >= floor else 0.0
            out[f"{s}:{e}"] = {"hot": round(hot, 3), "age": round(age, 1), "rate": r, "unit": unit,
                               "top": max(1, round((1 - pct) * 100)), "n": n}
    return out


LIVE_NAME = {s: v[0] for s, v in config.LIVE_PLATFORMS.items()}


def _live_new_categories(entities, sig_by_key):
    """直播平台的分区：
    - 新开的分区（程序开始运行一天以后才出现、不到 NEW_CAT_DAYS 天）：有几个主播在播就给多少热度，不用等 24 小时
    - 开始运行时就有、但当时还小的分区：返回在 day1 里，当老游戏打折（还是能看到）
    """
    now = time.time()
    first_run = db.kv_get("first_run") or now
    src_first = db.source_first_runs()
    new, day1 = {}, {}
    for (s, e), ent in entities.items():
        if s not in LIVE_NAME or (ent.get("extra") or {}).get("generic"):
            continue
        key = f"{s}:{e}"
        fs = ent.get("first_seen") or now
        if fs <= src_first.get(s, first_run) + DAY:
            day1[key] = f"{LIVE_NAME[s]}开始运行时就有这个分区"
            continue
        age = (now - fs) / DAY
        if age > config.NEW_CAT_DAYS:
            continue
        _, metric, low = config.LIVE_PLATFORMS[s]
        rooms = next((x["value"] for x in sig_by_key.get(key, []) if x["metric"] == metric), 0)
        if rooms < low:
            continue
        # 主播数：3 个起步、30 个满分；Kick 只有观看人数：300 起步、1 万满分
        hot = min(1.0, math.log(rooms / low * 3) / math.log(30))
        what = f"{int(rooms)} 个主播在播" if metric == "rooms" else f"{_fmt_num(rooms)} 人在看"
        new[key] = {"hot": round(hot, 3), "text": f"{LIVE_NAME[s]}新开的分区（{age:.0f} 天前出现），{what}"}
    return new, day1


def _star_rooms(established):
    """大主播现在在播什么（老分区里的不算）。返回 (列表, {分区: [主播]}, 大杂烩分区里的主播)。"""
    out, by_cat, generic = [], defaultdict(list), []
    for s, sname in LIVE_NAME.items():
        if not config.source_on(s):
            continue
        for x in db.kv_get("star_live:" + s, []) or []:
            key = f"{s}:{x['game']}"
            if established.get(key):
                continue
            st = dict(x, src=s, src_name=sname, cat_key=key, _norm=match.normalize(x.get("title") or ""))
            out.append(st)
            if x.get("generic"):
                generic.append(st)
            else:
                by_cat[key].append(st)
    out.sort(key=lambda x: -(x.get("heat") or 0))
    return out, by_cat, generic


def _age_text(days):
    if days < 1:
        return "不到 1 天"
    if days < 60:
        return f"{days:.0f} 天"
    if days < 730:
        return f"{days / 30:.0f} 个月"
    return f"{days / 365:.0f} 年"


_BIG_NORMS = None


def _big_game(card):
    global _BIG_NORMS
    if _BIG_NORMS is None:
        _BIG_NORMS = [(match.normalize(x), x) for x in config.KW_BIG_GAMES if match.normalize(x)]
    names = [match.normalize(n or "") for n in (card.get("name"), card.get("name_cn"), card.get("name_en"))]
    # Fortnite 创意地图的岛名里常带 Fortnite，那是平台名，不是"老牌大游戏"
    island = all(m.split(":", 1)[0] == "fortnite" for m in card.get("members") or [])
    for n in names:
        for b, raw in _BIG_NORMS:
            if island and raw in ("fortnite", "堡垒之夜"):
                continue
            if n and b in n:
                return raw
    return None


_SRC_NAME = {"steam": "Steam", "taptap": "TapTap", "tapmaker": "TapTap", "roblox": "Roblox",
             "haoyou": "好游快爆", "appcn": "App Store 中国", "appus": "App Store 美国"}


def _age_and_studio(card):
    """按上线多久降权（超过一年 = 0，进"老游戏"），大厂降权。都不排除。你打过星就不降。"""
    info, label, flags = card["info"], card["label"], card["flags"]
    ts = info.get("launch_ts")
    big_game = _big_game(card)
    if big_game:
        # 英雄联盟、绝区零、火影这类老牌大游戏（包括它的模拟器、手游版、新平台移植版）：上线日期不准也算老游戏
        w = 0.0
        flags.insert(0, {"t": f"老牌大游戏（{big_game}）", "c": "warn"})
    elif ts:
        age = max(0.0, (time.time() - ts) / DAY)
        w = launch.age_weight(age)
        src = info.get("launch_src") or ""
        where = _SRC_NAME.get(src) or (src + (f"：{info['launch_matched']}" if info.get("launch_matched") else ""))
        flags.insert(0, {"t": f"上线 {_age_text(age)}（{where}）",
                         "c": "ok" if w >= 0.7 else ("" if w > 0 else "warn")})
    elif card.get("_old_hint"):
        w = 0.0
        flags.insert(0, {"t": "老游戏：" + card["_old_hint"] + "（没查到上线时间，查到一年内上线的会自动回来）", "c": "warn"})
    else:
        w = config.UNKNOWN_AGE_WEIGHT
        flags.insert(0, {"t": "没查到上线时间", "c": "warn"})
    big = 1.0
    if not card.get("_heavy"):
        devs = list(card.get("devs") or []) + ([info["launch_dev"]] if info.get("launch_dev") else [])
        hit = next((d for d in devs if d and match.company_match(d, config.BIG_COMPANIES)), None)
        if hit:
            big = config.BIG_FACTOR
            flags.append({"t": f"大厂：{hit}（降权，不排除）", "c": ""})
    if (label.get("stars") or 0) >= 3:
        w, big = 1.0, 1.0           # 你觉得值得跟（3 星以上）：不管多老、多大都不打折
    fw = card.setdefault("factor_why", [])
    if w < 1:
        fw.insert(0, "上线时间 ×" + (f"{w:g}" if w > 0 else "0（老游戏）"))
    if big < 1:
        fw.append(f"大厂 ×{big:g}")
    card["age_w"] = w
    card["factor"] = round(card["factor"] * w * big, 3)


def _dur(since):
    h = (time.time() - (since or time.time())) / 3600
    if h < 1:
        return "刚开始播"
    if h < 48:
        return f"播了 {h:.0f} 小时"
    return f"播了 {h / 24:.0f} 天"


_EVENT_RE = None


def _event_hint(names):
    """名字里有更新、活动、节日字样：热度可能是活动带来的，不一定是玩法火了。"""
    global _EVENT_RE
    if _EVENT_RE is None:
        import re
        _EVENT_RE = re.compile(r"\[(upd|update|event|new|free)[^\]]*\]|\bupdate\b|halloween|christmas|easter|"
                               r"summer|winter|更新|活动|第.季|赛季|周年|万圣|圣诞|春节|新春", re.I)
    for n in names:
        if n and _EVENT_RE.search(n):
            return n
    return None


# ---------------- 老游戏 ----------------
def _established(entities):
    """返回 {"来源:编号": 原因}：程序开始运行时就已经很大的、或者上线很久的，算存量老游戏。"""
    now = time.time()
    first_run = db.kv_get("first_run") or now
    src_first = db.source_first_runs()
    first = {}
    with db.session() as c:
        for src, (m, lim, txt) in config.OLD_FIRST_LEVEL.items():
            for e, v, _ in c.execute("SELECT entity, value, MIN(ts) FROM metrics WHERE source = ? AND metric = ? "
                                     "GROUP BY entity", (src, m)):
                first[(src, e)] = v
    out = {}
    for (s, e), ent in entities.items():
        x = ent["extra"]
        key = f"{s}:{e}"
        if s == "steam" and x.get("release_ts") and not x.get("coming_soon"):
            age = (now - x["release_ts"]) / DAY
            if age > config.OLD_STEAM_DAYS:
                out[key] = f"Steam 上线 {age / 30:.0f} 个月"
            continue
        if s in ("taptap", "tapmaker"):
            rel = x.get("released_time") or 0
            if rel and not x.get("preorder") and (now - rel) / DAY > config.OLD_TAPTAP_DAYS:
                out[key] = f"TapTap 上线 {(now - rel) / DAY / 30:.0f} 个月"
            continue
        if s == "roblox" and x.get("created_ts"):
            age = (now - x["created_ts"]) / DAY
            if age > config.OLD_TAPTAP_DAYS:
                out[key] = f"Roblox 创建 {age / 30:.0f} 个月"
            continue
        rule = config.OLD_FIRST_LEVEL.get(s)
        if rule and not x.get("generic"):
            v = first.get((s, e))
            if v is not None and v >= rule[1] and (ent.get("first_seen") or 0) <= src_first.get(s, first_run) + DAY:
                out[key] = rule[2].format(v=int(v))
    return out


# ---------------- 2. 合并同一个游戏 ----------------
def build_groups(entities, labels, links, archive=None):
    """一个游戏 = 一份档案。archive 是游戏档案（radar/archive.py）给的 {成员: 档案号}；
    不给时只按平台编号分组。旧的 links 表（v67 以前按名字合并留下的）永远不参与。"""
    groups = defaultdict(list)
    for (src, e), ent in entities.items():
        m = f"{src}:{e}"
        gid = (archive or {}).get(m) or identity.game_key(src, e)
        if gid is None:
            continue
        groups[gid].append(m)
    return groups


_EN_SOURCES = ("steam", "roblox", "appus", "fortnite", "itch", "poki", "crazygames", "twitch", "kick")


def _display(members, entities):
    order = ["taptap", "tapmaker", "haoyou", "appcn", "mistore", "wxmini", "dymini", "huya", "douyu", "bili", "huya_t", "douyu_t",
             "bili_t", "bvideo", "steam", "roblox", "appus", "fortnite", "twitch", "kick", "itch", "poki", "crazygames"]
    order += sorted({m.split(":", 1)[0] for m in members} - set(order))
    cn, en = None, None
    for src in order:
        for key in members:
            s, e = key.split(":", 1)
            if s != src:
                continue
            ent = entities[(s, e)]
            nm = ent["name"]
            ncn = ent["extra"].get("name_cn")
            if not cn and match.has_cjk(nm):
                cn = nm
            if not cn and ncn and match.has_cjk(ncn):
                cn = ncn
            if not en and s in _EN_SOURCES:
                en = nm
    first = members[0].split(":", 1)
    fallback = entities[(first[0], first[1])]["name"]
    return cn or en or fallback, cn, en


def group_info(members, entities):
    info = {"tags": [], "devs": [], "size_gb": None, "price_usd": None, "cn_share": None,
            "release_ts": None, "images": [], "fallback_images": [], "steam_coming_soon": False, "generic": False}
    for key in members:
        s, e = key.split(":", 1)
        x = entities[(s, e)]["extra"]
        info["tags"] += x.get("tags") or []
        info["tags"] += x.get("genres") or []
        if x.get("genre"):
            info["tags"].append(x["genre"])
        info["devs"] += (x.get("developers") or []) + (x.get("publishers") or [])
        if x.get("dev"):
            info["devs"].append(x["dev"])
        if x.get("size_gb") is not None:
            info["size_gb"] = x["size_gb"]
        if x.get("price_usd") is not None:
            info["price_usd"] = x["price_usd"]
        if x.get("cn_share") is not None:
            info["cn_share"] = x["cn_share"]
        if x.get("release_ts"):
            info["release_ts"] = x["release_ts"]
        if x.get("coming_soon"):
            info["steam_coming_soon"] = True
        img = images.image_of(s, e, x)      # v51：TapTap 的 icon 有时是 {"small_url": ...}，以前直接当地址用了
        if img:
            if x.get("image") or x.get("icon"):
                info["images"].append(img)
            else:
                info["fallback_images"].append(img)   # Steam 用 appid 拼出来的封面，排在平台自己给的图后面
        if x.get("generic"):
            info["generic"] = True
        info["categories"] = info.get("categories", []) + (x.get("categories") or [])
        if x.get("discount"):
            info["discount"] = max(info.get("discount") or 0, x["discount"])
        rel = _release(s, x)
        if rel and (not info.get("launch_ts") or rel < info["launch_ts"]):
            info["launch_ts"], info["launch_src"] = rel, s
    return info


def _combine(hs):
    p = 1.0
    for h in hs:
        p *= (1 - max(0.0, min(1.0, h)))
    return 1 - p


def _merge_label(members, labels):
    out = {"stars": None, "blocked": False, "note": "", "aliases": [], "keywords": [], "dismissed": 0}
    latest = -1
    for m in members:
        l = labels.get(m)
        if not l:
            continue
        out["blocked"] = out["blocked"] or l.get("blocked")
        out["dismissed"] = max(out["dismissed"], l.get("dismissed") or 0)
        out["aliases"] += l.get("aliases") or []
        out["keywords"] += l.get("keywords") or []
        if (l.get("updated") or 0) > latest:
            latest = l.get("updated") or 0
            if l.get("stars"):
                out["stars"] = l["stars"]
            if l.get("note"):
                out["note"] = l["note"]
    return out


# ---------------- 3. 主流程 ----------------
import threading
_RUN_LOCK = threading.Lock()      # 同一时间只算一份（抓取那边的分析和你改标记后的重算不会撞在一起）


def run(allow_network=True, send_notify=True, allow_ai=None):
    """allow_ai：AI 读评论要不要联网，默认跟 allow_network 一样。快速重算时也可以单独让 AI 联网。"""
    with _RUN_LOCK:
        return _run_logged(allow_network, send_notify, allow_ai)


def _run_logged(allow_network, send_notify, allow_ai=None):
    activity.begin("分析打分")
    activity.say("开始分析打分")
    try:
        d = _run(allow_network, send_notify, allow_ai)
        t = d.get("totals") or {}
        nsp = sum(len(v) for v in (d.get("sprouts") or {}).values())
        activity.say(f"分析完成：必看 {t.get('cards', len(d['cards']))} 款，刚起势 {nsp} 个，用时 {d['seconds']} 秒")
        db.kv_set("analyze_error", None)
        return d
    except Exception as e:
        import traceback
        activity.say(f"分析失败：{e}")
        try:     # 面板顶上用红条显示，不再悄悄停在旧结果上
            db.kv_set("analyze_error", {"ts": int(time.time()), "error": str(e)[:200],
                                        "trace": traceback.format_exc()[-1500:]})
        except Exception:
            pass
        raise
    finally:
        activity.end()


def _social_terms(order, groups, entities, n=60):
    """排名靠前的游戏名交给抖音 / 快手 / 小红书 去搜（social.funnel.queue 读 kv radar_social_terms）。"""
    out = []
    for g in order:
        names = []
        for m in groups[g]:
            e = entities.get(tuple(m.split(":", 1))) or {}
            names += [(e.get("extra") or {}).get("name_cn"), e.get("name")]
        t = next((x for x in names if x and match.has_cjk(x)), None) or next((x for x in names if x), None)
        if t and t not in out:
            out.append(t)
        if len(out) >= n:
            break
    try:
        db.kv_set("radar_social_terms", out)
    except Exception:                                   # noqa: BLE001
        pass


def _bili_terms(order, groups, entities):
    """新游戏的名字交给 B站 按名字搜（opp.bili_track.search_games 读 kv radar_bili_terms）。"""
    out = []
    for g in order:
        ms = groups[g]
        names = []
        for m in ms:
            e = entities.get(tuple(m.split(":", 1))) or {}
            names += [(e.get("extra") or {}).get("name_cn"), e.get("name"), (e.get("extra") or {}).get("name_en")]
        names = [x for i, x in enumerate(names) if x and x not in names[:i]]
        names.sort(key=lambda x: not match.has_cjk(x))
        if names:
            out.append({"gid": g, "member": ms[0], "names": names[:3]})
    try:
        db.kv_set("radar_bili_terms", out)
    except Exception:                                   # noqa: BLE001
        pass


def _new_games(groups, entities, now):
    """{档案号: {days, how}}：上线不超过 NEW_MAX_DAYS 天的游戏（只看有发现平台成员、不是大杂烩分区的）。"""
    first_run = db.kv_get("first_run") or now
    src_first = db.source_first_runs()
    out = {}
    for g, ms in groups.items():
        if not any(m.split(":", 1)[0] in config.DISCOVERY_SOURCES for m in ms):
            continue
        if any(((entities.get(tuple(m.split(":", 1))) or {}).get("extra") or {}).get("generic") for m in ms):
            continue
        days, how = _game_age(ms, entities, src_first, first_run, now)
        if days is None or days > config.NEW_MAX_DAYS:
            continue
        nm, ncn, nen = _display(ms, entities)
        if _big_game({"name": nm, "name_cn": ncn, "name_en": nen, "members": ms}):
            continue
        out[g] = {"days": round(days, 1), "how": how}
    return out


def _plat_diag(groups, hs, elig, now_ts):
    """每个发现平台：32 小时内抓到几款、几款有分数、几款是大作 / 大厂、两种排名下最好第几。"""
    from .heat import rank_key
    with db.session() as c:
        seen = {r[0]: r[1] for r in c.execute(
            "SELECT source, COUNT(DISTINCT entity) FROM metrics WHERE ts >= ? GROUP BY source", (now_ts - 32 * 3600,))}
    pos_f = {g: i + 1 for i, g in enumerate(sorted(elig, key=lambda g: rank_key(hs[g], "final")))
             if hs[g]["final"] is not None}
    pos_r = {g: i + 1 for i, g in enumerate(sorted(elig, key=lambda g: rank_key(hs[g], "raw")))
             if hs[g]["raw"] is not None}
    out = []
    for s in sorted(config.DISCOVERY_SOURCES):
        if not config.source_on(s):
            continue
        p = s + ":"
        mine = [g for g in elig if any(m.startswith(p) for m in groups[g])]
        out.append({"src": s, "seen": seen.get(s, 0), "scored": sum(1 for g in mine if hs[g]["raw"] is not None),
                    "zero": sum(1 for g in mine if hs[g]["big"]),
                    "best": min((pos_f[g] for g in mine if g in pos_f), default=None),
                    "best_raw": min((pos_r[g] for g in mine if g in pos_r), default=None)})
    return out


def _run(allow_network=True, send_notify=True, allow_ai=None):
    t0 = time.time()
    entities = db.load_entities()
    labels = db.load_labels()
    links = db.load_links()
    signals, index = compute_signals(entities)
    signals = [s for s in signals if config.source_on(s["source"])]      # 关掉的平台，以前的数据也不再用
    index = {k: v for k, v in (index or {}).items() if config.source_on(k.split("/", 1)[0])} \
        if isinstance(index, dict) else index

    sig_by_key = defaultdict(list)
    for s in signals:
        sig_by_key[f"{s['source']}:{s['entity']}"].append(s)

    try:
        from . import archive
        amap = archive.rebuild(entities)
    except Exception as e:
        import traceback
        traceback.print_exc()
        activity.say(f"游戏档案重算失败，这一轮只按平台编号分组：{e}")
        amap = None
    try:
        from . import revisit
        plan = revisit.Plan()
        revisit.bili_signals(plan)
        plan.flush()
        if plan.stat["fired"]:
            activity.say(f"B站 新视频突增，{plan.stat['fired']} 款游戏回到每轮深查")
    except Exception:                             # noqa: BLE001
        import traceback
        traceback.print_exc()
    groups = build_groups(entities, labels, links, amap)
    established = _established(entities)

    # 供给检查用的 TapTap 列表：榜单 + TapTap制造 + 创意工坊，每个游戏用它自己最近一次的数据
    now_ts = int(time.time())
    tap_latest = {}
    with db.session() as c:
        for src in ("taptap", "tapmaker"):
            for e, m, v, _ in c.execute(
                    "SELECT entity, metric, value, MAX(ts) FROM metrics WHERE source = ? "
                    "AND metric IN ('downloads', 'reserves') AND ts >= ? GROUP BY entity, metric",
                    (src, now_ts - 3 * DAY)):
                tap_latest.setdefault((src, e), {})[m] = v
    tap_ctx = {}
    for (s, e), ent in entities.items():
        if s not in ("taptap", "tapmaker") or ent["extra"].get("gone"):
            continue
        lat = tap_latest.get((s, e), {})
        cur = tap_ctx.get(e)          # 同一个游戏可能同时在榜单和标签列表里，只算一次
        if cur and (cur.get("downloads") or 0) >= (lat.get("downloads") or 0):
            continue
        norm = match.normalize(ent["name"])
        tap_ctx[e] = {"key": f"{s}:{e}", "name": ent["name"], "norm": norm, "chars": set(norm),
                      "extra": ent["extra"], "downloads": lat.get("downloads"), "reserves": lat.get("reserves")}
    ctx = {"taptap": list(tap_ctx.values())}

    launch_sc = _launch_scores(entities)
    with db.session() as c:
        steam_reviews = {f"steam:{e}": v for e, v, _ in c.execute(
            "SELECT entity, value, MAX(ts) FROM metrics WHERE source = 'steam' AND metric = 'reviews' "
            "AND ts >= ? GROUP BY entity", (now_ts - 3 * DAY,))}

    live_new, live_day1 = _live_new_categories(entities, sig_by_key)
    stars, star_by_cat, star_titles = _star_rooms(established)
    rank_sc = _rank_scores(entities)
    # v50 刚起势（浮萍）：还小、但连着几天按比例往上涨的
    try:
        from . import sprouts as _sp
        sprout_rows = _sp.compute(entities)
    except Exception as e:
        import traceback
        traceback.print_exc()
        activity.say(f"刚起势计算出错：{type(e).__name__}：{e}")
        sprout_rows = {}
    sprout_sc = {r["key"]: r for rows in sprout_rows.values() for r in rows if r["hot"] > 0}

    # v81 评分（radar/heat.py）：异常值。先给所有档案算分，两种排名各取前 400 做卡片
    from . import heat
    hs, heat_meta = heat.score_groups(groups, entities)
    # v82：必看只有新游戏：上线不超过 NEW_MAX_DAYS 天；老牌大游戏、判断不了上线时间的不进
    ages = _new_games(groups, entities, now_ts)
    elig = [g for g in ages if g in hs]
    by_r = sorted(elig, key=lambda g: heat.rank_key(hs[g], "raw"))
    pick = set(by_r[:400]) | set(sorted(elig, key=lambda g: heat.rank_key(hs[g], "final"))[:400])
    _social_terms(by_r, groups, entities)
    _bili_terms(by_r + [g for g in ages if g not in hs], groups, entities)

    cards = []
    for gid, members in groups.items():
        if gid not in pick:
            continue
        sigs = [s for m in members for s in sig_by_key.get(m, [])]
        launch_m = {m: launch_sc[m] for m in members if m in launch_sc}
        launch_m.update({m: live_new[m] for m in members if m in live_new})
        for m in members:          # 榜单名次（一个游戏同时在几个榜上时，launch_m 里每个平台各留一个最热的）
            if m in rank_sc and rank_sc[m]["hot"] > (launch_m.get(m) or {}).get("hot", -1):
                launch_m[m] = rank_sc[m]
        sp = sorted((sprout_sc[m] for m in members if m in sprout_sc), key=lambda r: -r["hot"])
        # 开播时就很大的直播分区（英雄联盟、和平精英……）：大主播在里面播不算信号；上线时间查到后按年头降到 0
        live_old = {m for m in members if m.split(":", 1)[0] in LIVE_NAME and established.get(m)}
        disc = [m for m in members if m.split(":", 1)[0] in config.DISCOVERY_SOURCES]
        if not disc:
            continue
        # 大主播在播：分区里有大主播，或者大主播在大杂烩分区的标题里写了这个游戏名
        name, cn_name, en_name = _display(members, entities)
        star_hits = [st for m in members if m not in live_old for st in star_by_cat.get(m, [])]
        lhot = max([v["hot"] for v in launch_m.values()] + [0.0] + ([config.STAR_HOT] if star_hits else [])
                   + [r["hot"] for r in sp])
        info = group_info(members, entities)
        if info["generic"]:
            continue          # 大杂烩分区本身不是游戏（里面的具体游戏看大主播标题）
        label = _merge_label(members, labels)

        market_hot = defaultdict(list)
        for s in sigs:
            m = config.SOURCE_MARKET.get(s["source"], "global")
            market_hot[m].append(s["hot"])
            if s["source"] == "steam" and info.get("cn_share"):
                market_hot["cn"].append(s["hot"] * min(1.0, info["cn_share"] * 2))
        for m, v in launch_m.items():
            market_hot[config.SOURCE_MARKET.get(m.split(":", 1)[0], "global")].append(v["hot"])
        for r in sp:
            market_hot[config.SOURCE_MARKET.get(r["key"].split(":", 1)[0], "global")].append(r["hot"])
        mh = {m: _combine(v) for m, v in market_hot.items()}

        for st in star_hits:        # 大主播在哪个平台播，就算哪个市场的热度（Twitch 是海外）
            mk = config.SOURCE_MARKET.get(st["src"], "cn")
            mh[mk] = max(mh.get(mk, 0.0), config.STAR_HOT)
        hot = max(mh.values()) if mh else 0.0

        # 标签：只加说明、只改排序，不隐藏
        flags, factor, factor_why = [], 1.0, []
        for member in members:
            extra = entities[tuple(member.split(":", 1))].get("extra") or {}
            if extra.get("metric_provenance") == "official_browser_verified":
                flags.append({"t": "官方页面核验快照，后台自动刷新待验证", "c": "warn"})
            if extra.get("fetch_error"):
                flags.append({"t": "最近采样失败：" + extra["fetch_error"][:80], "c": "warn"})
        sampled_at = max((s["ts"] for s in sigs), default=0)
        if sampled_at and time.time() - sampled_at > 6 * 3600:
            flags.append({"t": "采样已过期，最近数据 " +
                          time.strftime("%m-%d %H:%M", time.localtime(sampled_at)), "c": "warn"})
        early = max((s for s in sigs if s.get("observed_growth") and s["hot"] > 0
                     and s.get("g24") is None and time.time() - s["ts"] <= 6 * 3600),
                    key=lambda s: s["hot"], default=None)
        if early:
            flow = early["observed_growth"]
            flags.append({"t": f"实际 {flow['hours']:.1f} 小时新增 {_fmt_num(flow['delta'])}"
                          f"（{_fmt_num(flow['rate'])}/小时），短期信号", "c": "hot"})
        for r in sp[:2]:
            flags.append({"t": r["text"], "c": "hot", "k": "sprout"})
        if star_hits:
            star_hits.sort(key=lambda s: -s["heat"])
            flags.append({"t": "大主播在播：" + "、".join(
                f"{s['nick']}（{s['src_name']}，{_dur(s['since'])}）" for s in star_hits[:3]), "c": "hot"})
        best_l = max(((k, v) for k, v in launch_m.items() if not v.get("text")),
                     key=lambda kv: kv[1]["hot"], default=None)
        for v in launch_m.values():
            if v.get("text") and v["hot"] > 0:
                flags.append({"t": v["text"], "c": "hot"})       # 直播平台新开的分区
        for m in members:
            s0 = m.split(":", 1)[0]
            if m in live_old or m in live_new:
                continue
            if s0 == "kick":
                vv = next((x["value"] for x in sig_by_key.get(m, []) if x["metric"] == "viewers"), None)
                if vv:
                    flags.append({"t": f"Kick 分类 {_fmt_num(vv)} 人在看", "c": ""})
                continue
            if s0 == "bvideo":
                vv = next((x["value"] for x in sig_by_key.get(m, []) if x["metric"] == "videos"), None)
                if vv:
                    flags.append({"t": f"B站游戏区排行 / 热门里有 {int(vv)} 个视频标题写了它", "c": ""})
                continue
            rv = next((x["value"] for x in sig_by_key.get(m, []) if x["metric"] == "rooms"), None)
            if not rv:
                continue
            if s0 in LIVE_NAME:
                flags.append({"t": f"{LIVE_NAME[s0]}分区 {int(rv)} 个主播在播", "c": ""})
            elif s0.endswith("_t") and s0[:-2] in LIVE_NAME:
                flags.append({"t": f"{LIVE_NAME[s0[:-2]]} {int(rv)} 个主播在大杂烩分区播（标题里写了）", "c": ""})
        if best_l and best_l[1]["hot"] > 0:
            v = best_l[1]
            flags.append({"t": f"上线以来每天 {_fmt_num(v['rate'])} {v['unit']}，同平台新游戏前 {v['top']}%", "c": "hot"})
        # 查不到上线时间时的备用判断：开始运行时就已经很大的
        olds = [established.get(m) or live_day1.get(m) for m in disc]
        old_hint = "；".join(sorted(set(o for o in olds if o)))[:60] if olds and all(olds) else None
        hv = scoring.heavy(info)
        if hv:
            flags.append({"t": hv, "c": ""})
            if (label.get("stars") or 0) < 3:
                factor *= config.HEAVY_FACTOR
                factor_why.append(f"重度大作 ×{config.HEAVY_FACTOR}")
        if info.get("discount"):
            # v52：只说明，不提醒、不扣分。打折带来的玩家也是玩家；首发打折很常见
            flags.append({"t": f"Steam 打折中 -{info['discount']}%", "c": ""})
        ev = _event_hint([entities[tuple(m.split(":", 1))]["name"] for m in members])
        if ev:
            flags.append({"t": "名字带更新/活动字样，热度可能是活动带来的", "c": "warn"})
        ag = ages.get(gid) or {}
        if ag.get("how") == "seen":
            flags.append({"t": f"雷达 {_age_text(ag['days'])}前第一次看到（没有上线日期）", "c": ""})
        elif ag.get("how") == "soon":
            flags.append({"t": "还没上线", "c": "ok"})
        cats = {x.lower() for x in info.get("categories") or []}
        if cats & {"online pvp", "pvp"}:
            flags.append({"t": "能和陌生人对战", "c": "ok"})
        elif cats & {"online co-op", "co-op"} and not (cats & {"single-player"}):
            flags.append({"t": "朋友联机型（要凑朋友）", "c": "warn"})
        elif cats & {"online co-op", "co-op"}:
            flags.append({"t": "单人也能玩，主打和朋友合作", "c": ""})

        cards.append({
            "akey": gid, "gid": gid, "name": name,
            "name_cn": cn_name, "name_en": en_name, "members": members,
            "urls": {m: u for m in members
                     for u in [((entities.get(tuple(m.split(":", 1))) or {}).get("extra") or {}).get("url")] if u},
            "info": {k: info.get(k) for k in ("size_gb", "price_usd", "cn_share", "release_ts", "steam_coming_soon",
                                              "launch_ts", "launch_src", "discount")},
            "tags": sorted(set(info["tags"]))[:15], "devs": sorted(set(info["devs"]))[:4],
            "categories": sorted(set(info.get("categories") or []))[:12],
            "image": (info["images"] + info["fallback_images"] + [""])[0],
            "label": label, "hot": round(hot, 3), "market_hot": {k: round(v, 3) for k, v in mh.items()},
            "flags": flags, "factor": round(factor, 3), "factor_why": factor_why,
            "heat": hs[gid], "raw": hs[gid]["raw"], "final": hs[gid]["final"], "age": ages.get(gid),
            "launch": best_l[1] if best_l else None,
            "fast": any(s["fast"] for s in sigs),
            "first_seen": min(entities[tuple(m.split(":", 1))]["first_seen"] or 0 for m in members),
            "signals": [{"key": f"{s['source']}:{s['entity']}", "source": s["source"],
                         "metric": s["metric"], "value": s["value"],
                         "g24": s["g24"], "gshort": s["gshort"], "z": s["z"], "cusum": s["cusum"],
                         "hot": s["hot"], "fast": s["fast"],
                         "observed_growth": s.get("observed_growth"), "sampled_at": s["ts"]}
                        for s in sorted(sigs, key=lambda x: -x["hot"])][:8],
            "_devs": info["devs"], "_info": info, "_old_hint": old_hint, "_heavy": bool(hv),
            "_reviews": {m: steam_reviews[m] for m in members if m in steam_reviews},
        })

    # 上线时间：没有的先查（直播分区、标题里找到的游戏），再按上线多久降权
    launch.fill(cards, allow_network=allow_network)
    for card in cards:
        _age_and_studio(card)

    # 历史对照：起步和它差不多的 Steam 小游戏后来有多少成功了（只加说明，不改分数）
    try:
        from . import history
        history.attach(cards)
    except Exception as e:
        import traceback
        traceback.print_exc()
        activity.say(f"历史对照出错：{type(e).__name__}：{e}")

    # AI 读评论（有密钥才用；老游戏不读），然后算"能不能跟"
    try:
        ai_status = ai.apply([c for c in cards if c["age_w"] > 0], entities,
                             allow_network=allow_network if allow_ai is None else allow_ai)
    except Exception as e:          # AI 出任何问题都不能让整次分析失败
        import traceback
        traceback.print_exc()
        activity.say(f"AI 读评论出错：{type(e).__name__}：{e}")
        ai_status = ai.status()
        ai_status.update({"error": f"{type(e).__name__}：{e}"[:200], "err_ts": int(time.time())})
    for card in cards:
        card["best_market"] = max(card["market_hot"], key=card["market_hot"].get) if card["market_hot"] else None
        for k in ("_info", "_ai_member", "_reviews", "_old_hint", "_heavy"):
            card.pop(k, None)

    # 同类检查：只当参考写在卡片上，不进分数（按名字搜，搜到的不一定是同玩法）
    st_all = db.kv_get("source_status", {}) or {}
    tap_ok = any((st_all.get(s) or {}).get("ok_ts", 0) > time.time() - 6 * 3600 for s in ("tapmaker", "taptap"))
    cards.sort(key=lambda c: heat.rank_key(c["heat"], "final"))
    checked = 0
    for i, card in enumerate(cards):
        card["markets"] = {}
        if i >= 400 or card["label"].get("blocked") or card["age_w"] <= 0:
            continue
        for mkt, h in card["market_hot"].items():
            best = None
            for target in config.MARKET_TARGETS.get(mkt, []):
                terms = ([card["name_cn"], card["name"]] if mkt == "cn" else [card["name_en"], card["name"]])
                terms += card["label"]["keywords"]
                terms = [t for i, t in enumerate(terms) if t and t not in terms[:i]]
                net_ok = allow_network and target != "taptap" and checked < config.SUPPLY_TOP_N
                if net_ok:
                    activity.progress(checked, config.SUPPLY_TOP_N,
                                      f"检查 {config.TARGET_NAMES.get(target, target)} 有没有人做：{terms[0] if terms else ''}")
                res = supply.check(target, terms, card["_devs"], ctx, allow_network=net_ok,
                                   game_id=card["gid"])
                if res and res.pop("fresh", False):
                    checked += 1
                w, why = scoring.window(target, res)
                if target == "taptap" and not tap_ok:
                    w, why = config.TAPTAP_UNKNOWN_WINDOW, "TapTap：最近 6 小时没抓到 TapTap 数据，判断不了有没有人做"
                entry = {"target": target, "window": round(w, 3), "why": why,
                         "items": (res or {}).get("items", [])[:6]}
                if best is None or w > best["window"]:
                    best = entry
                card["markets"].setdefault(mkt, {"targets": []})["targets"].append(entry)
            if best:
                card["markets"][mkt].update({"hot": round(h, 3), "best": best["target"], "window": best["window"]})
                if best["window"] <= 0.3 and mkt == card["best_market"]:
                    card["flags"].append({"t": f"{config.TARGET_NAMES.get(best['target'], best['target'])} 搜到同名/近名游戏（未确认是同玩法）", "c": ""})
    for card in cards:
        card.pop("_devs", None)
        mh = card["market_hot"]
        tracked = {config.SOURCE_MARKET.get(s["source"], "global") for s in card["signals"]}
        for a in config.MARKETS:
            for b in config.MARKETS:
                # 只有在 B 市场也有数据、但 B 市场没热度时才算（没数据不等于没热度）
                if a != b and b in tracked and mh.get(a, 0) >= 0.4 and mh.get(b, 0) < 0.1:
                    card["flags"].append({"t": f"{config.MARKETS[a]}火、{config.MARKETS[b]}没热度", "c": "blue"})

    # v76：必看 = 全部游戏按分数排名，没有分数门槛、没有分栏
    must = sorted(cards, key=lambda c: heat.rank_key(c["heat"], "final"))
    dismissed, blocked, old = [], [], []
    by_raw = sorted(cards, key=lambda c: heat.rank_key(c["heat"], "raw"))
    seen = {c["gid"] for c in must[:400]}
    cards_out = must[:400] + [c for c in by_raw[:400] if c["gid"] not in seen]
    plat_diag = _plat_diag(groups, hs, elig, now_ts)
    gid_of = {m: c["gid"] for c in cards for m in c["members"]}
    star_out = [{**{k: v for k, v in s.items() if not k.startswith("_")}, "gid": gid_of.get(s["cat_key"])}
                for s in stars[:80]]
    opportunities, observe = must, []

    # 各平台涨幅榜
    movers = {}
    for (source, metric) in config.ANALYZE:
        if not config.source_on(source):
            continue
        lst = [s for s in signals if s["source"] == source and s["metric"] == metric
               and s["level_ok"] and s["z"] is not None]
        lst.sort(key=lambda s: -s["z"])
        movers[f"{source}/{metric}"] = [{
            "key": f"{s['source']}:{s['entity']}",
            "name": (entities.get((s["source"], s["entity"])) or {}).get("name", s["entity"]),
            "value": s["value"], "g24": s["g24"], "gshort": s["gshort"], "z": round(s["z"], 2),
            "cusum": round(s["cusum"], 2), "new": s["is_new"]} for s in lst[:15]]

    # 关键词
    kw = [s for s in signals if s["source"].endswith("_kw") and s["level_ok"]]
    kw.sort(key=lambda s: (-(s["hot"]), -(s["z"] or 0), -s["value"]))
    keywords = []
    for s in kw[:40]:
        ent = entities.get((s["source"], s["entity"])) or {}
        keywords.append({"key": f"{s['source']}:{s['entity']}", "name": ent.get("name", s["entity"]),
                         "source": s["source"], "rooms": s["value"], "z": s["z"], "hot": s["hot"],
                         "new": s["is_new"], "games": ent.get("extra", {}).get("games", {}),
                         "sample": ent.get("extra", {}).get("sample", "")})

    # Name similarity is not evidence of identity.
    candidates = []

    now = int(time.time())
    today = time.strftime("%Y-%m-%d")
    holidays = [{"market": m, "name": n} for s, e, m, n in config.HOLIDAYS if s <= today <= e]

    with db.session() as c:
        c.executemany("INSERT OR REPLACE INTO scores(ts, akey, name, final, hot, imit, win) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      [(now, x["akey"], x["name"], x["final"], x["hot"], None,
                        (x["markets"].get(x["best_market"]) or {}).get("window", 0)) for x in cards
                       if x["final"] is not None])

    if send_notify:
        notify.process(opportunities + observe)

    dash = {
        "updated": now, "seconds": round(time.time() - t0, 1),
        "sources": {k: v for k, v in (db.kv_get("source_status", {}) or {}).items() if config.source_on(k)},
        "index": index, "holidays": holidays,
        # 面板只放前面这些（太多页面会卡）；totals 是真实总数，页面上照实写
        "cards": cards_out, "plat_diag": plat_diag, "dismissed": dismissed[:200], "blocked": blocked[:200], "ai": ai_status,
        "stars": star_out, "old": old[:300],
        "totals": {"cards": len(elig), "dismissed": 0, "blocked": 0, "old": 0},
        "heat_meta": heat_meta,
        "sprouts": {s: [{k: r[k] for k in ("key", "id", "name", "url", "label", "kind", "streak", "daily", "total",
                                           "pos", "now", "then", "gap", "days", "series", "hot")}
                        for r in rows[:60]] for s, rows in sprout_rows.items()},
        "todo": [{"name": n, "why": w, "need": need} for n, w, need in config.PLATFORM_TODO
                 if not (n == "Twitch" and config.TWITCH_CLIENT_ID and config.TWITCH_CLIENT_SECRET)],
        "movers": movers, "keywords": keywords, "candidates": candidates[:40],
        "config": {"new_max_days": config.NEW_MAX_DAYS, "opportunity_min": config.OPPORTUNITY_MIN, "notify_min": config.NOTIFY_MIN_SCORE,
                   "markets": config.MARKETS, "targets": config.TARGET_NAMES,
                   "fast_minutes": config.FAST_MINUTES, "slow_minutes": config.SLOW_MINUTES},
    }
    db.kv_set("dashboard", dash)
    try:
        from . import charts
        charts.clear()            # 新数据进来后，图表重新算
    except Exception:
        pass
    db.log_run("analyze", time.time() - t0, True,
               f"必看 {len(elig)} 款")
    return dash
