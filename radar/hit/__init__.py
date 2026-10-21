"""历史爆款研究（hit）：从历史数据里找出"起来过"的游戏，学规律。

方案见 docs/HIT_SPEC.md。数据靠 CDP 驱动本地已登录的浏览器取，
不走接口层的风控对抗。
"""
import threading

from .. import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS hit_weekly(number INTEGER, week TEXT, bvid TEXT, title TEXT, tname TEXT, tid INTEGER,
  play INTEGER, mid TEXT, up TEXT, pubdate INTEGER, ts INTEGER, PRIMARY KEY(number, bvid));
CREATE TABLE IF NOT EXISTS hit_videos(bvid TEXT PRIMARY KEY, title TEXT, tname TEXT, tid INTEGER, play INTEGER,
  mid TEXT, up TEXT, pubdate INTEGER, first_week INTEGER, first_ts INTEGER, source TEXT);
CREATE TABLE IF NOT EXISTS hit_games(gid TEXT PRIMARY KEY, name TEXT, norm TEXT, first_ts INTEGER,
  first_week INTEGER, n_video INTEGER, n_up INTEGER, play_sum INTEGER, source TEXT, state TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS hit_result(gid TEXT PRIMARY KEY, app_cn INTEGER, tap_follow INTEGER, tap_download INTEGER,
  tap_reserve INTEGER, steam_reviews INTEGER, steam_cn INTEGER, download_est INTEGER, evidence TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS hit_daily(gid TEXT, day TEXT, n_video INTEGER, n_up INTEGER, play_sum INTEGER,
  like_sum INTEGER, PRIMARY KEY(gid, day));
CREATE TABLE IF NOT EXISTS hit_curve(gid TEXT, day_offset INTEGER, play_cum INTEGER, n_video_cum INTEGER,
  n_up_cum INTEGER, PRIMARY KEY(gid, day_offset));
CREATE TABLE IF NOT EXISTS hit_labels(gid TEXT PRIMARY KEY, label TEXT, note TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS hit_rules(signal TEXT, bucket TEXT, n INTEGER, n_success INTEGER, ts INTEGER,
  PRIMARY KEY(signal, bucket, ts));
CREATE TABLE IF NOT EXISTS hit_topic(gid TEXT, bvid TEXT, pubdate INTEGER, play INTEGER, like_ INTEGER,
  reply INTEGER, mid TEXT, up TEXT, tid INTEGER, tname TEXT, title TEXT, tags TEXT, kw TEXT,
  PRIMARY KEY(gid, bvid));
CREATE TABLE IF NOT EXISTS hit_up(mid TEXT PRIMARY KEY, name TEXT, followers INTEGER, ts INTEGER);
CREATE TABLE IF NOT EXISTS hit_up_base(mid TEXT PRIMARY KEY, median_play INTEGER, n_video INTEGER,
  span_start INTEGER, span_end INTEGER, ts INTEGER);
CREATE TABLE IF NOT EXISTS hit_stat(gid TEXT, bvid TEXT, day TEXT, ts INTEGER, play INTEGER,
  like_ INTEGER, reply INTEGER, PRIMARY KEY(gid, bvid, day));
CREATE TABLE IF NOT EXISTS hit_span(gid TEXT, day TEXT, n_video INTEGER, n_up INTEGER, play_sum INTEGER,
  PRIMARY KEY(gid, day));
"""

_ready = {"done": False}
_lock = threading.Lock()


def ensure():
    with _lock:
        if _ready["done"]:
            return
        with db.session() as c:
            c.executescript(SCHEMA)
        _ready["done"] = True


def status(source, ok, count=0, err="", warn=""):
    from ..collect import _status
    _status(source, ok, count, err, warn)
