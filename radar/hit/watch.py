# -*- coding: utf-8 -*-
"""成功案例（hit）的自动值守：挂在雷达主循环里的一个线程。

为什么必须有这个：已经死掉的案例可以慢慢手动补，但**还活着的游戏补不回来**——
B站只给"现在的播放"，没有历史时间轴。今天不记，今天涨了多少就永久丢了。
所以一旦确定要盯，就必须每天自动记一次。

这个线程做的事：
  1. 每周必看：每天看一次有没有新一期（第一次运行会补齐历史期数）；
  2. 候选发现：每 DISC_EVERY_H 小时轮转核一批名字（各平台名字 + B站 自己冒出来的名字），起势的自动回溯；
  3. 每天一次快照：重搜最近 SNAP_DAYS 天，把播放现值写进 hit_stat，新视频补进 hit_topic；
  4. 补新出现的 UP 的粉丝数、以及万粉以上 UP 的平时播放基线。
B站 风控时（bili.paused）整个线程停下，等暂停结束。
"""
import threading
import time

from .. import activity, config
from ..sources import bili
from . import bsearch

SNAP_EVERY_H = 6          # 每 6 小时记一次快照（同一天多次 INSERT OR REPLACE，留最后一次）
SNAP_DAYS = 21            # 每次回看 21 天
FANS_EVERY_H = 24         # 粉丝数每天补一次
BASE_EVERY_H = 24         # 平时播放基线每天补一次
DISC_EVERY_H = 2          # 候选发现每 2 小时一轮（每轮轮转查 60 个名字，一天过完一圈）
BACKFILL_MAX = 1          # 每轮最多回溯几款（一款要 8~30 分钟，别一次堆太多）
WEEKLY_EVERY_H = 24       # 每周必看每天看一次有没有新一期


def start(stop_event=None):
    if not getattr(config, "HIT_ENABLED", True):
        return
    try:
        from . import ensure
        ensure()
    except Exception as e:                       # noqa: BLE001
        activity.say("成功案例建表失败：%s" % e)
        return
    threading.Thread(target=_loop, args=(stop_event or threading.Event(),),
                     name="hit", daemon=True).start()
    activity.say("成功案例值守已启动（播放快照 + 粉丝/基线补齐 + 候选发现）")


def _backfill_pending(n=BACKFILL_MAX):
    """把候选里分数最高、还没回溯的几款补齐历史。"""
    from . import discover
    done = 0
    for c in discover.top_candidates(limit=40):
        if done >= n:
            break
        if c.get("state") != "待回溯":
            continue
        try:
            r = discover.backfill(c["tag"], quiet=True)
            activity.say("成功案例：回溯 %s 完成，%d 条" % (c["tag"], r["n"]))
            done += 1
        except Exception as e:                    # noqa: BLE001
            activity.say("成功案例：回溯 %s 失败：%s" % (c["tag"], e))
    return done


def _snapped_today():
    """今天是否已经记过快照。重启一次就重跑全量快照没必要，只按天算。"""
    try:
        import datetime
        from .. import db
        today = datetime.date.today().strftime("%Y-%m-%d")
        with db.session() as c:
            r = c.execute("SELECT COUNT(*) FROM hit_stat WHERE day=?", (today,)).fetchone()
        return bool(r and r[0])
    except Exception:                             # noqa: BLE001
        return False


def _loop(stop):
    stop.wait(60)                             # 让主采集先起来，别一上来就抢带宽
    last = {"snap": 0.0, "fans": 0.0, "base": 0.0, "disc": 0.0, "week": 0.0, "err": 0}
    while not stop.is_set():
        now = time.time()
        if bili.paused():
            stop.wait(600)
            continue
        try:
            if now - last["week"] >= WEEKLY_EVERY_H * 3600:
                last["week"] = now
                from . import weekly
                r = weekly.run(quiet=True)
                if r.get("issues"):
                    activity.say("成功案例：每周必看新抓 %d 期，游戏视频 %d 条" % (r["issues"], r["games"]))
            # 发现排在第一位：它是唯一"晚一天就少一天数据"的事，快照其次。
            if now - last["disc"] >= DISC_EVERY_H * 3600:
                last["disc"] = now
                from . import discover
                n = discover.discover_once(quiet=True)
                if n:
                    activity.say("成功案例：发现 %d 个起势候选，开始回溯" % n)
                _backfill_pending()
            if not _snapped_today() and now - last["snap"] >= 600:
                last["snap"] = now
                n = bsearch.snapshot(days=SNAP_DAYS, quiet=True)
                activity.say("成功案例：播放快照写入 %d 条" % n)
            if now - last["fans"] >= FANS_EVERY_H * 3600:
                last["fans"] = now
                n = bsearch.followers()
                if n:
                    activity.say("成功案例：补了 %d 个 UP 的粉丝数" % n)
            if now - last["base"] >= BASE_EVERY_H * 3600:
                last["base"] = now
                n = bsearch.baselines()
                if n:
                    activity.say("成功案例：补了 %d 个 UP 的平时播放基线" % n)
        except bili.RiskError as e:
            bili.note_risk(e)
            activity.say("成功案例：B站风控，暂停 %d 分钟" % (bili.pause_left() // 60))
        except Exception as e:                    # noqa: BLE001
            last["err"] += 1
            activity.say("成功案例值守出错：%s" % e)
            if last["err"] % 5 == 1:
                from . import status
                try:
                    status("hit", False, 0, str(e))
                except Exception:                 # noqa: BLE001
                    pass
        stop.wait(600)
