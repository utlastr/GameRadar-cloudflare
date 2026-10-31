"""Persisted top-100 alias batch scheduling. No browser work in state readers."""
import time

from .. import db
from . import settings, store

PLATFORMS = {"douyin_kw": "douyin", "tiktok_kw": "tiktok"}
PAUSE_KEY = "social_keyword_pauses"
RETRY_KEY = "social_keyword_retries"


def progress(now=None):
    now = time.time() if now is None else now
    snapshot = db.kv_get("radar_social_batch", {}) or {}
    created = snapshot.get("created") or 0
    queries = snapshot.get("queries") or []
    terms = [q["term"] for q in queries]
    states = {p: store.lookup_states(p, terms, created, 5 if p == "douyin_kw" else 0)
              for p in PLATFORMS}
    retries = db.kv_get(RETRY_KEY, {}) or {}
    retry_states = {}
    for job, retry in retries.items():
        if job in PLATFORMS and retry.get("created") == created:
            retry_states[job] = store.lookup_states(job, terms, retry["since"])
    pauses = db.kv_get(PAUSE_KEY, {}) or {}
    totals, rows, complete_terms = {}, [], set()
    for job in PLATFORMS:
        totals[job] = {"done": 0, "pending": 0, "failed": 0,
                       "paused": pauses.get(job)}
    for query in queries:
        term = query["term"]
        platforms = {}
        for job in PLATFORMS:
            state = states[job].get(term, {})
            if job in retry_states:
                state = {**state, "attempts": retry_states[job].get(term, {}).get("attempts", 0)}
            done = bool(state.get("ok"))
            failed = not done and state.get("attempts", 0) >= 3
            status = "done" if done else "failed" if failed else "pending"
            totals[job][status] += 1
            platforms[job] = {**state, "done": done, "failed": failed,
                              "state": status}
        if all(s["done"] for s in platforms.values()):
            complete_terms.add(term)
        rows.append({**query, "platforms": platforms})
    games = [{**g, "completed_terms": sum(t in complete_terms for t in g["terms"])}
             for g in snapshot.get("games", [])]
    done = sum(p["done"] for p in totals.values())
    failed = sum(p["failed"] for p in totals.values())
    total = len(terms) * len(PLATFORMS)
    complete = bool(total and done == total)
    waits = {}
    gap = max(30, int(settings.get("SOCIAL_BATCH_GAP_SECONDS") or 60))
    limit = int(settings.get("SOCIAL_BATCH_PER_DAY") or 0)
    for job, platform in PLATFORMS.items():
        if totals[job]["done"] == len(terms):
            continue
        if not settings.platform_on(platform):
            waits[job] = "disabled"
        elif job in pauses:
            waits[job] = pauses[job]["reason"]
        elif limit <= 0 or store.lookups_today(job) >= limit:
            waits[job] = "daily_limit"
        elif now < max(store.last_lookup(job), store.last_try(job)) + gap:
            waits[job] = "interval"
    return {"created": created or None, "game_count": len(games),
            "term_count": len(terms), "total": total, "done": done, "failed": failed,
            "complete": complete,
            "state": "complete" if complete else "partial" if failed else "pending",
            "platforms": totals, "games": games, "queries": rows, "waits": waits,
            "coverage": "captured_search_results",
            "gap_seconds": gap, "per_day": limit,
            "ranking": db.kv_get("social_batch_ranking", {}) or {}}


def next_query(state):
    """Try unseen aliases before retrying failures; honor platform pacing/quotas."""
    candidates = []
    for index, row in enumerate(state["queries"]):
        for job in PLATFORMS:
            status = row["platforms"][job]
            if job not in state["waits"] and not status["done"] and not status["failed"]:
                candidates.append((status.get("attempts", 0), index, job, row["term"]))
    if not candidates:
        return None
    _, _, job, term = min(candidates)
    return job, term


def pause_if_required(job):
    status = store.status_all().get(job) or {}
    error = status.get("error") or ""
    if not any(hint in error for hint in
               ("人机验证", "验证码", "要登录", "请登录", "captcha", "rate limit", "429")):
        return
    pauses = db.kv_get(PAUSE_KEY, {}) or {}
    pauses[job] = {"reason": error, "url": status.get("url"), "ts": int(time.time())}
    pauses[job]["target_id"] = status.get("target_id")
    db.kv_set(PAUSE_KEY, pauses)


def resume_verified_pages():
    from . import page
    pauses = db.kv_get(PAUSE_KEY, {}) or {}
    for job, pause in pauses.items():
        if job not in PLATFORMS or not pause.get("target_id"):
            continue
        try:
            cleared = page.verification_cleared(pause["target_id"], pause.get("url"))
        except Exception:
            cleared = False
        if cleared:
            resume(job)


def resume(job):
    if job not in PLATFORMS:
        return {"ok": False, "error": "Unknown keyword platform"}
    pauses = db.kv_get(PAUSE_KEY, {}) or {}
    pauses.pop(job, None)
    db.kv_set(PAUSE_KEY, pauses)
    return {"ok": True}


def retry_failed(job):
    if job not in PLATFORMS:
        return {"ok": False, "error": "Unknown keyword platform"}
    snapshot = db.kv_get("radar_social_batch", {}) or {}
    retries = db.kv_get(RETRY_KEY, {}) or {}
    retries[job] = {"created": snapshot.get("created"), "since": int(time.time()) + 1}
    db.kv_set(RETRY_KEY, retries)
    return {"ok": True}
