"""YouTube 发现引擎：靠 mostPopular 轮询 + 频道上传列表，而不是 search。

为什么不用 search 当主力：
- search.list 一次 100 单位、最多 50 条，10,000 单位/天 = 100 次 = 5,000 个结果位，这是硬天花板。
- videos.list?chart=mostPopular 一次 1 单位、同样 50 条，10,000 单位 = 10,000 次。
- playlistItems.list 一次 1 单位、50 条，追某个频道的上传，等于 B站 的"UP 主最新投稿"。

所以分工：mostPopular 负责"现在什么游戏在火"并顺带发现频道，
频道上传列表负责"这批频道刚发了什么"，search 只留给用户点名的关键词。
"""
import threading
import time
from datetime import datetime, timedelta, timezone

from . import activity, config, db
from .youtube import GAMING_CATEGORY, _api, _init, _int_or_none, _key

# 覆盖主要地区的游戏区热榜。一个地区一次调用 = 1 单位。
REGIONS = ("US", "GB", "CA", "AU", "IN", "JP", "KR", "DE", "FR", "ES", "IT", "RU",
           "BR", "MX", "AR", "CL", "CO", "PL", "TR", "UA", "NL", "SE", "NO", "DK",
           "FI", "CZ", "RO", "HU", "GR", "PT", "IE", "NZ", "ZA", "ID", "TH", "VN",
           "PH", "TW", "HK", "SG", "MY", "PK", "BD", "SA", "AE", "IL", "EG", "NG")

DAILY_UNITS = 10000          # YouTube Data API 默认日配额
UNIT_RESERVE = 1200          # 留给详情补齐和关键词搜索，别一轮吃光
SWEEP_INTERVAL = 1800        # 热榜多久扫一轮（秒）
_sweep_lock = threading.Lock()
_thread = None
_last_progress_log = 0


def _progress(stage, error=""):
    global _last_progress_log
    previous = db.kv_get("youtube_feed_progress", {}) or {}
    db.kv_set("youtube_feed_progress", {
        "ts": time.time(), "running": True, "stage": stage,
        "error": str(error)[:400] or previous.get("error", "")})
    if error or time.time() - _last_progress_log >= 60:
        activity.say("YouTube：" + stage + ("；失败：" + str(error)[:200] if error else "，采集中"))
        _last_progress_log = time.time()


# ---------------- 配额记账 ----------------
def _utc_day(now=None):
    return datetime.fromtimestamp(now or time.time(), timezone.utc).date().isoformat()


def units_used(now=None):
    state = db.kv_get("youtube_units", {}) or {}
    day = _utc_day(now)
    if state.get("day") != day:
        return 0
    return int(state.get("used") or 0)


def _spend(n, kind="other", now=None):
    day = _utc_day(now)
    state = db.kv_get("youtube_units", {}) or {}
    if state.get("day") != day:
        state = {"day": day, "used": 0, "kinds": {}}
    state["used"] = int(state.get("used") or 0) + int(n)
    kinds = state.get("kinds") or {}
    kinds[kind] = int(kinds.get(kind) or 0) + int(n)
    state["kinds"] = kinds
    state["ts"] = int(now or time.time())
    db.kv_set("youtube_units", state)


def units_left(now=None):
    return max(0, DAILY_UNITS - UNIT_RESERVE - units_used(now))


# ---------------- 落库 ----------------
def _upsert_full(items, now, origin):
    """把 videos.list / playlistItems 的结果写进 youtube_videos（含播放数）。"""
    rows = []
    for item in items:
        vid = item.get("id")
        if isinstance(vid, dict):                       # playlistItems 的形状
            sn = item.get("snippet") or {}
            vid = (sn.get("resourceId") or {}).get("videoId")
            if not vid:
                continue
            published, title = sn.get("publishedAt"), sn.get("title", "")
            desc, cid, ctitle = sn.get("description", ""), sn.get("channelId", ""), sn.get("channelTitle", "")
            views = likes = comments = None
            details = {"origin": origin}
        else:
            sn = item.get("snippet") or {}
            st = item.get("statistics") or {}
            ct = item.get("contentDetails") or {}
            published, title = sn.get("publishedAt"), sn.get("title", "")
            desc, cid, ctitle = sn.get("description", ""), sn.get("channelId", ""), sn.get("channelTitle", "")
            views = _int_or_none(st.get("viewCount"))
            likes = _int_or_none(st.get("likeCount"))
            comments = _int_or_none(st.get("commentCount"))
            details = item
        tags = sn.get("tags")
        rows.append((vid, published, title, desc, cid, ctitle,
                     sn.get("categoryId") or GAMING_CATEGORY,
                     None if tags is None else __import__("json").dumps(tags, ensure_ascii=False),
                     (item.get("contentDetails") or {}).get("duration"),
                     sn.get("liveBroadcastContent"), views, likes, comments,
                     __import__("json").dumps(details, ensure_ascii=False), now, now))
    if not rows:
        return 0
    with db.session() as c:
        c.executemany("""INSERT INTO youtube_videos
            (video_id,published,title,description,channel_id,channel_title,category_id,tags,
             duration,live_broadcast,views,likes,comments,details_json,first_seen,last_seen)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(video_id) DO UPDATE SET
             published=COALESCE(excluded.published,youtube_videos.published),
             title=excluded.title,
             channel_id=COALESCE(NULLIF(excluded.channel_id,''),youtube_videos.channel_id),
             channel_title=COALESCE(NULLIF(excluded.channel_title,''),youtube_videos.channel_title),
             category_id=COALESCE(excluded.category_id,youtube_videos.category_id),
             tags=COALESCE(excluded.tags,youtube_videos.tags),
             views=COALESCE(excluded.views,youtube_videos.views),
             likes=COALESCE(excluded.likes,youtube_videos.likes),
             comments=COALESCE(excluded.comments,youtube_videos.comments),
             details_json=CASE WHEN youtube_videos.details_json='{}' THEN excluded.details_json
                               ELSE youtube_videos.details_json END,
             last_seen=excluded.last_seen""", rows)
    from .collect import _status
    progress = db.kv_get("youtube_feed_progress", {}) or {}
    _status("youtube", True, len(rows), warn=progress.get("error", ""))
    return len(rows)


# ---------------- 热榜轮询 ----------------
def sweep_trending(regions=None, now=None):
    """按地区扫游戏区热榜。每个地区 1 单位、最多 50 条。"""
    key = _key()
    if not key:
        return {"ok": False, "error": "没有 YouTube 密钥"}
    regions = list(regions or REGIONS)
    now = int(now or time.time())
    added, skipped, errors = 0, 0, []
    for region in regions:
        _progress("热榜 " + region)
        if units_left() <= 0:
            skipped = len(regions) - len(errors)
            errors.append("今日额度将尽，剩下的地区下一轮再扫")
            break
        try:
            data = _api("videos", {"part": "snippet,statistics,contentDetails",
                                   "chart": "mostPopular", "regionCode": region,
                                   "videoCategoryId": GAMING_CATEGORY, "maxResults": 50}, key)
            _spend(1, "trending")
            added += _upsert_full(data.get("items") or [], int(time.time()), "trending:" + region)
        except RuntimeError as exc:
            errors.append(region + "：" + str(exc)[:80])
            _progress("热榜 " + region, exc)
            time.sleep(0.4)
    return {"ok": bool(added), "regions": len(regions), "touched": added, "errors": errors[:6], "skipped": skipped}


# ---------------- 频道上传追踪 ----------------
def top_channels(limit=200, days=30):
    """从库里挑"最近抓到的游戏频道"，按出现次数排。这是自动发现出来的追踪名单。"""
    since = int(time.time()) - days * 86400
    with db.session() as c:
        rows = c.execute("""SELECT channel_id, channel_title, COUNT(*) n FROM youtube_videos
            WHERE channel_id!='' AND last_seen>=? GROUP BY channel_id
            ORDER BY n DESC LIMIT ?""", (since, int(limit))).fetchall()
    return [(r[0], r[1] or "") for r in rows]


def track_channels(channels=None, now=None, per_channel=50):
    """追每个频道的上传列表（UU 播放列表），等于"这个 UP 主最新发了什么"。"""
    key = _key()
    if not key:
        return {"ok": False, "error": "没有 YouTube 密钥"}
    now = int(now or time.time())
    if channels is None:
        channels = top_channels()
    added, errors, units = 0, [], 0
    for cid, name in channels:
        _progress("频道 " + (name or cid))
        if not cid.startswith("UC") or units_left() <= 0:
            break
        playlist = "UU" + cid[2:]
        try:
            data = _api("playlistItems", {"part": "snippet", "playlistId": playlist,
                                          "maxResults": min(50, per_channel)}, key)
            units += 1
            _spend(1, "channels")
            items = data.get("items") or []
            ids = [(i.get("snippet") or {}).get("resourceId", {}).get("videoId") for i in items]
            ids = [v for v in ids if v]
            added += _upsert_full(items, int(time.time()), "channel:" + (name or cid))
            for off in range(0, len(ids), 50):        # 补播放数：1 单位 / 50 条
                batch = ids[off:off + 50]
                try:
                    stats = _api("videos", {"part": "snippet,statistics,contentDetails",
                                            "id": ",".join(batch)}, key)
                    units += 1
                    _spend(1, "details")
                    _upsert_full(stats.get("items") or [], int(time.time()), "channel:" + (name or cid))
                except RuntimeError as exc:
                    errors.append("详情 " + str(exc)[:60])
                    _progress("频道详情", exc)
        except RuntimeError as exc:
            msg = str(exc)
            if "404" not in msg:                      # 404 = 这个频道没有上传列表，正常
                errors.append((name or cid) + "：" + msg[:70])
                _progress("频道 " + (name or cid), exc)
            time.sleep(0.3)
    return {"ok": bool(added), "channels": len(channels), "touched": added, "units": units, "errors": errors[:6]}


# ---------------- 一轮完整采集 ----------------
def run_once(regions=None, channel_limit=200):
    """热榜 + 频道追踪，一轮。search 不在这里，它留给关键词。"""
    _init()
    if not _key():
        return {"ok": False, "error": "settings.json 没配 YOUTUBE_API_KEY"}
    t0, before = time.time(), units_used()
    db.kv_set("youtube_feed_progress", {"ts": t0, "running": True, "error": ""})
    trend = sweep_trending(regions)
    chans = top_channels(limit=channel_limit)
    track = track_channels(chans)
    with db.session() as c:
        total = c.execute("SELECT COUNT(*) FROM youtube_videos").fetchone()[0]
        today = c.execute("""SELECT COUNT(*) FROM youtube_videos WHERE first_seen>=?""",
                          (int(time.time()) - 86400,)).fetchone()[0]
    out = {"ok": bool(trend["touched"] or track["touched"]), "total": total, "new_24h": today, "units": units_used() - before,
           "units_today": units_used(), "trending": trend, "channels": track,
           "seconds": round(time.time() - t0, 1)}
    db.kv_set("youtube_sweep", {"ts": int(time.time()), **{k: v for k, v in out.items()
                                                          if k in ("total", "new_24h", "units", "units_today")}})
    from .collect import _status
    errors = trend["errors"] + track["errors"]
    if not out["ok"] and not errors:
        errors = ["本轮未取得有效视频；检查额度与接口返回"]
    _status("youtube", out["ok"], trend["touched"] + track["touched"],
            err="；".join(errors) if not out["ok"] else "", warn="；".join(errors) if out["ok"] else "")
    db.kv_set("youtube_feed_progress", {"ts": time.time(), "running": False,
                                      "error": "；".join(errors)[:400]})
    return out


def state():
    sweep = db.kv_get("youtube_sweep", {}) or {}
    return {"units_today": units_used(), "units_left": units_left(),
            "daily_cap": DAILY_UNITS, "reserve": UNIT_RESERVE,
            "last_sweep": sweep, "channels_known": len(top_channels(limit=1000))}


def _sweeper(stop_event):
    while stop_event is None or not stop_event.is_set():
        try:
            if not _sweep_lock.locked():
                with _sweep_lock:
                    activity.say("YouTube：开始热榜及频道采集")
                    result = run_once()
                    if not result.get("ok"):
                        from .collect import _status
                        _status("youtube", False, err=result.get("error") or "本轮未取得有效视频")
                    activity.say("YouTube：本轮结束，状态 %s，库内 %s 条" % (
                        "成功" if result.get("ok") else "失败", result.get("total", 0)))
        except Exception as exc:
            from .collect import _status
            _status("youtube", False, err=str(exc)[:400])
            db.kv_set("youtube_feed_progress", {"ts": time.time(), "running": False,
                                              "error": str(exc)[:400]})
            activity.say("YouTube采集失败：" + str(exc)[:200])
        for _ in range(SWEEP_INTERVAL):
            if stop_event is not None and stop_event.is_set():
                return
            time.sleep(1)


def start(stop_event=None):
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_sweeper, args=(stop_event,), name="youtube-sweeper", daemon=True)
    _thread.start()
