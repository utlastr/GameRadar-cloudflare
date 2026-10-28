# -*- coding: utf-8 -*-
"""社交平台：抖音 / 抖音指数 / 哔哩指数 / 快手 / 小红书 / TikTok + 关键词漏斗。

一个后台线程，一分钟看一次该干什么，一次只干一件（不同时开很多标签，免得被风控）：
  热榜到点了 → 读一个热榜 → 漏斗重新分类；
  否则 → 挑一个今天还没查满、离上次够久的平台，查漏斗队列里下一个没查过的词。
面板：各平台 → 视频平台（点分组标题是"视频平台大厅"，下面每个平台一页），接口 /api/social/*。
数据表都以 soc_ 开头（store.py）。
"""
import threading
import time
import traceback

from .. import db, sampling
from . import funnel, jobs, page, settings, store, validation

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
        try:
            _tick()
            _state["err"] = ""
        except Exception as e:                   # noqa: BLE001
            _state["err"] = "%s" % e
            traceback.print_exc()
        finally:
            _state["busy"] = ""
        RUN_NOW.wait(60)
        force = RUN_NOW.is_set()
        RUN_NOW.clear()
        if force:
            for job, *_ in jobs.HOT_JOBS:
                _forced.add(job)


_forced = set()


def _tick():
    settings.load(fill_missing=False)
    if not settings.get("SOCIAL_ENABLED"):
        return
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
    # 2. 热榜
    for job, plat, url_name, max_lists in jobs.HOT_JOBS:
        if not settings.platform_on(plat):
            continue
        if not jobs.api_ready(job):
            continue
        state = db.kv_get("sampling:social:" + job, {}) or {}
        if job not in _forced and state.get("completed_slot", 0) >= sampling.latest_slot(now):
            continue
        force = job in _forced
        _forced.discard(job)
        if not browser:
            store.status(job, False, err="现有浏览器的 CDP 9222 未连接")
            continue
        _state["busy"] = store.JOB_NAMES.get(job, job)
        sampling.run_due("social:" + job,
                         lambda: jobs.run_hot(job, plat, url_name, max_lists), force=force)
        funnel.refresh()
        _state["funnel_ts"] = time.time()
        return
    # 3. 关键词
    per_day = int(settings.get("SOCIAL_KW_PER_DAY") or 0)
    gap = max(3, int(settings.get("SOCIAL_KW_GAP_MINUTES") or 12)) * 60
    if per_day <= 0:
        return
    q = funnel.queue()
    if not q:
        return
    for job, plat, kind in jobs.KW_JOBS:
        if not settings.platform_on(plat):
            continue
        if not jobs.api_ready(job):
            continue
        if kind != "api" and not browser:
            continue
        if store.lookups_today(job) >= per_day:
            continue
        if now - store.last_lookup(job) < gap or now - store.last_try(job) < gap:
            continue
        term = None
        for t in q:
            if now - store.last_lookup(job, t) > 20 * 3600:
                term = t
                break
        if not term:
            continue
        _state["busy"] = "%s：%s" % (store.JOB_NAMES.get(job, job), term)
        jobs.run_kw(job, term)
        return


# ---------------- 给面板的接口 ----------------

def api_state():
    ensure()
    s = settings.load(fill_missing=False)
    st = {k: dict(v) for k, v in store.status_all().items()}
    for job, *_ in jobs.HOT_JOBS:
        item = st.setdefault(job, {})
        if item.get("validation_version") != validation.VERSION:
            item.update(ok_ts=None, count=None, note="旧结果未校验，已隔离")
    hot = {}
    for job, plat, _, _ in jobs.HOT_JOBS:
        h = store.latest_hot(job)
        h["name"] = store.JOB_NAMES[job]
        hot[job] = h
    return {
        "validation_version": validation.VERSION,
        "legacy_quarantined": store.legacy_counts(),
        "enabled": bool(s.get("SOCIAL_ENABLED")),
        "running": _state["started"], "busy": _state["busy"], "error": _state["err"],
        "browser": page.available(), "port": page.port(),
        "jobs": [{"job": j, "name": store.JOB_NAMES[j], "on": settings.platform_on(p),
                  **(st.get(j) or {}), "ready": jobs.api_ready(j),
                  "ready_reason": validation.reason(j)}
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
    }


def api_term(term):
    ensure()
    return {"validation_version": validation.VERSION, "term": term,
            "lookups": store.lookups_for(term), "series": store.series_for(term)}


def api_label(term, label):
    ensure()
    if label not in ("game", "gameplay", "not", "old", ""):
        return {"ok": False, "error": "label 只能是 game / gameplay / not / old"}
    return funnel.set_label(term, label)


def api_run():
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
