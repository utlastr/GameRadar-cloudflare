# -*- coding: utf-8 -*-
"""每个平台怎么读。

两类：
- 热榜（每小时）：抖音指数热点榜、哔哩指数首页榜单、快手热榜、TikTok 创意中心热门话题。
  读到的词全部进漏斗（funnel.py）判断是不是游戏、新不新。
- 关键词（每天每个平台最多 SOCIAL_KW_PER_DAY 个，词来自漏斗筛出的新游戏和 SOCIAL_WATCH_TERMS）：
  抖音搜索、抖音指数逐日曲线、哔哩指数逐日曲线、快手搜索、小红书搜索；
  有官方密钥时：抖音开放平台视频搜索、TikTok 广告库。

浏览器那几项都在"雷达专用浏览器"（带调试端口、你登录过的）里打开一个后台标签读，读完关掉。
失败一律写进运行状态并写明原因，不编数据、不补零。
"""
import json
import random
import time

from .. import db
from ..net import HttpError, get_json, post_form, post_json
from . import extract, page, settings, store, validation

HOT_JOBS = [
    # job, 平台开关名, 网址名, 每份最多存几个榜单
    ("douyin_index_hot", "douyin_index", "douyin_index_hot", 6),
    ("bili_index_home", "bili_index", "bili_index_home", 8),
    ("kuaishou_hot", "kuaishou", "kuaishou_hot", 4),
    ("tiktok_hashtag", "tiktok", "tiktok_hashtag", 3),
]

KW_JOBS = [
    # job, 平台开关名, 类型
    ("douyin_kw", "douyin", "videos"),
    ("douyin_index_kw", "douyin_index", "series"),
    ("bili_index_kw", "bili_index", "series"),
    ("kuaishou_kw", "kuaishou", "videos"),
    ("xiaohongshu_kw", "xiaohongshu", "videos"),
    ("douyin_open", "douyin", "api"),
    ("tiktok_ads", "tiktok", "api"),
]


def _short_lists(found, max_lists):
    """Compatibility helper: accept only source-validated lists."""
    return [L for L in found.get("lists", [])
            if L.get("validation_version") == validation.VERSION][:max_lists]


def kind_of(job):
    return dict((j, k) for j, _, k in KW_JOBS).get(job)


def _why_empty(d, job, what):
    """页面没认出东西时，按页面文字猜原因（人机验证 / 要登录 / 打不开），都写上页面标题，方便查。"""
    title = (d.get("title") or "").strip()[:40]
    tail = "（页面标题「%s」，收到 %d 份数据；把 radar_data/social_debug/%s.json 发给助手）" % (
        title or "空", len(d.get("caps") or []), job)
    p = page.problem(d)
    if p:
        return p + tail
    return "页面打开了但没认出%s" % what + tail


def run_hot(job, platform, url_name, max_lists):
    if validation.reason(job):
        store.status(job, False, err=validation.reason(job))
        return 0
    u = settings.url(url_name, region=settings.get("SOCIAL_TIKTOK_REGION") or "US")
    if not u:
        store.status(job, False, err="没有网址（settings.json 的 SOCIAL_URLS 里可以填）")
        return 0
    try:
        d = page.open_capture(u, wait=9.0, scroll=1)
    except page.NoBrowser as e:
        store.status(job, False, err=str(e))
        return 0
    except Exception as e:                       # noqa: BLE001
        store.status(job, False, err="打开页面失败：%s" % e)
        return 0
    found = extract.find(d["caps"])
    store.debug_dump(job, extract.debug_digest(d, found))
    problem = page.problem(d)
    if problem:
        store.status(job, False, err=problem, url=d.get("url") or u)
        return 0
    lists = validation.hot_lists(found, job, d.get("url") or u)[:max_lists]
    n = store.save_hot(platform, job, lists)
    if n:
        store.status(job, True, n, note="%d 个已校验榜单" % len(lists),
                     validation_version=validation.VERSION)
    else:
        store.status(job, False, err=_why_empty(d, job, "榜单"), url=d.get("url") or u)
    return n


# ---------------- 关键词：浏览器 ----------------

def run_kw(job, term):
    store.status(job, False, err=validation.KEYWORD_REASON)
    return False


def _unverified_keyword_implementation(job, term):
    if job == "douyin_open":
        return _douyin_open(term)
    if job == "tiktok_ads":
        return _tiktok_ads(term)
    after = None
    if job == "bili_index_kw":
        u = settings.url("bili_index_kw", kw=term)
        if not u:
            u = settings.url("bili_index_home")
            after = page.type_search(term)
    else:
        u = settings.url(job, kw=term)
    if not u:
        store.status(job, False, err="没有网址")
        return False
    try:
        d = page.open_capture(u, wait=9.0, after=after, scroll=2 if kind_of(job) == "videos" else 0)
    except page.NoBrowser as e:
        store.status(job, False, err=str(e))
        return False
    except Exception as e:                       # noqa: BLE001
        store.status(job, False, err="打开页面失败：%s" % e)
        store.save_lookup(job, term, False, note=str(e))
        return False
    found = extract.find(d["caps"])
    store.debug_dump(job, extract.debug_digest(d, found))
    kind = kind_of(job)
    if kind == "series":
        series = [s for s in found["series"] if len(s["points"]) >= 7]
        if not series:
            note = page.problem(d) or "没认出逐日曲线（可能这个词没被收录）"
            store.save_lookup(job, term, False, note=note)
            store.status(job, False, err="「%s」：%s" % (term, _why_empty(d, job, "逐日曲线（可能这个词没被收录）")),
                         url=d.get("url") or u)
            return False
        store.save_series(job, term, series)
        summ = {"metrics": []}
        for s in series[:8]:
            vals = [v for _, v in s["points"]]
            last7 = vals[-7:]
            prev = vals[-37:-7] or vals[:-7]
            summ["metrics"].append({
                "metric": s["metric"], "days": len(vals), "from": s["points"][0][0], "to": s["points"][-1][0],
                "last": vals[-1], "avg7": sum(last7) / len(last7),
                "avg_prev30": (sum(prev) / len(prev)) if prev else None,
                "first30_avg": sum(vals[:30]) / min(30, len(vals)),
            })
        store.save_lookup(job, term, True, summ)
        store.status(job, True, len(series), note="「%s」%d 条曲线" % (term, len(series)))
        return True
    L = extract.best_video_list(found)
    if not L:
        note = page.problem(d) or "没认出视频列表"
        store.save_lookup(job, term, False, note=note)
        store.status(job, False, err="「%s」：%s" % (term, _why_empty(d, job, "视频列表")), url=d.get("url") or u)
        return False
    summ = extract.summarize_videos(L)
    store.save_lookup(job, term, True, summ)
    store.save_videos(dict((j, p) for j, p, _ in KW_JOBS).get(job, job), term, L["items"])
    store.status(job, True, summ["n"], note="「%s」%d 条" % (term, summ["n"]))
    return True


# ---------------- 关键词：抖音开放平台（官方） ----------------
# 文档：developer.open-douyin.com → OpenAPI 列表 → 搜索能力 → 抖音视频搜索（要申请"抖音视频垂搜（对外）"权限）

def _douyin_token():
    ck = settings.get("DOUYIN_OPEN_CLIENT_KEY")
    cs = settings.get("DOUYIN_OPEN_CLIENT_SECRET")
    if not ck or not cs:
        return None
    t = db.kv_get("social_douyin_token", {}) or {}
    if t.get("key") == ck and t.get("token") and t.get("exp", 0) > time.time() + 300:
        return t["token"]
    r = post_json("https://open.douyin.com/oauth/client_token/",
                  {"client_key": ck, "client_secret": cs, "grant_type": "client_credential"}, timeout=30)
    data = (r or {}).get("data") or {}
    tok = data.get("access_token")
    if not tok:
        raise RuntimeError("拿 client_token 失败：%s" % (data.get("description") or data.get("error_code") or r))
    db.kv_set("social_douyin_token", {"key": ck, "token": tok, "exp": time.time() + int(data.get("expires_in") or 7200)})
    return tok


def _device_id():
    d = db.kv_get("social_douyin_device")
    if not d:
        d = str(random.randint(10 ** 18, 9 * 10 ** 18))
        db.kv_set("social_douyin_device", d)
    return d


def _douyin_open(term):
    job = "douyin_open"
    try:
        tok = _douyin_token()
    except Exception as e:                       # noqa: BLE001
        store.status(job, False, err=str(e))
        return False
    if not tok:
        return False
    try:
        r = get_json("https://open.douyin.com/dy_open_api/v1/search/video/",
                     {"keyword": term, "count": 20, "device_id": _device_id(), "publish_time": 7, "sort_type": 1,
                      "cursor": 0},
                     headers={"access-token": tok, "content-type": "application/json"})
    except Exception as e:                       # noqa: BLE001
        store.status(job, False, err="请求失败：%s" % e)
        store.save_lookup(job, term, False, note=str(e))
        return False
    if not r or r.get("err_no") not in (0, None):
        msg = (r or {}).get("err_msg") or "返回为空"
        store.status(job, False, err="「%s」：%s（错误码 %s）" % (term, msg, (r or {}).get("err_no")))
        store.save_lookup(job, term, False, note=msg)
        return False
    inner = ((r.get("data") or {}).get("data") or r.get("data") or {})
    if inner.get("error_code") not in (0, None):
        msg = inner.get("description") or "出错"
        store.status(job, False, err="「%s」：%s（错误码 %s）" % (term, msg, inner.get("error_code")))
        store.save_lookup(job, term, False, note=msg)
        return False
    vids = inner.get("video_list") or []
    L = {"items": [{"rank": i + 1, "text": v.get("title") or "", "ts": v.get("create_time"), "url": v.get("link"),
                    "nums": {"digg_count": float((v.get("statistics") or {}).get("digg_count") or 0)}}
                   for i, v in enumerate(vids)]}
    summ = extract.summarize_videos(L)
    summ["window"] = "7 天内，按点赞排序（官方接口只给点赞数）"
    store.save_lookup(job, term, True, summ)
    store.save_videos("douyin", term, L["items"])
    store.status(job, True, len(vids), note="「%s」%d 条" % (term, len(vids)))
    return True


# ---------------- 关键词：TikTok 广告库（官方 Commercial Content API） ----------------
# 文档：developers.tiktok.com/doc/commercial-content-api-query-ads ；目前只有欧盟国家的广告

def _tiktok_token():
    ck = settings.get("TIKTOK_CCAPI_CLIENT_KEY")
    cs = settings.get("TIKTOK_CCAPI_CLIENT_SECRET")
    if not ck or not cs:
        return None
    t = db.kv_get("social_tiktok_token", {}) or {}
    if t.get("key") == ck and t.get("token") and t.get("exp", 0) > time.time() + 300:
        return t["token"]
    r = post_form("https://open.tiktokapis.com/v2/oauth/token/",
                  {"client_key": ck, "client_secret": cs, "grant_type": "client_credentials"})
    tok = (r or {}).get("access_token")
    if not tok:
        raise RuntimeError("拿 TikTok token 失败：%s" % ((r or {}).get("error_description") or r))
    db.kv_set("social_tiktok_token", {"key": ck, "token": tok, "exp": time.time() + int(r.get("expires_in") or 7200)})
    return tok


def _reach(s):
    x = extract.num(str(s or "").replace("+", ""))
    return x


def _tiktok_ads(term):
    job = "tiktok_ads"
    try:
        tok = _tiktok_token()
    except Exception as e:                       # noqa: BLE001
        store.status(job, False, err=str(e))
        return False
    if not tok:
        return False
    today = time.strftime("%Y%m%d")
    start = time.strftime("%Y%m%d", time.localtime(time.time() - 360 * 86400))
    fields = "ad.id,ad.first_shown_date,ad.last_shown_date,ad.status,ad.reach,advertiser.business_name"
    url = "https://open.tiktokapis.com/v2/research/adlib/ad/query/?fields=" + fields
    body = {"filters": {"ad_published_date_range": {"min": start, "max": today},
                        "country_code_list": list(settings.get("TIKTOK_CCAPI_COUNTRIES") or [])},
            "search_term": term[:50], "search_type": "exact_phrase", "max_count": 10}
    ads = []
    try:
        for _ in range(3):
            r = post_json(url, body, headers={"Authorization": "Bearer " + tok}, timeout=40)
            err = (r or {}).get("error") or {}
            if err.get("code") not in (None, "ok"):
                raise RuntimeError("%s：%s" % (err.get("code"), err.get("message")))
            data = (r or {}).get("data") or {}
            ads += data.get("ads") or []
            if str(data.get("has_more")).lower() != "true" or not data.get("search_id"):
                break
            body["search_id"] = data["search_id"]
            time.sleep(1.5)
    except (HttpError, RuntimeError) as e:
        store.status(job, False, err="「%s」：%s" % (term, e))
        store.save_lookup(job, term, False, note=str(e))
        return False
    firsts = [str((a.get("ad") or {}).get("first_shown_date") or "") for a in ads]
    lasts = [str((a.get("ad") or {}).get("last_shown_date") or "") for a in ads]
    reach = [_reach(((a.get("ad") or {}).get("reach") or {}).get("unique_users_seen")) for a in ads]
    advs = {}
    for a in ads:
        n = (a.get("advertiser") or {}).get("business_name") or "?"
        advs[n] = advs.get(n, 0) + 1
    summ = {"n": len(ads), "capped": len(ads) >= 30,
            "active": sum(1 for a in ads if str((a.get("ad") or {}).get("status")).lower() == "active"),
            "first_shown": min([f for f in firsts if f], default=None),
            "last_shown": max([x for x in lasts if x], default=None),
            "reach_sum": sum(x for x in reach if x) or None,
            "advertisers": sorted(advs.items(), key=lambda kv: -kv[1])[:5],
            "window": "近一年，欧盟广告，最多读 30 条"}
    store.save_lookup(job, term, True, summ)
    store.status(job, True, len(ads), note="「%s」%d 条广告" % (term, len(ads)))
    return True


def api_ready(job):
    if validation.reason(job):
        return False
    if job == "douyin_open":
        return bool(settings.get("DOUYIN_OPEN_CLIENT_KEY") and settings.get("DOUYIN_OPEN_CLIENT_SECRET"))
    if job == "tiktok_ads":
        return bool(settings.get("TIKTOK_CCAPI_CLIENT_KEY") and settings.get("TIKTOK_CCAPI_CLIENT_SECRET"))
    return True


def dumps(o):
    return json.dumps(o, ensure_ascii=False)
