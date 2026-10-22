# -*- coding: utf-8 -*-
"""抓 B站「每周必看」全部期数，挑出游戏视频。

走统一的 B站 客户端（代理、Cookie、风控暂停都共用）。断点续抓：已抓过的期数直接跳过。
后台每天调一次 run()，平时只会多出一期；第一次运行会把历史期数补齐。
命令行：python -m radar.hit.weekly [--refresh]
"""
import random
import sys
import time

from .. import db
from ..sources import bili
from . import ensure

GAME_TIDS = bili.GAME_TIDS
GAME_WORDS = ("游戏", "电竞", "单机", "Mugen", "音游", "手游", "桌游", "GMV", "我的世界")
BACKOFF = (40, 90, 180)


def done_issues():
    with db.session() as c:
        return {r[0] for r in c.execute("SELECT DISTINCT number FROM hit_weekly")}


def save_issue(number, week, items):
    now = int(time.time())
    with db.session() as c:
        for v in items:
            c.execute("INSERT OR REPLACE INTO hit_weekly(number, week, bvid, title, tname, tid, play, mid, up, "
                      "pubdate, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (number, week, v["bvid"], v["title"], v["tname"], v.get("tid"), v.get("play"),
                       v["mid"], v["up"], v.get("pubdate"), now))
            c.execute("INSERT OR IGNORE INTO hit_videos(bvid, title, tname, tid, play, mid, up, pubdate, "
                      "first_week, first_ts, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (v["bvid"], v["title"], v["tname"], v.get("tid"), v.get("play"), v["mid"], v["up"],
                       v.get("pubdate"), number, now, "weekly"))
            c.execute("UPDATE hit_videos SET play = ?, title = ? WHERE bvid = ? AND (play = 0 OR play IS NULL)",
                      (v.get("play"), v["title"], v["bvid"]))


def is_game(v):
    if bili.tid_of(v) in GAME_TIDS:
        return True
    t = v.get("tname") or ""
    return any(w in t for w in GAME_WORDS)


def run(refresh=False, quiet=True, max_issues=None):
    """返回 {"issues": 新抓期数, "games": 其中游戏视频条数}。被风控就停，下次接着抓。"""
    ensure()
    if bili.paused():
        return {"issues": 0, "games": 0, "paused": True}
    issues = bili.weekly_issues()
    have = set() if refresh else done_issues()
    todo = sorted((x for x in issues if x["number"] not in have), key=lambda x: -x["number"])
    if max_issues:
        todo = todo[:max_issues]
    got = games = fail = 0
    t0 = time.time()
    for i, it in enumerate(todo, 1):
        n = it["number"]
        items = None
        for wait in (0,) + BACKOFF:
            if wait:
                time.sleep(wait)
            try:
                items = bili.weekly_issue(n)
                break
            except bili.RiskError as e:
                bili.note_risk(e)
                return {"issues": got, "games": games, "paused": True}
            except Exception as e:                # noqa: BLE001
                if not quiet:
                    print("    第 %d 期失败：%s" % (n, str(e)[:60]))
        if items is None:
            fail += 1
            if fail > 12:
                break
            continue
        fail = 0
        save_issue(n, it.get("name") or "", items)
        g = sum(1 for v in items if is_game(v))
        games += g
        got += 1
        if not quiet and i % 20 == 0:
            print("  %d/%d  第 %d 期 %d 条/游戏 %d  已用 %ds" % (i, len(todo), n, len(items), g, int(time.time() - t0)))
        time.sleep(random.uniform(1.2, 2.2))
    return {"issues": got, "games": games}


def main():
    r = run(refresh="--refresh" in sys.argv, quiet=False)
    print("完成：新抓 %d 期，游戏视频 %d 条" % (r["issues"], r["games"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
