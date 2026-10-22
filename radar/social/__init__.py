# -*- coding: utf-8 -*-
"""社交平台：抖音 / 抖音指数 / 哔哩指数 / 快手 / 小红书 / TikTok + 关键词漏斗。

半日轮次跨平台最多 10 路并行，同平台串行。禁止自动关键词搜索。
面板：各平台 → 视频平台（点分组标题是"视频平台大厅"，下面每个平台一页），接口 /api/social/*。
数据表都以 soc_ 开头（store.py）。
"""
import threading
import time
import traceback

from .. import db
from . import douyin, funnel, incremental, jobs, page, schedule, settings, store

SCHEMA = store.SCHEMA
RUN_NOW = threading.Event()
_state = {"started": False, "busy": "", "funnel_ts": 0, "ai_ts": 0, "err": ""}


def ensure():
    store.ensure()


def start(stop_event):
    settings.load(fill_missing=True)
    if not settings.get("SOCIAL_ENABLED"):
        return
    if _state["started"]:
        return
    _state["started"] = True
    if stop_event is None:
        stop_event = threading.Event()
    t = threading.Thread(target=_loop, args=(stop_event,), name="social", daemon=True)
    t.start()


def _loop(stop_event):
    try:
        ensure()
    except Exception as e:                       # noqa: BLE001
        _state["err"] = "建表失败：%s" % e
        return
    # 开机先等一分钟，让别的采集先跑
    if stop_event.wait(60):
        return
    while not stop_event.is_set():
        force = RUN_NOW.is_set()
        RUN_NOW.clear()
        if force:
            _state["poll_after"] = 0
            if settings.get("SOCIAL_SCHEDULE_MODE") != "halfday":
                _forced.update(job for job, *_ in jobs.HOT_JOBS)
        try:
            _tick()
            _state["err"] = ""
        except Exception as e:                   # noqa: BLE001
            _state["err"] = "%s" % e
            traceback.print_exc()
        finally:
            _state["busy"] = ""
        # Drain runnable platform queues without an extra minute between tasks.
        delay = 0 if _state.pop("drain", False) else 1
        if stop_event.wait(delay):
            return
        if delay == 0:
            continue


_forced = set()


def _tick():
    if (not RUN_NOW.is_set() and not _state.get("drain")
            and time.time() < _state.get("poll_after", 0)):
        return
    settings.load(fill_missing=False)
    if settings.get("SOCIAL_SCHEDULE_MODE") == "halfday":
        if not page.available() and settings.get("SOCIAL_AUTO_BROWSER"):
            if time.time() - _state.get("launch_ts", 0) > 1800:
                _state["launch_ts"] = time.time()
                page.launch_browser(background=True)
        _state["busy"] = schedule.tick()
        _state["drain"] = _state["busy"].startswith("并发采集")
        _state["poll_after"] = 0 if _state["drain"] else time.time() + 60
        return
    _legacy_tick()


def _legacy_tick():
    settings.load(fill_missing=False)
    now = time.time()
    # 1. 漏斗：30 分钟重算一次（只是本地整理，不花钱）
    if now - _state["funnel_ts"] > 1800:
        _state["busy"] = "整理关键词"
        funnel.refresh()
        _state["funnel_ts"] = now
    # AI 判词：一天一次。上次成功到现在不满 24 小时就不跑；判过的词记住，永远不再问。
    # 失败了隔 2 小时再试，成功一次就等到明天这个时候。
    last_ok = db.kv_get("social_ai_last", 0) or 0
    if now - last_ok >= 23.5 * 3600 and now - _state["ai_ts"] > 7200:
        _state["ai_ts"] = now
        _state["busy"] = "AI 判词（一天一次）"
        try:
            n = funnel.ai_round()
            db.kv_set("social_ai_last", int(time.time()))
            if n:
                funnel.refresh()
            store.status("funnel_ai", True, n, note="AI 判断了 %d 个新词，明天这个时候再判新出现的" % n)
        except Exception as e:                   # noqa: BLE001
            store.status("funnel_ai", False, err=str(e))

    browser = page.available()
    # 雷达专用浏览器没开就自己在后台开（最小化），不用你一直手动开着
    if not browser and settings.get("SOCIAL_AUTO_BROWSER") and now - _state.get("launch_ts", 0) > 1800:
        _state["launch_ts"] = now
        _state["busy"] = "在后台打开雷达专用浏览器"
        try:
            page.launch_browser(background=True)
        except Exception:                        # noqa: BLE001
            pass
        browser = page.available()
    # 2. 热榜
    hot_gap = max(15, int(settings.get("SOCIAL_HOT_MINUTES") or 60)) * 60
    for job, plat, url_name, max_lists in jobs.HOT_JOBS:
        if not settings.platform_on(plat):
            continue
        if job not in _forced and now - store.last_try(job) < hot_gap:
            continue
        _forced.discard(job)
        if not browser:
            store.status(job, False, err="雷达专用浏览器没开（各平台 → 视频平台大厅里点“打开雷达专用浏览器”）")
            continue
        _state["busy"] = store.JOB_NAMES.get(job, job)
        jobs.run_hot(job, plat, url_name, max_lists)
        funnel.refresh()
        _state["funnel_ts"] = time.time()
        return
    # No automatic keyword search, including the legacy scheduling mode.


# ---------------- 给面板的接口 ----------------

def api_state():
    ensure()
    s = settings.load(fill_missing=False)
    st = store.status_all()
    hot = {}
    for job, plat, _, _ in jobs.HOT_JOBS:
        h = store.latest_hot(job, 60)
        h["name"] = store.JOB_NAMES[job]
        hot[job] = h
    return {
        "enabled": bool(s.get("SOCIAL_ENABLED")),
        "running": _state["started"], "busy": _state["busy"], "error": _state["err"],
        "browser": page.available(), "port": page.port(),
        "jobs": [{"job": j, "name": store.JOB_NAMES[j], "on": settings.platform_on(p),
                  "ready": jobs.api_ready(j), **(st.get(j) or {})}
                 for j, p, _ in [(a, b, c) for a, b, c, _ in jobs.HOT_JOBS] + list(jobs.KW_JOBS)],
        "ai": st.get("funnel_ai") or {},
        "settings": {"per_day": s.get("SOCIAL_KW_PER_DAY"), "gap": s.get("SOCIAL_KW_GAP_MINUTES"),
                     "hot_minutes": s.get("SOCIAL_HOT_MINUTES"), "watch": s.get("SOCIAL_WATCH_TERMS"),
                     "douyin_open": jobs.api_ready("douyin_open"), "tiktok_ads": jobs.api_ready("tiktok_ads"),
                     "region": s.get("SOCIAL_TIKTOK_REGION")},
        "buckets": funnel.buckets(),
        "bucket_names": funnel.BUCKETS,
        "queue": funnel.queue()[:30],
        "hot": hot,
        "lookups": store.latest_lookups(80),
        "publications": incremental.report(),
        "douyin_games": douyin.report(include_growth=False),
        "schedule": schedule.state(),
    }


def api_term(term):
    ensure()
    return {"term": term, "lookups": store.lookups_for(term), "series": store.series_for(term)}


def api_label(term, label):
    ensure()
    if label not in ("game", "gameplay", "not", "old", ""):
        return {"ok": False, "error": "label 只能是 game / gameplay / not / old"}
    return funnel.set_label(term, label)


def api_run():
    if settings.get("SOCIAL_SCHEDULE_MODE") == "halfday":
        RUN_NOW.set()
        return {"ok": True, "note": "继续到期轮次；已成功任务不会重跑"}
    RUN_NOW.set()
    return {"ok": True, "note": "马上去读一遍热榜（一个一个来，几分钟内读完）"}


def api_browser():
    return page.launch_browser()


def http_get(h, path, q):
    """server.py 转过来的 GET。h 是请求处理对象（有 _send / _json）。"""
    try:
        if path == "/api/social/state":
            h._json(api_state())
        elif path == "/api/social/term":
            h._json(api_term(q.get("term", [""])[0]))
        else:
            h._send("not found", "text/plain", 404)
    except Exception as e:                       # noqa: BLE001
        h._json({"error": "社交平台出错：%s" % e}, 500)


def http_post(h, path, body):
    try:
        if path == "/api/social/label":
            h._json(api_label(str(body.get("term") or ""), str(body.get("label") or "")))
        elif path == "/api/social/run":
            h._json(api_run())
        elif path == "/api/social/browser":
            h._json(api_browser())
        elif path == "/api/social/open":
            url = str(body.get("url") or "")
            if not url.startswith("http"):
                h._json({"ok": False, "note": "网址不对"})
            else:
                h._json(page.show(url))
        elif path == "/api/social/watch":
            h._json(api_watch(str(body.get("term") or ""), bool(body.get("add", True))))
        else:
            h._json({"error": "unknown"}, 404)
    except Exception as e:                       # noqa: BLE001
        h._json({"error": "社交平台出错：%s" % e}, 500)


def api_watch(term, add=True):
    """在 settings.json 的 SOCIAL_WATCH_TERMS 里加/删一个词。"""
    import json
    import os
    term = (term or "").strip()
    if not term:
        return {"ok": False}
    p = settings._path()
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {"ok": False, "error": "settings.json 读不出来，没改"}
    w = [x for x in (d.get("SOCIAL_WATCH_TERMS") or []) if x != term]
    if add:
        w.append(term)
    d["SOCIAL_WATCH_TERMS"] = w
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    settings.load(fill_missing=False)
    return {"ok": True, "watch": w}
