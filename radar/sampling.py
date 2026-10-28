"""Persistent 08:00 / 20:00 Asia/Shanghai sampling slots with startup catch-up."""
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from . import db

TZ = timezone(timedelta(hours=8))
_locks = {}
_guard = threading.Lock()


def latest_slot(now=None):
    local = datetime.fromtimestamp(time.time() if now is None else now, TZ)
    hour = 20 if local.hour >= 20 else 8
    slot = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if local.hour < 8:
        slot = (slot - timedelta(days=1)).replace(hour=20)
    return int(slot.timestamp())


def next_slot(now=None):
    local = datetime.fromtimestamp(time.time() if now is None else now, TZ)
    for hour in (8, 20):
        slot = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if slot > local:
            return int(slot.timestamp())
    return int((local + timedelta(days=1)).replace(
        hour=8, minute=0, second=0, microsecond=0).timestamp())


def run_due(name, fn, force=False, now=None):
    now = time.time() if now is None else now
    slot = latest_slot(now)
    with _guard:
        lock = _locks.setdefault(name, threading.Lock())
    if not lock.acquire(blocking=False):
        return None
    key = "sampling:" + name
    try:
        previous = db.kv_get(key, {}) or {}
        if not force and previous.get("completed_slot", 0) >= slot:
            return None
        if not force and previous.get("retry_after", 0) > now and previous.get("slot") == slot:
            return None
        state = {**previous, "slot": slot, "started": int(now), "running": True,
                 "pid": os.getpid(), "catch_up": now - slot > 60, "error": ""}
        db.kv_set(key, state)
        try:
            result = fn()
        except Exception as e:
            state.update(running=False, error=str(e)[:400], retry_after=int(now + 1800))
            db.kv_set(key, state)
            raise
        state.update(running=False, completed_slot=slot, finished=int(time.time()), retry_after=0)
        db.kv_set(key, state)
        return result
    finally:
        lock.release()


def summary():
    with db.session() as c:
        import json
        jobs = {r[0].split(":", 1)[1]: json.loads(r[1])
                for r in c.execute("SELECT k,v FROM kv WHERE k LIKE 'sampling:%' AND k!='sampling:platforms'")}
    for state in jobs.values():
        if state.get("running") and state.get("pid") != os.getpid():
            state.update(running=False, interrupted=True)
    return {"timezone": "Asia/Shanghai", "hours": [8, 20],
            "next": next_slot(), "jobs": jobs}
