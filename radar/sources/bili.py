"""B站：程序里唯一的 B站 客户端（v72 起直播、视频、机会雷达、成功案例都走这里）。

- 请求头、登录 Cookie、wbi 签名、风控暂停只有这一份。
- Cookie 顺序：settings.json 的 BILI_COOKIE → radar_data/bili_cookie.json（6 小时内）→ 已登录浏览器（CDP 9222 端口）。
  都没有就按访客请求（先打开一次 bilibili.com 拿 buvid3）。
- 平台拦截后停止派发请求；只采用平台提供的 Retry-After，不自行推算封禁时长。
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
from email.utils import parsedate_to_datetime
from html import unescape

from .. import config, db
from ..net import _UA, _get_opener, get_json_strict, get_text

PARENTS = {2: "网游", 3: "手游", 6: "单机"}
GAME_TIDS = {4, 17, 65, 121, 136, 171, 172, 173, 19}
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


class RiskError(RuntimeError):
    pass


_risk_lock = threading.RLock()
_request_lock = threading.RLock()


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


def headers(kind="www", referer=None):
    h = dict({"live": _HEAD_LIVE, "search": _HEAD_SEARCH}.get(kind, _HEAD_WWW))
    h["User-Agent"] = _UA
    if referer:
        h["Referer"] = referer
    ck = cookie()
    if ck:
        h["Cookie"] = ck
    return h


def warmup():
    _guard()
    if time.time() - _warm["ts"] < 3600:
        return
    try:
        _http(lambda: get_text("https://www.bilibili.com/", retries=0))
        _http(lambda: get_text("https://live.bilibili.com/", retries=0))
    except RiskError:
        raise
    except Exception:
        pass
    _warm["ts"] = time.time()


_warmup = warmup


# ---------------- 风控暂停（全程序共用） ----------------
def paused():
    return bool(risk_status())


def risk_status():
    state = db.kv_get("bili_risk_state", {}) or {}
    if state.get("active"):
        until = state.get("retry_at")
        if not until or float(until) > time.time():
            return state
    legacy = float(db.kv_get("bili_pause_until", 0) or 0)
    if legacy > time.time():
        return {"active": True, "legacy": True, "retry_at": None,
                "message": "历史客户端退避（不是当前平台拦截或验证要求）",
                "legacy_deadline": legacy}
    return {}


def pause_left():
    return max(0, int((risk_status().get("retry_at") or 0) - time.time()))


def pause_message():
    state = risk_status()
    if not state:
        return ""
    if state.get("legacy"):
        deadline = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(state["legacy_deadline"]))
        return "历史客户端退避，将于 " + deadline + " 自动结束；未收到当前平台验证要求"
    when = state.get("retry_at")
    timing = ("平台要求等待至 " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))
              if when else "平台未提供恢复时间；需要人工检查账号访问状态")
    if state.get("verification_required"):
        timing += "；接口明确要求官方验证"
    endpoint = "；接口：" + state["endpoint"] if state.get("endpoint") else ""
    return "B站采集已暂停：" + state.get("message", "平台拦截") + endpoint + "；" + timing


def resume_after_verification(verified=False):
    """Operator acknowledgement, never an automatic retry of a platform challenge."""
    if verified is not True:
        raise ValueError("必须先确认已经完成B站官方验证")
    with _request_lock, _risk_lock:
        state = risk_status()
        if state.get("retry_at") and state["retry_at"] > time.time():
            raise ValueError("平台要求的等待时间尚未结束")
        db.kv_set("bili_risk_last", state)
        db.kv_set("bili_risk_state", {})
        db.kv_set("bili_pause_until", 0)
        db.kv_set("bili_risk_hits", 0)


def _retry_at(err):
    value = getattr(err, "retry_after", None) or (getattr(err, "headers", None) or {}).get("Retry-After")
    if not value:
        return None
    try:
        return time.time() + max(0, int(value))
    except (ValueError, TypeError):
        try:
            return parsedate_to_datetime(value).timestamp()
        except (ValueError, TypeError, OverflowError):
            return None


def note_risk(err, endpoint=None):
    """Record one incident; propagated/local pause exceptions never extend it."""
    if isinstance(err, RiskError):
        return True
    s = str(err)
    if not (getattr(err, "code", None) in (403, 412, 429)
            or re.search(r"(?:code[=:]\s*|HTTP(?: Error)?\s+)-?(?:412|352|403|429)\b", s)
            or "v_voucher" in s):
        return False
    with _risk_lock:
        current = risk_status()
        if current and not current.get("legacy"):
            return True
        message = re.sub(r"(v_voucher[=:]\s*)[^;\s]+", r"\1[redacted]", s)[:300]
        state = {"active": True, "ts": time.time(), "message": message,
                 "retry_at": _retry_at(err), "verification_required": "v_voucher" in s}
        if endpoint:
            url = urllib.parse.urlsplit(endpoint)
            if url.scheme == "https" and url.hostname in (
                    "api.bilibili.com", "api.live.bilibili.com", "www.bilibili.com", "live.bilibili.com"):
                state["endpoint"] = "https://" + url.hostname + url.path
        db.kv_set("bili_risk_state", state)
        db.kv_set("bili_risk_hits", 1)
    try:
        from .. import activity
        activity.say(pause_message())
    except Exception:
        pass
    return True


def reset_risk():
    if not paused() and db.kv_get("bili_risk_hits", 0):
        db.kv_set("bili_risk_hits", 0)


def check(d, what, endpoint=None):
    if not isinstance(d, dict):
        raise RuntimeError(f"{what}：返回的不是数据")
    code = d.get("code")
    if code not in (0, None):
        msg = f"{what}：B站返回 code={code} {d.get('message') or d.get('msg') or ''}"
        if code in (-412, -352, 412, 352):
            note_risk(msg, endpoint)
            raise RiskError(msg + "（风控拦截，已暂停 B站 请求）")
        raise RuntimeError(msg)
    reset_risk()
    return d.get("data") or {}


_check = check


def _guard():
    if paused():
        raise RiskError(pause_message())


# =====================================================================================
# v102：撞墙就换线，永不停工
#
# 以前的做法是：任何一处返回 -352 → 记一个"全局暂停"→ 所有请求在发出去之前就被拦住 →
# 没有请求就永远不会再撞墙，也永远不会恢复。B站 回 -352 时通常不给 Retry-After，
# 于是 retry_at 只能是 null，而旧逻辑把 null 当成"永远有效"，这就是"一撞就再也不重试"。
#
# 现在的做法：
#   1. 一条线路 = 一个 host+path。撞墙只让这一条线路退避 15 秒 → 5 分钟（有 Retry-After 就尊重，
#      但最多 5 分钟），别的线路完全不受影响。
#   2. 同一次任务有多条线路（排行榜 / 全站热门 / 分区最新 / 网页），按顺序试，撞一条换一条。
#   3. 撞墙同时换一副身份（换 UA、重新取 Cookie），让下一次请求看起来是新客户端。
#   4. 没有任何"全局停工"状态：`paused()` 永远 False，调用方不会再因为风控跳过整轮采样。
# =====================================================================================
UAS = (
    _UA,
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
)
BACKOFF = (15, 40, 90, 180, 300)        # 同一条线路连续撞墙的退避秒数，封顶 5 分钟
ROUTE_WAIT_CAP = 45                     # 单次请求前最多为退避等这么久，等不到也照发
_routes = {}                            # 线路名 -> {fails, down_until, why, ts}
_ident = {"i": 0}
_state_lock = threading.RLock()
_current = threading.local()
_RISK_RE = re.compile(r"(?:code[=:]\s*|HTTP(?: Error)?\s+)-?(?:412|352|403|429)\b")


def _route_name(url):
    """线路名 = 域名 + 路径。同一接口带不同参数算同一条线路。"""
    try:
        parts = urllib.parse.urlsplit(url or "")
    except ValueError:
        return "unknown"
    if not parts.hostname:
        return "unknown"
    return (parts.hostname or "unknown") + (parts.path or "/")


def route_ok(url):
    """这条线路通了：清掉它的退避计数。"""
    name = _route_name(url)
    with _state_lock:
        cur = _routes.get(name)
        if cur and (cur.get("fails") or cur.get("down_until")):
            _routes[name] = {"fails": 0, "down_until": 0, "why": "", "ts": time.time()}


def route_down(url, why="", retry_after=None):
    """这条线路撞墙了：退避一会儿，其他线路照常用。返回退避秒数。"""
    name = _route_name(url)
    with _state_lock:
        cur = _routes.get(name) or {}
        fails = int(cur.get("fails") or 0) + 1
        wait = BACKOFF[min(fails, len(BACKOFF)) - 1]
        if retry_after:
            try:
                wait = max(wait, min(300, int(float(retry_after))))
            except (TypeError, ValueError):
                pass
        _routes[name] = {"fails": fails, "down_until": time.time() + wait,
                         "why": str(why)[:160], "ts": time.time()}
    return wait


def route_wait(url):
    """这条线路还要退避多少秒（0 = 现在就能用）。"""
    with _state_lock:
        cur = _routes.get(_route_name(url)) or {}
    return max(0, int((cur.get("down_until") or 0) - time.time()))


def next_identity():
    """换身份：换 UA、丢掉缓存 Cookie（下次重新从浏览器/配置取）。"""
    with _state_lock:
        _ident["i"] += 1
        _ck["v"], _ck["ts"] = None, 0
        _warm["ts"] = 0
        return _ident["i"]


def _sleep_at_most(seconds, label=""):
    """退避期间也不干等：最多等 ROUTE_WAIT_CAP 秒就继续往前冲。"""
    left = min(int(seconds or 0), ROUTE_WAIT_CAP)
    if left <= 0:
        return
    try:
        from .. import activity
        activity.say("B站线路 %s 退避中，%d 秒后继续（其他线路照常）" % (label or "?", left))
    except Exception:
        pass
    time.sleep(left + 0.5)


def risk_status():
    """只给界面看的摘要：哪些线路在退避。空 dict = 全好。不再控制任何流程。"""
    now = time.time()
    with _state_lock:
        down = {k: dict(v) for k, v in _routes.items() if (v.get("down_until") or 0) > now}
    if not down:
        return {}
    left = {k: max(0, int(v["down_until"] - now)) for k, v in down.items()}
    return {"active": True, "routes": down, "left": left,
            "retry_at": min(v["down_until"] for v in down.values()),
            "message": "、".join("%s 退避 %d 秒" % (k, s) for k, s in list(left.items())[:4]),
            "verification_required": any("v_voucher" in (v.get("why") or "") for v in down.values())}


def paused():
    """永远 False。B站 没有"停工"这个状态了；留着是因为别处还在调用它。"""
    return False


def pause_left():
    return 0


def pause_message():
    st = risk_status()
    return ("B站部分线路退避中（" + st["message"] + "），已换线继续") if st else ""


def resume_after_verification(verified=False):
    """人工放行 / 手动重置：清掉所有线路的退避记录。"""
    with _state_lock:
        _routes.clear()
    db.kv_set("bili_risk_state", {})
    db.kv_set("bili_pause_until", 0)
    db.kv_set("bili_risk_hits", 0)
    return True


def _retry_at(err):
    value = getattr(err, "retry_after", None) or (getattr(err, "headers", None) or {}).get("Retry-After")
    if not value:
        return None
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        try:
            return max(0, int(parsedate_to_datetime(value).timestamp() - time.time()))
        except (ValueError, TypeError, OverflowError):
            return None


def note_risk(err, endpoint=None):
    """记一次撞墙：只退避撞墙的那条线路 + 换身份。返回 True。绝不设全局暂停。"""
    if isinstance(err, RiskError):
        return True
    s = str(err)
    if not (getattr(err, "code", None) in (403, 412, 429) or _RISK_RE.search(s) or "v_voucher" in s):
        return False
    url = endpoint or getattr(_current, "url", "") or getattr(err, "url", "")
    if url and route_wait(url) > 0:      # 这条线路已经在退避，不重复叠加
        return True
    route_down(url, s, _retry_at(err))
    next_identity()
    db.kv_set("bili_risk_hits", int(db.kv_get("bili_risk_hits", 0) or 0) + 1)
    try:
        from .. import activity
        activity.say("B站撞墙，换线重试：" + _route_name(url) + "（" + s[:70] + "）")
    except Exception:
        pass
    return True


def reset_risk():
    if db.kv_get("bili_risk_hits", 0):
        db.kv_set("bili_risk_hits", 0)


def headers(kind="www", referer=None):
    h = dict({"live": _HEAD_LIVE, "search": _HEAD_SEARCH}.get(kind, _HEAD_WWW))
    with _state_lock:
        h["User-Agent"] = UAS[_ident["i"] % len(UAS)]
    if referer:
        h["Referer"] = referer
    ck = cookie()
    if ck:
        h["Cookie"] = ck
    return h


def _http(fn, endpoint=None):
    """发一次请求。撞墙就退避这条线路、换身份、抛出 RiskError，让上层换下一条线路。"""
    url = endpoint or ""
    if url:
        left = route_wait(url)
        if left:
            _sleep_at_most(left, _route_name(url))
    with _request_lock:
        _current.url = url
        try:
            result = fn()
        except RiskError:
            raise
        except (RuntimeError, urllib.error.HTTPError) as e:
            if note_risk(e, url):
                raise RiskError(str(e)[:200]) from e
            raise
        if isinstance(result, dict):
            code = result.get("code")
            if code in (-412, -352, 412, 352):
                msg = "B站返回 code=%s %s" % (code, result.get("message") or result.get("msg") or "")
                note_risk(RuntimeError(msg), url)
                raise RiskError(msg)
            data = result.get("data")
            if result.get("v_voucher") or (isinstance(data, dict) and data.get("v_voucher")):
                note_risk("B站返回 v_voucher：需要官方验证", url)
                raise RiskError("B站要求官方验证（v_voucher）")
        if url:
            route_ok(url)
        return result


def _with_routes(label, routes, rounds=3, waits=(10, 25)):
    """按顺序试多条线路；撞墙就换下一条；全都撞墙就歇一会儿再来，最多 rounds 轮。

    routes: [(说明, 无参函数), ...]。返回第一个成功的结果，全都失败就抛出最后一个错误。
    """
    last = None
    for r in range(rounds):
        risk_hit = False
        for desc, fn in routes:
            try:
                return fn()
            except RiskError as e:
                last = e
                risk_hit = True
                continue
            except Exception as e:
                last = e
        if not risk_hit:
            break
        if r + 1 < rounds:
            time.sleep(waits[min(r, len(waits) - 1)])
    raise last if last else RuntimeError(label + "：没有可用线路")


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
                                     headers=headers(), retries=0, timeout=12),
              endpoint="https://api.bilibili.com/x/web-interface/nav")
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


def _http(fn, endpoint=None):
    # Guard again after acquiring the lock: queued workers must not send after a denial.
    with _request_lock:
        _guard()
        try:
            result = fn()
            if isinstance(result, dict):
                if result.get("code") in (-412, -352, 412, 352):
                    check(result, "B站请求", endpoint)
                data = result.get("data")
                if result.get("v_voucher") or (isinstance(data, dict) and data.get("v_voucher")):
                    note_risk("B站返回 v_voucher：需要官方验证", endpoint)
                    raise RiskError(pause_message())
            return result
        except RiskError:
            raise
        except (RuntimeError, urllib.error.HTTPError) as e:
            if note_risk(e, endpoint):
                raise RiskError(pause_message()) from e
            raise


def get_signed(url, params, kind="www", referer=None, retries=1, timeout=12):
    _guard()
    h = headers(kind, referer)
    d = _http(lambda: get_json_strict(url, wbi_sign(params), headers=h, retries=retries, timeout=timeout),
              endpoint=url)
    if isinstance(d, dict) and d.get("code") == -403:
        wbi_key(force=True)
        d = _http(lambda: get_json_strict(url, wbi_sign(params), headers=h, retries=retries, timeout=timeout),
                  endpoint=url)
    return d


def _plain(url, params, kind="www", referer=None, retries=1, timeout=12):
    _guard()
    return _http(lambda: get_json_strict(url, params, headers=headers(kind, referer), retries=retries,
                                         timeout=timeout), endpoint=url)


# ---------------- 接口 ----------------
def clean_search_item(v):
    title = unescape(re.sub(r"<[^>]+>", "", v.get("title") or ""))
    return {"bvid": v.get("bvid"), "play": int(v.get("play") or 0), "like": int(v.get("like") or 0),
            "reply": int(v.get("video_review") or 0), "mid": str(v.get("mid") or ""),
            "up": v.get("author") or "", "tid": tid_of(v), "tname": v.get("typename") or "",
            "pubdate": int(v.get("pubdate") or 0), "title": title, "tag": v.get("tag") or "",
            "desc": v.get("description") or ""}


def search(kw, beg=None, end=None, page=1, order="pubdate", tids=4, page_size=30):
    """带签名的视频搜索。失败一定抛出来，不能当成 0 条。

    三条线路：wbi 签名接口 → 老的普通搜索接口 → 搜索网页。撞一条换一条。
    """
    p = {"search_type": "video", "keyword": kw, "order": order, "page": page, "page_size": page_size}
    if tids:
        p["tids"] = tids
    if beg:
        p["pubtime_begin_s"] = int(beg)
    if end:
        p["pubtime_end_s"] = int(end)

    def wbi_route():
        return check(get_signed("https://api.bilibili.com/x/web-interface/wbi/search/type", p, kind="search",
                                retries=0, timeout=15), "B站搜索(wbi)")

    def plain_route():
        return check(_plain("https://api.bilibili.com/x/web-interface/search/type", dict(p, page_size=page_size),
                            kind="search", retries=0, timeout=15), "B站搜索(普通)")

    def html_route():
        return _search_from_html(kw, page)

    data = _with_routes("B站搜索", [("wbi 接口", wbi_route), ("普通接口", plain_route), ("搜索网页", html_route)])
    return {"total": int(data.get("numResults") or 0), "pages": int(data.get("numPages") or 0),
            "list": [clean_search_item(v) for v in (data.get("result") or []) if v.get("bvid")]}


def _search_from_html(kw, page=1):
    """搜索网页兜底：直接从 search.bilibili.com 的页面里抠 BV 号和标题。"""
    q = urllib.parse.urlencode({"keyword": kw, "page": page, "order": "pubdate", "tids": 4})
    html = _http(lambda: get_text("https://search.bilibili.com/video?" + q, headers=headers("search"),
                                 retries=0, timeout=20),
                 endpoint="https://search.bilibili.com/video")
    got, seen = [], set()
    for m in re.finditer(r'"bvid":"(BV[0-9A-Za-z]{10})"', html or ""):
        bv = m.group(1)
        if bv in seen:
            continue
        seen.add(bv)
        tail = html[m.end():m.end() + 1200]
        title = re.search(r'"title":"(.*?)","', tail)
        play = re.search(r'"play":(\d+)', tail)
        got.append({"bvid": bv,
                    "title": unescape(re.sub(r"<[^>]+>", "", (title.group(1) if title else bv))),
                    "play": int(play.group(1)) if play else 0,
                    "author": "", "mid": "", "pubdate": 0, "tname": "", "description": ""})
    if not got:
        raise RuntimeError("搜索网页里没抠到 BV 号（页面可能改版）")
    return {"numResults": len(got), "numPages": 1, "result": got}


def view(bvid):
    def api_route():
        return check(_plain("https://api.bilibili.com/x/web-interface/view", {"bvid": bvid}, retries=0),
                     "B站视频详情")

    def html_route():
        return _view_from_html(bvid)

    return _with_routes("B站视频详情 " + bvid, [("详情接口", api_route), ("视频网页", html_route)])


def _view_from_html(bvid):
    """视频网页兜底：从 __INITIAL_STATE__ / meta 里抠播放数、标题、UP 主。"""
    html = _http(lambda: get_text("https://www.bilibili.com/video/%s/" % bvid,
                                  headers=headers(referer="https://www.bilibili.com/"),
                                  retries=0, timeout=20),
                 endpoint="https://www.bilibili.com/video/%s/" % bvid)
    if not html:
        raise RuntimeError("视频网页是空的")

    def grab(pattern, cast=str, default=None):
        m = re.search(pattern, html)
        if not m:
            return default
        try:
            return cast(m.group(1))
        except (TypeError, ValueError):
            return default

    title = grab(r'"title":"(.*?)"', default="") or grab(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"',
                                                         default="")
    title = unescape(re.sub(r"<[^>]+>", "", title or ""))
    out = {"bvid": bvid, "title": title,
           "stat": {"view": grab(r'"view":(\d+)', int, 0)},
           "owner": {"mid": grab(r'"mid":(\d+)', str, ""), "name": grab(r'"name":"(.*?)"', default="")},
           "pubdate": grab(r'"pubdate":(\d+)', int, 0),
           "desc": grab(r'"desc":"(.*?)"', default=""), "tname": ""}
    if not out["stat"]["view"] and not title:
        raise RuntimeError("视频网页里没抠到数据（页面可能改版）")
    return out


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
                        {"rid": rid, "ps": ps, "pn": pn, "type": 0}, timeout=15), "B站分区最新").get("archives") or []


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
    _guard()
    req = urllib.request.Request("https://api.bilibili.com/x/v1/dm/list.so?oid=%s" % cid, headers=headers())
    def read():
        with _get_opener().open(req, timeout=15) as r:
            return r.read()
    raw = _http(read)
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
    """三个大分区按人气排序翻页，每页 30 个直播间。每条线路撞墙就换下一条，不停。"""
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

            def v3():
                return check(_plain("https://api.live.bilibili.com/room/v3/area/getRoomList",
                                    params, kind="live", retries=0), "B站直播列表(v3)")

            def v1():
                return check(_plain("https://api.live.bilibili.com/room/v1/area/getRoomList",
                                    params, kind="live", retries=0), "B站直播列表(v1)")

            data = _with_routes("B站直播列表", [("v3 接口", v3), ("v1 接口", v1)])
            lst = data.get("list") if isinstance(data, dict) else data
            lst = lst or []
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
    """游戏区排行榜（近 3 天）+ 全站热门里游戏分区的视频 + 分区最新投稿。

    三条发现线路按顺序试，撞一条换一条，全撞墙也不会放弃这一轮。
    """
    warmup()
    out, errors = {}, []
    origins = []

    def ranking():
        data = check(_plain("https://api.bilibili.com/x/web-interface/ranking/v2", {"rid": 4, "type": "all"}),
                     "游戏区排行榜")
        return data.get("list") or []

    def popular(pages=5):
        got = []
        for pn in range(1, pages + 1):
            data = check(_plain("https://api.bilibili.com/x/web-interface/popular", {"ps": 50, "pn": pn}),
                         "全站热门")
            lst = data.get("list") or []
            got += [v for v in lst if "游戏" in (v.get("tname") or "") or tid_of(v) in GAME_TIDS]
            if not lst or data.get("no_more"):
                break
            time.sleep(0.6)
        return got

    def newest():
        return [v for v in newlist(1) if v.get("bvid")]

    for desc, fn in (("游戏区排行榜", ranking), ("全站热门", popular), ("游戏区最新投稿", newest)):
        try:
            for v in fn() or []:
                bv = v.get("bvid")
                if bv and bv not in out:
                    out[bv] = v
                    origins.append(desc)
            if out:
                break
        except RiskError as e:
            errors.append("%s：%s" % (desc, str(e)[:80]))
        except Exception as e:
            errors.append("%s：%s" % (desc, str(e)[:100]))

    if not out:
        raise RuntimeError("B站游戏视频一个都没拿到：" + "；".join(errors[:2]))
    origin = origins[0] if origins else "游戏排行／热门"
    return [dict(video_record(v), discovery_source=origin) for k, v in out.items() if k], errors


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
