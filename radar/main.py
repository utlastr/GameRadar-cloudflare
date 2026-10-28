"""雷达主程序：定时抓取 → 分析 → 提醒，同时开一个本地看板网页。

融合进电脑管家：
    from radar import main
    url = main.start_background()   # 后台运行，返回看板地址
"""
import threading
import time
import traceback
import webbrowser

from . import activity, analyze, collect, config, db, maintenance, sampling, server

# 正在抓取或分析时持有这个锁；自动更新重启前会等它释放，避免写数据写到一半
BUSY = threading.Lock()


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception:
        traceback.print_exc()


def run_once(notify=True):
    activity.install()
    db.init()
    collect.collect_live(force=True)
    collect.collect_slow(force=True)
    return analyze.run(send_notify=notify)


def _after_live(last):
    # 下次直播抓取按"最久没抓的那个直播平台"算：重启后刚抓过的不会马上重抓，也不会拖太久
    last["live"] = collect.oldest_ok(collect.LIVE_SOURCES) or time.time()


def _live_if_due(last):
    """Scheduled snapshots are not supplemented by the old interval loop."""
    return


def _tick(last):
    now = time.time()
    force = server.RUN_NOW.is_set()
    server.RUN_NOW.clear()
    if force:
        activity.say("收到“立即刷新”，开始抓取全部平台")
        collect.request_sample()
        from . import steamwatch
        threading.Thread(target=lambda: _safe(sampling.run_due, "steam",
                         lambda: steamwatch.run_once(full=True), force=True),
                         name="steam-manual", daemon=True).start()
    reanalyze = server.REANALYZE.is_set()
    server.REANALYZE.clear()
    if force or reanalyze or time.time() - last.get("analyze", 0) > 300:
        _safe(analyze.run, allow_network=False, send_notify=False)
        last["analyze"] = time.time()
    if now - last["maint"] >= 86400:
        activity.begin("每日维护")
        _safe(maintenance.daily)
        activity.end()
        last["maint"] = time.time()
    nxt = sampling.next_slot()
    activity.idle(nxt, nxt)


def loop(stop_event=None):
    last = {"live": 0.0, "slow": 0.0, "maint": 0.0}
    activity.say("平台数字每天北京时间 08:00、20:00 采样；错过的时段启动后立即补采，同一时段不重复。")
    with BUSY:
        # 启动（包括更新后重启）先用已有数据按新规则算一遍，面板马上是新结果，不用等抓完
        _safe(analyze.run, allow_network=False, send_notify=False)
    # 历史对照：后台单独一个线程慢慢补 Steam 评论曲线，断点续抓，不占主循环
    from . import history
    _safe(history.start)
    from . import video
    _safe(video.start, stop_event)
    from . import bili_growth
    _safe(bili_growth.start, stop_event)
    from . import steamwatch
    _safe(steamwatch.start, stop_event)
    _safe(collect.start_sampling, stop_event)
    # 地图（v54）：后台单独一个线程，每天抓一次各国 App Store 游戏榜，算各地口味相似度
    from . import worldmap
    _safe(worldmap.start)
    # 机会雷达：B站早期信号（OPP_ENABLED 关掉就不启动）
    if getattr(config, "OPP_ENABLED", False):
        from . import opp
        _safe(opp.start, stop_event)
    # 成功案例：每日播放快照（活着的游戏不记就永久丢数据）
    if getattr(config, "HIT_ENABLED", True):
        from .hit import watch as hit_watch
        _safe(hit_watch.start, stop_event)
    # 社交平台：抖音/抖音指数/哔哩指数/快手/小红书/TikTok + 关键词漏斗（settings.json 里 SOCIAL_ENABLED 关）
    try:
        from . import social
        _safe(social.start, stop_event)
    except Exception as e:      # 社交平台出任何问题都不影响雷达本身
        print("社交平台没启动：%s" % e)
    # v48：引力引擎签名探测已经完成使命（微信 / 抖音小游戏已接上），不再每次启动都跑。
    # 以后对方改了算法，可以临时把下面两行加回来：from . import gravity_probe; _safe(gravity_probe.start)
    while not (stop_event and stop_event.is_set()):
        with BUSY:
            _tick(last)
        for _ in range(30):
            if server.RUN_NOW.is_set() or server.REANALYZE.is_set() or (stop_event and stop_event.is_set()):
                break
            time.sleep(1)


def start_background(open_browser=False):
    db.init()
    _, url = server.start()
    stop = threading.Event()
    threading.Thread(target=loop, args=(stop,), daemon=True).start()
    if open_browser:
        webbrowser.open(url)
    return url


def run_forever(open_browser=None):
    activity.install()
    db.init()
    _, url = server.start()
    print("热度雷达已启动，看板地址：", url)
    print("关闭这个窗口就会停止。")
    if config.OPEN_BROWSER if open_browser is None else open_browser:
        webbrowser.open(url)
    loop()
