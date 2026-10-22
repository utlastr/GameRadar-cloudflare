"""B站：程序里唯一的 B站 客户端（v72 起直播、视频、机会雷达、成功案例都走这里）。

- 请求头、登录 Cookie、wbi 签名、风控暂停只有这一份。
- Cookie 顺序：settings.json 的 BILI_COOKIE → radar_data/bili_cookie.json（6 小时内）→ 已登录浏览器（CDP 9222 端口）。
  都没有就按访客请求（先打开一次 bilibili.com 拿 buvid3）。
- 任何一次返回 -412 / -352（风控）：所有 B站 请求一起暂停 30 分钟，连续 3 次暂停 2 小时。
- 游戏分区编号、书名号正则、名字归一化也只在这里定义一次。
"""
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
import zlib
from html import unescape

from .. import config, db
from ..net import _UA, _get_opener, get_json, get_json_strict, get_text

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
    if time.time() - _warm["ts"] < 3600:
        return
    try:
        get_text("https://www.bilibili.com/", retries=0)
        get_text("https://live.bilibili.com/", retries=0)
    except Exception:
        pass
    _warm["ts"] = time.time()


_warmup = warmup


# ---------------- 风控暂停（全程序共用） ----------------
def paused():
    return float(db.kv_get("bili_pause_until", 0) or 0) > time.time()


def pause_left():
    return max(0, int(float(db.kv_get("bili_pause_until", 0) or 0) - time.time()))


def note_risk(err):
    """风控错误：暂停 30 分钟；连续 3 次停 2 小时（工程参数）。返回是否属于风控。"""
    s = str(err)
    if not any(k in s for k in ("-412", "-352", "code=412", "code=352", "Error 412")):
        return False
    n = int(db.kv_get("bili_risk_hits", 0) or 0) + 1
    db.kv_set("bili_risk_hits", n)
    minutes = 120 if n >= 3 else 30
    db.kv_set("bili_pause_until", time.time() + minutes * 60)
    try:
        from .. import activity
        activity.say("B站风控（第 %d 次），所有 B站 请求暂停 %d 分钟" % (n, minutes))
    except Exception:
        pass
    return True


def reset_risk():
    if db.kv_get("bili_risk_hits", 0):
        db.kv_set("bili_risk_hits", 0)


def check(d, what):
    if not isinstance(d, dict):
        raise RuntimeError(f"{what}：返回的不是数据")
    code = d.get("code")
    if code not in (0, None):
        msg = f"{what}：B站返回 code={code} {d.get('message') or d.get('msg') or ''}"
        if code in (-412, -352, 412, 352):
            note_risk(msg)
            raise RiskError(msg + "（风控拦截，已暂停 B站 请求）")
        raise RuntimeError(msg)
    reset_risk()
    return d.get("data") or {}


_check = check


def _guard():
    if paused():
        raise RiskError("B站风控暂停中，还剩 %d 分钟" % (pause_left() // 60 + 1))


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
    d = get_json_strict("https://api.bilibili.com/x/web-interface/nav", headers=headers(), retries=1, timeout=12)
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


def _http(fn):
    try:
        return fn()
    except RiskError:
        raise
    except RuntimeError as e:
        if note_risk(e):
            raise RiskError(str(e)[:200] + "（风控拦截，已暂停 B站 请求）")
        raise


def get_signed(url, params, kind="www", referer=None, retries=1, timeout=12):
    _guard()
    h = headers(kind, referer)
    d = _http(lambda: get_json_strict(url, wbi_sign(params), headers=h, retries=retries, timeout=timeout))
    if isinstance(d, dict) and d.get("code") == -403:
        wbi_key(force=True)
        d = _http(lambda: get_json_strict(url, wbi_sign(params), headers=h, retries=retries, timeout=timeout))
    return d


def _plain(url, params, kind="www", referer=None, retries=1, timeout=12):
    _guard()
    return _http(lambda: get_json_strict(url, params, headers=headers(kind, referer), retries=retries,
                                         timeout=timeout))


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
    return check(_plain("https://api.bilibili.com/x/web-interface/view", {"bvid": bvid}, retries=0), "B站视频详情")


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
    with _get_opener().open(req, timeout=15) as r:
        raw = r.read()
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
            if page == 1 and pid == 2:
                data = check(get_json_strict(url, params, headers=headers("live"), retries=1), "B站直播列表")
            else:
                d = get_json(url, params, headers=headers("live"), retries=1)
                if not isinstance(d, dict) or d.get("code") not in (0, None):
                    break
                data = d.get("data") or {}
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
    warmup()
    out, errors = {}, []
    try:
        data = check(_plain("https://api.bilibili.com/x/web-interface/ranking/v2", {"rid": 4, "type": "all"}),
                     "游戏区排行榜")
        for v in data.get("list") or []:
            out[v.get("bvid")] = v
    except Exception as e:
        errors.append(str(e))
    for pn in range(1, 6):
        try:
            data = check(_plain("https://api.bilibili.com/x/web-interface/popular", {"ps": 50, "pn": pn}), "全站热门")
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
