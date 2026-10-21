"""ID-based TapTap priority sampling, independent of the full crawl."""
import threading
import time

from . import activity, config, db
from .sources import taptap

_started = False


def record(app, provenance="official_page"):
    """Store only observed counters from the requested official app record."""
    from . import collect
    entity = str(app["id"])
    if not entity.isdecimal():
        raise ValueError("TapTap ID 必须是数字")
    parsed = taptap.parse_app(app)
    missing = [name for key, name in (("downloads", "热度"), ("fans", "关注"))
               if parsed["metrics"].get(key) is None]
    if missing:
        raise RuntimeError(f"TapTap {entity} 页面上没读到{'和'.join(missing)}，本次不更新旧值")
    now = int(time.time())
    previous = db.load_entities().get(("tapmaker", entity), {}).get("extra") or {}
    parsed["extra"] = {**previous, **parsed["extra"], "track": True, "gone": False,
                       "detail_at": now, "fetch_attempt_at": now, "fetch_error": "",
                       "metric_provenance": provenance}
    collect._save("tapmaker", now, [parsed])
    return parsed


_TRANSIENT = ("timeout", "timed out", "连接", "连不上", "没读到", "验证", "空的", "SSLEOF")


def _sample(entity, tries=2):
    """采一个 ID。超时、页面没读到这类偶发失败，隔几秒再采一次（TapTap 有时候就是一回慢一回快）。"""
    last = None
    for attempt in range(tries):
        try:
            app = taptap.detail(entity, strict=True)
            if app == taptap.GONE:
                raise RuntimeError("官方页面返回404，保留旧采样，不当成热度归零")
            return record(app)
        except Exception as e:
            last = e
            if attempt + 1 >= tries or not any(t in str(e).lower() for t in _TRANSIENT):
                raise
            time.sleep(3)
    raise last


def targets():
    entities = db.load_entities()
    pinned = [str(e) for e in config.TAPTAP_WATCH_IDS if str(e).isdecimal()]
    recent = [(e, v) for (s, e), v in entities.items()
              if s == "tapmaker" and e.isdecimal() and not v["extra"].get("gone")
              and time.time() - (v["extra"].get("released_time") or 0) <= 30 * 86400]
    recent.sort(key=lambda ev: (ev[1]["extra"].get("fetch_attempt_at") or 0,
                               ev[1]["extra"].get("detail_at") or 0))
    return list(dict.fromkeys(pinned + [e for e, _ in recent]))[:config.TAPTAP_PRIORITY_PER_RUN]


def run_once():
    started = int(time.time())
    ids, results, errors = targets(), [], []
    db.kv_set("tap_priority", {"ts": started, "running": True, "ids": ids, "results": [], "errors": []})
    for entity in ids:
        try:
            parsed = _sample(entity)
            results.append({"id": entity, "name": parsed["name"], "metrics": parsed["metrics"],
                            "ts": parsed["extra"]["detail_at"]})
            activity.say(f"TapTap ID {entity}：热度 {parsed['metrics']['downloads']:,}，"
                         f"关注 {parsed['metrics']['fans']:,}")
        except Exception as e:
            error = str(e)[:180]
            errors.append({"id": entity, "error": error})
            with db.session() as c:
                import json
                row = c.execute("SELECT extra FROM entities WHERE source='tapmaker' AND entity=?",
                                (entity,)).fetchone()
                if row:
                    extra = json.loads(row[0] or "{}")
                    extra.update(fetch_attempt_at=int(time.time()), fetch_error=error)
                    c.execute("UPDATE entities SET extra=? WHERE source='tapmaker' AND entity=?",
                              (json.dumps(extra, ensure_ascii=False), entity))
            activity.say(f"TapTap ID {entity} 采样失败：{error}")
        db.kv_set("tap_priority", {"ts": started, "finished": int(time.time()), "running": True,
                                  "ids": ids, "results": results, "errors": errors})
    report = {"ts": started, "finished": int(time.time()), "running": False,
              "ids": ids, "results": results, "errors": errors}
    db.kv_set("tap_priority", report)
    db.log_run("collect:tap_priority", time.time() - started, not errors,
               f"{len(results)}/{len(ids)} 个ID成功；" + "；".join(f"{e['id']}：{e['error']}" for e in errors))
    return report


def start(stop_event=None):
    global _started
    if _started:
        return
    _started = True
    def loop():
        stop = stop_event or threading.Event()
        while not stop.is_set():
            if config.source_on("tapmaker"):
                try:
                    report = run_once()
                    if report["results"]:
                        from . import analyze
                        analyze.run(allow_network=False, send_notify=False, allow_ai=False)
                except Exception as e:
                    activity.say("TapTap 重点采样失败：" + str(e)[:200])
            stop.wait(max(5, config.TAPTAP_PRIORITY_MINUTES) * 60)
    threading.Thread(target=loop, name="taptap-priority", daemon=True).start()
