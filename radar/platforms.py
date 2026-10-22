"""各国 / 地区常用的平台（v81，地图用；评分用其中的视频平台）。

四类：综合应用商店 / 小游戏平台 / 直播平台 / 视频平台。每个国家先看本国特例，再看所在地理区的特例，
都没有就用通用列表；页面上标明是哪一种。一个国家里的省 / 州跟国家一样。
图标：App Store 搜索（按名字核对）或网站图标，后台下载一次存 radar_data/platform_icons，30 天更新一次。
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request

from . import config, db

CATS = [("store", "App stores"), ("mini", "Mini-game platforms"), ("live", "Live streaming"), ("video", "Video")]

# 键: (名字, 图标)。图标 = ("ios", 搜索词, 商店国家, 名字里要有的字) / ("site", 图标网址) / ("same", 另一个键)
APPS = {
    "appstore": ("App Store", ("site", "https://www.apple.com/favicon.ico")),
    "googleplay": ("Google Play", ("site", "https://play.google.com/favicon.ico")),
    "huawei": ("Huawei AppGallery", ("site", "https://appgallery.huawei.com/favicon.ico")),
    "xiaomi": ("Xiaomi GetApps", ("site", "https://app.mi.com/favicon.ico")),
    "oppo": ("OPPO App Market", ("site", "https://www.oppo.com/favicon.ico")),
    "vivo": ("vivo App Store", ("site", "https://www.vivo.com.cn/favicon.ico")),
    "myapp": ("Tencent MyApp", ("site", "https://sj.qq.com/favicon.ico")),
    "taptap": ("TapTap", ("site", "https://www.taptap.cn/favicon.ico")),
    "samsung": ("Galaxy Store", ("site", "https://www.samsung.com/favicon.ico")),
    "onestore": ("ONE store", ("site", "https://www.onestore.co.kr/favicon.ico")),
    "rustore": ("RuStore", ("site", "https://www.rustore.ru/favicon.ico")),
    "wechat": ("WeChat", ("ios", "WeChat", "us", "wechat")),
    "wechat_mini": ("WeChat Mini Games", ("same", "wechat")),
    "channels": ("WeChat Channels", ("same", "wechat")),
    "douyin": ("Douyin", ("ios", "抖音", "cn", "抖音")),
    "douyin_mini": ("Douyin Mini Games", ("same", "douyin")),
    "douyin_live": ("Douyin Live", ("same", "douyin")),
    "kuaishou": ("Kuaishou", ("ios", "快手", "cn", "快手")),
    "kuaishou_mini": ("Kuaishou Mini Games", ("same", "kuaishou")),
    "kuaishou_live": ("Kuaishou Live", ("same", "kuaishou")),
    "bilibili": ("Bilibili", ("ios", "哔哩哔哩", "cn", "哔哩哔哩")),
    "bili_live": ("Bilibili Live", ("same", "bilibili")),
    "xiaohongshu": ("Xiaohongshu", ("ios", "小红书", "cn", "小红书")),
    "qq_mini": ("QQ Mini Games", ("ios", "QQ", "cn", "qq")),
    "alipay_mini": ("Alipay Mini Games", ("ios", "支付宝", "cn", "支付宝")),
    "g4399": ("4399", ("site", "https://www.4399.com/favicon.ico")),
    "g7k7k": ("7k7k", ("site", "https://www.7k7k.com/favicon.ico")),
    "douyu": ("Douyu", ("ios", "斗鱼", "cn", "斗鱼")),
    "huya": ("Huya", ("ios", "虎牙直播", "cn", "虎牙")),
    "youtube": ("YouTube", ("ios", "YouTube", "us", "youtube")),
    "yt_live": ("YouTube Live", ("same", "youtube")),
    "yt_playables": ("YouTube Playables", ("same", "youtube")),
    "tiktok": ("TikTok", ("ios", "TikTok", "us", "tiktok")),
    "tiktok_live": ("TikTok LIVE", ("same", "tiktok")),
    "instagram": ("Instagram", ("ios", "Instagram", "us", "instagram")),
    "facebook": ("Facebook", ("ios", "Facebook", "us", "facebook")),
    "fb_live": ("Facebook Gaming", ("same", "facebook")),
    "fb_instant": ("Facebook Instant Games", ("same", "facebook")),
    "snapchat": ("Snapchat", ("ios", "Snapchat", "us", "snapchat")),
    "twitch": ("Twitch", ("ios", "Twitch", "us", "twitch")),
    "kick": ("Kick", ("ios", "Kick", "us", "kick")),
    "kwai": ("Kwai", ("ios", "Kwai", "br", "kwai")),
    "poki": ("Poki", ("site", "https://poki.com/favicon.ico")),
    "crazygames": ("CrazyGames", ("site", "https://www.crazygames.com/favicon.ico")),
    "telegram": ("Telegram", ("ios", "Telegram", "us", "telegram")),
    "tg_mini": ("Telegram Mini Apps", ("same", "telegram")),
    "vk": ("VK Video", ("ios", "VK", "ru", "vk")),
    "vk_live": ("VK Video Live", ("same", "vk")),
    "vk_games": ("VK Play", ("site", "https://vkplay.ru/favicon.ico")),
    "yandex_games": ("Yandex Games", ("site", "https://yandex.ru/favicon.ico")),
    "rutube": ("RUTUBE", ("ios", "RUTUBE", "ru", "rutube")),
    "niconico": ("Niconico", ("ios", "niconico", "jp", "ニコニコ")),
    "nico_live": ("Niconico Live", ("same", "niconico")),
    "openrec": ("OPENREC", ("site", "https://www.openrec.tv/favicon.ico")),
    "mirrativ": ("Mirrativ", ("ios", "Mirrativ", "jp", "mirrativ")),
    "chzzk": ("CHZZK", ("ios", "치지직", "kr", "치지직")),
    "soop": ("SOOP", ("ios", "SOOP", "kr", "soop")),
    "loco": ("Loco", ("ios", "Loco", "in", "loco")),
    "rooter": ("Rooter", ("ios", "Rooter", "in", "rooter")),
    "moj": ("Moj", ("ios", "Moj", "in", "moj")),
    "josh": ("Josh", ("ios", "Josh", "in", "josh")),
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


def _ios_icon(term, cc, want):
    q = urllib.parse.urlencode({"term": term, "country": cc, "entity": "software", "limit": 10})
    body, _ = _fetch("https://itunes.apple.com/search?" + q)
    for r in (json.loads(body.decode("utf-8", "replace")) or {}).get("results") or []:
        if want.lower() in (r.get("trackName") or "").lower():
            return r.get("artworkUrl100") or r.get("artworkUrl60")
    return None


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
        if os.path.exists(_file(key)) and now - (s.get("ts") or 0) < REFRESH:
            continue
        if not os.path.exists(_file(key)) and s.get("err_ts") and now - s["err_ts"] < 86400:
            continue
        try:
            url = _ios_icon(spec[1], spec[2], spec[3]) if spec[0] == "ios" else spec[1]
            if not url:
                raise RuntimeError("App Store 没搜到名字里带「%s」的应用" % spec[3])
            data, ctype = _fetch(url)
            if not data or len(data) < 64:
                raise RuntimeError("图标是空的")
            with open(_file(key), "wb") as f:
                f.write(data)
            st[key] = {"ts": now, "type": ctype or "image/png", "url": url}
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
    have = {k for k in APPS if os.path.exists(_file(_src_key(k)))}
    try:
        from .worldmap import COUNTRIES
        ccs = list(COUNTRIES)
    except Exception:                                   # noqa: BLE001
        ccs = list(COUNTRY)
    return {"cats": CATS, "apps": {k: {"name": v[0], "icon": k in have} for k, v in APPS.items()},
            "by_cc": {cc: for_cc(cc) for cc in ccs},
            "errors": {k: v.get("error") for k, v in st.items() if v.get("error") and k not in have}}
