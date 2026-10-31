"""Public realtime endpoints, with one rate limiter and persistent denial state."""
import re
import threading
import time
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import unescape

from . import config, db
from .net import get_json_strict
from .sources.bili import normalize, tid_of

BEIJING = timezone(timedelta(hours=8))
LEGACY_GAME_SECTIONS = {
    17: "单机游戏", 65: "网络游戏", 172: "手机游戏", 171: "电子竞技",
    136: "音游", 173: "桌游棋牌", 121: "GMV", 19: "MUGEN",
}
LATEST_GAME_SCOPE = "分区最新"
REGION_FEED_SCOPE = "/x/v2/region/dynamic/list"
REQUEST_GAP = 1.0
MAX_INFLIGHT = 1
_lock = threading.RLock()
_last_request = 0.0


class RiskError(RuntimeError):
    def __init__(self, message, scope=None):
        super().__init__(message)
        self.scope = scope


def pause_info(scope=None):
    state = db.kv_get("bili_realtime_denial", {}) or {}
    until = state.get("retry_at")
    if state and (not until or until > time.time()):
        return dict(state, until=until, seconds=max(0, (until or 0) - time.time()),
                    remaining=max(0, (until or 0) - time.time()))
    # Keep the old persisted pause keys readable. They are only a local
    # cooldown marker; a denial remains the authoritative shared stop state.
    legacy = max(db.kv_get("bili_pause_until", 0) or 0,
                 db.kv_get("bili_pause_until:" + scope, 0) or 0) if scope else (
                     db.kv_get("bili_pause_until", 0) or 0)
    if legacy and legacy > time.time():
        return {"until": legacy, "seconds": legacy - time.time(),
                "remaining": legacy - time.time(), "scope": scope}
    return {}


def paused(scope=None):
    return bool(pause_info(scope))


def pause_left(scope=None):
    return int(pause_info(scope).get("seconds") or pause_info(scope).get("remaining") or 0)


def _guard(scope=None):
    state = pause_info(scope)
    if state:
        raise RiskError(state.get("error") or "B站请求处于冷却暂停", scope)


def preserve_cooldown_budget():
    return None


def close_anonymous_session():
    return None


def request_metrics():
    return {"gap_seconds": REQUEST_GAP, "max_inflight": MAX_INFLIGHT}


@contextmanager
def request_slot(scope=None):
    with _lock:
        _guard(scope)
        yield


def _deny(error, scope=None):
    retry = getattr(error, "headers", {}).get("Retry-After") if getattr(error, "headers", None) else None
    try:
        until = time.time() + max(1, int(retry)) if retry else None
    except (ValueError, TypeError):
        until = None
    message = str(error)[:300]
    db.kv_set("bili_realtime_denial", {
        "ts": time.time(), "retry_at": until, "error": message, "scope": scope})
    raise RiskError(message, scope)


def _plain(url, params, kind="www", referer=None, retries=0, timeout=12,
           authenticated=False, persistent=False):
    global _last_request
    with _lock:
        _guard()
        time.sleep(max(0, REQUEST_GAP - (time.monotonic() - _last_request)))
        _last_request = time.monotonic()
        try:
            data = get_json_strict(url, params, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
                retries=0, timeout=timeout)
        except Exception as exc:
            if getattr(exc, "code", None) in (403, 412, 429) or re.search(
                    r"(?:HTTP(?: Error)?\s+|code[=:]\s*)-?(?:403|412|352|429)\b", str(exc)):
                _deny(exc, url)
            raise
        if isinstance(data, dict):
            payload = data.get("data")
            if data.get("code") in (-412, -352, 412, 352, 403, 429) or data.get("v_voucher") or (
                    isinstance(payload, dict) and payload.get("v_voucher")):
                _deny(RuntimeError("B站限制请求：code=%s %s" % (
                    data.get("code"), data.get("message") or "需要官方验证")), url)
        return data


def check(data, what, scope=None):
    if not isinstance(data, dict) or data.get("code") not in (0, None):
        raise RuntimeError("%s：%s" % (what, str(data)[:300]))
    return data.get("data") or {}


def published_day(value):
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(int(value), BEIJING).date().isoformat()
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(BEIJING)
    return parsed.date().isoformat()

def aid_to_bvid(aid):
    """Base58 conversion used by Bilibili's public AV/BV format."""
    aid = int(aid)
    if not 0 < aid < 1 << 51:
        raise ValueError("Invalid archive ID")
    alphabet = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"
    value, chars = ((1 << 51) | aid) ^ 23442827791579, list("BV1000000000")
    for index in range(11, 2, -1):
        value, remainder = divmod(value, 58)
        chars[index] = alphabet[remainder]
    chars[3], chars[9] = chars[9], chars[3]
    chars[4], chars[7] = chars[7], chars[4]
    return "".join(chars)

def legacy_region_feed(tid, page_size=100):
    """One app region-feed sample, never a chronological/full-directory claim."""
    tid, size = int(tid), max(1, min(100, int(page_size)))
    if tid not in LEGACY_GAME_SECTIONS:
        raise ValueError("Unknown legacy game child")
    data = check(_plain("https://app.bilibili.com/x/v2/region/dynamic/list",
                        {"rid": tid, "pn": 1, "ps": size, "pull": "true",
                         "mobi_app": "android", "platform": "android", "build": 7660300},
                        authenticated=False, timeout=18, persistent=True),
                 "B站旧子分区采样", REGION_FEED_SCOPE)
    if not isinstance(data, dict) or not isinstance(data.get("new"), list):
        raise RuntimeError("Region feed omitted its video list")
    out, times, ignored = {}, [], 0
    for row in data["new"]:
        if not isinstance(row, dict):
            raise RuntimeError("Malformed region-feed card")
        if row.get("goto") != "av":
            ignored += 1
            continue
        if int(row.get("rid") or 0) != tid:
            raise RuntimeError("Region feed ignored its legacy child filter")
        bvid = row.get("bvid") or aid_to_bvid(row.get("param"))
        if not re.fullmatch(r"BV1[0-9A-Za-z]{9}", bvid):
            raise RuntimeError("Region feed returned an invalid BV ID")
        published = int(row.get("pubdate") or 0)
        if published <= 0:
            raise RuntimeError("Region feed omitted publication day")
        times.append(datetime.fromtimestamp(published, BEIJING).isoformat())
        record = {"bvid": bvid, "date": published_day(published),
                  "title": unescape(row.get("title") or ""), "tid": tid,
                  "owner_id": str(row.get("mid") or ""), "owner_name": row.get("name") or ""}
        for name, key in (("views", "play"), ("likes", "like"),
                          ("favorites", "favourite"), ("replies", "reply")):
            record[name] = int(row[key]) if key in row else None
        out.setdefault(bvid, record)
    return {"list": list(out.values()), "ignored": ignored, "requested_size": size,
            "earliest": min(times) if times else None, "latest": max(times) if times else None}

def latest_game_posts(page_size=100):
    """The rolling legacy game-parent head; no historical cursor or census claim."""
    data = check(_plain("https://api.bilibili.com/x/web-interface/newlist",
                        {"rid": 4, "pn": 1, "ps": max(1, min(100, int(page_size))), "type": 0},
                        authenticated=False, persistent=True, timeout=12),
                 "B站游戏最新投稿", LATEST_GAME_SCOPE)
    rows = data.get("archives")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Latest game list is empty or missing")
    out, stamps = [], []
    for row in rows:
        bvid, tid = row.get("bvid"), int(row.get("tid") or 0)
        published = int(row.get("pubdate") or 0)
        if not re.fullmatch(r"BV1[0-9A-Za-z]{9}", bvid or ""):
            raise RuntimeError("Latest game list contains an invalid BV ID")
        if tid not in LEGACY_GAME_SECTIONS or published <= 0:
            raise RuntimeError("Latest game list ignored its legacy game filter")
        owner = row.get("owner") or {}
        # Fresh-head counter zeros can be placeholders; fetch counters with full Tags.
        out.append({"bvid": bvid, "tid": tid, "tid_v2": int(row.get("tidv2") or 0),
                    "date": published_day(published), "published": published,
                    "title": unescape(row.get("title") or ""),
                    "owner_id": str(owner.get("mid") or ""),
                    "owner_name": owner.get("name") or ""})
        stamps.append(published)
    if len({r["bvid"] for r in out}) != len(out):
        raise RuntimeError("Latest game list repeats BV IDs within a page")
    if any(a < b for a, b in zip(stamps, stamps[1:])):
        raise RuntimeError("Latest game list is not ordered by publication time")
    if time.time() - stamps[0] > 180 or stamps[0] - time.time() > 60:
        raise RuntimeError("Latest game list has a stale or invalid publication watermark")
    return {"list": out, "earliest": min(stamps), "latest": max(stamps)}

def dated_videos(tid, day, page=1, page_size=100, tid_version=1, persistent=False):
    """One dated directory page. Missing counts and challenge responses are errors."""
    day = datetime.strptime(day, "%Y-%m-%d").date().isoformat()
    page_size = max(1, min(100, int(page_size)))
    params = {"main_ver": "v3", "search_type": "video", "view_type": "hot_rank",
              "copy_right": -1, "new_web_tag": 1, "order": "pubdate",
              "page": int(page), "pagesize": page_size,
              "time_from": day.replace("-", ""), "time_to": day.replace("-", ""),
              "cate_id": int(tid)}
    if tid_version != 1:
        params["tid_version"] = int(tid_version)
    data = check(_plain("https://api.bilibili.com/x/web-interface/newlist_rank",
                        params, timeout=12, authenticated=False, persistent=persistent),
                 "B站日期目录", "日期目录")
    if not isinstance(data, dict) or "numResults" not in data or "numPages" not in data:
        raise RuntimeError("Date directory omitted numResults/numPages")
    rows = data.get("result")
    total, pages = int(data["numResults"]), int(data["numPages"])
    if rows is None and total == 0:
        rows = []
    if not isinstance(rows, list) or total < 0 or pages < 0:
        raise RuntimeError("Malformed date directory")
    if total > 0 and (not pages or not rows):
        raise RuntimeError("Date directory returned an empty page before its reported end")
    if len(rows) > page_size:
        raise RuntimeError("Date directory returned more rows than requested")
    for row in rows:
        if not re.fullmatch(r"BV[0-9A-Za-z]{10}", row.get("bvid") or ""):
            raise RuntimeError("Date directory row omitted a valid BV ID")
        if published_day(row.get("pubdate")) != day:
            raise RuntimeError("Date directory ignored the requested date")
    actual_size = int(data.get("pagesize") or data.get("page_size") or page_size)
    if actual_size < 1 or actual_size > 100:
        raise RuntimeError("Invalid directory page size")
    return {"total": total, "pages": pages, "page_size": actual_size, "list": rows}

def detail_with_tags(bvid, persistent=False, timeout=25):
    """Fetch all requested counters and Tags together; never treat a missing Tags as []."""
    data = check(_plain("https://api.bilibili.com/x/web-interface/view/detail",
                        {"bvid": bvid}, authenticated=False, timeout=timeout, persistent=persistent),
                 "B站视频详情及标签", "视频详情及标签")
    view, tags = data.get("View"), data.get("Tags")
    if not isinstance(view, dict) or view.get("bvid") != bvid or not isinstance(tags, list):
        raise RuntimeError("Video detail omitted View/BV ID/Tags")
    stat, owner = view.get("stat") or {}, view.get("owner") or {}
    keys = {"views": "view", "likes": "like", "coins": "coin", "favorites": "favorite",
            "shares": "share", "replies": "reply"}
    if any(key not in stat for key in keys.values()):
        raise RuntimeError("Video detail omitted a requested counter")
    if any(not isinstance(t, dict) or not isinstance(t.get("tag_name"), str) for t in tags):
        raise RuntimeError("Video detail returned a malformed Tag")
    return {"bvid": bvid, "date": published_day(view.get("pubdate")),
            "title": unescape(view.get("title") or ""), "owner_id": str(owner.get("mid") or ""),
            "owner_name": owner.get("name") or "", "tid": int(view.get("tid") or 0),
            "tid_v2": int(view.get("tid_v2") or 0), "tags": [t["tag_name"] for t in tags],
            **{name: int(stat[key]) for name, key in keys.items()}}
