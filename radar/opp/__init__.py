"""机会雷达（opp）：早期发现"这款电脑游戏值得移植到手机"。

方案见 docs/OPP_SPEC.md。第一期只做 B站为主的部分。
百度、微博、抖音、Steam 中文、手机缺口、回溯、校准在后面几期——这里不写空壳。
"""
import threading
import time

from .. import activity, config, db

SCHEMA = """
CREATE TABLE IF NOT EXISTS opp_videos(bvid TEXT PRIMARY KEY, aid INTEGER, cid INTEGER, tid INTEGER, mid TEXT,
  title TEXT, tags TEXT, pubdate INTEGER, duration INTEGER, first_seen INTEGER, track_until INTEGER,
  descr TEXT, owner TEXT);
CREATE TABLE IF NOT EXISTS opp_vsamples(bvid TEXT, ts INTEGER, view INTEGER, lk INTEGER, coin INTEGER, fav INTEGER,
  share INTEGER, reply INTEGER, dm INTEGER, PRIMARY KEY(bvid, ts));
CREATE TABLE IF NOT EXISTS opp_uploaders(mid TEXT PRIMARY KEY, name TEXT, followers INTEGER, base_median INTEGER,
  base_n INTEGER, base_est INTEGER, ts INTEGER);
CREATE TABLE IF NOT EXISTS opp_demand(bvid TEXT, ts INTEGER, kind TEXT, n_total INTEGER, n_mobile INTEGER,
  n_name INTEGER, n_want INTEGER, PRIMARY KEY(bvid, ts, kind));
CREATE TABLE IF NOT EXISTS opp_games(gid TEXT PRIMARY KEY, name_cn TEXT, name_en TEXT, steam_appid TEXT,
  aliases TEXT, t0 INTEGER, state TEXT, state_ts INTEGER, source TEXT, big INTEGER);
CREATE TABLE IF NOT EXISTS opp_vmap(bvid TEXT, gid TEXT, method TEXT, conf REAL, PRIMARY KEY(bvid, gid));
CREATE TABLE IF NOT EXISTS opp_unknown(name TEXT PRIMARY KEY, first_ts INTEGER, n_up INTEGER, bvids TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS opp_search(gid TEXT, src TEXT, term TEXT, day TEXT, value REAL, PRIMARY KEY(gid, src, term, day));
CREATE TABLE IF NOT EXISTS opp_hot(src TEXT, ts INTEGER, rank INTEGER, word TEXT, gid TEXT);
CREATE TABLE IF NOT EXISTS opp_steam(appid TEXT, day TEXT, cn_reviews INTEGER, cn_new INTEGER, players INTEGER,
  PRIMARY KEY(appid, day));
CREATE TABLE IF NOT EXISTS opp_supply(gid TEXT, ts INTEGER, store TEXT, app_id TEXT, name TEXT, dev TEXT,
  ratings INTEGER, released TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS opp_features(gid TEXT, d REAL, ts INTEGER, data TEXT, PRIMARY KEY(gid, d, ts));
CREATE TABLE IF NOT EXISTS opp_scores(gid TEXT, ts INTEGER, d REAL, model_v INTEGER, p REAL, reasons TEXT,
  PRIMARY KEY(gid, ts));
CREATE TABLE IF NOT EXISTS opp_cases(cid INTEGER PRIMARY KEY, name_cn TEXT, name_en TEXT, steam_appid TEXT,
  month TEXT, label TEXT, note TEXT, gid TEXT, origin TEXT, backfill TEXT);
CREATE TABLE IF NOT EXISTS opp_models(v INTEGER PRIMARY KEY, ts INTEGER, report TEXT, params TEXT);
CREATE TABLE IF NOT EXISTS opp_alerts(gid TEXT, state TEXT, ts INTEGER);
"""

_ready = {"done": False}
_lock = threading.Lock()

TRACK_FAST_HOURS = 72          # 发布 0–72 小时：每 2 小时采样一次
TRACK_SLOW_DAYS = 21           # 3–21 天：每天一次，之后停止
SAMPLE_FAST_SEC = 2 * 3600
SAMPLE_SLOW_SEC = 24 * 3600
DEMAND_AT_HOURS = (12, 24, 72)  # 评论/弹幕在这几个时间点各抓一次


def ensure():
    """建表。只执行一次。"""
    with _lock:
        if _ready["done"]:
            return
        with db.session() as c:
            c.executescript(SCHEMA)
        _ready["done"] = True


def status(source, ok, count=0, err="", warn=""):
    """写进"运行情况"表，格式和现有平台一致。"""
    from ..collect import _status
    _status(source, ok, count, err, warn)


# ---------------- 请求预算与风控暂停 ----------------

def _budget():
    return db.kv_get("opp_budget", {}) or {}


def budget_left():
    hour = int(time.time() // 3600)
    b = _budget()
    if b.get("hour") != hour:
        return int(getattr(config, "OPP_BILI_BUDGET", 600))
    return max(0, int(getattr(config, "OPP_BILI_BUDGET", 600)) - int(b.get("used") or 0))


def budget_use(n=1):
    hour = int(time.time() // 3600)
    b = _budget()
    if b.get("hour") != hour:
        b = {"hour": hour, "used": 0}
    b["used"] = int(b.get("used") or 0) + n
    db.kv_set("opp_budget", b)


from ..sources.bili import note_risk, paused, reset_risk   # noqa: E402,F401  风控暂停全程序共用一份


# ---------------- 启动 ----------------

from .api import api_game, api_list          # noqa: E402  server.py 用


def start(stop_event=None):
    if not getattr(config, "OPP_ENABLED", False):
        return
    try:
        ensure()
    except Exception as e:                       # noqa: BLE001
        activity.say("机会雷达建表失败：%s" % e)
        return
    threading.Thread(target=_loop, args=(stop_event or threading.Event(),),
                     name="opp", daemon=True).start()
    activity.say("机会雷达已启动（平台：B站 · 视频）")


def _loop(stop):
    from . import bili_track, features
    stop.wait(20)                     # 让主采集先起来，别抢带宽
    last = {"discover": 0.0, "track": 0.0, "demand": 0.0, "map": 0.0, "feat": 0.0, "search": 0.0}
    while not stop.is_set():
        now = time.time()
        try:
            if not paused() and now - last["search"] >= 10 * 60:
                last["search"] = now
                bili_track.search_games()
        except Exception as e:                   # noqa: BLE001
            activity.say("B站按名字搜出错：%s" % e)
        try:
            if not paused():
                if now - last["discover"] >= 20 * 60:
                    last["discover"] = now
                    bili_track.discover_once()
                if now - last["track"] >= 10 * 60:
                    last["track"] = now
                    bili_track.track_once()
                if now - last["demand"] >= 300:
                    last["demand"] = now
                    bili_track.demand_once()
                if now - last["map"] >= 30 * 60:
                    last["map"] = now
                    bili_track.map_once()
            if now - last["feat"] >= 30 * 60:
                last["feat"] = now
                features.refresh_games()
        except Exception as e:                   # noqa: BLE001
            activity.say("机会雷达循环出错：%s" % e)
        stop.wait(30)
