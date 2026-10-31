"""本地看板网页服务（只在自己电脑上打开，不联网发布）。地址默认 http://127.0.0.1:8765"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, db

RUN_NOW = threading.Event()        # 看板上点"立即刷新"
REANALYZE = threading.Event()      # 改了标记后重新打分
_re_lock = threading.Lock()
_re_timer = None


def reanalyze_soon():
    """你打星、点不做、点无关以后 2 秒内用已有数据重算（不联网），不用等这一轮抓取做完。连点几下只算一次。"""
    global _re_timer

    def go():
        from . import analyze
        try:
            analyze.run(allow_network=False, send_notify=False)
        except Exception:
            import traceback
            traceback.print_exc()

    with _re_lock:
        if _re_timer:
            _re_timer.cancel()
        _re_timer = threading.Timer(2.0, go)
        _re_timer.daemon = True
        _re_timer.start()


def _detail(key):
    """一个游戏（来源:编号）的资料。不在必看里的游戏点开时用。"""
    from . import images
    if ":" not in key:
        return {}
    s, e = key.split(":", 1)
    out = {"key": key, "source": s}
    with db.session() as c:
        row = c.execute("SELECT name, first_seen, last_seen, extra FROM entities "
                        "WHERE source = ? AND entity = ?", (s, e)).fetchone()
        rv = None
        if s == "steam":
            r = c.execute("SELECT value FROM metrics WHERE source = 'steam' AND entity = ? AND metric = 'reviews' "
                          "ORDER BY ts DESC LIMIT 1", (e,)).fetchone()
            rv = r[0] if r else None
    x = {}
    if row:
        try:
            x = json.loads(row[3]) if row[3] else {}
        except ValueError:
            x = {}
        out.update(name=row[0], first_seen=row[1], last_seen=row[2])
    # 太长的字段（简介、评论原文）不给，页面用不上
    out["extra"] = {k: v for k, v in x.items()
                    if not k.startswith("_") and len(json.dumps(v, ensure_ascii=False)) <= 1500}
    if isinstance(x.get("desc"), str):
        out["extra"]["desc"] = x["desc"][:300]
    out["image"] = images.image_of(s, e, x)
    out["label"] = db.load_labels().get(key)
    if s == "steam" and e.isdigit():
        try:
            from . import history
            h, meta = history.for_appid(int(e), reviews_now=rv, rel=x.get("release_ts"), price=x.get("price_usd"))
            out["hist"] = h
            if meta:
                out["hist_meta"] = meta
                out.setdefault("name", meta.get("name"))
                if not x.get("release_ts") and meta.get("released"):
                    out["extra"]["release_ts"] = meta["released"]
        except Exception as ex:
            out["hist"] = {"note": f"算历史对照时出错：{type(ex).__name__}：{ex}"[:120]}
    return out


MAP_DIR = os.path.join(config.WEB_DIR, "map")
_map_cache = {}


def _map_asset(name):
    """地图用的图片和人口数据（以 base64 文本存在 radar/web/map/*.b64，仓库里只放文本）。"""
    if name in _map_cache:
        return _map_cache[name]
    import base64
    with open(os.path.join(MAP_DIR, "assets.json"), encoding="utf-8") as f:
        types = json.load(f)
    if name not in types:
        return None
    with open(os.path.join(MAP_DIR, name + ".b64"), encoding="ascii") as f:
        blob = base64.b64decode(f.read())
    _map_cache[name] = (blob, types[name])
    return _map_cache[name]


def _int(q, k, default, lo, hi):
    try:
        v = int((q.get(k) or [default])[0])
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, ctype="application/json; charset=utf-8", code=200, cache="no-store"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass  # 浏览器那边先断开了（刷新、关页面），不用管，也不在日志里刷一大段英文

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False), code=code)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(config.WEB_DIR, "index.html"), encoding="utf-8") as f:
                self._send(f.read(), "text/html; charset=utf-8")
        elif u.path == "/map":
            with open(os.path.join(config.WEB_DIR, "map.html"), encoding="utf-8") as f:
                self._send(f.read(), "text/html; charset=utf-8")
        elif u.path == "/map/lib.js":
            with open(os.path.join(MAP_DIR, "globe.gl.min.js"), "rb") as f:
                self._send(f.read(), "application/javascript; charset=utf-8", cache="max-age=86400")
        elif u.path == "/map/countries.geojson":
            with open(os.path.join(MAP_DIR, "countries.geojson"), "rb") as f:
                self._send(f.read(), "application/json; charset=utf-8", cache="max-age=86400")
        elif u.path.startswith("/map/a/"):
            a = _map_asset(u.path[len("/map/a/"):])
            if not a:
                self._send("not found", "text/plain", 404)
                return
            self._send(a[0], a[1], cache="max-age=86400")
        elif u.path == "/api/world":
            from . import worldmap
            self._json(worldmap.payload())
        elif u.path == "/api/platforms":
            from . import platforms
            self._json(platforms.payload())
        elif u.path.startswith("/map/picon/"):
            from . import platforms
            a = platforms.icon(u.path[len("/map/picon/"):])
            if not a:
                self._send("not found", "text/plain", 404)
                return
            self._send(a[0], a[1], cache="max-age=86400")
        elif u.path == "/api/regions":
            from . import regions
            b = regions.public_json()
            if b:
                self._send(b, "application/json; charset=utf-8")
            else:
                self._send("not found", "text/plain", 404)
        elif u.path == "/api/incgrid":
            from . import incgrid
            b = incgrid.blob()
            if b:
                self._send(b, "application/octet-stream", cache="max-age=3600")
            else:
                self._send("not found", "text/plain", 404)
        elif u.path == "/vendor/echarts.min.js":
            from . import vendor
            p = vendor.ensure_echarts()
            if not p:
                self._send("图表组件下载失败", "text/plain; charset=utf-8", 404)
                return
            with open(p, "rb") as f:
                self._send(f.read(), "application/javascript; charset=utf-8", cache="max-age=86400")
        elif u.path == "/api/state":
            from . import updater
            d = db.kv_get("dashboard", {}) or {}
            from . import health
            d["health"] = health.snapshot()
            d["version"] = updater.local_version()[0]
            d["analyze_error"] = db.kv_get("analyze_error")
            try:
                from . import ai
                d["ai"] = ai.status()
            except Exception:
                pass
            from . import video
            d["video_hotspots"] = video.summary()
            from . import youtube
            d["youtube"] = youtube.public_state()
            from . import collect
            d["sources"] = collect.source_status()
            d["release_discovery"] = db.kv_get("release_discovery", {}) or {}
            from .sources import steam
            d["release_pool"] = steam.release_pool()
            from . import steam_cycle
            d["steam_cycle"] = steam_cycle.state()
            try:
                from . import history
                d["history"] = history.summary()
            except Exception as e:
                d["history"] = {"error": f"{type(e).__name__}：{e}"[:200], "counts": {}, "table": []}
            self._json(d)
        elif u.path == "/api/video-hotspots":
            from . import video
            self._json(video.summary())
        elif u.path == "/api/bili-daily":
            from . import bili_daily
            self._json(bili_daily.summary(q.get("day", [None])[0]))
        elif u.path == "/api/bili-terms":
            from . import bili_terms
            try:
                result = (bili_terms.matched_catalogue() if q.get("matched") == ["1"] else
                          bili_terms.catalogue(q.get("q", [""])[0], q.get("status", ["all"])[0],
                                               _int(q, "page", 1, 1, 10000000),
                                               _int(q, "page_size", 100, 1, 100)))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            else:
                self._json(result)
        elif u.path == "/api/bili-stream":
            from . import bili_stream
            self._json(bili_stream.summary())
        elif u.path == "/api/bili-growth":
            from . import bili_growth, bili_terms
            try:
                result = bili_growth.payload(q.get("day", [None])[0])
                result = {**result, "groups": [dict(r) for r in result.get("groups", [])],
                          "tags": [dict(r) for r in result.get("tags", [])]}
                with db.session() as c:
                    bili_terms.archive_links(c, result["groups"] + result["tags"])
                self._json(result)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
        elif u.path == "/api/youtube/state":
            from . import youtube
            self._json(youtube.public_state())
        elif u.path == "/api/youtube/videos":
            from . import youtube
            self._json({"videos": youtube.videos(q.get("after", [None])[0], q.get("before", [None])[0],
                                                   _int(q, "offset", 0, 0, 100000000),
                                                   _int(q, "limit", 50, 1, 500))})
        elif u.path == "/api/series":
            keys = (q.get("keys", [""])[0]).split(",")
            days = _int(q, "days", 7, 1, 90)
            since = int(time.time()) - days * 86400
            out = {}
            with db.session() as c:
                for key in keys[:30]:
                    if ":" not in key:
                        continue
                    s, e = key.split(":", 1)
                    rows = c.execute("SELECT metric, ts, value FROM metrics WHERE source = ? AND entity = ? "
                                     "AND ts >= ? ORDER BY ts", (s, e, since)).fetchall()
                    d = {}
                    for m, ts, v in rows:
                        # v51：所有指标都给（页面按天汇总后画图）
                        d.setdefault(m, []).append([ts, v])
                    out[key] = d
            self._json(out)
        elif u.path == "/api/heat/series":
            # v96：详情图表里的 B站（和其他视频平台）每天新增播放
            from . import heat
            self._json(heat.game_series(q.get("gid", [""])[0], _int(q, "days", 90, 1, 90)))
        elif u.path == "/api/entity":
            # v50：各平台里任何一个游戏点开，看它的基本资料
            key = q.get("key", [""])[0]
            out = {}
            if ":" in key:
                s, e = key.split(":", 1)
                with db.session() as c:
                    row = c.execute("SELECT name, first_seen, last_seen, extra FROM entities "
                                    "WHERE source = ? AND entity = ?", (s, e)).fetchone()
                if row:
                    try:
                        x = json.loads(row[3]) if row[3] else {}
                    except ValueError:
                        x = {}
                    keep = ("url", "image", "developers", "publishers", "genre", "tags", "lists", "release_ts",
                            "released_time", "coming_soon", "preorder", "full_name", "name_cn", "price_usd",
                            "discount", "score", "top_title", "top_nick", "stars", "stat_date", "labels")
                    out = {"name": row[0], "first_seen": row[1], "last_seen": row[2],
                           "extra": {k: x[k] for k in keep if k in x}}
            self._json(out)
        elif u.path == "/api/detail":
            # v51：任何一个游戏点开都有完整详情（资料、图片、你打的分、Steam 的历史对照）
            self._json(_detail(q.get("key", [""])[0]))
        elif u.path == "/api/board":
            from . import charts
            self._json(charts.board(q.get("scope", ["maker"])[0], q.get("period", ["week"])[0]))
        elif u.path == "/api/games":
            from . import charts
            self._json(charts.games(q.get("scope", ["maker"])[0]))
        elif u.path == "/api/fresh":
            from . import charts
            self._json(charts.fresh())
        elif u.path == "/api/raw":
            from . import charts
            self._json(charts.raw(q.get("source", ["tapmaker"])[0]))
        elif u.path == "/api/stats":
            from . import charts
            self._json(charts.run_stats(_int(q, "days", 7, 1, 30)))
        elif u.path == "/api/index":
            from . import charts
            self._json(charts.index_series(_int(q, "days", 7, 1, 90)))
        elif u.path == "/api/activity":
            from . import activity
            from . import health
            self._json({**activity.snapshot(int(q.get("after", ["0"])[0] or 0)),
                        "health": health.snapshot()})
        elif u.path == "/api/health":
            from . import health
            self._json(health.snapshot())
        elif u.path == "/api/runs":
            with db.session() as c:
                rows = [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY ts DESC LIMIT 50")]
            self._json(rows)
        elif u.path == "/api/opp/list":
            from . import opp
            self._json(opp.api_list(q.get("state", [""])[0]))
        elif u.path == "/api/opp/game":
            from . import opp
            self._json(opp.api_game(q.get("id", [""])[0]))
        elif u.path == "/api/opp/unknown":
            from .opp import gamemap
            self._json(gamemap.unknown_list())
        elif u.path == "/api/hit/list":
            from .hit import api as hitapi
            self._json(hitapi.api_list())
        elif u.path == "/api/hit/game":
            from .hit import api as hitapi
            self._json(hitapi.api_game(q.get("id", [""])[0]))
        elif u.path == "/api/archive/report":
            from . import archive
            self._json(archive.report())
        elif u.path == "/api/archive/game":
            from . import archive
            gid = q.get("id", [""])[0]
            self._json({"members": archive.cluster_view(gid), "bili": archive.bili_block(gid)})
        elif u.path.startswith("/api/social/"):
            from . import social    # 社交平台（抖音/快手/小红书/TikTok/指数）+ 关键词漏斗
            social.http_get(self, u.path, q)
        else:
            self._send("not found", "text/plain", 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except ValueError:
            body = {}
        p = urlparse(self.path).path
        if p.startswith("/api/bili-growth/") and not isinstance(body, dict):
            self._json({"error": "请求内容必须是对象"}, 400)
            return
        if p == "/api/label":
            db.save_label(body["member"], stars=body.get("stars"), blocked=body.get("blocked"),
                          note=body.get("note"), aliases=body.get("aliases"), keywords=body.get("keywords"),
                          dismissed=body.get("dismissed"))
            reanalyze_soon()
            self._json({"ok": True})
        elif p in ("/api/link", "/api/archive/decide"):
            from . import archive
            try:
                r = archive.decide(body["a"], body["b"], body.get("relation"), body.get("origin") or "api")
            except (KeyError, ValueError) as e:
                self._json({"error": str(e)}, 400)
                return
            reanalyze_soon()
            self._json(r)
        elif p == "/api/archive/split":
            from . import archive
            try:
                r = archive.split(body["member"])
            except (KeyError, ValueError) as e:
                self._json({"error": str(e)}, 400)
                return
            reanalyze_soon()
            self._json(r)
        elif p == "/api/archive/undo":
            from . import archive
            r = archive.undo(body["a"], body["b"])
            reanalyze_soon()
            self._json(r)
        elif p == "/api/bili/verified":
            from .sources import bili
            try:
                bili.resume_after_verification(body.get("verified"))
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            self._json({"ok": True})
        elif p == "/api/run":
            RUN_NOW.set()
            self._json({"ok": True})
        elif p == "/api/bili-growth/edge":
            from . import bili_growth
            try:
                self._json(bili_growth.edge_decision(body.get("a"), body.get("b"),
                                                     body.get("blocked", True), body.get("day")))
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)
        elif p == "/api/bili-growth/rebuild":
            from . import bili_growth
            try:
                day = body.get("day")
                if day is not None:
                    from datetime import date, datetime
                    from . import bili_realtime_client as bili
                    if date.fromisoformat(day) >= datetime.now(bili.BEIJING).date():
                        raise ValueError("增长榜只比较已结束的北京时间日期")
                if "smoothing" in body:
                    smoothing = float(body["smoothing"])
                    if not 0.001 <= smoothing <= 1000000:
                        raise ValueError("c 必须在0.001至1000000之间")
                    db.kv_set("bili_growth_smoothing", smoothing)
                self._json(bili_growth.build(day))
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)
        elif p == "/api/world/run":
            from . import worldmap
            worldmap.RUN_NOW.set()
            self._json({"ok": True})
        elif p == "/api/youtube/run":
            from . import youtube
            try:
                if not isinstance(body, dict):
                    raise ValueError("请求内容必须是对象")
                after, before = str(body.get("after") or ""), str(body.get("before") or "")
                self._json(youtube.run_async(after, before, 100), code=202)
            except (ValueError, TypeError) as e:
                self._json({"ok": False, "error": str(e)}, 400)
        elif p.startswith("/api/social/"):
            from . import social
            social.http_post(self, p, body)
        else:
            self._json({"error": "unknown"}, 404)


def start():
    srv = ThreadingHTTPServer((config.SERVER_HOST, config.SERVER_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from . import vendor
    threading.Thread(target=vendor.ensure_echarts, daemon=True).start()   # 提前把图表组件下载好
    threading.Thread(target=_warm_map, daemon=True).start()               # 提前把地图数据读进内存，第一次打开地图不用等
    return srv, f"http://{config.SERVER_HOST}:{config.SERVER_PORT}/"


def _warm_map():
    """地区边界 + 人口网格第一次读要解码好几秒（纯 Python），启动时后台先做掉，打开地图时直接用缓存。"""
    try:
        from . import regions
        regions.load_geo()
        for name in ("popgrid", "earth-night", "earth-topology", "night-sky"):
            _map_asset(name)
    except Exception:
        pass
