"""Bounded Steam discovery and real player sampling, independent of slow crawls."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import activity, config, db, sampling
from .sources import steam

_started = False
_run_lock = threading.Lock()


def _sample(app):
    metrics, errors = {}, []
    for label, fetch in (
            ("players", lambda: steam.current_players(app["appid"], retries=0, timeout=10)),
            ("reviews", lambda: steam.review_summary(app["appid"], retries=0, timeout=10))):
        try:
            value = fetch()
            if label == "players" and value is not None:
                if value == 0:
                    with db.session() as c:
                        last = c.execute("SELECT value FROM metrics WHERE source='steam' "
                                         "AND entity=? AND metric='players' ORDER BY ts DESC LIMIT 1",
                                         (str(app["appid"]),)).fetchone()
                    if last and last[0] >= 100:
                        confirm = fetch()
                        if confirm is None:
                            errors.append("players: anomalous zero could not be confirmed")
                            continue
                        value = confirm
                metrics["players"] = value
            elif label == "reviews" and value:
                metrics.update(value)
            else:
                errors.append(label + ": missing")
        except Exception as e:
            errors.append(label + ": " + str(e)[:160])
    return app, metrics, errors, int(time.time())


def _targets(trending, releases, now, full=False):
    # Reserve half the batch for rotating tracked games, not just today's tab.
    releases = releases if full else releases[:max(1, config.STEAM_PRIORITY_PER_RUN // 2)]
    candidates = {str(a["appid"]): a for a in trending + releases}
    with db.session() as c:
        rows = c.execute(
            """SELECT entity,name FROM entities WHERE source='steam'
            AND COALESCE(json_extract(extra,'$.coming_soon'),0)=0
            AND COALESCE(json_extract(extra,'$.type'),'game')='game'
            AND (json_extract(extra,'$.release_ts') BETWEEN ? AND ?
                 OR json_extract(extra,'$.steam_priority_at')>?)
            ORDER BY COALESCE(json_extract(extra,'$.steam_priority_at'),0),last_seen
            LIMIT ?""", (now - config.STEAM_DISCOVERY_DAYS * 86400, now,
                         now - 7 * 86400, 100000 if full else config.STEAM_PRIORITY_PER_RUN)).fetchall()
    for entity, name in rows:
        if entity.isdecimal():
            candidates.setdefault(entity, {"appid": int(entity), "name": name})
    return list(candidates.values()) if full else list(candidates.values())[:max(1, config.STEAM_PRIORITY_PER_RUN)]


def _record(app, metrics, ts, errors=None):
    from . import collect
    entity = str(app["appid"])
    # Merge at write time so another sampler's metadata is not overwritten.
    with db.session() as c:
        old = c.execute("SELECT name,extra FROM entities WHERE source='steam' AND entity=?",
                        (entity,)).fetchone()
        extra = json.loads(old["extra"] or "{}") if old else {}
        name = (old["name"] if old else None) or app.get("name") or entity
        extra.update(steam_priority_at=ts, url=f"https://store.steampowered.com/app/{entity}/")
        extra.update(fetch_error="; ".join(errors or []), detail_attempt_at=ts)
        if metrics:
            extra.pop("metrics_pending", None)
        if app.get("provider"):
            extra["steam_trending"] = {k: v for k, v in app.items() if k.startswith("provider")}
        if "players" in metrics:
            extra["players_provenance"] = "Steam GetNumberOfCurrentPlayers"
            extra["players_verified_at"] = ts
            if metrics["players"] == 0:
                extra["players_zero_verified"] = (extra.get("players_zero_verified", []) + [ts])[-8:]
        if "store_reviews" in metrics:
            extra["store_reviews_provenance"] = "Steam store page reviewCount (not appreviews all-purchase total)"
        collect._save("steam", ts, [{"entity": entity, "name": name,
                                   "extra": extra, "metrics": metrics}])


def run_once(full=False):
    if not _run_lock.acquire(blocking=False):
        return db.kv_get("steam_priority", {}) or {}
    started = int(time.time())
    report = {"ts": started, "running": True, "trending": [], "results": [],
              "errors": [], "feeds": {}}
    report.update(scope="recent15-all" if full else "bounded", phase="discovery")
    db.kv_set("steam_priority", report)
    try:
        if full:
            try:
                report["release_scan"] = steam.scan_recent_releases(full=True)
                report["seeded"] = steam.seed_recent_releases()
            except Exception as e:
                report["errors"].append("release_scan: " + str(e)[:250])
        for name, fetch in (("trending", steam.trending),
                            ("popularnew", lambda: steam.store_tab("popularnew"))):
            try:
                rows = fetch()
                report["feeds"][name] = {"ok": True, "count": len(rows)}
                if name == "trending":
                    report["trending"] = rows
                else:
                    report["releases"] = rows
            except Exception as e:
                report["feeds"][name] = {"ok": False, "error": str(e)[:250]}
                report["errors"].append(name + ": " + str(e)[:250])
        targets = _targets(report["trending"], report.get("releases", []), started, full=full)
        report["ids"] = [str(a["appid"]) for a in targets]
        report.update(total=len(targets), completed=0, phase="sampling",
                      scope="recent15-all" if full else "bounded")
        db.kv_set("steam_priority", report)
        with ThreadPoolExecutor(max_workers=8 if full else 4, thread_name_prefix="steam-sample") as pool:
            futures = [pool.submit(_sample, app) for app in targets]
            for future in as_completed(futures):
                app, metrics, errors, sampled = future.result()
                _record(app, metrics, sampled, errors)
                if metrics:
                    report["results"].append({"appid": app["appid"], "name": app["name"],
                                              "metrics": metrics, "ts": sampled})
                report["errors"].extend(f"{app['appid']} {e}" for e in errors)
                report["finished"] = int(time.time())
                report["completed"] += 1
                db.kv_set("steam_priority", report)
        report["running"] = False
        report["phase"] = "complete"
        report["finished"] = int(time.time())
        db.kv_set("steam_priority", report)
        from . import collect
        collect._status("steam", bool(report["results"]), len(report["results"]),
                        err="; ".join(report["errors"][:3]) if not report["results"] else "",
                        warn=f"{len(report['errors'])} 项采样异常" if report["errors"] else "")
        db.log_run("collect:steam_priority", time.time() - started,
                   not report["errors"], f"{len(report['results'])}/{len(targets)} sampled; "
                   + "; ".join(report["errors"][:3]))
        activity.say(f"Steam 趋势采样：趋势榜 {len(report['trending'])} 款，"
                     f"更新 {len(report['results'])}/{len(targets)} 款")
        return report
    except Exception as e:
        report.update(running=False, finished=int(time.time()), error=str(e)[:250])
        db.kv_set("steam_priority", report)
        raise
    finally:
        _run_lock.release()


def summary():
    report = db.kv_get("steam_priority", {}) or {}
    samples = {r["appid"]: r for r in report.get("results", [])}
    now = time.time()
    rows = []
    for r in report.get("trending", []):
        sample = samples.get(r["appid"], {})
        asof = r.get("provider_sampled_at")
        rows.append(dict(r, key=f"steam:{r['appid']}", players=sample.get("metrics", {}).get("players"),
                         sampled=sample.get("ts"), stale=not asof or now - asof > 36 * 3600))
    return dict(report, trending=rows, stale=now - report.get("ts", 0) > 36 * 3600)


def start(stop_event=None):
    global _started
    if _started:
        return
    _started = True
    stop = stop_event or threading.Event()

    def loop():
        while not stop.is_set():
            if config.source_on("steam"):
                try:
                    sampling.run_due("steam", lambda: run_once(full=True))
                except Exception as e:
                    activity.say("Steam 趋势采样失败：" + str(e)[:200])
            stop.wait(max(.5, min(30, sampling.next_slot() - time.time())))

    threading.Thread(target=loop, name="steam-priority", daemon=True).start()
