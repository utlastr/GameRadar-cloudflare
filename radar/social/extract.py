# -*- coding: utf-8 -*-
"""从页面记下来的数据里，不靠写死接口格式，找出三种东西：

- 榜单：一串（≥3 个）对象，每个都有一段文字（词 / 标题 / 话题）和数字（热度、名次……）
- 曲线：一串（≥5 个）对象，每个都有日期和数值（指数逐日）
- 视频：榜单里带点赞 / 评论 / 播放 / 发布时间的那种

平台改了字段名也大多还能认出来；认不出来时，原始数据摘要会存进 radar_data/social_debug/，发给助手修。
"""
import re
import time
from urllib.parse import urlparse

TEXT_KEYS = ("word", "keyword", "key_word", "keywords", "query", "sentence", "hot_word", "show_word",
             "word_name", "keyword_name", "search_word", "hashtag_name", "hashtag", "topic", "topic_name",
             "display_title", "title", "caption", "desc", "name", "content", "label", "text")
DATE_KEYS = ("date", "datetime", "day", "dt", "time", "x", "timestamp", "stat_date", "p_date", "ts")
ID_LIKE = re.compile(r"(^|_)(id|uid|mid|aid|cid|sec_uid|item_id|aweme_id|photo_id|note_id|group_id)$|Id$", re.I)
_NUM_CN = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([万wW亿kK千]?)\s*\+?\s*$")


_JUNK_TEXT = re.compile(r"您|请输入|请选择|请填写|请问|满意|反馈|客服|二维码|联系方式|微信号|官网首页|登录|注册|下载APP|下载 APP|"
                        r"隐私|用户协议|关于我们|帮助中心|退出登录|上一页|下一页|查看更多|全部分类|^其他")
_JUNK_SRC = re.compile(r"survey|questionnaire|feedback|wenjuan|nps|setting|config|i18n|locale|abtest|ab_test|slardar|"
                       r"monitor|/report|/track|menu|captcha|verify", re.I)


def junk_text(t):
    """问卷题目、菜单、按钮之类，不是热词。"""
    return bool(_JUNK_TEXT.search(t or "") or re.search(
        r"^middleware_|^@(?:douyinfe|ies)/|^webpack|^__|^https?://", t or "", re.I))


def junk_src(src):
    """来源网址像问卷 / 配置 / 埋点的数据，不当榜单。只看 fetch / xhr 的网址（parse 没有网址）。"""
    s = str(src or "")
    if not (s.startswith("fetch:") or s.startswith("xhr:")):
        return False
    return bool(_JUNK_SRC.search(s.split("?")[0]))


def num(v):
    """数字或"1.2万""3.4w""5k"这类写法 → float；不是数字返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = _NUM_CN.match(v.replace(",", ""))
        if not m:
            return None
        x = float(m.group(1))
        u = m.group(2)
        return x * {"万": 1e4, "w": 1e4, "W": 1e4, "亿": 1e8, "k": 1e3, "K": 1e3, "千": 1e3}.get(u, 1)
    return None


def _flat(d, prefix="", depth=0, out=None):
    """对象摊平成 {"a.b": 值}（最多三层），列表只看它有多长。"""
    if out is None:
        out = {}
    for k, v in d.items():
        key = prefix + str(k)
        if isinstance(v, dict) and depth < 3:
            _flat(v, key + ".", depth + 1, out)
        elif isinstance(v, list):
            out[key + "#len"] = len(v)
            if v and isinstance(v[0], (str, int, float)) and len(v) <= 10:
                out[key] = v
        else:
            out[key] = v
    return out


def _last(k):
    return k.rsplit(".", 1)[-1]


def _text_of(f):
    best = None
    for tk in TEXT_KEYS:
        for k, v in f.items():
            if _last(k).lower() == tk and isinstance(v, str):
                s = v.strip()
                if 1 <= len(s) <= 200:
                    return s, k
                if best is None and s:
                    best = (s[:200], k)
    return best or (None, None)


def _ts_of(f):
    """发布时间（秒）。字段名里带 time / date / pub 的 10 位或 13 位数字。"""
    for k, v in f.items():
        lk = _last(k).lower()
        if not any(w in lk for w in ("time", "date", "pub", "created")):
            continue
        x = num(v)
        if x is None:
            continue
        if 1.2e9 < x < 2.2e9:
            return int(x)
        if 1.2e12 < x < 2.2e12:
            return int(x / 1000)
    return None


def _nums(f, text_key):
    out = {}
    for k, v in f.items():
        if k == text_key or k.endswith("#len"):
            continue
        if ID_LIKE.search(_last(k)):
            continue
        x = num(v)
        if x is None:
            continue
        out[k] = x
    return out


def _walk(o, path, found, depth=0):
    if depth > 9:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            _walk(v, path + "." + str(k), found, depth + 1)
    elif isinstance(o, list):
        if len(o) >= 3 and sum(1 for x in o if isinstance(x, dict)) >= 0.8 * len(o):
            found.append((path, o))
        for i, v in enumerate(o[:60]):
            if isinstance(v, (dict, list)):
                _walk(v, path + "[]", found, depth + 1)


def _date_of(f):
    for k, v in f.items():
        lk = _last(k).lower()
        if lk not in DATE_KEYS:
            continue
        if isinstance(v, str):
            m = re.match(r"^(\d{4})[-/]?(\d{2})[-/]?(\d{2})", v.strip())
            if m:
                return "%s-%s-%s" % m.groups(), k
        x = num(v)
        if x is not None:
            if 1.2e12 < x < 2.2e12:
                x /= 1000
            if 1.2e9 < x < 2.2e9:
                return time.strftime("%Y-%m-%d", time.localtime(x)), k
            if 19000101 <= x <= 21001231:
                s = str(int(x))
                return "%s-%s-%s" % (s[:4], s[4:6], s[6:8]), k
    return None, None


def find(caps):
    """返回 {"lists": [...], "series": [...]}。

    lists: [{"path","src","items":[{"rank","text","nums","ts","raw_keys"}]}]
    series: [{"path","src","metric","points":[[day, value], ...]}]
    """
    lists, series = [], []
    seen = set()
    for c in caps:
        found = []
        _walk(c.get("obj"), "$", found)
        for path, arr in found:
            flats = [_flat(x) for x in arr if isinstance(x, dict)]
            # 曲线
            dated = []
            dkey = None
            for f in flats:
                d, dk = _date_of(f)
                if d:
                    dated.append((d, f))
                    dkey = dkey or dk
            if len(dated) >= 5 and len(dated) >= 0.8 * len(flats):
                metrics = {}
                for d, f in dated:
                    for k, x in _nums(f, dkey).items():
                        if _last(k).lower() in DATE_KEYS:
                            continue
                        metrics.setdefault(k, []).append([d, x])
                for k, pts in metrics.items():
                    if len(pts) >= 5:
                        sig = ("s", path, k, pts[0][0], pts[-1][0], len(pts))
                        if sig in seen:
                            continue
                        seen.add(sig)
                        series.append({"path": path, "src": c.get("src"), "metric": k, "points": pts})
                continue
            # 榜单
            items = []
            for i, f in enumerate(flats):
                t, tk = _text_of(f)
                if not t:
                    continue
                items.append({"rank": i + 1, "text": t, "text_key": tk, "nums": _nums(f, tk), "ts": _ts_of(f),
                              "url": _url_of(f)})
            if len(items) >= 3 and len(items) >= 0.6 * len(flats):
                sig = ("l", path, items[0]["text"], items[-1]["text"], len(items))
                if sig in seen:
                    continue
                seen.add(sig)
                lists.append({"path": path, "src": c.get("src"), "items": items})
    return {"lists": lists, "series": series}


def _url_of(f):
    for k, v in f.items():
        if isinstance(v, str) and v.startswith("http") and any(w in _last(k).lower() for w in ("url", "link", "share")):
            if not any(v.lower().split("?")[0].endswith(e) for e in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".image")):
                return v[:300]
    return None


# ---------------- 视频指标 ----------------
_LIKE = re.compile(r"digg|like|liked|praise|点赞", re.I)
_CMT = re.compile(r"comment|reply|评论", re.I)
_SHARE = re.compile(r"share|forward|转发|分享", re.I)
_FAV = re.compile(r"collect|fav|收藏", re.I)
_PLAY = re.compile(r"play|view|播放", re.I)


def video_stats(item):
    """从一个榜单项里认出 点赞 / 评论 / 转发 / 收藏 / 播放。认不出的项为 None。"""
    out = {"likes": None, "comments": None, "shares": None, "favs": None, "plays": None}
    for k, x in (item.get("nums") or {}).items():
        lk = _last(k)
        if "#" in lk or x < 0:
            continue
        for key, rx in (("likes", _LIKE), ("comments", _CMT), ("shares", _SHARE), ("favs", _FAV), ("plays", _PLAY)):
            if rx.search(lk) and (out[key] is None or x > out[key]):
                if key == "plays" and any(w in lk.lower() for w in ("duration", "time", "type", "mode")):
                    continue
                out[key] = x
                break
    return out


def best_video_list(found):
    """在找到的榜单里挑最像"视频列表"的那一串（带点赞数的项最多的）。"""
    best, score = None, 0
    for L in found["lists"]:
        n = sum(1 for it in L["items"] if video_stats(it)["likes"] is not None)
        if n > score:
            best, score = L, n
    return best if score >= 3 else None


def video_items(caps, platform):
    """Require content IDs; arbitrary numeric lists and albums are not videos."""
    out = {}
    keys = {"douyin": ("aweme_id",), "kuaishou": ("photo_id", "photoId"),
            "xiaohongshu": ("note_id", "noteId")}.get(platform, ())

    def visit(obj, depth=0):
        if depth > 18:
            return
        if isinstance(obj, list):
            for value in obj:
                visit(value, depth + 1)
        elif isinstance(obj, dict):
            record = obj
            if platform == "kuaishou" and isinstance(obj.get("photo"), dict):
                record = dict(obj["photo"])
                record["photo_id"] = record.get("id")
                record["author"] = obj.get("author") or {}
                record["cover"] = record.get("coverUrl")
                record["publish_time"] = record.get("timestamp")
            if platform == "xiaohongshu" and isinstance(obj.get("note_card"), dict):
                record = dict(obj["note_card"])
                record["note_id"] = obj.get("id") or obj.get("note_id")
            vid = next((str(record[k]) for k in keys if record.get(k)), None)
            if vid and re.fullmatch(r"[0-9A-Za-z_-]{5,80}", vid):
                f = _flat(record)
                title, tk = _text_of(f)
                if title and any(record.get(k) for k in
                                 ("statistics", "interact_info", "video", "images", "caption", "cover")):
                    user = record.get("author") or record.get("user") or {}
                    user = user if isinstance(user, dict) else {}
                    roots = {"douyin": "https://www.douyin.com/video/",
                             "kuaishou": "https://www.kuaishou.com/short-video/",
                             "xiaohongshu": "https://www.xiaohongshu.com/explore/"}
                    out[vid] = {"vid": vid, "text": title, "nums": _nums(f, tk),
                                "ts": _publish_ts(record), "url": roots[platform] + vid,
                                "author_id": str(user.get("uid") or user.get("user_id") or
                                                 user.get("userId") or user.get("id") or ""),
                                "description": str(record.get("desc") or record.get("caption") or title)}
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    visit(value, depth + 1)

    for cap in caps:
        if not junk_src(cap.get("src")):
            visit(cap.get("obj"))
    return list(out.values())


def _publish_ts(record):
    # Nested author dates and request timestamps are not publication dates.
    for k in ("create_time", "publish_time", "pubdate"):
        value = num(record.get(k))
        if value is not None:
            if 1.2e12 < value < 2.2e12:
                value /= 1000
            if 1.2e9 < value < 2.2e9:
                return int(value)
    return None


def stable_video_id(platform, item):
    """Reject albums, CDN images and unstable title-based identifiers."""
    u = urlparse(item.get("url") or "")
    rules = {"douyin": ("douyin.com", r"/video/(\d+)"),
             "xiaohongshu": ("xiaohongshu.com", r"/explore/([0-9A-Za-z_-]+)"),
             "kuaishou": ("kuaishou.com", r"/short-video/([0-9A-Za-z_-]+)")}
    if platform not in rules:
        return None
    host, pattern = rules[platform]
    if u.hostname != host and not (u.hostname or "").endswith("." + host):
        return None
    m = re.fullmatch(pattern + r"/?", u.path)
    return m.group(1) if m else None


def douyin_cards(cards):
    """Rendered video links are usable evidence; rounded relative dates are not exact timestamps."""
    out = {}
    for card in cards:
        vid = stable_video_id("douyin", card)
        if not vid or not card.get("title"):
            continue
        value = num(card.get("likes"))
        out[vid] = {"vid": vid, "url": card["url"].split("?")[0], "text": card["title"],
                    "ts": None, "author_id": "", "author": card.get("author") or "",
                    "publication_label": card.get("age"),
                    "nums": {"digg_count": value} if value is not None else {}}
    return list(out.values())


def summarize_videos(L, now=None):
    """视频列表 → 一行摘要：条数、总点赞、最高点赞、7 天内发布条数、最新发布时间。"""
    now = now or time.time()
    vids = []
    for it in L["items"]:
        st = video_stats(it)
        vids.append({"title": it["text"][:120], "ts": it.get("ts"), "url": it.get("url"), **st})
    likes = [v["likes"] for v in vids if v["likes"] is not None]
    plays = [v["plays"] for v in vids if v["plays"] is not None]
    recent = [v for v in vids if v["ts"] and now - v["ts"] <= 7 * 86400]
    return {
        "n": len(vids),
        "likes_sum": sum(likes) if likes else None,
        "likes_max": max(likes) if likes else None,
        "plays_sum": sum(plays) if plays else None,
        "n_7d": len(recent) if any(v["ts"] for v in vids) else None,
        "likes_7d": sum(v["likes"] or 0 for v in recent) if recent else None,
        "latest_ts": max((v["ts"] for v in vids if v["ts"]), default=None),
        "top": sorted(vids, key=lambda v: -(v["likes"] or 0))[:5],
    }


def debug_digest(d, found):
    """存进调试文件的摘要：页面标题、文字开头、每份数据的来源和结构、认出了什么。"""
    caps = []
    for c in d.get("caps") or []:
        o = c.get("obj")
        keys = list(o.keys())[:20] if isinstance(o, dict) else ("list[%d]" % len(o) if isinstance(o, list) else "")
        caps.append({"src": c.get("src"), "n": c.get("n"), "keys": keys})
    return {
        "ts": int(time.time()), "url": d.get("url"), "title": d.get("title"),
        "text_head": (d.get("text") or "")[:2500],
        "caps": caps[-500:],
        "lists": [{"path": L["path"], "src": L["src"], "n": len(L["items"]),
                   "first": [{"text": it["text"][:60], "nums": dict(list(it["nums"].items())[:8])}
                             for it in L["items"][:3]]} for L in found["lists"][:40]],
        "series": [{"path": s["path"], "src": s["src"], "metric": s["metric"], "n": len(s["points"]),
                    "first": s["points"][:2], "last": s["points"][-2:]} for s in found["series"][:40]],
    }
