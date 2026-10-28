"""面板图表用的数据：周度/月度大盘、每期前十、游戏明细、运行记录、大盘指数。

大盘口径（和常见的 TapTap 数据大盘一致）：
  - 每期的期初、期末各取一次每个游戏的累计热度（下载）
  - 存量游戏：期初、期末都有数 → 新增 = 期末 − 期初
  - 新游戏：期初没有、上线时间在这一期里 → 新增 = 期末
  - 其他不算。每期按新增排序取前 600 名求和，再除以这一期的天数 = 日均新增
  - 周度每周日 03:00 切一期，月度每月 1 日 03:00 切一期；第一期从开始有数据算起，最后一期到最新数据为止
"""
import json
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

from . import config, db

TOP_N = 600
SCOPES = {
    "maker": ("tapmaker", "maker", "TapTap 制造"),
    "workshop": ("tapmaker", "workshop", "创意工坊（不含 TapTap 制造）"),
    "taptap": ("taptap", None, "TapTap 新品榜"),
}

_cache = {}
_lock = threading.Lock()


def _cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (now, val)
    return val


def clear():
    with _lock:
        _cache.clear()


def _entities(source, kind):
    import json
    out = {}
    with db.session() as c:
        for e, name, extra in c.execute("SELECT entity, name, extra FROM entities WHERE source = ?", (source,)):
            try:
                x = json.loads(extra) if extra else {}
            except ValueError:
                x = {}
            if kind and x.get("kind") != kind:
                continue
            out[e] = {"name": name, "extra": x}
    return out


def _range(c, source, metric):
    lo, hi = c.execute("SELECT MIN(ts), MAX(ts) FROM metrics WHERE source = ? AND metric = ?",
                       (source, metric)).fetchone()
    h = c.execute("SELECT MIN(hour_ts) FROM metrics_hourly WHERE source = ? AND metric = ?",
                  (source, metric)).fetchone()[0]
    if h is not None and (lo is None or h < lo):
        lo = h
    return lo, hi


def _values_at(c, source, metric, t, tol):
    """每个游戏在 t 时刻（往前 tol 秒内）最近的一次数。"""
    out = {}
    for e, v, _ in c.execute(
            "SELECT entity, vmax, MAX(hour_ts) FROM metrics_hourly WHERE source = ? AND metric = ? "
            "AND hour_ts BETWEEN ? AND ? GROUP BY entity", (source, metric, t - tol, t)):
        out[e] = v
    for e, v, _ in c.execute(
            "SELECT entity, value, MAX(ts) FROM metrics WHERE source = ? AND metric = ? "
            "AND ts BETWEEN ? AND ? GROUP BY entity", (source, metric, t - tol, t)):
        out[e] = v
    return out


def _values_from(c, source, metric, t, span):
    """每个游戏在 t 之后 span 秒内第一次的数（第一期的期初用）。"""
    out = {}
    for e, v, _ in c.execute(
            "SELECT entity, value, MIN(ts) FROM metrics WHERE source = ? AND metric = ? "
            "AND ts BETWEEN ? AND ? GROUP BY entity", (source, metric, t, t + span)):
        out[e] = v
    return out


def _boundaries(start, end, period):
    d = datetime.fromtimestamp(start).replace(hour=3, minute=0, second=0, microsecond=0)
    out = []
    if period == "week":
        d += timedelta(days=(6 - d.weekday()) % 7)        # 周日
        if d.timestamp() <= start:
            d += timedelta(days=7)
        while d.timestamp() <= end:
            out.append(int(d.timestamp()))
            d += timedelta(days=7)
    else:
        d = d.replace(day=1)
        if d.timestamp() <= start:
            d = (d + timedelta(days=32)).replace(day=1)
        while d.timestamp() <= end:
            out.append(int(d.timestamp()))
            d = (d + timedelta(days=32)).replace(day=1)
    return out


# ---------------- 周度 / 月度大盘 ----------------
def board(scope, period):
    if scope not in SCOPES:
        scope = "maker"
    period = "month" if period == "month" else "week"
    return _cached(("board", scope, period), 600, lambda: _board(scope, period))


def _board(scope, period):
    source, kind, label = SCOPES[scope]
    res = {"scope": scope, "period": period, "label": label, "top_n": TOP_N, "periods": [], "range": None}
    ents = _entities(source, kind)
    if not ents:
        return res
    with db.session() as c:
        lo, hi = _range(c, source, "downloads")
        if not hi or hi - lo < 3000:
            return res
        bounds = _boundaries(lo, hi, period)
        pts = [lo] + [b for b in bounds if b > lo + 3 * 3600]
        if hi - pts[-1] > 3000:          # 第二次抓取后就出第一期（不完整），不用等满一周
            pts.append(hi)
        if len(pts) < 2:
            return res
        tol = (2 if period == "week" else 3) * 86400
        vals = []
        for i, t in enumerate(pts):
            v = _values_from(c, source, "downloads", t, 12 * 3600) if i == 0 else _values_at(c, source, "downloads", t, tol)
            vals.append({e: x for e, x in v.items() if e in ents})
    res["range"] = [lo, hi]
    bset = set(bounds)
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        va_all, vb_all = vals[i], vals[i + 1]
        rows = []
        for e, vb in vb_all.items():
            if not vb:
                continue
            va = va_all.get(e)
            if va:
                rows.append((max(vb - va, 0), e, "存量", va, vb))
            else:
                rel = ents[e]["extra"].get("released_time") or 0
                if rel and a < rel <= b:
                    rows.append((vb, e, "新增", 0, vb))
        rows.sort(key=lambda r: -r[0])
        top = rows[:TOP_N]
        days = (b - a) / 86400
        sold = sum(r[0] for r in top if r[2] == "存量")
        snew = sum(r[0] for r in top if r[2] == "新增")
        res["periods"].append({
            "s": a, "e": b, "days": round(days, 5), "all": len(rows),
            "old": sum(1 for r in top if r[2] == "存量"), "new": sum(1 for r in top if r[2] == "新增"),
            "sold": sold, "snew": snew, "s600": sold + snew,
            "daily": (sold + snew) / days if days > 0 else 0,
            "partial": (i == 0 and a not in bset) or (i == len(pts) - 2 and b not in bset),
            "top10": [{"key": f"{source}:{e}", "id": e, "name": ents[e]["name"], "type": t,
                       "release": ents[e]["extra"].get("released_time") or 0,
                       "start": va, "end": vb, "new": n, "daily": n / days if days > 0 else 0}
                      for n, e, t, va, vb in rows[:10]],
        })
    return res


# ---------------- 全部游戏明细 ----------------
def games(scope):
    if scope not in SCOPES:
        scope = "maker"
    return _cached(("games", scope), 300, lambda: _games(scope))


def _score(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _games(scope):
    source, kind, label = SCOPES[scope]
    ents = _entities(source, kind)
    now = int(time.time())
    series = defaultdict(dict)
    with db.session() as c:
        for m in ("downloads", "fans", "reviews"):
            for e, ts, v in c.execute(
                    "SELECT entity, ts, value FROM metrics WHERE source = ? AND metric = ? AND ts >= ? ORDER BY ts",
                    (source, m, now - 56 * 3600)):
                if e in ents:
                    series[m].setdefault(e, []).append((ts, v))
    rows = []
    for e, pts in series["downloads"].items():
        ts_c, v_c = pts[-1]
        # 和大约 24 小时前比；刚开始运行不满 24 小时的，和能找到的最早一次比，并注明间隔
        cands = [(ts, v) for ts, v in pts if ts <= ts_c - 3000]
        prev = min(cands, key=lambda p: abs(p[0] - (ts_c - 86400))) if cands else None
        x = ents[e]["extra"]
        new24 = v_c - prev[1] if prev else None

        def last(m):
            p = series[m].get(e)
            return p[-1][1] if p else None

        rows.append({
            "key": f"{source}:{e}", "id": e, "name": ents[e]["name"], "downloads": v_c, "ts": ts_c,
            "new24": round(new24) if new24 is not None else None, "prev": prev[1] if prev else None,
            "span": round((ts_c - prev[0]) / 3600, 1) if prev else None,
            "fans": last("fans"), "reviews": last("reviews"), "score": _score(x.get("score")),
            "release": x.get("released_time") or 0,
            "tags": [t for t in (x.get("tags") or []) if t not in ("TapTap制造", "创意工坊")][:5],
        })
    return {"scope": scope, "label": label, "updated": max((r["ts"] for r in rows), default=None), "rows": rows}


# ---------------- 运行记录 ----------------
def run_stats(days=7):
    days = max(1, min(30, int(days)))
    return _cached(("stats", days), 300, lambda: _run_stats(days))


def _run_stats(days):
    now = int(time.time())
    off = -time.timezone          # 按本地日期分天
    runs = []
    with db.session() as c:
        for ts, job, sec, ok, detail in c.execute(
                "SELECT ts, job, seconds, ok, detail FROM runs WHERE ts >= ? ORDER BY ts", (now - days * 86400,)):
            if not (job.startswith("collect:") or job == "analyze"):
                continue
            m = re.search(r"(\d+) 条", detail or "")
            runs.append({"ts": ts, "job": job, "seconds": round(sec or 0, 1), "ok": bool(ok),
                         "count": int(m.group(1)) if (m and ok) else None})
        sources = [r[0] for r in c.execute("SELECT DISTINCT source FROM entities")]
        volume = {}
        for s in sources:
            for d, n in c.execute(
                    "SELECT ((ts + ?) / 86400) * 86400 - ? AS d, COUNT(*) FROM metrics "
                    "WHERE source = ? AND ts >= ? GROUP BY d", (off, off, s, now - 14 * 86400)):
                volume.setdefault(s, {})[int(d)] = n
        entities = {s: n for s, n in c.execute("SELECT source, COUNT(*) FROM entities GROUP BY source")}
        total = c.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
        hourly = c.execute("SELECT COUNT(*) FROM metrics_hourly").fetchone()[0]
    try:
        size = os.path.getsize(config.DB_PATH)
    except OSError:
        size = None
    return {"runs": runs, "volume": volume, "entities": entities, "rows": total,
            "rows_hourly": hourly, "db_size": size}


# ---------------- 大盘指数 ----------------
def index_series(days=7):
    days = max(1, min(90, int(days)))
    return _cached(("index", days), 300, lambda: _index_series(days))


def _index_series(days):
    since = int(time.time()) - days * 86400
    out = {}
    with db.session() as c:
        for (s, m) in config.ANALYZE:
            if not config.source_on(s):
                continue
            pts = c.execute("SELECT ts, value FROM metrics WHERE source = ? AND metric = ? AND entity = '__index__' "
                            "AND ts >= ? ORDER BY ts", (s, m + "_g24", since)).fetchall()
            if pts:
                out[f"{s}/{m}"] = [[t, v] for t, v in pts]
    return out


# ---------------- 数据核对：某个数据源最近两次抓到的原始数字 ----------------
def raw(source):
    """Latest two observations per game AND metric, never a platform-wide timestamp."""
    now = int(time.time())
    with db.session() as c:
        sources = [r[0] for r in c.execute("SELECT DISTINCT source FROM entities ORDER BY source")
                   if config.source_on(r[0])]
        entities = {}
        for e, n, fs, ls, x in c.execute(
                "SELECT entity,name,first_seen,last_seen,extra FROM entities WHERE source=?",
                (source,)):
            if e == "__index__":
                continue
            try:
                extra = json.loads(x or "{}")
            except ValueError:
                extra = {}
            entities[e] = {"name": n, "extra": extra, "first_seen": fs, "last_seen": ls}
        cur, prev = defaultdict(dict), defaultdict(dict)
        sampled, previous_at = defaultdict(dict), defaultdict(dict)
        for e, m, v, ts, rn in c.execute(
                """SELECT entity,metric,value,ts,rn FROM (
                SELECT entity,metric,value,ts,
                ROW_NUMBER() OVER(PARTITION BY entity,metric ORDER BY ts DESC) rn
                FROM metrics WHERE source=? AND entity!='__index__'
                AND metric NOT LIKE '%_g24') WHERE rn<=2""", (source,)):
            if e not in entities:
                continue
            if rn == 1:
                cur[e][m], sampled[e][m] = v, ts
            else:
                prev[e][m], previous_at[e][m] = v, ts
        fetches = c.execute("SELECT COUNT(DISTINCT ts) FROM metrics WHERE source = ? AND entity != '__index__'",
                            (source,)).fetchone()[0]
    st = (db.kv_get("source_status", {}) or {}).get(source, {})
    rows = []
    for e, ent in entities.items():
        x = ent["extra"]
        ts = max(sampled[e].values(), default=None)
        rows.append({"key": f"{source}:{e}", "id": e, "name": ent["name"],
                     "cur": cur[e], "prev": prev[e], "sampled": sampled[e],
                     "unverified_zero": [m for m, v in cur[e].items()
                         if source == "steam" and m == "players" and v == 0
                         and x.get("players_verified_at") != sampled[e].get(m)],
                     "previous_at": previous_at[e], "ts": ts,
                     "stale": bool(ts and now - ts > 36 * 3600),
                     "state": "sampled" if ts else ("error" if x.get("fetch_error") else "pending"),
                     "error": x.get("fetch_error") or "",
                     "first_seen": ent["first_seen"], "last_seen": ent["last_seen"],
                     "release": x.get("release_ts") or x.get("released_time") or x.get("created_ts") or 0,
                     "url": x.get("url") or x.get("source_url") or "", "extra": x})
    return {"source": source, "sources": sources,
            "ts": max((r["ts"] for r in rows if r["ts"]), default=None),
            "prev_ts": max((t for times in previous_at.values() for t in times.values()), default=None),
            "fetches": fetches, "entities": len(entities),
            "metrics": sorted({m for d in cur.values() for m in d}), "rows": rows, "status": st,
            "sampled_count": sum(bool(r["cur"]) for r in rows),
            "pending_count": sum(not r["cur"] for r in rows)}


_GROWTH_METRICS = {"players", "downloads", "fans", "reviews", "store_reviews",
                   "reserves", "rooms", "viewers", "visits", "views", "videos"}


def _new_reason(row, source, now):
    x = row["extra"]
    if x.get("gone") or x.get("coming_soon") or x.get("preorder"):
        return None
    if source == "steam" and x.get("type", "game") != "game":
        return None
    release = row["release"]
    if release:
        return "近15天上线" if 0 <= now - release <= config.RISING_DAYS * 86400 else None
    # A new-release board is explicit evidence, not an inferred release date.
    if "rank_new" in row["cur"] or x.get("new_candidate") or any(
            any(word in label for word in ("新游榜", "新品榜", "热门新品", "近期发行"))
            for label in x.get("lists", [])):
        return "新游榜 · 发行日期待核实"
    if source in ("roblox", "fortnite", "itch", "poki", "crazygames",
                  "huya", "douyu", "bili", "twitch", "kick", "mistore",
                  "wxmini", "appcn", "appus", "tapmaker"):
        if row["first_seen"] and 0 <= now - row["first_seen"] <= config.RISING_DAYS * 86400:
            return "近15天首次发现 · 发行日期待核实"
    return None


def rising(source):
    """Rank observed growth in recent games; keep new games awaiting a baseline visible."""
    return _cached(("rising", source), 30, lambda: _rising(source))


def _rising(source):
    from . import heat, match
    now = int(time.time())
    data = raw(source)
    baselines = {}
    with db.session() as c:
        for e, m, v, ts in c.execute("""
            WITH latest AS (
                SELECT entity,metric,MAX(ts) ts FROM metrics
                WHERE source=? AND entity!='__index__' GROUP BY entity,metric
            ), ranked AS (
                SELECT p.entity,p.metric,p.value,p.ts,
                    ROW_NUMBER() OVER(PARTITION BY p.entity,p.metric
                        ORDER BY ABS(l.ts-p.ts-86400),p.ts DESC) rn
                FROM metrics p JOIN latest l ON p.entity=l.entity AND p.metric=l.metric
                WHERE p.source=? AND p.ts BETWEEN l.ts-259200 AND l.ts-3600
            ) SELECT entity,metric,value,ts FROM ranked WHERE rn=1
            """, (source, source)):
            baselines[(e, m)] = (v, ts)
    big_words = [match.normalize(n) for n in config.KW_BIG_GAMES if match.normalize(n)]
    candidates, excluded = [], {"old": 0, "big": 0}
    no_growth_count = 0
    for row in data["rows"]:
        reason = _new_reason(row, source, now)
        if not reason:
            excluded["old"] += 1
            continue
        ent = {"name": row["name"], "extra": row["extra"]}
        if heat.big_reason([row["key"]], {(source, row["id"]): ent},
                           [match.normalize(row["name"])], big_words):
            excluded["big"] += 1
            continue
        options = []
        uncertain_baseline = False
        x = row["extra"]
        for metric, current in row["cur"].items():
            rank = metric.startswith("rank_")
            if metric not in _GROWTH_METRICS and not rank:
                continue
            baseline = baselines.get((row["id"], metric))
            previous = baseline[0] if baseline else None
            current_ts = row["sampled"].get(metric)
            previous_ts = baseline[1] if baseline else None
            if not current_ts or now - current_ts > 36 * 3600 or metric == "rank_expect":
                continue
            evidence = "observed"
            # Rank changes published by the platform have their own provenance.
            mark = (x.get("marks") or {}).get(metric)
            if previous is None and rank and isinstance(mark, (int, float)) and mark > 0:
                previous, evidence = current + mark, "platform_rank_change"
            if previous is None:
                continue
            if source == "steam" and metric == "players" and previous == 0 and (
                    previous_ts not in x.get("players_zero_verified", [])):
                uncertain_baseline = True
                continue
            hours = (current_ts - previous_ts) / 3600 if current_ts and previous_ts else None
            if evidence == "observed" and (hours is None or hours < 1 or hours > 72):
                continue
            delta = previous - current if rank else current - previous
            if delta <= 0 or (not rank and current < 0):
                continue
            fraction = delta / previous if previous > 0 else None
            options.append({"metric": metric, "value": current, "previous": previous,
                            "delta": delta, "growth": fraction, "hours": hours,
                            "sampled": current_ts, "previous_ts": previous_ts,
                            "kind": "rank" if rank else "level", "evidence": evidence,
                            "state": "growth" if fraction is not None else "zero_baseline"})
        if options:
            best = max(options, key=lambda r: (r["growth"] is not None,
                                               r["growth"] or 0, r["delta"]))
        else:
            metrics = [m for m in row["cur"] if m in _GROWTH_METRICS or m.startswith("rank_")]
            if not uncertain_baseline and any((row["id"], m) in baselines and
                    now - row["sampled"].get(m, 0) <= 36 * 3600 for m in metrics):
                no_growth_count += 1
                continue
            metric = next((m for m in ("players", "downloads", "rank_new") if m in metrics),
                          metrics[0] if metrics else None)
            best = {"metric": metric, "value": row["cur"].get(metric), "previous": None,
                    "delta": None, "growth": None, "hours": None, "sampled": row["sampled"].get(metric),
                    "kind": "rank" if metric and metric.startswith("rank_") else "level",
                    "state": "baseline_pending" if metrics else "sample_pending"}
        candidates.append({**best, "key": row["key"], "id": row["id"], "name": row["name"],
                           "release": row["release"], "why": reason, "url": row["url"],
                           "stale": bool(best.get("sampled") and now - best["sampled"] > 36 * 3600),
                           "error": row["error"]})
    order = {"growth": 0, "zero_baseline": 1, "baseline_pending": 2,
             "sample_pending": 3, "no_positive_growth": 4}
    candidates.sort(key=lambda r: (order[r["state"]], -(r["growth"] or 0),
                                    -(r["delta"] or 0), r["name"]))
    return {"source": source, "days": config.RISING_DAYS, "rows": candidates,
            "measured": sum(r["state"] in ("growth", "zero_baseline") for r in candidates),
            "total": len(data["rows"]), "excluded": excluded,
            "no_growth_count": no_growth_count, "updated": now}


# ---------------- 新冒头：最近才出现、或刚上线不久的 ----------------
FRESH_METRIC = {"tapmaker": "downloads", "taptap": "downloads", "steam": "players", "roblox": "players",
                "huya": "rooms", "douyu": "rooms", "huya_kw": "rooms", "douyu_kw": "rooms",
                "bili": "rooms", "twitch": "rooms", "kick": "viewers", "fortnite": "players",
                "haoyou": "rank_*", "mistore": "rank_game", "appcn": "rank_*", "appus": "rank_*",
                "itch": "rank_*", "poki": "rank_popular", "crazygames": "rank_hot",
                "wxmini": "rank_*", "dymini": "rank_*"}
_LIVE_FRESH = ("huya", "douyu", "bili", "twitch", "kick")


def fresh():
    return _cached(("fresh",), 300, _fresh)


def _fresh_reason(src, fs, x, first_run, now):
    day = 86400
    later = fs - first_run > day          # 程序开始运行一天以后才第一次出现
    if src == "steam":
        if x.get("type") and x["type"] != "game":
            return None
        if x.get("coming_soon"):
            return "即将推出"
        rel = x.get("release_ts")
        if rel and now - rel < 120 * day:
            return f"上线 {max(0, (now - rel) / day):.0f} 天"
        return "最近才上榜" if later else None
    if src in ("taptap", "tapmaker"):
        if x.get("gone"):
            return None
        if x.get("preorder"):
            return "预约中" if src == "taptap" else None
        rel = x.get("released_time") or 0
        lim = 30 if src == "tapmaker" else 60
        if rel and now - rel < lim * day:
            return f"上线 {max(0, (now - rel) / day):.0f} 天"
        return None
    if src == "dymini" and any("新游榜" in t for t in (x.get("lists") or [])):
        return "抖音小游戏新游榜"
    if src in ("roblox", "fortnite", "itch", "poki", "crazygames", "mistore", "wxmini", "dymini"):
        return f"{(now - fs) / day:.0f} 天前第一次上榜" if later else None
    if src in ("haoyou", "appcn", "appus"):
        if x.get("preorder"):
            return "预约中"
        rel = x.get("release_ts")
        if rel and now - rel < 60 * day:
            return f"上线 {max(0, (now - rel) / day):.0f} 天"
        return f"{(now - fs) / day:.0f} 天前第一次上榜" if later else None
    if src in _LIVE_FRESH:
        if x.get("generic") or later is False:
            return None
        return "新出现的分区"
    if src.endswith("_kw"):
        return "新词" if later else "标题词"
    return None


def _fresh():
    import json
    now = int(time.time())
    first_run0 = db.kv_get("first_run") or now
    src_first = db.source_first_runs()
    out = {}
    with db.session() as c:
        for src, m in FRESH_METRIC.items():
            if not config.source_on(src):
                continue
            first_run = src_first.get(src, first_run0)
            is_rank = m.startswith("rank_")
            ents = {}
            for e, name, fs, extra in c.execute("SELECT entity, name, first_seen, extra FROM entities WHERE source = ?",
                                                (src,)):
                try:
                    x = json.loads(extra) if extra else {}
                except ValueError:
                    x = {}
                why = _fresh_reason(src, fs or 0, x, first_run, now)
                if why:
                    ents[e] = (name, why, x.get("url") or "")
            if not ents:
                continue
            pts = defaultdict(list)
            if m == "rank_*":          # 在好几个榜上：每次取最靠前的名次
                for e, ts, v in c.execute("SELECT entity, ts, MIN(value) FROM metrics WHERE source = ? "
                                          "AND metric LIKE 'rank_%' AND ts >= ? GROUP BY entity, ts ORDER BY ts",
                                          (src, now - 30 * 3600)):
                    if e in ents:
                        pts[e].append((ts, v))
            else:
                for e, ts, v in c.execute("SELECT entity, ts, value FROM metrics WHERE source = ? AND metric = ? "
                                          "AND ts >= ? ORDER BY ts", (src, m, now - 30 * 3600)):
                    if e in ents:
                        pts[e].append((ts, v))
            rows = []
            if src == "steam":
                for e, (name, why, url) in ents.items():
                    if e not in pts:
                        rows.append({"key": f"steam:{e}", "id": e, "name": name,
                                     "why": why + " · 等待实时采样", "metric": m,
                                     "value": None, "delta": None, "span": None, "url": url})
            for e, p in pts.items():
                ts_c, v_c = p[-1]
                if now - ts_c > 6 * 3600:
                    continue                  # 最近 6 小时没再抓到，已经掉榜
                cands = [q for q in p if q[0] <= ts_c - 3000]
                prev = min(cands, key=lambda q: abs(q[0] - (ts_c - 86400))) if cands else None
                name, why, url = ents[e]
                d = None
                if prev:
                    d = (prev[1] - v_c) if is_rank else (v_c - prev[1])     # 名次：往前了几名算正
                rows.append({"key": f"{src}:{e}", "id": e, "name": name, "why": why,
                             "metric": "rank" if is_rank else m, "value": v_c, "delta": d, "url": url,
                             "span": round((ts_c - prev[0]) / 3600, 1) if prev else None})
            if is_rank:
                rows.sort(key=lambda r: (r["value"] or 9999))
            else:
                rows.sort(key=lambda r: (r["delta"] is None, -(r["delta"] or 0), -(r["value"] or 0)))
            if rows:
                out[src] = rows[:150]
    return out
