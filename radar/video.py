"""Video evidence is keyed by BV ID and never merged into games by title."""
import json
import re
import threading
import time
from collections import defaultdict

from . import activity, config, db
from .sources import bili, steam

SCHEMA = """
CREATE TABLE IF NOT EXISTS video_records(
 bvid TEXT PRIMARY KEY,title TEXT,owner_id TEXT,owner TEXT,published INTEGER,
 description TEXT,first_seen INTEGER,last_seen INTEGER,origins TEXT,game_ids TEXT);
CREATE TABLE IF NOT EXISTS video_samples(
 bvid TEXT,ts INTEGER,views INTEGER,PRIMARY KEY(bvid,ts));
CREATE INDEX IF NOT EXISTS idx_video_sample_ts ON video_samples(ts);
"""
_BV = re.compile(r"BV[0-9A-Za-z]{10}\Z")
_STEAM_LINK = re.compile(r"https?://store\.steampowered\.com/app/(\d+)(?:[/\s?#]|$)")
_started = False
_lock = threading.Lock()


def init():
    with db.session() as c:
        c.executescript(SCHEMA)


def save(records, origin, now=None):
    now = int(time.time() if now is None else now)
    init()
    with db.session() as c:
        for v in records:
            bvid = v.get("bvid") or ""
            if not _BV.fullmatch(bvid):
                continue
            old = c.execute("SELECT origins,description,game_ids FROM video_records WHERE bvid=?", (bvid,)).fetchone()
            origins = set(json.loads(old[0])) if old else set()
            origins.add(origin)
            desc = v.get("description") or (old[1] if old else "")
            ids = set(json.loads(old[2])) if old else set()
            ids.update("steam:" + appid for appid in _STEAM_LINK.findall(desc))
            c.execute(
                """INSERT INTO video_records VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(bvid) DO UPDATE SET title=excluded.title,
                owner_id=CASE WHEN excluded.owner_id!='' THEN excluded.owner_id ELSE video_records.owner_id END,
                owner=CASE WHEN excluded.owner!='' THEN excluded.owner ELSE video_records.owner END,
                published=CASE WHEN excluded.published>0 THEN excluded.published ELSE video_records.published END,
                description=excluded.description,last_seen=excluded.last_seen,
                origins=excluded.origins,game_ids=excluded.game_ids""",
                (bvid, v.get("title") or bvid, v.get("owner_id") or "", v.get("owner") or "",
                 v.get("published") or 0, desc, now, now,
                 json.dumps(sorted(origins), ensure_ascii=False), json.dumps(sorted(ids))))
            views = v.get("views")
            if isinstance(views, int) and views >= 0:
                c.execute("INSERT OR REPLACE INTO video_samples VALUES(?,?,?)", (bvid, now, views))


def growth(points):
    """Real elapsed hours, no fabricated launch-to-now growth."""
    if len(points) < 2:
        return {}
    ts, views = points[-1]
    base = next((p for p in reversed(points[:-1]) if 1800 <= ts - p[0] <= 24 * 3600), None)
    if base is None or views < base[1]:
        return {}
    hours = (ts - base[0]) / 3600
    delta = views - base[1]
    result = {"delta": delta, "hours": hours, "rate": delta / hours,
              "ratio": delta / base[1] if base[1] > 0 else None}
    older = next((p for p in reversed(points) if 1800 <= base[0] - p[0] <= 24 * 3600), None)
    if older is not None and base[1] > older[1]:
        prior_rate = (base[1] - older[1]) / ((base[0] - older[0]) / 3600)
        result["acceleration"] = result["rate"] / prior_rate
    return result


def summary(now=None):
    now = int(time.time() if now is None else now)
    risk = bili.risk_status()
    init()
    with db.session() as c:
        videos = [dict(r) for r in c.execute("SELECT * FROM video_records ORDER BY last_seen DESC LIMIT 1000")]
        points = defaultdict(list)
        for r in c.execute("SELECT bvid,ts,views FROM video_samples WHERE ts>=? ORDER BY ts", (now - 8 * 86400,)):
            points[r[0]].append((r[1], r[2]))
    rows = []
    for v in videos:
        p = points.get(v["bvid"], [])
        if not p:
            continue
        v["views"] = p[-1][1]
        v["sample_ts"] = p[-1][0]
        v["stale"] = now - p[-1][0] > max(7200, config.BVIDEO_MINUTES * 180)
        v["growth"] = growth(p)
        recent = 0 <= now - v["published"] <= 14 * 86400 if v["published"] else False
        v["fast"] = not v["stale"] and v["growth"].get("rate", 0) >= config.BVIDEO_FAST_VIEWS_PER_HOUR
        v["attention"] = not v["stale"] and (v["fast"] or (recent and v["views"] >= config.BVIDEO_EARLY_VIEWS))
        v["reason"] = ("播放加速" if v["fast"] else "近期高播放") if v["attention"] else ("数据过期" if v["stale"] else "持续观察")
        if risk:
            v.update(fast=False, attention=False, reason="暂停前历史数据")
        v["origins"] = json.loads(v["origins"])
        v["game_ids"] = json.loads(v["game_ids"])
        v["url"] = "https://www.bilibili.com/video/" + v["bvid"] + "/"
        rows.append(v)
    rows.sort(key=lambda v: (-int(v["attention"]), -v["growth"].get("rate", 0), -v["views"]))
    searches = []
    for term in config.BVIDEO_SEARCH_TERMS:
        matches = [v for v in rows if "搜索：" + term in v["origins"]]
        fresh = [v for v in matches if not v["stale"]]
        searches.append({"term": term, "videos": len(matches),
                         "authors": len({v["owner_id"] for v in matches if v["owner_id"]}),
                         "new_24h": sum(0 <= now - v["published"] <= 86400 for v in matches),
                         "observed_delta": sum(v["growth"].get("delta", 0) for v in fresh),
                         "windows": sum(bool(v["growth"]) for v in fresh)})
    st = dict(db.kv_get("video_collection", {}) or {})
    status = "未采集"
    if st.get("ts"):
        status = "失败" if st.get("failed") else ("覆盖不全" if st.get("errors") else "已采样")
        if now - st["ts"] > max(7200, config.BVIDEO_MINUTES * 180):
            status = "数据过期"
    if risk:
        status = ("等待历史客户端退避结束" if risk.get("legacy") else
                  "已暂停，接口要求验证" if risk.get("verification_required") else
                  "已暂停，等待人工检查" if not risk.get("retry_at") else "等待平台允许重试")
    elif st.get("deferred"):
        status = "等待恢复采样"
        st.update(deferred=False, awaiting_sample=True, deferred_reason="")
    authors = {v["owner_id"] for v in rows if v["attention"] and v["owner_id"]}
    fallback = {"mode": "history" if risk and rows else "unavailable" if risk else "live",
                "samples": len(rows), "latest_sample_ts": max((v["sample_ts"] for v in rows), default=None)}
    return {"status": status, "collection": st, "rows": rows, "risk": risk, "fallback": fallback,
            "pause_message": bili.pause_message() if risk else "",
            "total": len(videos), "attention": sum(v["attention"] for v in rows),
            "authors": len(authors), "searches": searches}


def collect():
    """Independent discovery plus targeted search plus persistent ID tracking."""
    init()
    now, errors, fetched = int(time.time()), [], set()
    previous = db.kv_get("video_collection", {}) or {}
    deferred = bili.pause_message()
    endpoints = []
    def ingest(records, origin):
        save(records, origin)
        fetched.update(v["bvid"] for v in records if _BV.fullmatch(v.get("bvid") or ""))
        db.kv_set("video_collection", {"ts": now, "count": len(fetched), "running": True,
                  "errors": errors[:40], "failed": False, "endpoints": endpoints,
                  "coverage": "有限榜单、配置的关键词与已发现视频；不代表B站全站"})
    if not deferred:
        try:
            records, warnings = bili.game_videos()
            origin = records[0].get("discovery_source", "游戏排行／热门") if records else "游戏排行／热门"
            ingest(records, origin)
            errors.extend(warnings)
            endpoints.append(origin)
        except bili.RiskError as e:
            deferred = str(e)
        except Exception as e:
            errors.append("发现入口：" + str(e)[:180])
    for term in config.BVIDEO_SEARCH_TERMS[:8]:
        for order in ("pubdate", "click"):
            if deferred or bili.paused():
                deferred = deferred or bili.pause_message()
                break
            try:
                ingest(bili.search_videos(term, order), "搜索：" + term)
                endpoints.append("搜索：" + term + "/" + order)
            except bili.RiskError as e:
                deferred = str(e)
                break
            except Exception as e:
                errors.append("搜索 " + term + "：" + str(e)[:120])
            time.sleep(0.3)
    if bili.paused():
        deferred = deferred or bili.pause_message()
    with db.session() as c:
        tracked = [r[0] for r in c.execute(
            """SELECT v.bvid FROM video_records v LEFT JOIN video_samples s ON v.bvid=s.bvid
            WHERE v.last_seen>=? GROUP BY v.bvid ORDER BY v.last_seen ASC,MAX(s.views) DESC LIMIT ?""",
            (now - 14 * 86400, config.BVIDEO_TRACK_PER_RUN))]
    detail_ids = list(dict.fromkeys(list(config.BVIDEO_WATCH_IDS) + tracked))
    # Resolve descriptions of top newly discovered videos to explicit store IDs.
    with db.session() as c:
        new = [r[0] for r in c.execute(
            """SELECT v.bvid FROM video_records v JOIN video_samples s ON v.bvid=s.bvid
            WHERE v.description='' GROUP BY v.bvid ORDER BY MAX(s.views) DESC LIMIT ?""",
            (config.BVIDEO_DETAIL_PER_RUN,))]
    detail_ids = list(dict.fromkeys(detail_ids + new))
    for bvid in detail_ids:
        if deferred or bili.paused():
            deferred = deferred or bili.pause_message()
            break
        if time.time() - now > 180:
            errors.append("本轮采样时间已到，剩余 BV 号下一轮继续")
            break
        if not _BV.fullmatch(bvid):
            continue
        try:
            ingest([bili.video_detail(bvid)], "BV持续追踪")
        except bili.RiskError as e:
            deferred = str(e)
            break
        except Exception as e:
            errors.append(bvid + "：" + str(e)[:100])
        time.sleep(0.3)
    report = {"ts": now, "count": len(fetched), "errors": errors[:40],
              "failed": not bool(fetched) and not bool(deferred), "endpoints": endpoints,
              "deferred": bool(deferred), "deferred_reason": deferred,
              "last_ok_ts": now if fetched else previous.get("last_ok_ts", previous.get("ts") if previous.get("count") else None),
              "last_count": len(fetched) if fetched else previous.get("last_count", previous.get("count", 0)),
              "search_terms": config.BVIDEO_SEARCH_TERMS,
              "coverage": "有限榜单、配置的关键词与已发现视频；不代表B站全站",
              "tracked": len(detail_ids)}
    db.kv_set("video_collection", report)
    from . import collect as collectors
    if deferred:
        if fetched:
            collectors._status("bvideo", True, len(fetched), warn="后续采样暂停")
        collectors._deferred("bvideo", deferred)
        activity.say(f"B站视频：已保留本轮 {len(fetched)} 个BV号；后续采样暂停")
        raise bili.RiskError(deferred)
    collectors._status("bvideo", bool(fetched), len(fetched),
                       err="；".join(errors[:3]) if not fetched else "",
                       warn="；".join(errors[:3]))
    db.log_run("collect:bvideo", time.time() - now, bool(fetched),
               f"{len(fetched)} 个BV号；" + ("；".join(errors[:3]) if errors else "采样完成"))
    activity.say(f"B站视频：采样 {len(fetched)} 个BV号，{len(errors)} 项异常")
    if not fetched:
        raise RuntimeError("B站视频采集失败：" + "；".join(errors[:2]))
    return len(fetched), "；".join(errors[:3])


def notify_hotspots():
    from . import notify
    if not config.NOTIFY_ENABLED:
        return
    sent = db.kv_get("video_alerts", {}) or {}
    now = int(time.time())
    day = time.strftime("%Y-%m-%d")
    midnight = int(time.mktime(time.strptime(day, "%Y-%m-%d")))
    with db.session() as c:
        game_alerts = c.execute("SELECT count(*) FROM alerts WHERE ts>=?", (midnight,)).fetchone()[0]
    today = sum(v.get("day") == day for v in sent.values()) + game_alerts
    for v in summary(now)["rows"]:
        if today >= config.NOTIFY_MAX_PER_DAY:
            break
        if not v["attention"] or now - (sent.get(v["bvid"], {}).get("ts") or 0) < 86400:
            continue
        identity = "、".join(v["game_ids"]) or "游戏归属待核对"
        notify.popup("视频热点：" + v["title"][:35],
                     f"{v['views']:,} 次播放 · {v['reason']} · {identity}\n{v['url']}")
        sent[v["bvid"]] = {"ts": now, "day": day}
        today += 1
        db.kv_set("video_alerts", sent)


def start(stop_event=None):
    if config.BVIDEO_STREAM_ENABLED:
        from . import bili_latest
        bili_latest.start(stop_event)
        return
    global _started
    with _lock:
        if _started:
            return
        _started = True
    def loop():
        stop = stop_event or threading.Event()
        next_video = 0
        while not stop.is_set():
            now = time.time()
            if now >= next_video and config.source_on("bvideo"):
                try:
                    collect()
                    notify_hotspots()
                except bili.RiskError:
                    pass
                except Exception as e:
                    activity.say(str(e))
                next_video = time.time() + max(5, config.BVIDEO_MINUTES) * 60
            stop.wait(5)
    def release_loop():
        from . import steam_cycle
        stop = stop_event or threading.Event()
        steam_cycle.loop(stop)
    threading.Thread(target=loop, name="video-discovery", daemon=True).start()
    threading.Thread(target=release_loop, name="release-discovery", daemon=True).start()
