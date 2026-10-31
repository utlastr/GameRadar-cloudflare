"""抓取数据并存进数据库。每个平台单独抓，一个坏了不影响别的。"""
import gzip
import json
import os
import random
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import activity, config, db, match, revisit
from .sources import (appstore_rank, bili, crazygames, douyu, fortnite, gravity, haoyou, huya, itch, kick, mistore,
                      poki, roblox, steam, taptap, twitch)

_status_lock = threading.RLock()


def _align(ts, minutes):
    return int(ts // (minutes * 60) * minutes * 60)


def _archive(source, ts, items):
    """精简原始数据按天压缩存档（以后想算新指标可以从这里重新算）。"""
    day = time.strftime("%Y-%m-%d", time.gmtime(ts))
    folder = os.path.join(config.RAW_DIR, day)
    os.makedirs(folder, exist_ok=True)
    rec = {"ts": ts, "items": [{"e": it["entity"], "n": it.get("name"), "m": it.get("metrics")}
                               for it in items]}
    with gzip.open(os.path.join(folder, f"{source}.jsonl.gz"), "at", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _save(source, ts, items):
    with db.session() as c:
        db.upsert_entities(c, source, ts, items)
        rows = [(it["entity"], m, v) for it in items
                for m, v in (it.get("metrics") or {}).items() if v is not None]
        db.put_metrics(c, ts, source, rows)
    try:
        _archive(source, ts, items)
    except Exception:
        pass


def _status(source, ok, count=0, err="", warn=""):
    with _status_lock:
        _write_status(source, ok, count, err, warn)


def _write_status(source, ok, count=0, err="", warn=""):
    st = db.kv_get("source_status", {}) or {}
    s = st.get(source, {})
    now = int(time.time())
    if ok:
        s.update({"ok_ts": now, "count": count, "error": "", "warn": warn[:300]})
    else:
        s.update({"err_ts": now, "error": err[:300]})
    st[source] = s
    db.kv_set("source_status", st)


NAMES = {"steam": "Steam", "roblox": "Roblox", "taptap": "TapTap", "tapmaker": "TapTap制造/创意工坊",
         "huya": "虎牙", "douyu": "斗鱼", "haoyou": "好游快爆", "mistore": "小米商店", "appcn": "App Store 中国游戏榜",
         "appus": "App Store 美国游戏榜", "itch": "itch.io", "poki": "Poki", "crazygames": "CrazyGames",
         "fortnite": "Fortnite 创意地图", "kick": "Kick", "twitch": "Twitch", "bili": "B站直播", "bvideo": "B站视频",
         "wxmini": "微信小游戏", "dymini": "抖音小游戏"}

# 抓得不频繁的榜单：多久抓一次（小时）。没写的按 SLOW_MINUTES（每小时）抓；失败的下一轮就重试
SLOW_EVERY_HOURS = {"haoyou": 2, "mistore": 3, "appcn": 2, "appus": 2, "itch": 2, "poki": 2, "crazygames": 1,
                    "fortnite": 1, "bvideo": 2, "wxmini": 3, "dymini": 3}


def _run(source, fn, ts):
    if not config.SOURCES_ENABLED.get(source, True):
        return
    name = NAMES.get(source, source)
    activity.begin(f"抓取 {name}")
    activity.say(f"开始抓取 {name}")
    t0 = time.time()
    try:
        n = fn(ts)
        warn = ""
        if isinstance(n, tuple):          # (条数, 部分失败的说明)
            n, warn = n
        _status(source, True, n, warn=warn)
        db.log_run("collect:" + source, time.time() - t0, True, f"{n} 条" + (f"；部分失败：{warn}" if warn else ""))
        activity.say(f"{name} 完成：{n} 条，用时 {time.time() - t0:.0f} 秒" + (f"（部分失败：{warn}）" if warn else ""))
    except Exception as e:
        _status(source, False, err=str(e))
        # 错误原因放最前面，后面才是详细的出错位置（面板上只显示前几百个字）
        db.log_run("collect:" + source, time.time() - t0, False, f"{e}\n\n{traceback.format_exc()}")
        activity.say(f"{name} 失败：{e}")
    finally:
        activity.end()


# ---------------- 直播 ----------------
def _live(source, rooms, ts):
    """v20 起看两样：每个游戏分区有几个主播在播（新开的分区最重要）；大主播现在在播什么。

    不再从标题括号里拆词：括号里大多是主播标签、比赛名，噪音太大。
    """
    rooms = [r for r in rooms if r["game"] and r["game"] not in config.NON_GAME_CATEGORIES]
    stars = _update_stars(source, rooms, ts)
    games = {}
    for r in rooms:
        g = r["game"]
        e = games.setdefault(g, {"entity": g, "name": g, "metrics": {"heat": 0, "rooms": 0},
                                 "_streamers": set(),
                                 "extra": {"top_title": "", "top_heat": -1, "top_nick": "", "stars": [],
                                           "generic": g in config.GENERIC_CATEGORIES}})
        e["metrics"]["heat"] += r["heat"]
        e["_streamers"].add(r["streamer"])
        if r["heat"] > e["extra"]["top_heat"]:
            e["extra"].update(top_heat=r["heat"], top_title=r["title"][:60], top_nick=r.get("nick") or "")
        if r["streamer"] in stars and len(e["extra"]["stars"]) < 5:
            e["extra"]["stars"].append(r.get("nick") or r["streamer"])
    out_games = []
    for e in games.values():
        e["metrics"]["rooms"] = len(e.pop("_streamers"))
        out_games.append(e)
    _save(source, ts, out_games)
    return len(out_games)


def _update_stars(source, rooms, ts):
    """大主播名单：最近 30 天里任何一次进过本平台游戏区热度前 STAR_TOP 的主播。

    名单记住 30 天，所以大主播换去播小游戏、热度掉下来，也还认得他。
    同时记下每个大主播现在在播什么、从什么时候开始播这个分区。
    """
    key = "stars:" + source
    known = db.kv_get(key, {}) or {}
    top = sorted(rooms, key=lambda r: -r["heat"])[:config.STAR_TOP]
    for r in top:
        k = known.get(r["streamer"]) or {}
        known[r["streamer"]] = {"nick": r.get("nick") or "", "best": max(k.get("best") or 0, r["heat"]), "ts": ts}
    cutoff = ts - config.STAR_KEEP_DAYS * 86400
    known = {u: v for u, v in known.items() if (v.get("ts") or 0) >= cutoff}
    db.kv_set(key, known)

    prev = {x["uid"]: x for x in (db.kv_get("star_live:" + source, []) or [])}
    live = []
    for r in rooms:
        if r["streamer"] not in known:
            continue
        p = prev.get(r["streamer"])
        since = p["since"] if p and p.get("game") == r["game"] else ts
        live.append({"uid": r["streamer"], "nick": r.get("nick") or known[r["streamer"]]["nick"],
                     "game": r["game"], "title": r["title"][:80], "heat": r["heat"],
                     "best": known[r["streamer"]]["best"], "since": since,
                     "generic": r["game"] in config.GENERIC_CATEGORIES})
    db.kv_set("star_live:" + source, live)
    return set(known)


def _huya(ts):
    rooms = huya.live_rooms(config.HUYA_MAX_PAGES,
                           progress=lambda d, n: activity.progress(d, n, f"直播间列表 第 {d} / {n} 页"))
    return _live("huya", rooms, ts)


def _douyu(ts):
    rooms = douyu.live_rooms(config.DOUYU_MAX_PAGES,
                            progress=lambda d, n: activity.progress(d, n, f"直播间列表 第 {d} / {n} 页"))
    return _live("douyu", rooms, ts)


def _bili(ts):
    rooms = bili.live_rooms(config.BILI_MAX_PAGES,
                            progress=lambda d, n: activity.progress(d, n, f"直播间列表 第 {d} / {n} 页"))
    return _live("bili", rooms, ts)


def _twitch(ts):
    rooms = twitch.live_rooms(config.TWITCH_CLIENT_ID, config.TWITCH_CLIENT_SECRET, config.TWITCH_MAX_PAGES,
                              progress=lambda d, n: activity.progress(d, n, f"直播列表 第 {d} / {n} 页"))
    return _live("twitch", rooms, ts)


def _kick(ts):
    items = kick.categories(progress=lambda d, n: activity.progress(d, n, f"分类列表 第 {d} / {n} 页"))
    items = [it for it in items if it["name"] not in config.NON_GAME_CATEGORIES]
    for it in items:
        it["extra"]["generic"] = it["name"] in config.GENERIC_CATEGORIES
    _save("kick", ts, items)
    return len(items)


# ---------------- 只有名次的榜单 ----------------
def _board(source, items, errors, ts):
    if not items:
        raise RuntimeError("一个都没拿到：" + "；".join(errors[:3]) if errors else "榜单是空的")
    _save(source, ts, items)
    return len(items), "；".join(errors)[:300]


def _haoyou(ts):
    items, errors = haoyou.boards(progress=lambda d, n: activity.progress(d, n, f"榜单 {d} / {n}"))
    for it in items:
        x = it["extra"]
        if "rank_expect" in it["metrics"] and len(it["metrics"]) == 1:
            x["preorder"] = True                    # 只在预约榜上 = 还没上线
        else:
            rel = haoyou.parse_release(x.get("text"), ts)
            if rel:
                x["release_ts"] = rel
    # 以前解析到的上线时间留着（好游快爆只在上线前后几周写"X天前上线"，之后改成显示下载量）
    known = _known("haoyou")
    for it in items:
        old = (known.get(it["entity"]) or (None, 0, {}))[2]
        if not it["extra"].get("release_ts") and old.get("release_ts"):
            it["extra"]["release_ts"] = old["release_ts"]
    return _board("haoyou", items, errors, ts)


def _mistore(ts):
    return _board("mistore", mistore.ranking(), [], ts)


def _appcn(ts):
    items, errors = appstore_rank.charts("cn", "中国")
    return _board("appcn", items, errors, ts)


def _appus(ts):
    items, errors = appstore_rank.charts("us", "美国")
    return _board("appus", items, errors, ts)


def _itch(ts):
    items, errors = itch.boards()
    return _board("itch", items, errors, ts)


def _wxmini(ts):
    items, errors = gravity.boards("wxmini", progress=lambda d, n: activity.progress(d, n, f"榜单 第 {d} / {n} 页"))
    return _board("wxmini", items, errors, ts)


def _dymini(ts):
    items, errors = gravity.boards("dymini", progress=lambda d, n: activity.progress(d, n, f"榜单 第 {d} / {n} 页"))
    return _board("dymini", items, errors, ts)


def _poki(ts):
    return _board("poki", poki.popular(), [], ts)


def _crazygames(ts):
    return _board("crazygames", crazygames.hot(), [], ts)


def _fortnite(ts):
    items, errors = fortnite.islands(progress=lambda d, n, t="": activity.progress(d, n, t))
    return _board("fortnite", items, errors, ts)


def _bvideo(ts):
    # Manual/sample runs must use the rolling latest-post collector. The old
    # video.collect() path is a historical ranking sweep with a 30-minute wait.
    from . import bili_latest
    result = bili_latest.poll_once(force=True)
    if result.get("status") in ("error", "paused", "gap", "stale"):
        raise RuntimeError(result.get("error") or "B站最新投稿采集异常")
    return result.get("new", 0), (
        "实时列表出现断档" if result.get("status") == "gap" else "")


# ---------------- Steam ----------------
def _steam(ts):
    errors = []
    try:
        scanned = steam.scan_recent_releases()
        activity.say(f"Steam 近 30 天发行目录扫描完成：新增/更新 {scanned} 条")
    except Exception as e:
        errors.append("近期发行扫描：" + str(e))
    steam.seed_recent_releases()
    ids, ranks, lists = [], {}, {}

    def add(appid, list_name):
        if appid not in ids:
            ids.append(appid)
        lists.setdefault(appid, [])
        if list_name not in lists[appid]:
            lists[appid].append(list_name)

    activity.progress(0, 0, "读取榜单")
    try:
        for r in steam.most_played():
            ranks[r["appid"]] = r
            add(r["appid"], "在线人数前 200")
    except Exception as e:
        errors.append(str(e))
    known = {}
    with db.session() as c:
        for r in c.execute("SELECT entity, extra FROM entities WHERE source = 'steam'"):
            try:
                known[r[0]] = json.loads(r[1]) if r[1] else {}
            except ValueError:
                known[r[0]] = {}
    recent = steam.discovery_candidates(known, ts, new_only=False)
    recent_ids = {str(x["appid"]) for x in recent}
    for app in recent:
        key = str(app["appid"])
        extra = known.get(key) or {"name": app["name"], "type": "game"}
        if app.get("released") and (extra.get("coming_soon") or not extra.get("release_ts")):
            extra.update(release_ts=app["released"], coming_soon=False)
        extra["review_seed"] = app.get("review_seed")
        known[key] = extra

    # Rotate the eligible new-release pool by oldest real metric sample.
    with db.session() as c:
        new_batch = [r[0] for r in c.execute(
            """SELECT e.entity FROM entities e
               JOIN hist_steam h ON h.appid=CAST(e.entity AS INTEGER)
               LEFT JOIN metrics m ON m.source='steam' AND m.entity=e.entity
               WHERE e.source='steam' AND h.released BETWEEN ? AND ? AND h.reviews>=?
               GROUP BY e.entity
               ORDER BY MAX(m.ts) IS NOT NULL, MAX(m.ts), h.reviews DESC
               LIMIT ?""",
            (ts - config.STEAM_DISCOVERY_DAYS * 86400, ts, config.STEAM_REVIEW_MIN,
             config.STEAM_DISCOVERY_METRICS_PER_RUN))]

    hot_ids = list(ranks)
    if len(hot_ids) < config.STEAM_HOT_POOL:
        try:
            for appid in steam.search_list("topsellers", config.STEAM_HOT_POOL):
                if appid not in hot_ids:
                    hot_ids.append(appid)
                if len(hot_ids) >= config.STEAM_HOT_POOL:
                    break
        except Exception as e:
            errors.append(str(e))
    if len(hot_ids) < config.STEAM_HOT_POOL:
        with db.session() as c:
            prior = [int(r[0]) for r in c.execute(
                """SELECT m.entity FROM metrics m
                   JOIN (SELECT entity, MAX(ts) ts FROM metrics
                         WHERE source='steam' AND metric='players' GROUP BY entity) z
                     ON z.entity=m.entity AND z.ts=m.ts
                   WHERE m.source='steam' AND m.metric='players'
                   ORDER BY m.value DESC LIMIT ?""",
                (config.STEAM_HOT_POOL * 2,))]
        for appid in prior:
            if appid not in hot_ids:
                hot_ids.append(appid)
            if len(hot_ids) >= config.STEAM_HOT_POOL:
                break
    hot_ids = hot_ids[:config.STEAM_HOT_POOL]
    for appid in hot_ids:
        add(appid, "在线人数热门池")
    for entity in new_batch:
        appid = int(entity)
        add(appid, "近 30 天且评论至少 20")
    if not ids:
        raise RuntimeError("Steam 在线热门池和近 30 天达标新游池都没有可追踪游戏：" + "; ".join(errors))

    # Store the complete eligible catalog, but only sample a bounded rotating batch each run.
    items, fetched = [], 0
    top20 = {int(appid) for appid, r in ranks.items() if (r.get("rank") or 9999) <= config.STEAM_NEW_TOP}
    top20_seen = set(db.kv_get("steam_top20_seen", []) or [])
    if not top20_seen:
        with db.session() as c:
            top20_seen = {int(r[0]) for r in c.execute(
                """SELECT DISTINCT entity FROM metrics WHERE source='steam'
                   AND metric='rank_played' AND value<=?""",
                (config.STEAM_NEW_TOP,))}
    first_top20 = top20 - top20_seen
    # New releases need their details before already-known chart entries.
    ids.sort(key=lambda appid: (bool((known.get(str(appid)) or {}).get("fetched_at")),
                               -((known.get(str(appid)) or {}).get("release_ts") or 0)))
    detail_jobs, player_jobs, item_by_id = {}, {}, {}
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="steam-players") as pool:
        for i, appid in enumerate(ids, 1):
            activity.progress(i, len(ids), f"提交 Steam 采样 {i} / {len(ids)}")
            key = str(appid)
            extra = dict(known.get(key) or {})
            stale = ts - (extra.get("fetched_at") or 0) > 7 * 86400
            if stale and fetched < config.MAX_STEAM_DETAILS_PER_RUN:
                fetched += 1
                detail_jobs[pool.submit(steam.detail, appid, retries=0, timeout=10)] = key
            if extra.get("type") and extra["type"] != "game":
                continue
            metrics = {}
            r = ranks.get(appid)
            if r:
                metrics["rank_played"] = r.get("rank")
                if r.get("peak"):
                    metrics["peak_daily"] = r["peak"]
            if not extra.get("coming_soon"):
                player_jobs[pool.submit(
                    steam.current_players, appid, retries=0, timeout=8)] = appid
            if metrics:
                extra.pop("metrics_pending", None)
            if appid in first_top20:
                extra["steam_top20_first_ts"] = int(ts)
            extra["lists"] = lists.get(appid, [])
            item = {"entity": key, "name": extra.get("name") or key,
                    "metrics": metrics, "extra": extra}
            items.append(item)
            item_by_id[key] = item

        for j, future in enumerate(as_completed(detail_jobs), 1):
            activity.progress(j, len(detail_jobs), f"Steam 详情 {j} / {len(detail_jobs)}")
            key = detail_jobs[future]
            try:
                info = future.result()
            except Exception:
                info = None
            item = item_by_id.get(key)
            if not item:
                continue
            if info:
                item["extra"].update(info)
                item["name"] = info.get("name") or item["name"]
            else:
                item["extra"]["failed"] = True
            if item["extra"].get("type") and item["extra"]["type"] != "game":
                item["metrics"].pop("players", None)
                item["metrics"].pop("rank_played", None)
                item["metrics"].pop("peak_daily", None)
            elif item["extra"].get("coming_soon"):
                item["metrics"].pop("players", None)

        for j, future in enumerate(as_completed(player_jobs), 1):
            activity.progress(j, len(player_jobs), f"Steam 在线人数 {j} / {len(player_jobs)}")
            appid = player_jobs[future]
            try:
                players = future.result()
            except Exception:
                players = None
            if players is not None:
                item = item_by_id.get(str(appid))
                if (item is not None and item["extra"].get("type") in (None, "game")
                        and not item["extra"].get("coming_soon")):
                    item["metrics"]["players"] = players
    items = [it for it in items
             if not it["extra"].get("type") or it["extra"].get("type") == "game"]

    # 折扣：打折带来的热度不等于玩法火了，卡片上要标出来
    activity.progress(0, 0, "价格和折扣")
    try:
        pr = steam.prices([it["entity"] for it in items if not it["extra"].get("coming_soon")])
    except Exception:
        pr = {}
    for it in items:
        p = pr.get(it["entity"])
        if p:
            it["extra"]["discount"] = p["discount"]
            if p["price_usd"]:
                it["extra"]["price_usd"] = p["price_usd"]

    hot_id_set = {str(x) for x in hot_ids}
    with db.session() as c:
        cached_reviews = {r[0]: r[1] for r in c.execute(
            """SELECT m.entity,m.value FROM metrics m
               JOIN (SELECT entity,MAX(ts) ts FROM metrics
                     WHERE source='steam' AND metric='reviews' GROUP BY entity) x
                 ON x.entity=m.entity AND x.ts=m.ts
               WHERE m.source='steam' AND m.metric='reviews'""")}
    # Recent releases already carry Steam's catalog review count. Reuse cumulative
    # review totals for chart games and only query IDs with no known count.
    needs_review = []
    for it in items:
        key = it["entity"]
        known_count = it["extra"].get("review_seed")
        if known_count is None:
            known_count = it["metrics"].get("reviews", cached_reviews.get(key))
        if known_count is not None:
            it["metrics"]["reviews"] = int(known_count)
        elif key in hot_id_set and key not in recent_ids:
            needs_review.append(it)

    accepted = []
    for it in items:
        key = it["entity"]
        company_names = (it["extra"].get("developers") or []) + (it["extra"].get("publishers") or [])
        is_hot_only = key in hot_id_set and key not in recent_ids
        if is_hot_only and any(match.company_match(
                name, config.BIG_COMPANIES + config.BIG_COMPANIES_EXTRA) for name in company_names):
            continue
        if key in recent_ids or it["metrics"].get("reviews", 0) >= config.STEAM_REVIEW_MIN:
            accepted.append(it)
    _save("steam", ts, accepted)

    activity.say(f"Steam：在线榜返回 {len(ranks)} 款，热门探测 {len(hot_ids)} 款；"
                 f"近 30 天评论达标 {len(recent_ids)} 款，本轮新游采样 {len(new_batch)} 款；"
                 f"缺评论数需核验 {len(needs_review)} 款")
    checked = 0
    pending_save = []
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="steam-reviews") as pool:
        review_jobs = {
            pool.submit(steam.review_total, it["entity"], retries=0, timeout=10): it
            for it in needs_review
        }
        for j, future in enumerate(as_completed(review_jobs), 1):
            activity.progress(j, len(review_jobs), f"评论数核验 {j} / {len(review_jobs)}")
            it = review_jobs[future]
            try:
                total = future.result()
            except Exception:
                continue
            if total is None:
                continue
            checked += 1
            it["metrics"]["reviews"] = total
            if total < config.STEAM_REVIEW_MIN:
                continue
            pending_save.append(it)
            if int(it["entity"]) in first_top20:
                it["extra"]["steam_top20_first_ts"] = int(ts)
            it["metrics"].pop("reviews_cn", None)
            it["extra"].pop("cn_share", None)
            if checked % 20 == 0:
                _save("steam", ts, pending_save)
                accepted.extend(pending_save)
                pending_save = []
    if pending_save:
        _save("steam", ts, pending_save)
        accepted.extend(pending_save)
    for it in accepted:
        if int(it["entity"]) in first_top20 and it["metrics"].get("reviews", 0) >= config.STEAM_REVIEW_MIN:
            it["extra"]["steam_top20_first_ts"] = int(ts)
        it["metrics"].pop("reviews_cn", None)
        it["extra"].pop("cn_share", None)
    qualified_top20 = {
        int(it["entity"]) for it in items
        if int(it["entity"]) in top20
        and it["metrics"].get("reviews", 0) >= config.STEAM_REVIEW_MIN
    }
    db.kv_set("steam_top20_seen", sorted(top20_seen | qualified_top20))
    _save("steam", ts, accepted)
    return len(accepted)


# ---------------- Roblox ----------------
def _roblox(ts):
    items = roblox.trending()
    if not items:
        raise RuntimeError("Roblox 没有返回数据")
    _save("roblox", ts, items)
    return len(items)


# ---------------- 抓得久的数据源中途插空抓直播 ----------------
_between_fn = None
_between_last = 0.0


def _between():
    """TapTap 详情一抓就是几十分钟；中途到点了先抓一轮虎牙斗鱼，直播数据不断档。"""
    global _between_last
    if _between_fn and time.time() - _between_last >= 60:
        _between_last = time.time()
        try:
            _between_fn()
        except Exception:
            traceback.print_exc()


def _known(source):
    out = {}
    with db.session() as c:
        for e, name, last_seen, extra in c.execute(
                "SELECT entity, name, last_seen, extra FROM entities WHERE source = ?", (source,)):
            try:
                x = json.loads(extra) if extra else {}
            except ValueError:
                x = {}
            out[e] = (name, last_seen or 0, x)
    return out


def _detail_pause():
    time.sleep(random.uniform(1, 3))       # 和参考网站一样：每款之间随机停 1–3 秒


# ---------------- 断点续抓 ----------------
# TapTap 一轮要打开几百个详情页（30–60 分钟）。每打开 RESUME_EVERY 款就把结果存进数据库、把进度记下来；
# 程序中途重启（更新、关机）后，2 小时内接着上次的进度抓，不重新翻列表、不重开已经打开过的详情页。
RESUME_EVERY = 25
RESUME_MAX_AGE = 2 * 3600


def _resume_get(source):
    r = db.kv_get("resume:" + source)
    if r and time.time() - (r.get("started") or 0) < RESUME_MAX_AGE:
        return r
    return None


def _resume_put(source, r):
    db.kv_set("resume:" + source, r)


def _resume_clear(source):
    db.kv_set("resume:" + source, None)


# ---------------- TapTap 榜单 ----------------
def _taptap(ts):
    """照参考网站 TapTrack：先翻全部榜单网页拿游戏，再逐个打开详情页。中途重启会接着上次的进度。"""
    r = _resume_get("taptap")
    if r:
        ts, board, done, ok = r["ts"], r["board"], set(r["done"]), r["ok"]
        activity.say(f"TapTap 榜单：接着上次的进度抓（{len(done)} / {len(board)} 款已打开过）")
    else:
        raw = taptap.board_lists(progress=lambda d, n: activity.progress(d, n, f"榜单网页 第 {d} / {n} 页"))
        board = {str(a): {"name": b["name"], "lists": [list(x) for x in b["lists"]]} for a, b in raw.items()}
        done, ok = set(), 0
        r = {"ts": ts, "started": int(time.time()), "board": board, "done": [], "ok": 0}
        _resume_put("taptap", r)
    known = _known("taptap")
    ids = list(board)
    batch = []
    plan = revisit.Plan(ts)
    tried = 0

    def flush():
        if batch:
            _save("taptap", ts, batch)
            batch.clear()
        plan.flush()
        r.update({"done": sorted(done), "ok": ok})
        _resume_put("taptap", r)

    for i, key in enumerate(ids, 1):
        if key in done:
            continue
        activity.progress(i, len(ids), f"打开游戏详情 {i} / {len(ids)}")
        if i % 100 == 0:
            activity.say(f"TapTap 榜单：详情页 {i} / {len(ids)}，成功 {ok} 款")
        b = board[key]
        member = "taptap:" + key
        rank_m = {"rank_" + taptap.BOARD_RANK_KEYS[c]: p for c, p in b["lists"] if taptap.BOARD_RANK_KEYS.get(c)}
        plan.signal(member, revisit.rank_value(rank_m))
        app = None
        if plan.due(member):
            _between()
            tried += 1
            app = taptap.detail(key)
            _detail_pause()
        prev = (known.get(key) or (None, 0, {}))[2]
        if app and app != taptap.GONE:
            ok += 1
            it = taptap.parse_app(app)
            it["extra"]["detail_at"] = ts
            plan.done(member, it["metrics"], ts)
        else:
            x = dict(prev)
            x.setdefault("preorder", False)       # 在下载榜上的都已上线
            it = {"entity": key, "name": (known.get(key) or (b["name"],))[0] or b["name"], "metrics": {}, "extra": x}
        it["extra"]["lists"] = [f"{c}第{p}" for c, p in b["lists"]]
        for c, p in b["lists"]:
            k = taptap.BOARD_RANK_KEYS.get(c)
            if k:
                it["metrics"]["rank_" + k] = p
        it["metrics"] = {k: v for k, v in it["metrics"].items() if v is not None}
        batch.append(it)
        done.add(key)
        if len(batch) >= RESUME_EVERY:
            flush()
    flush()
    _resume_clear("taptap")
    if tried and not ok:
        raise RuntimeError(f"榜单拿到 {len(ids)} 款，该打开的 {tried} 款详情页一个都没打开（热度数据缺失）")
    activity.say(f"TapTap 榜单：{len(ids)} 款，详情成功 {ok} 款；" + plan.summary())
    return len(ids)


# ---------------- TapTap 制造 / 创意工坊 ----------------
def _tapmaker(ts):
    """照参考网站 taptap-maker-tracker：翻 TapTap制造 标签页 1–50 页拿游戏，再逐个打开详情页。

    标签页（约 500 款）每轮都翻；以前发现、现在掉出标签页的老游戏（含以前的创意工坊游戏），
    按抓取调度（radar/revisit.py）到点的才补，上线 14 天内的优先，每轮最多 TAPMAKER_DETAIL_PER_RUN 款。
    标签页上的也按调度：平稳的间隔翻倍，名次突升或增速异常的回到每轮都查。
    创意工坊没有标签页（TapTap 打开是 404），参考网站也没公开它的做法，所以不再找新的创意工坊游戏。
    """
    known = _known("tapmaker")
    plan = revisit.Plan(ts)
    watch = {str(e) for e in config.TAPTAP_WATCH_IDS}
    r = _resume_get("tapmaker")
    if r:
        ts, ids, done = r["ts"], r["ids"], set(r["done"])
        listed = {e: tuple(v) for e, v in r["listed"].items()}
        ok, gone, tracked = r["ok"], r["gone"], r["tracked"]
        ids = list(dict.fromkeys([str(e) for e in config.TAPTAP_WATCH_IDS] + ids))
        activity.say(f"TapTap制造：接着上次的进度抓（{len(done)} / {len(ids)} 款已打开过，不重新翻标签页）")
    else:
        lst = taptap.tag_list(taptap.MAKER_TAG, pages=config.TAPTAP_TAG_HTML_PAGES,
                              progress=lambda d, n: activity.progress(d, n, f"TapTap制造·网页 第 {d} / {n} 页"))
        listed = {str(app["id"]): (pos, app["title"]) for pos, app in lst}

        old = []
        for e, (name, last_seen, x) in known.items():
            if e in listed or not x.get("track"):
                continue                      # 热度一直不到 100 的老游戏不再补（只留名字给供给检查）
            fresh = ts - (x.get("released_time") or 0) < 14 * 86400
            if plan.due("tapmaker:" + e):
                old.append((0 if fresh else 1, x.get("detail_at") or 0, e))
        old.sort()
        ids = list(dict.fromkeys([str(e) for e in config.TAPTAP_WATCH_IDS] +
                                 list(listed) + [e for _, _, e in old[:config.TAPMAKER_DETAIL_PER_RUN]]))
        done, ok, gone, tracked = set(), 0, 0, 0
        r = {"ts": ts, "started": int(time.time()), "ids": ids, "listed": {e: list(v) for e, v in listed.items()},
             "done": [], "ok": 0, "gone": 0, "tracked": 0}
        _resume_put("tapmaker", r)

    six_hourly = (ts // 3600) % 6 == 0
    batch = []
    tried = 0

    def flush():
        if batch:
            for item in batch:
                _save("tapmaker", item.pop("_sampled_at"), [item])
            batch.clear()
        plan.flush()
        r.update({"done": sorted(done), "ok": ok, "gone": gone, "tracked": tracked})
        _resume_put("tapmaker", r)

    for i, e in enumerate(ids, 1):
        if e in done:
            continue
        activity.progress(i, len(ids), f"打开游戏详情 {i} / {len(ids)}")
        if i % 100 == 0:
            activity.say(f"TapTap制造：详情页 {i} / {len(ids)}，成功 {ok} 款")
        name, _, prev = known.get(e) or (listed.get(e, (0, e))[1], 0, {})
        member = "tapmaker:" + e
        if e in listed:
            plan.signal(member, revisit.rank_value({"rank_page": listed[e][0]}))
        app = None
        if e in watch or plan.due(member):
            _between()
            tried += 1
            app = taptap.detail(e)
            _detail_pause()
        sampled_at = int(time.time())
        if app == taptap.GONE:
            gone += 1
            x = dict(prev, gone=True, detail_at=sampled_at)
            it = {"entity": e, "name": name, "metrics": {}, "extra": x}
        elif not app:
            x = dict(prev)
            if not prev:
                x.update({"preorder": False, "tags": [], "lists": []})   # 能出现在标签页的都已上线
            it = {"entity": e, "name": listed.get(e, (0, name))[1] or name, "metrics": {}, "extra": x}
        else:
            ok += 1
            it = taptap.parse_app(app)
            it["extra"]["detail_at"] = sampled_at
            it["extra"]["gone"] = False
            plan.done(member, it["metrics"], sampled_at)
        x = it["extra"]
        if e in listed:
            x["kind"] = "maker"
            x["lists"] = [f"TapTap制造·网页第{listed[e][0]}"]
        else:
            x["kind"] = x.get("kind") or prev.get("kind") or ""
            x["lists"] = []
        dl = it["metrics"].get("downloads") or 0
        x["track"] = bool(prev.get("track") or dl >= config.TAPMAKER_MIN_STORE)
        it["metrics"] = {k: v for k, v in it["metrics"].items() if v is not None}
        if not x["track"]:
            it["metrics"] = {}            # 热度太低的只记名字，不记每小时数据
        else:
            tracked += 1
            if not six_hourly:
                it["metrics"].pop("reviews", None)   # 评价数 6 小时记一次就够
        it["_sampled_at"] = sampled_at
        batch.append(it)
        done.add(e)
        if len(batch) >= RESUME_EVERY:
            flush()
    flush()
    _resume_clear("tapmaker")
    if tried and not ok:
        raise RuntimeError(f"标签页拿到 {len(listed)} 款，该打开的 {tried} 款详情页一个都没打开（热度数据缺失）")
    activity.say(f"TapTap制造：标签页 {len(listed)} 款，补抓老游戏 {len(ids) - len(listed)} 款，"
                 f"详情成功 {ok} 款，已下线 {gone} 款，持续跟踪 {tracked} 款；" + plan.summary())
    return len(ids)


LIVE_SOURCES = ("huya", "douyu", "bili", "twitch", "kick")
SLOW_MAIN = ("steam", "roblox", "tapmaker")


def _ok_ts(source):
    return ((db.kv_get("source_status", {}) or {}).get(source) or {}).get("ok_ts") or 0


def _skip_fresh(sources, minutes, force):
    """上次成功抓取还没到间隔的平台跳过（重启、更新后不从头重抓）。返回 (要抓的, 跳过说明)。"""
    run, skipped = [], []
    now = time.time()
    for s in sources:
        if not config.SOURCES_ENABLED.get(s, True):
            continue
        age = now - _ok_ts(s)
        interval = SLOW_EVERY_HOURS.get(s, 0) * 60 or minutes
        if not force and age < interval * 60 - 120:
            skipped.append(f"{NAMES.get(s, s)}（{int(age // 60)} 分钟前）")
        else:
            run.append(s)
    return run, skipped


def oldest_ok(sources):
    """这几个平台里最久没抓的那个的成功时间（从没成功过的不算，免得一直失败的平台让程序不停重抓）。"""
    st = db.kv_get("source_status", {}) or {}
    ts = []
    for s in sources:
        v = st.get(s) or {}
        if config.SOURCES_ENABLED.get(s, True) and v.get("ok_ts") and (v.get("err_ts") or 0) <= v["ok_ts"]:
            ts.append(v["ok_ts"])          # 正在失败的平台不算（它的下次重试跟着别的平台走）
    return min(ts) if ts else 0


def source_status():
    """Keep stored platform status visible even when collection is paused."""
    status = {k: dict(v) for k, v in (db.kv_get("source_status", {}) or {}).items()}
    now = time.time()
    for source, value in status.items():
        value["paused"] = bool(config.STEAM_ONLY and source not in ("steam", "bvideo", "youtube"))
        seconds = (90 if source == "bvideo" else 5400 if source == "youtube" else
                   max(300, (config.FAST_MINUTES if source in LIVE_SOURCES else
                             SLOW_EVERY_HOURS.get(source, config.SLOW_MINUTES / 60) * 60) * 60 + 300))
        value["stale"] = bool(not value["paused"] and value.get("ok_ts") and
                              now - value["ok_ts"] > seconds)
    return status


def collect_live(force=False):
    """返回这次实际抓了几个平台。"""
    if getattr(config, "STEAM_ONLY", False):
        return 0
    ts = _align(time.time(), config.FAST_MINUTES)
    run, skipped = _skip_fresh(LIVE_SOURCES, config.FAST_MINUTES, force)
    if skipped:
        activity.say("刚抓过，这次跳过：" + "、".join(skipped) + f"（每 {config.FAST_MINUTES} 分钟抓一次）")
    fns = {"huya": _huya, "douyu": _douyu, "bili": _bili, "twitch": _twitch, "kick": _kick}
    for s in run:
        _run(s, fns[s], ts)
    return len(run)


def collect_slow(between=None, middle=None, force=False):
    """先抓几分钟就能抓完的平台（Steam、App Store、好游快爆……），抓完调用 middle()（分析一次，
    面板马上有全部平台的结果），最后抓 TapTap（要逐个打开几百个详情页，一轮 30–60 分钟）。
    上次成功还没到间隔的平台跳过；TapTap 上次没抓完的接着抓。返回这次实际抓了几个平台。
    between：抓得久的时候中途调用（到点了就插一轮直播抓取）。"""
    global _between_fn
    _between_fn = between
    ts = _align(time.time(), config.SLOW_MINUTES)
    fns = {"steam": _steam}
    if not getattr(config, "STEAM_ONLY", False):
        fns.update({"roblox": _roblox, "appcn": _appcn, "appus": _appus, "haoyou": _haoyou,
           "mistore": _mistore, "fortnite": _fortnite, "crazygames": _crazygames, "poki": _poki, "itch": _itch,
           "wxmini": _wxmini, "dymini": _dymini})
    n = 0
    try:
        run, skipped = _skip_fresh(list(fns), config.SLOW_MINUTES, force)
        if skipped:
            activity.say("刚抓过，这次跳过：" + "、".join(skipped))
        for s in run:
            _between()
            _run(s, fns[s], ts)
        n += len(run)
        if middle and run:
            middle()

        if getattr(config, "STEAM_ONLY", False):
            return n
        tm_resume = _resume_get("tapmaker")
        tm_run, tm_skip = _skip_fresh(["tapmaker"], config.SLOW_MINUTES, force)
        tp_resume = _resume_get("taptap")
        tp_age = time.time() - _ok_ts("taptap")
        tp_due = tp_resume or tp_age >= config.TAPTAP_BOARD_HOURS * 3600 - 600
        if (tm_resume or tm_run) or (tp_due and config.SOURCES_ENABLED.get("taptap", True)):
            activity.say("开始抓 TapTap：要逐个打开几百个游戏详情页，一轮 30–60 分钟，进度在右上角；"
                         "这期间直播平台照常每 30 分钟抓一次；中途重启会接着抓")
        if tm_resume or tm_run:
            _between()
            _run("tapmaker", _tapmaker, ts)
            n += 1
        elif tm_skip:
            activity.say("刚抓过，这次跳过：" + "、".join(tm_skip))
        if tp_due:
            _between()
            _run("taptap", _taptap, ts)
            n += 1
        else:
            activity.say(f"TapTap 榜单每 {config.TAPTAP_BOARD_HOURS} 小时抓一次，"
                         f"下次约 {time.strftime('%H:%M', time.localtime(time.time() + config.TAPTAP_BOARD_HOURS * 3600 - tp_age))}")
    finally:
        _between_fn = None
    return n
