"""Twice-daily, resumable collection of every visible release in the last 30 days."""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

from . import activity, config, db, history
from .sources import steam
from .net import get_json

TZ = timezone(timedelta(hours=8))
KEY = "steam_release_cycle"
_lock = threading.Lock()


def latest_slot(now):
    local = datetime.fromtimestamp(now, TZ)
    slot = local.replace(hour=20 if local.hour >= 20 else 8, minute=0, second=0, microsecond=0)
    if local.hour < 8:
        slot = (slot - timedelta(days=1)).replace(hour=20)
    return int(slot.timestamp())


def next_slot(now):
    return latest_slot(now + 12 * 3600)


def state():
    s = db.kv_get(KEY, {}) or {}
    public = {k: v for k, v in s.items() if k not in ("games", "last_page_ids")}
    return {**public, "timezone": "Asia/Shanghai", "hours": [8, 20],
            "next_slot": next_slot(time.time()), "window_days": 30,
            "total": len(s.get("games", {})),
            "done": sum(g.get("done", False) for g in s.get("games", {}).values()),
            "failed": sum(bool(g.get("error")) for g in s.get("games", {}).values())}


def tick(now=None):
    if not config.source_on("steam") or not _lock.acquire(blocking=False):
        return
    try:
        _tick(time.time() if now is None else now)
    finally:
        _lock.release()


def _tick(now):
    from . import collect
    s = db.kv_get(KEY, {}) or {}
    slot = latest_slot(now)
    if s.get("completed_slot", 0) >= slot:
        return
    if s.get("phase") == "complete" or not s.get("slot"):
        s = {"slot": slot, "started": int(now), "phase": "discover", "cursor": 0,
             "games": {}, "completed_slot": s.get("completed_slot", 0),
             "pages": 0, "error": ""}
    if s.get("retry_after", 0) > now:
        return
    s["heartbeat"] = int(now)
    try:
        if s["phase"] == "discover":
            d = get_json(history.SEARCH_URL,
                         {"infinite": 1, "start": s["cursor"], "count": 50,
                          "sort_by": "Released_DESC", "category1": 998,
                          "cc": config.STEAM_CC, "l": "english", "ndl": 1},
                         retries=0, timeout=25)
            if not isinstance(d, dict) or not d.get("success"):
                raise RuntimeError("Steam 发行列表返回失败")
            rows = history._parse_page(d.get("results_html") or "")
            if not rows:
                raise RuntimeError("Steam 发行页为空，无法确认已覆盖 30 天，稍后重试")
            page_ids = [r["appid"] for r in rows]
            if page_ids == s.get("last_page_ids"):
                raise RuntimeError("Steam 翻页返回重复内容，保留进度等待重试")
            cutoff = s["slot"] - 30 * 86400
            for row in rows:
                released = row.get("released")
                if released and cutoff <= released <= now:
                    s["games"].setdefault(str(row["appid"]), {**row, "done": False})
            s["pages"] += 1
            s["cursor"] += len(rows)
            s["last_page_ids"] = page_ids
            if any(r.get("released") and r["released"] < cutoff for r in rows):
                if not s["games"]:
                    raise RuntimeError("Steam 最近 30 天游戏为空，未计为成功")
                s["phase"] = "sample"
            s.update(error="", retry_after=0)
            db.kv_set(KEY, s)
            return
        pending = [(k, g) for k, g in s["games"].items()
                   if not g.get("done") and g.get("retry_after", 0) <= now]
        if not pending:
            if all(g.get("done") for g in s["games"].values()):
                s.update(phase="complete", completed_slot=s["slot"], finished=int(now), error="")
                collect._status("steam", True, len(s["games"]))
                db.log_run("collect:steam", now - s["started"], True,
                           "最近 30 天发行游戏完整批次：%d 款" % len(s["games"]))
                activity.say("Steam 早晚批次完成：%d 款" % len(s["games"]))
                from . import server
                server.REANALYZE.set()
            db.kv_set(KEY, s)
            return
        appid, game = min(pending, key=lambda row: row[1].get("attempts", 0))
        try:
            players = steam.current_players(appid)
            reviews = steam.review_total(appid)
            if players is None or reviews is None:
                raise RuntimeError("在线人数或评论数缺失")
            with db.session() as c:
                old = c.execute("SELECT extra FROM entities WHERE source='steam' AND entity=?",
                                (appid,)).fetchone()
            extra = json.loads(old[0]) if old else {}
            extra.update(release_ts=game["released"], type="game", coming_soon=False,
                         discovered_by="release_scan", lists=["最近30天发行"],
                         price_usd=game.get("price"))
            extra.pop("metrics_pending", None)
            collect._save("steam", int(now), [{"entity": appid, "name": game["name"],
                           "extra": extra, "metrics": {"players": players, "reviews": reviews}}])
            game.update(done=True, sampled=int(now), error="", retry_after=0)
            s.update(error="", last_sample=int(now))
        except Exception as e:
            game.update(attempts=game.get("attempts", 0) + 1,
                        error=str(e)[:250], retry_after=int(now + 1800))
            s["error"] = "%s：%s" % (game["name"], game["error"])
        s["retry_after"] = 0
        db.kv_set(KEY, s)
    except Exception as e:
        s.update(error=str(e)[:350], retry_after=int(now + 120))
        db.kv_set(KEY, s)
        activity.say("Steam 早晚批次等待重试：" + s["error"])


def loop(stop):
    while not stop.is_set():
        try:
            tick()
        except Exception as e:
            activity.say("Steam 批次出错：" + str(e)[:250])
        stop.wait(1)
