"""Collect and retain public YouTube Gaming-category video metadata."""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import config, db

GAMING_CATEGORY = "20"
BEIJING = timezone(timedelta(hours=8))
SCHEMA = """
CREATE TABLE IF NOT EXISTS youtube_videos(
 video_id TEXT PRIMARY KEY, published TEXT, title TEXT, description TEXT,
 channel_id TEXT, channel_title TEXT, category_id TEXT, tags TEXT,
 duration TEXT, live_broadcast TEXT, views INTEGER, likes INTEGER, comments INTEGER,
 details_json TEXT, first_seen INTEGER, last_seen INTEGER);
CREATE INDEX IF NOT EXISTS idx_youtube_published ON youtube_videos(published);
CREATE TABLE IF NOT EXISTS youtube_windows(
 window_id TEXT PRIMARY KEY, published_after TEXT, published_before TEXT,
 next_page_token TEXT, search_done INTEGER, status TEXT, search_pages INTEGER,
 returned_items INTEGER, detail_items INTEGER, started INTEGER, updated INTEGER, error TEXT);
"""
_lock = threading.Lock()
_worker_lock = threading.Lock()
_worker_started = False
_worker_stop = None
_window_thread = None


def _init():
    with db.session() as c:
        c.executescript(SCHEMA)


def _key():
    try:
        with open(config.USER_SETTINGS, encoding="utf-8") as f:
            value = json.load(f).get("YOUTUBE_API_KEY", "")
    except (OSError, ValueError, AttributeError):
        value = ""
    return value.strip() if isinstance(value, str) else ""


def _api(endpoint, params, key):
    query = urllib.parse.urlencode({**params, "key": key})
    url = "https://www.googleapis.com/youtube/v3/%s?%s" % (endpoint, query)
    request = urllib.request.Request(url, headers={"User-Agent": "GameRadar/YouTubeDataCollector"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            data = {}
        error = data.get("error") or {}
        reasons = [x.get("reason") for x in error.get("errors", []) if x.get("reason")]
        message = str(error.get("message") or "YouTube API HTTP %d" % exc.code).replace(key, "[REDACTED]")
        raise RuntimeError("HTTP %d %s: %s" % (exc.code, ",".join(reasons), message)) from None
    except Exception as exc:
        raise RuntimeError(str(exc).replace(key, "[REDACTED]")) from None


def _pacific_day(now=None):
    utc = datetime.fromtimestamp(now or time.time(), timezone.utc)
    march_first = datetime(utc.year, 3, 1, tzinfo=timezone.utc)
    first_sunday_march = 1 + (6 - march_first.weekday()) % 7
    second_sunday_march = first_sunday_march + 7
    november_first = datetime(utc.year, 11, 1, tzinfo=timezone.utc)
    first_sunday_november = 1 + (6 - november_first.weekday()) % 7
    dst_start = datetime(utc.year, 3, second_sunday_march, 10, tzinfo=timezone.utc)
    dst_end = datetime(utc.year, 11, first_sunday_november, 9, tzinfo=timezone.utc)
    offset = timedelta(hours=-7 if dst_start <= utc < dst_end else -8)
    return (utc + offset).date().isoformat()


def _search_budget(now=None):
    day = _pacific_day(now)
    state = db.kv_get("youtube_search_budget", {}) or {}
    if state.get("day") != day:
        state = {"day": day, "used": 0}
    return state


def _spend_search_call(now=None):
    state = _search_budget(now)
    if state["used"] >= 100:
        return False
    state["used"] += 1
    db.kv_set("youtube_search_budget", state)
    return True


def _window_id(after, before):
    return after + "/" + before


def _utc_iso(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("时间戳必须包含时区")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _upsert_search_items(items, now):
    rows = []
    for item in items:
        vid = (item.get("id") or {}).get("videoId")
        s = item.get("snippet") or {}
        if not vid:
            continue
        rows.append((vid, s.get("publishedAt"), s.get("title", ""), s.get("description", ""),
                     s.get("channelId", ""), s.get("channelTitle", ""),
                     s.get("categoryId", GAMING_CATEGORY), None, None, None,
                     None, None, None, "{}", now, now))
    if rows:
        with db.session() as c:
            c.executemany("""INSERT INTO youtube_videos VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(video_id) DO UPDATE SET
                 published=COALESCE(excluded.published,youtube_videos.published),
                 title=excluded.title, description=excluded.description,
                 channel_id=excluded.channel_id, channel_title=excluded.channel_title,
                 category_id=COALESCE(excluded.category_id,youtube_videos.category_id),
                 last_seen=excluded.last_seen""", rows)
    return len(rows)


def _save_details(items, now):
    rows = []
    for item in items:
        s, st, content = item.get("snippet") or {}, item.get("statistics") or {}, item.get("contentDetails") or {}
        tags = s.get("tags", [])
        rows.append((s.get("publishedAt"), s.get("title", ""), s.get("description", ""),
                     s.get("channelId", ""), s.get("channelTitle", ""), s.get("categoryId"),
                     json.dumps(tags, ensure_ascii=False), content.get("duration"),
                     s.get("liveBroadcastContent"), _int_or_none(st.get("viewCount")),
                     _int_or_none(st.get("likeCount")), _int_or_none(st.get("commentCount")),
                     json.dumps(item, ensure_ascii=False), now, item.get("id")))
    if rows:
        with db.session() as c:
            c.executemany("""UPDATE youtube_videos SET published=?,title=?,description=?,channel_id=?,
                channel_title=?,category_id=?,tags=?,duration=?,live_broadcast=?,views=?,likes=?,comments=?,
                details_json=?,last_seen=? WHERE video_id=?""", rows)
    return len(rows)


def _int_or_none(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _details(ids, key, now):
    count = 0
    for offset in range(0, len(ids), 50):
        batch = ids[offset:offset + 50]
        data = _api("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(batch)}, key)
        items = data.get("items") or []
        count += _save_details(items, now)
        returned = {item.get("id") for item in items}
        missing = [vid for vid in batch if vid not in returned]
        if missing:
            with db.session() as c:
                c.executemany("""UPDATE youtube_videos SET details_json=?,last_seen=?
                    WHERE video_id=?""",
                    [(json.dumps({"detail_unavailable": True}), now, vid) for vid in missing])
    return count


def begin_window(after, before):
    """Create or resume an RFC3339 publication-time window."""
    after, before = _utc_iso(after), _utc_iso(before)
    start, end = datetime.fromisoformat(after.replace("Z", "+00:00")), datetime.fromisoformat(before.replace("Z", "+00:00"))
    if start >= end:
        raise ValueError("after/before 必须是递增且带时区的 RFC3339 时间")
    wid, now = _window_id(after, before), int(time.time())
    _init()
    with db.session() as c:
        c.execute("""INSERT OR IGNORE INTO youtube_windows
            (window_id,published_after,published_before,next_page_token,search_done,status,
             search_pages,returned_items,detail_items,started,updated,error)
            VALUES(?,?,?,NULL,0,'queued',0,0,0,?,?, '')""", (wid, after, before, now, now))
    return wid


def collect_window(after, before, max_search_calls=100):
    """Fetch a category/date window; persist each page for quota-safe resume."""
    if not _key():
        return {"ok": False, "status": "missing_key", "error": "settings.json 未配置 YOUTUBE_API_KEY"}
    max_search_calls = min(100, max(0, int(max_search_calls)))
    wid = begin_window(after, before)
    key, now, used_this_run = _key(), int(time.time()), 0
    with _lock:
        try:
            _retry_missing_details(key, now)
        except RuntimeError as exc:
            _set_window(wid, status="details_pending", error=str(exc)[:400])
        state = _window(wid)
        if state["search_done"]:
            missing = _count_missing_details(state)
            if missing:
                _set_window(wid, status="details_pending", error="%d 条视频详情待补" % missing)
            else:
                _set_window(wid, status="complete", error="")
        while used_this_run < max_search_calls:
            state = _window(wid)
            if state["search_done"]:
                break
            budget = _search_budget()
            if budget["used"] >= 100:
                _set_window(wid, status="quota_limited", error="YouTube 搜索 API 今日 100 次请求上限已用完")
                break
            if not _spend_search_call():
                _set_window(wid, status="quota_limited", error="YouTube 搜索 API 今日 100 次请求上限已用完")
                break
            used_this_run += 1
            params = {"part": "snippet", "type": "video", "videoCategoryId": GAMING_CATEGORY,
                      "order": "date", "publishedAfter": state["published_after"],
                      "publishedBefore": state["published_before"], "maxResults": 50}
            if state["next_page_token"]:
                params["pageToken"] = state["next_page_token"]
            _set_window(wid, status="running", error="")
            try:
                page = _api("search", params, key)
            except RuntimeError as exc:
                _set_window(wid, status="error", error=str(exc)[:400])
                break
            items = page.get("items") or []
            _upsert_search_items(items, now)
            ids = list(dict.fromkeys((x.get("id") or {}).get("videoId") for x in items))
            ids = [x for x in ids if x]
            detail_count, detail_error = 0, ""
            try:
                detail_count = _details(ids, key, now)
            except RuntimeError as exc:
                detail_error = str(exc)[:300]
            next_token = page.get("nextPageToken")
            with db.session() as c:
                c.execute("""UPDATE youtube_windows SET next_page_token=?, search_done=?, status=?,
                    search_pages=search_pages+1, returned_items=returned_items+?, detail_items=detail_items+?,
                    updated=?, error=? WHERE window_id=?""",
                    (next_token, int(not bool(next_token)), "pagination_exhausted" if not next_token and not detail_error
                     else "details_pending" if not next_token else "running", len(ids), detail_count,
                     int(time.time()), detail_error, wid))
            if not next_token:
                break
        state = _window(wid)
        if state["status"] == "running" and not state["search_done"]:
            limited = _search_budget()["used"] >= 100
            _set_window(wid, status="quota_limited" if limited else "paused",
                        error="YouTube 搜索 API 今日 100 次请求上限已用完，保留断点" if limited
                        else "本轮分页预算用完，保留断点")
    return report(wid, used_this_run)


def _retry_missing_details(key, now):
    with db.session() as c:
        ids = [r[0] for r in c.execute(
            "SELECT video_id FROM youtube_videos WHERE details_json='{}' ORDER BY published LIMIT 500")]
    for offset in range(0, len(ids), 50):
        _details(ids[offset:offset + 50], key, now)


def run_async(after, before, max_search_calls=100):
    global _window_thread
    if not _key():
        return {"ok": False, "status": "missing_key", "error": "settings.json 未配置 YOUTUBE_API_KEY"}
    try:
        begin_window(after, before)
    except ValueError as exc:
        return {"ok": False, "status": "invalid_window", "error": str(exc)}
    with _worker_lock:
        if _window_thread and _window_thread.is_alive():
            return {"ok": False, "status": "running", "error": "YouTube 采集任务正在运行"}
        def run():
            try:
                collect_window(after, before, max_search_calls=max_search_calls)
            except Exception as exc:
                try:
                    wid = _window_id(_utc_iso(after), _utc_iso(before))
                    _set_window(wid, status="error", error=str(exc)[:400])
                except Exception:
                    pass
        _window_thread = threading.Thread(target=run, name="youtube-window", daemon=True)
        _window_thread.start()
    return {"ok": True, "status": "started"}


def _window(wid):
    with db.session() as c:
        row = c.execute("SELECT * FROM youtube_windows WHERE window_id=?", (wid,)).fetchone()
    return dict(row)


def _set_window(wid, **values):
    if not values:
        return
    fields = ",".join(k + "=?" for k in values)
    with db.session() as c:
        c.execute("UPDATE youtube_windows SET " + fields + " WHERE window_id=?", (*values.values(), wid))


def report(wid=None, calls=0):
    _init()
    with db.session() as c:
        if wid:
            w = c.execute("SELECT * FROM youtube_windows WHERE window_id=?", (wid,)).fetchone()
        else:
            w = c.execute("SELECT * FROM youtube_windows ORDER BY updated DESC LIMIT 1").fetchone()
        total = c.execute("SELECT COUNT(*) FROM youtube_videos").fetchone()[0]
        tag = c.execute("""SELECT COUNT(*),SUM(CASE WHEN tags IS NOT NULL AND tags!='[]' THEN 1 ELSE 0 END),
            SUM(CASE WHEN tags='[]' THEN 1 ELSE 0 END),SUM(CASE WHEN tags IS NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN views IS NOT NULL THEN 1 ELSE 0 END),SUM(CASE WHEN likes IS NOT NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN comments IS NOT NULL THEN 1 ELSE 0 END) FROM youtube_videos""").fetchone()
    state = dict(w) if w else None
    if state:
        with db.session() as c:
            scoped = c.execute("""SELECT COUNT(*),
                SUM(CASE WHEN tags IS NOT NULL AND tags!='[]' THEN 1 ELSE 0 END),
                SUM(CASE WHEN tags='[]' THEN 1 ELSE 0 END),SUM(CASE WHEN tags IS NULL THEN 1 ELSE 0 END),
                SUM(CASE WHEN views IS NOT NULL THEN 1 ELSE 0 END),SUM(CASE WHEN likes IS NOT NULL THEN 1 ELSE 0 END),
                SUM(CASE WHEN comments IS NOT NULL THEN 1 ELSE 0 END)
                FROM youtube_videos WHERE published>=? AND published<?""",
                (state["published_after"], state["published_before"])).fetchone()
    else:
        scoped = tag
    return {"ok": bool(state and state["status"] == "pagination_exhausted"), "window": state,
            "search_calls_this_run": calls, "search_calls_today_pacific": _search_budget()["used"],
            "stored_video_count": total, "api_key_configured": bool(_key()),
            "window_video_count": scoped[0] or 0,
            "fields": {"videos": scoped[0] or 0, "nonempty_tags": scoped[1] or 0, "empty_tags": scoped[2] or 0,
                       "tags_not_enriched": scoped[3] or 0, "with_views": scoped[4] or 0,
                       "with_likes": scoped[5] or 0, "with_comments": scoped[6] or 0}}


def _count_window(state):
    with db.session() as c:
        return c.execute("SELECT COUNT(*) FROM youtube_videos WHERE published>=? AND published<?",
                          (state["published_after"], state["published_before"])).fetchone()[0]


def _count_missing_details(state):
    with db.session() as c:
        return c.execute("""SELECT COUNT(*) FROM youtube_videos WHERE published>=? AND published<?
            AND details_json='{}'""", (state["published_after"], state["published_before"])).fetchone()[0]


def next_half_day_window(now=None):
    """Last closed 12-hour Beijing period, ending seven hours before 00:00/12:00."""
    local = datetime.fromtimestamp(now or time.time(), BEIJING)
    boundary = local.replace(hour=12 if local.hour >= 12 else 0, minute=0, second=0, microsecond=0)
    end = boundary - timedelta(hours=7)
    start = end - timedelta(hours=12)
    return start.isoformat(), end.isoformat()


def _scheduler(stop_event):
    local = datetime.now(BEIJING)
    boundary = local.replace(hour=12 if local.hour >= 12 else 0, minute=0, second=0, microsecond=0)
    last_boundary = boundary.timestamp()
    while not stop_event.is_set():
        local = datetime.now(BEIJING)
        boundary = local.replace(hour=12 if local.hour >= 12 else 0, minute=0, second=0, microsecond=0)
        boundary_ts = boundary.timestamp()
        if last_boundary != boundary_ts and local.timestamp() >= boundary_ts:
            try:
                _init()
                with db.session() as c:
                    pending = c.execute("""SELECT published_after,published_before FROM youtube_windows
                        WHERE status IN ('queued','running','paused','quota_limited','details_pending')
                        ORDER BY started LIMIT 1""").fetchone()
                if pending:
                    _scheduled_collect(pending[0], pending[1], 50)
                else:
                    after, before = next_half_day_window(local.timestamp())
                    _scheduled_collect(after, before, 50)
            except Exception as exc:  # keep the app collector alive
                db.kv_set("youtube_last_error", str(exc)[:400])
            last_boundary = boundary_ts
        stop_event.wait(60)


def _scheduled_collect(after, before, max_search_calls):
    global _window_thread
    with _worker_lock:
        if _window_thread and _window_thread.is_alive():
            return
        def run():
            try:
                result = collect_window(after, before, max_search_calls)
                db.kv_set("youtube_last_error", "" if result.get("ok") else
                          result.get("error") or result.get("status") or "时间窗未完成")
            except Exception as exc:
                db.kv_set("youtube_last_error", str(exc)[:400])
        _window_thread = threading.Thread(target=run,
                                          name="youtube-window", daemon=True)
        _window_thread.start()


def videos(after=None, before=None, offset=0, limit=100):
    """Return a page of stored videos with decoded tag lists."""
    _init()
    where, args = [], []
    if after:
        where.append("published>=?")
        args.append(_utc_iso(after))
    if before:
        where.append("published<?")
        args.append(_utc_iso(before))
    clause = " WHERE " + " AND ".join(where) if where else ""
    with db.session() as c:
        rows = c.execute("SELECT * FROM youtube_videos" + clause +
                         " ORDER BY published DESC,video_id LIMIT ? OFFSET ?",
                         (*args, min(500, max(1, int(limit))), max(0, int(offset)))).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["tags"] = json.loads(item["tags"]) if item["tags"] is not None else None
            item["details"] = json.loads(item.pop("details_json") or "{}")
        except ValueError:
            item["tags"], item["details"] = None, {}
        out.append(item)
    return out


def public_state():
    return report()


def start(stop_event=None):
    global _worker_started, _worker_stop
    if _worker_started:
        return
    _worker_stop = stop_event or threading.Event()
    _worker_started = True
    threading.Thread(target=_scheduler, args=(_worker_stop,), name="youtube-game-videos", daemon=True).start()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Collect YouTube Gaming-category videos for a publication window")
    parser.add_argument("--after", required=True, help="RFC3339 timestamp with timezone")
    parser.add_argument("--before", required=True, help="RFC3339 timestamp with timezone")
    parser.add_argument("--max-search-calls", type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(collect_window(args.after, args.before, args.max_search_calls), ensure_ascii=False, indent=2))
