# -*- coding: utf-8 -*-
"""抓 B站「每周必看」全部期数，挑出游戏视频。

用 CDP 在无头浏览器里翻页取（不开窗口、不打扰人）。
断点续抓：已抓过的期数直接跳过。被限流会退避重试，连接断了会重连。
"""
import json
import random
import re
import sys
import time

sys.path.insert(0, r"D:\GameRadar-main")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from radar import cdp, db                            # noqa: E402
from radar.hit import ensure                          # noqa: E402

GAME_TIDS = {4, 17, 65, 171, 172, 173, 121, 136, 19}
GAME_WORDS = ("游戏", "电竞", "单机", "Mugen", "音游", "手游", "桌游", "GMV", "我的世界")

JS_ONE = """
(async (n) => {
  const r = await fetch('https://api.bilibili.com/x/web-interface/popular/series/one?number=' + n,
                        {credentials:'include'});
  const j = await r.json();
  if (j.code !== 0) return {code: j.code, list: []};
  const d = j.data || {};
  return {code: 0, name: d.name || '', list: (d.list || []).map(v => {
    const s = v.stat || {};
    return {
      bvid: v.bvid, title: v.title || v.short_title || '', tname: v.tname || '',
      tid: v.tid, play: s.view || 0, like: s.like || 0, reply: s.reply || 0,
      danmaku: s.danmaku || 0, fav: s.favorite || 0,
      mid: String((v.owner||{}).mid || ''), up: (v.owner||{}).name || '',
      pubdate: v.pubdate || 0
    };
  })};
})(%d)
"""

JS_LIST = """
(async () => {
  const r = await fetch('https://api.bilibili.com/x/web-interface/popular/series/list',
                        {credentials:'include'});
  const j = await r.json();
  return ((j.data||{}).list||[]).map(x => ({number:x.number, name:x.name}));
})()
"""

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
            # 老数据播放量是 0（当初取错字段），重抓时补上
            c.execute("UPDATE hit_videos SET play = ?, title = ? WHERE bvid = ? AND (play = 0 OR play IS NULL)",
                      (v.get("play"), v["title"], v["bvid"]))


def is_game(v):
    if v.get("tid") in GAME_TIDS:
        return True
    t = v.get("tname") or ""
    return any(w in t for w in GAME_WORDS)


class Session:
    """管着那个无头页面，坏了能自己重开。"""

    def __init__(self, port=9223):
        self.port = port
        self.tab = None

    def open(self):
        if self.tab:
            try:
                cdp.close_tab(self.tab)
            except Exception:                     # noqa: BLE001
                pass
            self.tab = None
        self.tab = cdp.open_tab("https://www.bilibili.com/v/popular/weekly",
                                self.port, wait=4.0, background=True)

    def js(self, expr, timeout=45):
        if not self.tab:
            self.open()
        try:
            return self.tab.js(expr, timeout=timeout)
        except Exception:                          # noqa: BLE001
            self.open()                            # 连接断了，重开一个页面再试一次
            return self.tab.js(expr, timeout=timeout)

    def close(self):
        if self.tab:
            try:
                cdp.close_tab(self.tab)
            except Exception:                     # noqa: BLE001
                pass


def main():
    ensure()
    port = cdp.launch_headless(9223)
    print("无头浏览器端口:", port)
    # 无头是全新 profile，没登录会被按匿名严格限流：把登录态搬过来
    if cdp.available(9222):
        n = cdp.copy_cookies(9222, 9223, ("bilibili.com",))
        print("登录态搬过来 %d 条 cookie" % n)
    else:
        print("9222 不可用，无头将按匿名跑（会被限流）")
    refresh = "--refresh" in sys.argv          # 重抓全部（补播放量）
    s = Session(port)
    try:
        issues = s.js(JS_LIST) or []
        print("总期数:", len(issues))
        have = set() if refresh else done_issues()
        todo = [x for x in issues if x["number"] not in have]
        todo.sort(key=lambda x: -x["number"])       # 先抓新的
        print("已抓:", len(have), " 待抓:", len(todo))

        t0 = time.time()
        hits = 0
        fail = 0
        for i, it in enumerate(todo, 1):
            n = it["number"]
            r = None
            for attempt, wait in enumerate((0,) + BACKOFF):
                if wait:
                    print("    第 %d 期限流，等 %ds" % (n, wait))
                    time.sleep(wait)
                try:
                    r = s.js(JS_ONE % n) or {}
                except Exception as e:            # noqa: BLE001
                    print("    第 %d 期异常 %s" % (n, str(e)[:50]))
                    r = {"code": -1, "list": []}
                    time.sleep(3)
                    continue
                if r.get("code") == 0:
                    break
                if r.get("code") not in (-352, -412):
                    break
            if not r or r.get("code") != 0:
                fail += 1
                if fail > 12:
                    print("连续失败太多，先停下。已抓 %d 期，剩下的下次继续。" % len(done_issues()))
                    break
                continue
            fail = 0
            items = r.get("list") or []
            save_issue(n, it.get("name") or "", items)
            g = sum(1 for v in items if is_game(v))
            hits += g
            if i % 20 == 0:
                print("  %d/%d  第 %d 期 %d 条/游戏 %d  已用 %ds"
                      % (i, len(todo), n, len(items), g, int(time.time() - t0)))
            time.sleep(random.uniform(1.2, 2.2))

        with db.session() as c:
            nw = c.execute("SELECT COUNT(DISTINCT number) FROM hit_weekly").fetchone()[0]
            nv = c.execute("SELECT COUNT(*) FROM hit_videos").fetchone()[0]
        print("\n完成：已抓 %d 期（共 %d），视频 %d 条，用时 %ds"
              % (nw, len(issues), nv, int(time.time() - t0)))
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
