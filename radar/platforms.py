"""各国 / 地区常用的平台（v81，地图用；评分用其中的视频平台）。

四类：综合应用商店 / 小游戏平台 / 直播平台 / 视频平台。每个国家先看本国特例，再看所在地理区的特例，
都没有就用通用列表；页面上标明是哪一种。一个国家里的省 / 州跟国家一样。
图标（v83）：有 iOS 版的按 App Store 编号查（名字核对，查不到再按名字搜），浏览器直接加载苹果的图片地址，和游戏图标同一个来源；
App Store 里没有 App 的（安卓应用商店、网页游戏站）用官网首页声明的图标，存 radar_data/platform_icons。30 天更新一次。
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request

from . import config, db

CATS = [("store", "App stores"), ("mini", "Mini-game platforms"), ("live", "Live streaming"), ("video", "Video")]

# 键: (名字, 图标)。图标 = ("ios", App Store 编号或 None, 搜索词, 商店国家, 名字里要有的字) / ("site", 官网首页) / ("same", 另一个键)
# 有 iOS 版的一律走 App Store（和游戏图标同一个来源，浏览器直接加载苹果的图片地址）；
# 在 App Store 里没有 App 的（安卓应用商店、网页游戏站）只能用官网自己声明的图标。
APPS = {
    "appstore": ("App Store", ("ios", 1234793120, "App Store Connect", "us", "app store connect")),
    "googleplay": ("Google Play", ("site", "https://play.google.com/store/games")),
    "huawei": ("Huawei AppGallery", ("site", "https://appgallery.huawei.com/")),
    "xiaomi": ("Xiaomi GetApps", ("site", "https://app.mi.com/")),
    "oppo": ("OPPO App Market", ("site", "https://www.oppo.com/cn/")),
    "vivo": ("vivo App Store", ("site", "https://www.vivo.com.cn/")),
    "myapp": ("Tencent MyApp", ("site", "https://sj.qq.com/")),
    "taptap": ("TapTap", ("ios", None, "TapTap", "cn", "taptap")),
    "samsung": ("Galaxy Store", ("site", "https://galaxystore.samsung.com/")),
    "onestore": ("ONE store", ("site", "https://www.onestore.co.kr/")),
    "rustore": ("RuStore", ("site", "https://www.rustore.ru/")),
    "wechat": ("WeChat", ("ios", 414478124, "WeChat", "us", "wechat")),
    "wechat_mini": ("WeChat Mini Games", ("same", "wechat")),
    "channels": ("WeChat Channels", ("same", "wechat")),
    "douyin": ("Douyin", ("ios", 1142110895, "抖音", "cn", "抖音")),
    "douyin_mini": ("Douyin Mini Games", ("same", "douyin")),
    "douyin_live": ("Douyin Live", ("same", "douyin")),
    "kuaishou": ("Kuaishou", ("ios", 440948110, "快手", "cn", "快手")),
    "kuaishou_mini": ("Kuaishou Mini Games", ("same", "kuaishou")),
    "kuaishou_live": ("Kuaishou Live", ("same", "kuaishou")),
    "bilibili": ("Bilibili", ("ios", 736536022, "哔哩哔哩", "cn", "哔哩哔哩")),
    "bili_live": ("Bilibili Live", ("same", "bilibili")),
    "xiaohongshu": ("Xiaohongshu", ("ios", 741292507, "小红书", "cn", "小红书")),
    "qq_mini": ("QQ Mini Games", ("ios", 444934666, "QQ", "cn", "qq")),
    "alipay_mini": ("Alipay Mini Games", ("ios", 333206289, "支付宝", "cn", "支付宝")),
    "g4399": ("4399", ("site", "https://www.4399.com/")),
    "g7k7k": ("7k7k", ("site", "https://www.7k7k.com/")),
    "douyu": ("Douyu", ("ios", 863882795, "斗鱼", "cn", "斗鱼")),
    "huya": ("Huya", ("ios", None, "虎牙直播", "cn", "虎牙")),
    "youtube": ("YouTube", ("ios", 544007664, "YouTube", "us", "youtube")),
    "yt_live": ("YouTube Live", ("same", "youtube")),
    "yt_playables": ("YouTube Playables", ("same", "youtube")),
    "tiktok": ("TikTok", ("ios", 835599320, "TikTok", "us", "tiktok")),
    "tiktok_live": ("TikTok LIVE", ("same", "tiktok")),
    "instagram": ("Instagram", ("ios", 389801252, "Instagram", "us", "instagram")),
    "facebook": ("Facebook", ("ios", 284882215, "Facebook", "us", "facebook")),
    "fb_live": ("Facebook Gaming", ("same", "facebook")),
    "fb_instant": ("Facebook Instant Games", ("same", "facebook")),
    "snapchat": ("Snapchat", ("ios", 447188370, "Snapchat", "us", "snapchat")),
    "twitch": ("Twitch", ("ios", 460177396, "Twitch", "us", "twitch")),
    "kick": ("Kick", ("ios", None, "Kick Streaming", "us", "kick")),
    "kwai": ("Kwai", ("ios", None, "Kwai", "br", "kwai")),
    "poki": ("Poki", ("site", "https://poki.com/")),
    "crazygames": ("CrazyGames", ("site", "https://www.crazygames.com/")),
    "telegram": ("Telegram", ("ios", 686449807, "Telegram", "us", "telegram")),
    "tg_mini": ("Telegram Mini Apps", ("same", "telegram")),
    "vk": ("VK Video", ("ios", 564177498, "VK", "ru", "vk")),
    "vk_live": ("VK Video Live", ("same", "vk")),
    "vk_games": ("VK Play", ("site", "https://vkplay.ru/")),
    "yandex_games": ("Yandex Games", ("site", "https://yandex.ru/games/")),
    "rutube": ("RUTUBE", ("ios", None, "RUTUBE", "ru", "rutube")),
    "niconico": ("Niconico", ("ios", None, "niconico", "jp", "ニコニコ")),
    "nico_live": ("Niconico Live", ("same", "niconico")),
    "openrec": ("OPENREC", ("ios", None, "OPENREC", "jp", "openrec")),
    "mirrativ": ("Mirrativ", ("ios", None, "Mirrativ", "jp", "mirrativ")),
    "chzzk": ("CHZZK", ("ios", None, "치지직", "kr", "치지직")),
    "soop": ("SOOP", ("ios", None, "SOOP", "kr", "soop")),
    "loco": ("Loco", ("ios", None, "Loco", "in", "loco")),
    "rooter": ("Rooter", ("ios", None, "Rooter", "in", "rooter")),
    "moj": ("Moj", ("ios", None, "Moj", "in", "moj")),
    "josh": ("Josh", ("ios", None, "Josh", "in", "josh")),
}

GENERAL = {
    "store": ["googleplay", "appstore"],
    "mini": ["poki", "crazygames", "fb_instant", "yt_playables"],
    "live": ["twitch", "yt_live", "kick", "tiktok_live"],
    "video": ["youtube", "tiktok", "instagram", "facebook"],
}

# 地理区（worldmap.COUNTRIES 的第二列）的特例
REGION = {
    "东南亚": {"live": ["yt_live", "fb_live", "tiktok_live"], "video": ["youtube", "tiktok", "facebook", "instagram"]},
    "西亚": {"video": ["youtube", "tiktok", "snapchat", "instagram"]},
    "中亚": {"mini": ["yandex_games", "tg_mini", "poki"], "live": ["twitch", "yt_live", "vk_live"],
           "video": ["youtube", "tiktok", "instagram", "vk"]},
    "北非": {"live": ["yt_live", "tiktok_live", "twitch"], "video": ["youtube", "tiktok", "facebook", "instagram"]},
    "西非": {"live": ["yt_live", "tiktok_live", "twitch"], "video": ["youtube", "tiktok", "facebook", "instagram"]},
    "东非": {"live": ["yt_live", "tiktok_live", "twitch"], "video": ["youtube", "tiktok", "facebook", "instagram"]},
    "中非": {"live": ["yt_live", "tiktok_live", "twitch"], "video": ["youtube", "tiktok", "facebook", "instagram"]},
    "南部非洲": {"live": ["yt_live", "tiktok_live", "twitch"], "video": ["youtube", "tiktok", "facebook", "instagram"]},
}

# 国家 / 地区的特例
COUNTRY = {
    "cn": {"store": ["huawei", "xiaomi", "oppo", "vivo", "myapp", "appstore", "taptap"],
           "mini": ["wechat_mini", "douyin_mini", "kuaishou_mini", "qq_mini", "alipay_mini", "g4399", "g7k7k"],
           "live": ["douyin_live", "kuaishou_live", "bili_live", "douyu", "huya"],
           "video": ["douyin", "kuaishou", "bilibili", "xiaohongshu", "channels"]},
    "hk": {"live": ["yt_live", "twitch", "fb_live"], "video": ["youtube", "instagram", "facebook", "xiaohongshu"]},
    "mo": {"live": ["yt_live", "twitch", "fb_live"], "video": ["youtube", "instagram", "facebook", "xiaohongshu"]},
    "tw": {"live": ["twitch", "yt_live", "fb_live"], "video": ["youtube", "tiktok", "instagram", "facebook"]},
    "jp": {"live": ["yt_live", "twitch", "nico_live", "openrec", "mirrativ"],
           "video": ["youtube", "tiktok", "niconico", "instagram"]},
    "kr": {"store": ["googleplay", "appstore", "onestore", "samsung"], "live": ["chzzk", "soop", "yt_live"],
           "video": ["youtube", "tiktok", "instagram"]},
    "ru": {"store": ["googleplay", "appstore", "rustore"], "mini": ["yandex_games", "vk_games", "tg_mini"],
           "live": ["vk_live", "twitch", "yt_live"], "video": ["vk", "youtube", "rutube", "tiktok"]},
    "by": {"store": ["googleplay", "appstore", "rustore"], "mini": ["yandex_games", "vk_games", "tg_mini"],
           "live": ["vk_live", "twitch", "yt_live"], "video": ["youtube", "vk", "tiktok"]},
    "in": {"live": ["yt_live", "loco", "rooter"], "video": ["youtube", "instagram", "moj", "josh"]},
    "br": {"video": ["youtube", "tiktok", "instagram", "kwai"]},
    "mx": {"video": ["youtube", "tiktok", "facebook", "kwai"]},
}

ICON_DIR = os.path.join(config.DATA_DIR, "platform_icons")
REFRESH = 30 * 86400        # 工程参数：图标 30 天更新一次
_lock = threading.Lock()
_busy = {"on": False}


def _region_of(cc):
    try:
        from .worldmap import COUNTRIES
        return (COUNTRIES.get(cc) or (None, None))[1]
    except Exception:                                   # noqa: BLE001
        return None


def for_cc(cc):
    """{类别: {"apps": [键], "how": "country" / "region" / "general"}}"""
    cc = (cc or "").lower()
    reg = REGION.get(_region_of(cc)) or {}
    own = COUNTRY.get(cc) or {}
    out = {}
    for k, _ in CATS:
        if k in own:
            out[k] = {"apps": own[k], "how": "country"}
        elif k in reg:
            out[k] = {"apps": reg[k], "how": "region"}
        else:
            out[k] = {"apps": GENERAL[k], "how": "general"}
    return out


def video_keys(cc):
    return list(for_cc(cc)["video"]["apps"])


# ---------------- 图标 ----------------

def _file(key):
    return os.path.join(ICON_DIR, key + ".img")


def _fetch(url, timeout=20):
    from .net import _UA, _get_opener
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with _get_opener().open(req, timeout=timeout) as r:
        return r.read(), (r.headers.get("Content-Type") or "").split(";")[0]


def _art(r):
    return r.get("artworkUrl512") or r.get("artworkUrl100") or r.get("artworkUrl60")


def _ios_icon(tid, term, cc, want):
    """先按固定编号查（名字核对通过才用），查不到再按名字搜：名字完全一样 > 以它开头 > 名字更短。"""
    w = want.lower()
    if tid:
        try:
            body, _ = _fetch(f"https://itunes.apple.com/lookup?id={tid}&country={cc}")
            for r in (json.loads(body.decode("utf-8", "replace")) or {}).get("results") or []:
                if w in (r.get("trackName") or "").lower() and _art(r):
                    return _art(r)
        except Exception:                               # noqa: BLE001
            pass
    q = urllib.parse.urlencode({"term": term, "country": cc, "entity": "software", "limit": 25})
    body, _ = _fetch("https://itunes.apple.com/search?" + q)
    hits = [r for r in (json.loads(body.decode("utf-8", "replace")) or {}).get("results") or []
            if w in (r.get("trackName") or "").lower() and _art(r)]
    hits.sort(key=lambda r: ((r.get("trackName") or "").lower() != w, not (r.get("trackName") or "").lower().startswith(w),
                             len(r.get("trackName") or "")))
    return _art(hits[0]) if hits else None


def _img_type(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:4] == b"GIF8":
        return "image/gif"
    if b[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    head = b[:512].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in b[:2048].lower()):
        return "image/svg+xml"
    return None


def _site_icon(home):
    """官网首页里声明的图标，按 apple-touch-icon > 尺寸最大的 icon > /apple-touch-icon.png > /favicon.ico 依次试，只收真正的图片。"""
    import re
    cands = []
    try:
        body, _ = _fetch(home)
        html = body[:400000].decode("utf-8", "replace")
        for tag in re.findall(r"<link\b[^>]*>", html, re.I):
            rel = (re.search(r'rel\s*=\s*["\']?([^"\'>]+)', tag, re.I) or [None, ""])[1].lower()
            href = (re.search(r'href\s*=\s*["\']?([^"\' >]+)', tag, re.I) or [None, ""])[1]
            if "icon" not in rel or not href or href.startswith("data:"):
                continue
            sz = re.search(r'sizes\s*=\s*["\']?(\d+)', tag, re.I)
            rank = (2 if "apple-touch" in rel else 1, int(sz.group(1)) if sz else 0)
            cands.append((rank, urllib.parse.urljoin(home, href)))
    except Exception:                                   # noqa: BLE001
        pass
    cands.sort(key=lambda x: x[0], reverse=True)
    urls = [u for _, u in cands] + [urllib.parse.urljoin(home, "/apple-touch-icon.png"), urllib.parse.urljoin(home, "/favicon.ico")]
    last = "官网没有可用的图标"
    for u in urls:
        try:
            data, _ = _fetch(u)
            t = _img_type(data or b"")
            if t and len(data) >= 64:
                return data, t, u
            last = f"{u} 不是图片"
        except Exception as e:                          # noqa: BLE001
            last = f"{u}：{e}"[:200]
    raise RuntimeError(last)


def _src_key(key):
    seen = set()
    while key in APPS and APPS[key][1][0] == "same" and key not in seen:
        seen.add(key)
        key = APPS[key][1][1]
    return key


def _refresh_icons():
    os.makedirs(ICON_DIR, exist_ok=True)
    st = db.kv_get("platform_icons", {}) or {}
    now = int(time.time())
    for key, (_, spec) in APPS.items():
        if spec[0] == "same":
            continue
        s = st.get(key) or {}
        if _ready(key, s) and now - (s.get("ts") or 0) < REFRESH and s.get("src") == spec[0]:
            continue
        if not _ready(key, s) and s.get("err_ts") and now - s["err_ts"] < 86400:
            continue
        try:
            if spec[0] == "ios":
                url = _ios_icon(spec[1], spec[2], spec[3], spec[4])
                if not url:
                    raise RuntimeError("App Store 没搜到名字里带「%s」的应用" % spec[4])
                st[key] = {"ts": now, "src": "ios", "url": url}
            else:
                data, ctype, url = _site_icon(spec[1])
                with open(_file(key), "wb") as f:
                    f.write(data)
                st[key] = {"ts": now, "src": "site", "type": ctype, "url": url}
        except Exception as e:                          # noqa: BLE001
            st[key] = {**s, "err_ts": now, "error": str(e)[:200]}
        db.kv_set("platform_icons", st)
        time.sleep(1)


def _kick():
    with _lock:
        if _busy["on"]:
            return
        _busy["on"] = True

    def run():
        try:
            _refresh_icons()
        finally:
            _busy["on"] = False
    threading.Thread(target=run, name="platform_icons", daemon=True).start()


def _ready(key, s):
    if s.get("src") == "ios":
        return bool(s.get("url"))
    return s.get("src") == "site" and os.path.exists(_file(key))


def _icon_url(key, st):
    """ios：苹果的图片地址，浏览器直接加载（和游戏图标一样）；site：本机缓存的官网图标。"""
    k = _src_key(key)
    s = st.get(k) or {}
    if not _ready(k, s):
        return ""
    return s["url"] if s["src"] == "ios" else f"/map/picon/{k}?t={s.get('ts') or 0}"


def icon(key):
    """(图片字节, 类型) 或 None。"""
    k = _src_key(key)
    st = (db.kv_get("platform_icons", {}) or {}).get(k) or {}
    try:
        with open(_file(k), "rb") as f:
            return f.read(), st.get("type") or "image/png"
    except OSError:
        return None


def payload():
    _kick()
    st = db.kv_get("platform_icons", {}) or {}
    try:
        from .worldmap import COUNTRIES
        ccs = list(COUNTRIES)
    except Exception:                                   # noqa: BLE001
        ccs = list(COUNTRY)
    apps = {k: {"name": v[0], "icon": _icon_url(k, st)} for k, v in APPS.items()}
    return {"cats": CATS, "apps": apps, "by_cc": {cc: for_cc(cc) for cc in ccs},
            "errors": {k: v.get("error") for k, v in st.items() if v.get("error") and not _ready(k, v)}}
