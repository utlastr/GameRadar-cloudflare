# -*- coding: utf-8 -*-
"""社交平台（抖音 / 快手 / 小红书 / TikTok / 哔哩指数 / 抖音指数）的设置。

这些设置不放进 config.py（那个文件别的功能也在改），自己从 radar_data/settings.json 读：
文件里没有的项用这里的默认值；第一次运行会把缺的项补进 settings.json，方便你改。
改完右键托盘图标退出再打开生效。
"""
import json
import os
import threading

from .. import config

DEFAULTS = {
    # 总开关
    "SOCIAL_ENABLED": True,
    "SOCIAL_SCHEDULE_MODE": "halfday",
    "SOCIAL_MAX_WORKERS": 10,
    "SOCIAL_DISCOVERY_TERMS": [],
    # 你登录各平台用的浏览器的调试端口（"打开雷达专用浏览器"按钮开的就是这个端口）
    "SOCIAL_CDP_PORT": 9222,
    # 热榜多久读一次（分钟）
    "SOCIAL_HOT_MINUTES": 60,
    # 每个平台每天最多查几个关键词（查得越多越容易被风控）
    "SOCIAL_KW_PER_DAY": 10,
    # 同一个平台两次关键词查询至少隔几分钟
    "SOCIAL_KW_GAP_MINUTES": 12,
    "SOCIAL_KW_REFRESH_MINUTES": 360,
    "SOCIAL_SIGNAL_REFRESH_MINUTES": 15,
    "SOCIAL_REQUESTS_PER_DAY": 40,
    # 不管漏斗怎么判，每天都要查的词（游戏名）
    "SOCIAL_WATCH_TERMS": [],
    # 各平台开关：false 就不读
    "SOCIAL_PLATFORMS": {"douyin": True, "douyin_index": True, "bili_index": True,
                         "kuaishou": True, "xiaohongshu": True, "tiktok": True},
    # 判不出是不是游戏的词，交给 AI（要 settings.json 里有 AI_KEY）。一天跑一次，判过的词不再问；
    # 一次最多问几批（每批 40 个词）
    "SOCIAL_AI_BATCHES": 20,
    # 雷达专用浏览器没开时，雷达自己在后台（最小化）把它打开
    "SOCIAL_AUTO_BROWSER": True,
    # TikTok 创意中心看哪个国家
    "SOCIAL_TIKTOK_REGION": "US",
    # 网址模板（平台改版时可以在 settings.json 里改，{kw} 会替换成关键词）
    "SOCIAL_URLS": {},
    # 抖音开放平台（官方接口，审核通过后填）
    "DOUYIN_OPEN_CLIENT_KEY": "",
    "DOUYIN_OPEN_CLIENT_SECRET": "",
    # TikTok 商业内容接口 Commercial Content API（官方，免费申请，目前只有欧盟广告）
    "TIKTOK_CCAPI_CLIENT_KEY": "",
    "TIKTOK_CCAPI_CLIENT_SECRET": "",
    "TIKTOK_CCAPI_COUNTRIES": ["DE", "FR", "IT", "ES", "NL", "PL"],
}

URLS = {
    # 抖音
    "douyin_hot": "https://www.douyin.com/hot",
    "douyin_kw": "https://www.douyin.com/search/{kw}?type=video",
    # 抖音指数（原巨量算数）
    "douyin_index_hot": "https://trendinsight.oceanengine.com/arithmetic-index/hot",
    "douyin_index_kw": "https://trendinsight.oceanengine.com/arithmetic-index/analysis?keyword={kw}&appName=aweme",
    # 哔哩指数：关键词页的网址还不知道，留空就在首页的搜索框里输入
    "bili_index_home": "https://trends.bilibili.com/",
    "bili_index_kw": "",
    # 快手
    "kuaishou_hot": "https://www.kuaishou.com/?isHome=1",
    "kuaishou_kw": "https://www.kuaishou.com/search/video?searchKey={kw}",
    # 小红书
    "xiaohongshu_kw": "https://www.xiaohongshu.com/search_result?keyword={kw}&source=web_search_result_notes",
    # TikTok 创意中心（公开）
    "tiktok_hashtag": "https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/pc/en"
                      "?period=7&countryCode={region}",
}

_lock = threading.Lock()
_cache = {"data": None}


def _path():
    return getattr(config, "USER_SETTINGS", os.path.join(config.DATA_DIR, "settings.json"))


def _read():
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def load(fill_missing=True):
    """读一次设置；fill_missing=True 时把缺的项补进 settings.json（文件读得出来才补，格式错的文件不碰）。"""
    with _lock:
        d = _read()
        if fill_missing and os.path.exists(_path()) and d:
            miss = {k: v for k, v in DEFAULTS.items() if k not in d}
            if miss:
                d2 = dict(d)
                d2.update(miss)
                try:
                    with open(_path(), "w", encoding="utf-8") as f:
                        json.dump(d2, f, ensure_ascii=False, indent=2)
                except OSError:
                    pass
        out = dict(DEFAULTS)
        for k in DEFAULTS:
            if k in d:
                out[k] = d[k]
        if isinstance(out.get("SOCIAL_PLATFORMS"), dict):
            out["SOCIAL_PLATFORMS"] = {**DEFAULTS["SOCIAL_PLATFORMS"], **out["SOCIAL_PLATFORMS"]}
        _cache["data"] = out
        return out


def get(key):
    d = _cache["data"] or load(fill_missing=False)
    return d.get(key, DEFAULTS.get(key))


def platform_on(p):
    return bool((get("SOCIAL_PLATFORMS") or {}).get(p, True))


def url(name, **kw):
    over = get("SOCIAL_URLS") or {}
    tpl = over.get(name) if isinstance(over, dict) and over.get(name) is not None else URLS.get(name, "")
    if not tpl:
        return ""
    from urllib.parse import quote
    vals = {k: quote(str(v), safe="") for k, v in kw.items()}
    try:
        return tpl.format(**vals)
    except (KeyError, IndexError):
        return tpl
