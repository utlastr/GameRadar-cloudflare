"""B站：程序里唯一的 B站 客户端（v72 起直播、视频、机会雷达、成功案例都走这里）。

- 请求头、登录 Cookie、wbi 签名、风控暂停只有这一份。
- Cookie 顺序：settings.json 的 BILI_COOKIE → radar_data/bili_cookie.json（6 小时内）→ 已登录浏览器（CDP 9222 端口）。
  都没有就按访客请求（先打开一次 bilibili.com 拿 buvid3）。
- -412 暂停全部入口；-352 只暂停对应入口。暂停 30 分钟，连续 3 次暂停 2 小时。
- 游戏分区编号、书名号正则、名字归一化也只在这里定义一次。
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import Counter, deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import unescape

from .. import config, db
from ..net import _UA, _get_opener, get_json_strict, get_text

PARENTS = {2: "网游", 3: "手游", 6: "单机"}
GAME_TIDS = {4, 17, 65, 121, 136, 171, 172, 173, 19}
LEGACY_GAME_SECTIONS = {
    17: "单机游戏", 65: "网络游戏", 172: "手机游戏", 171: "电子竞技",
    136: "音游", 173: "桌游棋牌", 121: "GMV", 19: "MUGEN",
}
REGION_FEED_SCOPE = "/x/v2/region/dynamic/list"
LATEST_GAME_SCOPE = "分区最新"
BRACKET = re.compile(r"[《【\[]([^》】\]]{2,40})[》】\]]")
STEAM_LINK = re.compile(r"store\.steampowered\.com/app/(\d+)")
_HEAD_LIVE = {"Referer": "https://live.bilibili.com/", "Origin": "https://live.bilibili.com"}
_HEAD_WWW = {"Referer": "https://www.bilibili.com/", "Origin": "https://www.bilibili.com"}
_HEAD_SEARCH = {"Referer": "https://search.bilibili.com/", "Origin": "https://search.bilibili.com"}
DM_PARAMS = {"dm_img_list": "[]",
             "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
             "dm_cover_img_str": "QU5HTEUgKEludGVsLCBJbnRlbChSKSBVSEQgR3JhcGhpY3MgNjMwICgweDAwMDAzRTlCKSBEaXJlY3QzRDExIHZzXzVfMCBwc181XzApLCBvciBzaW1pbGFy",
             "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}'}
_warm = {"ts": 0}
_ck = {"v": None, "ts": 0}
COOKIE_CACHE_HOURS = 6
_request_lock = threading.RLock()
_last_request = 0.0
REQUEST_GAP = 60 / max(1, min(240, float(config.BILI_REQUESTS_PER_MINUTE)))
MAX_INFLIGHT = max(1, min(4, int(config.BILI_MAX_INFLIGHT)))
_anonymous_local = threading.local()
_request_condition = threading.Condition()
_request_local = threading.local()
_request_queues = {name: deque() for name in ("latest", "detail", "directory", "other")}
_request_cycle = ("latest",) + ("detail",) * 8 + ("directory", "other")
_request_cursor = 0
_request_active = 0
_request_inflight = 0
_request_last_group = None
_request_events = deque(maxlen=20000)
_request_results = deque(maxlen=20000)
_risk_lock = threading.RLock()


class RiskError(RuntimeError):
    def __init__(self, message, scope=None):
        super().__init__(message)
        self.scope = scope


# ---------------- 名字 ----------------
# 只去"同一个产品的不同叫法"。"手游 / 手机版"是另一个产品（正是要判断有没有的那个），"游戏"会把"鱿鱼游戏"变成"鱿鱼"，都不去。
_NOISE = ("官方中文版", "中文版", "试玩版", "试玩", "demo", "正式版", "完整版", "steam版", "pc版")
_PUNCT = re.compile(r"[\s\u3000!-/:-@\[-`{-~·—…“”‘’《》【】「」、，。！？；：（）()]+")


def normalize(s):
    """B站 标签 / 书名号名字的归一化：全角转半角、小写、去标点、去"中文版/试玩"这类尾巴。"""
    s = (s or "").strip()
    if not s:
        return ""
    out = []
    for ch in s:
        o = ord(ch)
        if o == 0x3000:
            out.append(" ")
        elif 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        else:
            out.append(ch)
    s = _PUNCT.sub("", "".join(out).lower())
    for w in _NOISE:
        if s.endswith(w) and len(s) > len(w):
            s = s[: -len(w)]
    return s.strip()


def bracket_names(title):
    return [m.group(1).strip() for m in BRACKET.finditer(title or "") if 2 <= len(m.group(1).strip()) <= 40]


def tid_of(v):
    try:
        return int(v.get("tid") if v.get("tid") is not None else v.get("typeid"))
    except (TypeError, ValueError):
        return None


# ---------------- Cookie / 请求头 ----------------
def _cookie_file():
    return os.path.join(config.DATA_DIR, "bili_cookie.json")


def _from_browser(port=9222):
    from .. import cdp
    if not cdp.available(port):
        return ""
    tab = cdp.open_tab("https://www.bilibili.com/", port, wait=4.0, background=True)
    try:
        ck = tab.cookies(["https://www.bilibili.com"])
    finally:
        cdp.close_tab(tab)
    if "SESSDATA" not in ck:
        return ""
    val = "; ".join("%s=%s" % (k, v) for k, v in ck.items())
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(_cookie_file(), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "cookie": val}, f)
    except OSError:
        pass
    return val


def cookie(force=False):
    """登录 Cookie；拿不到返回空字符串（按访客请求）。"""
    s = (getattr(config, "BILI_COOKIE", "") or "").strip()
    if s:
        return s if "=" in s else "SESSDATA=" + s
    if not force and _ck["v"] is not None and time.time() - _ck["ts"] < 600:
        return _ck["v"]
    val = ""
    if not force:
        try:
            with open(_cookie_file(), encoding="utf-8") as f:
                d = json.load(f)
            if time.time() - float(d.get("ts") or 0) < COOKIE_CACHE_HOURS * 3600:
                val = d.get("cookie") or ""
        except (OSError, ValueError):
            pass
    if not val:
        try:
            val = _from_browser()
        except Exception:
            val = ""
    _ck.update(v=val, ts=time.time())
    return val


def headers(kind="www", referer=None, authenticated=True):
    h = dict({"live": _HEAD_LIVE, "search": _HEAD_SEARCH}.get(kind, _HEAD_WWW))
    h["User-Agent"] = _UA
    if referer:
        h["Referer"] = referer
    ck = cookie() if authenticated else ""
    if ck:
        h["Cookie"] = ck
    return h


def warmup():
    if time.time() - _warm["ts"] < 3600:
        return
    try:
        _http(lambda: get_text("https://www.bilibili.com/", retries=0), "网页")
        _http(lambda: get_text("https://live.bilibili.com/", retries=0), "直播页")
    except Exception:
        pass
    _warm["ts"] = time.time()


_warmup = warmup


# ---------------- 风控暂停（全程序共用） ----------------
def _risk_key(key, scope=None):
    return key + (":" + scope if scope else "")


def paused(scope=None):
    return pause_left(scope) > 0


def pause_left(scope=None):
    until = float(db.kv_get("bili_pause_until", 0) or 0)
    if scope:
        until = max(until, float(db.kv_get(_risk_key("bili_pause_until", scope), 0) or 0))
    return max(0, int(until - time.time() + 0.999))


def note_risk(err, scope=None):
    """-352 按入口暂停，-412 全局暂停；已处理的 RiskError 不重复累计。"""
    if isinstance(err, RiskError):
        return True
    s = str(err)
    if not any(k in s for k in ("-412", "-352", "code=412", "code=352", "Error 412",
                                "Error 429", "v_voucher")):
        return False
    origin = scope
    if any(k in s for k in ("-412", "code=412", "Error 412", "Error 429")):
        scope = None
    with _risk_lock:
        now = time.time()
        # Responses already in flight belong to the same cooldown incident.
        if pause_left(scope):
            return True
        key = _risk_key("bili_risk_hits", scope)
        n = int(db.kv_get(key, 0) or 0) + 1
        minutes = 120 if n >= 3 else 30
        until = now + minutes * 60
        db.kv_set(key, n)
        db.kv_set(_risk_key("bili_pause_until", scope), until)
        db.kv_set(_risk_key("bili_last_risk", scope), {
            "at": now, "until": until, "origin": origin,
            "error": s[:300], "incident": n, "scope": scope,
        })
        if scope is None:
            _reduce_request_budget()
    try:
        from .. import activity
        activity.say("B站风控（第 %d 次），%s暂停 %d 分钟；入口：%s；原因：%s" %
                     (n, scope or "所有 B站 请求", minutes, origin or "未标记", s[:160]))
    except Exception:
        pass
    return True


def reset_risk(scope=None):
    with _risk_lock:
        key = _risk_key("bili_risk_hits", scope)
        if not paused(scope) and db.kv_get(key, 0):
            db.kv_set(key, 0)


def _reduce_request_budget():
    ceiling = 60 / REQUEST_GAP if REQUEST_GAP else 120
    previous = float(db.kv_get("bili_adaptive_requests_per_minute", ceiling) or ceiling)
    db.kv_set("bili_adaptive_requests_per_minute", max(6, min(ceiling, previous) / 2))


def preserve_cooldown_budget():
    """Upgrade an existing cooldown without shortening it or resetting its history."""
    with _risk_lock:
        if paused() and db.kv_get("bili_adaptive_requests_per_minute") is None:
            _reduce_request_budget()


def effective_request_gap():
    budget = db.kv_get("bili_adaptive_requests_per_minute")
    return max(REQUEST_GAP, 60 / max(6, float(budget))) if budget else REQUEST_GAP


def pause_info(scope=None):
    now = time.time()
    candidates = [(float(db.kv_get("bili_pause_until", 0) or 0), None)]
    if scope:
        candidates.append((float(db.kv_get(_risk_key("bili_pause_until", scope), 0) or 0), scope))
    until, selected = max(candidates, key=lambda item: item[0])
    return dict(db.kv_get(_risk_key("bili_last_risk", selected), {}) or {},
                until=until, seconds=max(0, int(until - now + 0.999)), scope=selected)


def check(d, what, scope=None):
    if not isinstance(d, dict):
        raise RuntimeError(f"{what}：返回的不是数据")
    data = d.get("data")
    if d.get("v_voucher") or (isinstance(data, dict) and data.get("v_voucher")):
        msg = f"{what}: v_voucher verification required"
        note_risk(msg, scope)
        raise RiskError(msg, scope)
    code = d.get("code")
    if code not in (0, None):
        message = str(d.get("message") or d.get("msg") or "").strip()
        msg = f"{what}：B站返回 code={code}"
        if message and message != str(code):
            msg += " " + message
        if code in (-412, -352, 412, 352):
            note_risk(msg, scope)
            raise RiskError(msg + "（风控拦截，已暂停对应 B站 请求）", scope)
        raise RuntimeError(msg)
    return d.get("data") or {}


_check = check


def _guard(scope=None):
    if paused(scope):
        raise RiskError("B站%s风控暂停中，还剩 %d 分钟" %
                        (scope or "", (pause_left(scope) + 59) // 60), scope)


def _endpoint(url):
    path = urllib.parse.urlparse(url).path
    if "/space/" in path:
        return "空间投稿"
    if "/search/" in path:
        return "搜索"
    if "/ranking/" in path:
        return "排行榜"
    return {"newlist": "分区最新", "newlist_rank": "日期目录",
            "detail": "视频详情及标签", "view": "视频详情", "popular": "全站热门",
            "card": "用户名片", "main": "评论"}.get(path.rsplit("/", 1)[-1], path)


# ---------------- wbi 签名 ----------------
_MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9,
          42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1,
          60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]
_wbi = {"key": "", "ts": 0}


def _fname(url):
    return (url or "").rsplit("/", 1)[-1].split(".", 1)[0]


def wbi_key(force=False):
    if not force and _wbi["key"] and time.time() - _wbi["ts"] < 10 * 3600:
        return _wbi["key"]
    d = _http(lambda: get_json_strict("https://api.bilibili.com/x/web-interface/nav",
                                    headers=headers(), retries=0, timeout=12), "登录状态")
    img = ((d or {}).get("data") or {}).get("wbi_img") or {}
    raw = _fname(img.get("img_url")) + _fname(img.get("sub_url"))
    key = "".join(raw[i] for i in _MIXIN)[:32] if len(raw) >= 64 else ""
    if not key:
        raise RuntimeError("wbi 取 key 失败：nav 返回里没有 wbi_img（code=%s）" % (d or {}).get("code"))
    _wbi.update(key=key, ts=time.time())
    return key


def _quote(s, safe="", encoding=None, errors=None):
    return urllib.parse.quote(s, safe="", encoding=encoding, errors=errors)


def wbi_sign(params):
    p = dict(params or {})
    p["wts"] = int(time.time())
    clean = [(k, "".join(ch for ch in str(v) if ch not in "!'()*")) for k, v in sorted(p.items())]
    query = urllib.parse.urlencode(clean, quote_via=_quote)        # 和网页一样用 %20，不用 +
    p["w_rid"] = hashlib.md5((query + wbi_key()).encode("utf-8")).hexdigest()
    return p


def _request_class(scope):
    return {LATEST_GAME_SCOPE: "latest", "视频详情及标签": "detail",
            "日期目录": "directory"}.get(scope, "other")


@contextmanager
def request_slot(scope=None):
    """Weighted admission to a bounded pool; all starts share the same rate gate."""
    global _request_active, _request_cursor, _request_last_group
    if getattr(_request_local, "depth", 0):
        _request_local.depth += 1
        try:
            yield
        finally:
            _request_local.depth -= 1
        return
    group, ticket, acquired = _request_class(scope), object(), False
    with _request_condition:
        _request_queues[group].append(ticket)
        try:
            while True:
                chosen = None
                if _request_active < MAX_INFLIGHT:
                    if _request_queues["latest"] and _request_last_group != "latest":
                        chosen = ("latest", None)
                    else:
                        for offset in range(len(_request_cycle)):
                            index = (_request_cursor + offset) % len(_request_cycle)
                            name = _request_cycle[index]
                            if name == "latest" and _request_last_group == "latest" and any(
                                    queue for key, queue in _request_queues.items() if key != "latest"):
                                continue
                            if _request_queues[name]:
                                chosen = (name, index)
                                break
                if chosen and chosen[0] == group and _request_queues[group][0] is ticket:
                    _request_queues[group].popleft()
                    if chosen[1] is not None:
                        _request_cursor = (chosen[1] + 1) % len(_request_cycle)
                    _request_last_group = group
                    _request_active += 1
                    acquired = True
                    break
                _request_condition.wait()
        finally:
            if not acquired:
                _request_queues[group].remove(ticket)
                _request_condition.notify_all()
    try:
        _request_local.depth = 1
        try:
            yield
        finally:
            _request_local.depth = 0
    finally:
        with _request_condition:
            _request_active -= 1
            _request_condition.notify_all()


def request_metrics():
    now = time.time()
    with _request_condition:
        recent = [event for event in _request_events if event[0] >= now - 60]
        finished = [event for event in _request_results if event[0] >= now - 60]
        queued = {name: len(queue) for name, queue in _request_queues.items()}
        admitted, active = _request_active, _request_inflight
    gap = effective_request_gap()
    return {"last_minute": dict(Counter(event[1] for event in recent)),
            "requests_last_minute": len(recent), "waiting": queued,
            "latency_free_ceiling_per_minute": 60 / gap if gap else None,
            "configured_ceiling_per_minute": 60 / REQUEST_GAP if REQUEST_GAP else None,
            "limit_source": "application_policy", "spacing": "request_start",
            "max_inflight": MAX_INFLIGHT, "inflight": active, "admitted": admitted,
            "completed_last_minute": len(finished),
            "outcomes_last_minute": dict(Counter(event[4] for event in finished)),
            "mean_wire_seconds": round(sum(event[2] for event in finished) / len(finished), 3)
                if finished else None,
            "mean_wait_seconds": round(sum(event[3] for event in finished) / len(finished), 3)
                if finished else None, "database": db.session_metrics()}


def _http(fn, scope=None):
    global _last_request, _request_inflight
    queued = time.monotonic()
    _guard(scope)
    with request_slot(scope):
        gap = effective_request_gap()
        # Only admission/start spacing is serial. Network latency uses the bounded pool.
        with _request_lock:
            _guard(scope)
            delay = gap - (time.monotonic() - _last_request)
            if delay > 0:
                time.sleep(delay)
            _guard(scope)
            started = _last_request = time.monotonic()
            with _request_condition:
                _request_events.append((time.time(), _request_class(scope)))
                _request_inflight += 1
        outcome = "error"
        try:
            d = fn()
            if isinstance(d, dict) and (d.get("code") in (-412, -352, 412, 352) or
                    d.get("v_voucher") or
                    (isinstance(d.get("data"), dict) and d["data"].get("v_voucher"))):
                check(d, "B站" + (scope or ""), scope)
            if isinstance(d, dict) and d.get("code") == 0 and not paused(scope):
                reset_risk(scope)
            outcome = str(d.get("code", "ok")) if isinstance(d, dict) else "ok"
            return d
        except RiskError:
            outcome = "risk"
            raise
        except (RuntimeError, urllib.error.HTTPError) as e:
            if note_risk(e, scope):
                outcome = "risk"
                raise RiskError(str(e)[:200] + "（风控拦截，已暂停对应 B站 请求）", scope)
            raise
        finally:
            with _request_condition:
                _request_inflight -= 1
                _request_results.append((time.time(), _request_class(scope),
                                         time.monotonic() - started, started - queued, outcome))


def get_signed(url, params, kind="www", referer=None, retries=1, timeout=12):
    scope = _endpoint(url)
    _guard(scope)
    h = headers(kind, referer)
    signed = wbi_sign(params)
    d = _http(lambda: get_json_strict(url, signed, headers=h, retries=0, timeout=timeout), scope)
    if isinstance(d, dict) and d.get("code") == -403:
        wbi_key(force=True)
        signed = wbi_sign(params)
        d = _http(lambda: get_json_strict(url, signed, headers=h, retries=0, timeout=timeout), scope)
    return d


def _anonymous_json(url, params, request_headers, timeout):
    """Keep-alive for anonymous polling; no login, redirects, challenge solving or retries."""
    import requests
    session = getattr(_anonymous_local, "session", None)
    if session is None:
        session = _anonymous_local.session = requests.Session()
        session.trust_env = False
    proxies = ({"http": config.PROXY, "https": config.PROXY} if config.PROXY else
               {k: v for k, v in urllib.request.getproxies().items() if k in ("http", "https")})
    session.cookies.clear()
    try:
        with session.get(url, params=params, headers=request_headers, proxies=proxies,
                         timeout=timeout, allow_redirects=False) as response:
            if response.status_code == 403:
                note_risk("code=-352: HTTP 403", _endpoint(url))
                raise RiskError("B站匿名请求被拒绝，已暂停此入口", _endpoint(url))
            if response.status_code >= 300:
                raise RuntimeError(f"HTTP Error {response.status_code}")
            try:
                return response.json()
            except ValueError:
                if re.search(r"captcha|challenge|验证|访问过于频繁|风控", response.text, re.I):
                    note_risk("code=-352: verification page", _endpoint(url))
                    raise RiskError("B站返回验证页面，已暂停此入口", _endpoint(url))
                raise RuntimeError("B站匿名接口未返回 JSON")
    except requests.RequestException as exc:
        raise RuntimeError("B站匿名请求失败: " + str(exc)[:200]) from exc


def close_anonymous_session():
    session = getattr(_anonymous_local, "session", None)
    if session is not None:
        session.close()
        del _anonymous_local.session


def _plain(url, params, kind="www", referer=None, retries=1, timeout=12, authenticated=True,
           persistent=False):
    scope = _endpoint(url)
    _guard(scope)
    if persistent:
        if authenticated:
            raise ValueError("Persistent sampling transport must remain anonymous")
        return _http(lambda: _anonymous_json(url, params, headers(kind, referer, False), timeout), scope)
    return _http(lambda: get_json_strict(url, params, headers=headers(kind, referer, authenticated),
                                         retries=0, timeout=timeout), scope)


# ---------------- 接口 ----------------
def clean_search_item(v):
    title = unescape(re.sub(r"<[^>]+>", "", v.get("title") or ""))
    return {"bvid": v.get("bvid"), "play": int(v.get("play") or 0), "like": int(v.get("like") or 0),
            "reply": int(v.get("video_review") or 0), "mid": str(v.get("mid") or ""),
            "up": v.get("author") or "", "tid": tid_of(v), "tname": v.get("typename") or "",
            "pubdate": int(v.get("pubdate") or 0), "title": title, "tag": v.get("tag") or "",
            "desc": v.get("description") or ""}


def search(kw, beg=None, end=None, page=1, order="pubdate", tids=4, page_size=30):
    """带签名的视频搜索。失败一定抛出来，不能当成 0 条。"""
    p = {"search_type": "video", "keyword": kw, "order": order, "page": page, "page_size": page_size}
    if tids:
        p["tids"] = tids
    if beg:
        p["pubtime_begin_s"] = int(beg)
    if end:
        p["pubtime_end_s"] = int(end)
    data = check(get_signed("https://api.bilibili.com/x/web-interface/wbi/search/type", p, kind="search",
                            retries=1, timeout=15), "B站搜索")
    return {"total": int(data.get("numResults") or 0), "pages": int(data.get("numPages") or 0),
            "list": [clean_search_item(v) for v in (data.get("result") or []) if v.get("bvid")]}


def view(bvid):
    return check(_plain("https://api.bilibili.com/x/web-interface/view", {"bvid": bvid}, retries=0,
                        authenticated=False), "B站视频详情")


def card(mid):
    data = check(_plain("https://api.bilibili.com/x/web-interface/card", {"mid": mid}), "B站用户名片")
    return {"follower": int(data.get("follower") or 0), "name": (data.get("card") or {}).get("name") or ""}


def space_videos(mid, ps=30):
    p = {"mid": mid, "ps": ps, "pn": 1, "order": "pubdate", "platform": "web", "web_location": 1550101,
         "order_avoided": "true", "index": 0, "tid": 0, "keyword": ""}
    p.update(DM_PARAMS)
    data = check(get_signed("https://api.bilibili.com/x/space/wbi/arc/search", p,
                            referer="https://space.bilibili.com/%s/video" % mid, retries=1), "B站空间视频")
    return ((data.get("list") or {}).get("vlist")) or []


def newlist(pn, ps=50, rid=4):
    return check(_plain("https://api.bilibili.com/x/web-interface/newlist",
                        {"rid": rid, "ps": ps, "pn": pn, "type": 0}, timeout=15,
                        authenticated=False), "B站分区最新").get("archives") or []


BEIJING = timezone(timedelta(hours=8))


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


def replies(aid, pages=5, between=None):
    out, nxt = [], 0
    for _ in range(pages):
        if between and not between():
            break
        data = check(get_signed("https://api.bilibili.com/x/v2/reply/wbi/main",
                                {"oid": aid, "type": 1, "mode": 3, "next": nxt, "ps": 20}), "B站评论")
        for rep in data.get("replies") or []:
            out.append((rep.get("content") or {}).get("message") or "")
        cur = data.get("cursor") or {}
        nxt = cur.get("next") or 0
        if cur.get("is_end") or not nxt:
            break
        time.sleep(1.2)
    return out


def danmaku(cid, limit=3000):
    """弹幕 XML 是 deflate 压缩的。走和其他请求一样的代理设置。"""
    def read():
        req = urllib.request.Request("https://api.bilibili.com/x/v1/dm/list.so?oid=%s" % cid, headers=headers())
        with _get_opener().open(req, timeout=15) as r:
            return r.read()
    raw = _http(read, "弹幕")
    try:
        xml = zlib.decompress(raw, -15).decode("utf-8", "replace")
    except zlib.error:
        xml = raw.decode("utf-8", "replace")
    return re.findall(r"<d p=\"[^\"]*\">([^<]*)</d>", xml)[:limit]


def weekly_issues():
    """每周必看的期数列表。"""
    data = check(_plain("https://api.bilibili.com/x/web-interface/popular/series/list", {}), "每周必看期数")
    return [{"number": x.get("number"), "name": x.get("name") or ""} for x in data.get("list") or []]


def weekly_issue(number):
    data = check(_plain("https://api.bilibili.com/x/web-interface/popular/series/one", {"number": number}),
                 "每周必看第 %s 期" % number)
    out = []
    for v in data.get("list") or []:
        s, o = v.get("stat") or {}, v.get("owner") or {}
        out.append({"bvid": v.get("bvid"), "title": v.get("title") or v.get("short_title") or "",
                    "tname": v.get("tname") or "", "tid": v.get("tid"), "play": int(s.get("view") or 0),
                    "mid": str(o.get("mid") or ""), "up": o.get("name") or "", "pubdate": int(v.get("pubdate") or 0)})
    return out


# ---------------- 直播 / 排行（雷达主程序用） ----------------
def live_rooms(max_pages=25, progress=None):
    """三个大分区按人气排序翻页，每页 30 个直播间。"""
    warmup()
    rooms, total = [], len(PARENTS) * max_pages
    done = 0
    for pid, pname in PARENTS.items():
        for page in range(1, max_pages + 1):
            done += 1
            if progress:
                progress(done, total)
            params = {"platform": "web", "parent_area_id": pid, "cate_id": 0, "area_id": 0,
                      "sort_type": "online", "page": page, "page_size": 30}
            url = "https://api.live.bilibili.com/room/v3/area/getRoomList"
            data = check(_plain(url, params, kind="live"), "B站直播列表")
            lst = data.get("list") or []
            if not lst:
                break
            for r in lst:
                rooms.append({"game": (r.get("area_name") or r.get("area_v2_name") or "").strip(),
                              "heat": int(r.get("online") or 0),
                              "streamer": str(r.get("uid") or r.get("roomid") or ""),
                              "nick": r.get("uname") or "", "title": r.get("title") or ""})
            if (lst[-1].get("online") or 0) < 20:
                break
            time.sleep(0.6)
    if not rooms:
        raise RuntimeError("B站直播一个直播间都没拿到")
    return rooms


def game_videos():
    """游戏区排行榜（近 3 天）+ 全站热门里游戏分区的视频。返回 [{bvid, title, views, tname}]。"""
    _guard()
    out, errors = {}, []
    try:
        data = check(_plain("https://api.bilibili.com/x/web-interface/ranking/v2", {"rid": 4, "type": "all"},
                            authenticated=False),
                     "游戏区排行榜")
        for v in data.get("list") or []:
            out[v.get("bvid")] = v
    except RiskError as e:
        if paused():
            raise
        errors.append(str(e))
    except Exception as e:
        errors.append(str(e))
    for pn in range(1, 6):
        try:
            data = check(_plain("https://api.bilibili.com/x/web-interface/popular", {"ps": 50, "pn": pn},
                                authenticated=False), "全站热门")
        except Exception as e:
            errors.append(str(e))
            break
        lst = data.get("list") or []
        for v in lst:
            if "游戏" in (v.get("tname") or "") or tid_of(v) in GAME_TIDS:
                out.setdefault(v.get("bvid"), v)
        if not lst or data.get("no_more"):
            break
        time.sleep(0.6)
    if not out:
        raise RuntimeError("B站游戏视频一个都没拿到：" + "；".join(errors[:2]))
    return [video_record(v) for k, v in out.items() if k], errors


def video_record(v):
    owner = v.get("owner") or {}
    title = unescape(re.sub(r"<[^>]+>", "", v.get("title") or ""))
    return {"bvid": v.get("bvid") or "", "title": title,
            "views": int((v.get("stat") or {}).get("view", v.get("play") or 0) or 0),
            "owner_id": str(owner.get("mid") or v.get("mid") or ""),
            "owner": owner.get("name") or v.get("author") or "",
            "published": int(v.get("pubdate") or 0), "tname": v.get("tname") or v.get("typename") or "",
            "description": v.get("desc") or v.get("description") or ""}


def search_videos(term, order="pubdate"):
    p = {"search_type": "video", "keyword": term, "order": order, "page": 1, "page_size": 30, "tids": 4}
    data = check(get_signed("https://api.bilibili.com/x/web-interface/wbi/search/type", p, kind="search",
                            retries=0, timeout=12), "视频搜索")
    return [video_record(v) for v in data.get("result") or [] if v.get("bvid")]


def video_detail(bvid):
    data = view(bvid)
    if not data.get("bvid"):
        raise RuntimeError("视频详情缺少 BV 号")
    return video_record(data)
