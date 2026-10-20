"""雷达主程序：定时抓取 → 分析 → 提醒，同时开一个本地看板网页。

融合进电脑管家：
    from radar import main
    url = main.start_background()   # 后台运行，返回看板地址
"""
import threading
import time
import traceback
import webbrowser

from . import activity, analyze, collect, config, db, maintenance, server

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
    """TapTap 等抓得久的时候中途调用：直播到点了就先抓一轮，直播数据不断档。"""
    if time.time() - last["live"] < config.FAST_MINUTES * 60:
        return
    saved = activity.state()
    _safe(collect.collect_live)
    _after_live(last)
    activity.restore(saved)


def _tick(last):
    now = time.time()
    force = server.RUN_NOW.is_set()
    server.RUN_NOW.clear()
    if force:
        activity.say("收到“立即刷新”，开始抓取全部平台")
    live_on = any(config.source_on(s) for s in collect.LIVE_SOURCES)
    # 直播平台都关了：不再每 30 分钟空跑一次分析
    live_due = live_on and (force or now - last["live"] >= config.FAST_MINUTES * 60)
    slow_due = force or now - last["slow"] >= config.SLOW_MINUTES * 60
    ran = 0
    if live_due:
        ran += _safe(collect.collect_live, force=force) or 0
        _after_live(last)
    if slow_due:
        started = time.time()
        # 先抓 Steam、App Store 等几分钟能抓完的平台，抓完分析一次（含 AI 读评论）；再抓 TapTap（30–60 分钟），抓完再分析一次
        # 上次成功还没到间隔的平台跳过；TapTap 上次没抓完的接着抓
        ran += _safe(collect.collect_slow, between=lambda: _live_if_due(last),
                     middle=lambda: _safe(analyze.run), force=force) or 0
        # 按开始时间和"最久没抓的主要平台"算下一轮，重启后不会马上从头重抓
        last["slow"] = min(started, collect.oldest_ok(collect.SLOW_MAIN) or started)
    if ran or ((live_due or slow_due) and not last.get("net_analyzed")):
        _safe(analyze.run)            # 什么都没抓（全是刚抓过的）就不重复分析；启动后第一次照样分析一次，让 AI 能读
        last["net_analyzed"] = True
    elif server.REANALYZE.is_set():
        server.REANALYZE.clear()
        _safe(analyze.run, allow_network=False, send_notify=False)
    if now - last["maint"] >= 86400:
        activity.begin("每日维护")
        _safe(maintenance.daily)
        activity.end()
        last["maint"] = time.time()
    nxt_slow = last["slow"] + config.SLOW_MINUTES * 60
    activity.idle(last["live"] + config.FAST_MINUTES * 60 if live_on else nxt_slow, nxt_slow)


def loop(stop_event=None):
    last = {"live": 0.0, "slow": 0.0, "maint": 0.0}
    activity.say("雷达开始运行：上次抓过、还没到时间的平台直接跳过；TapTap 上次没抓完的接着抓。"
                 "顺序：直播 → Steam、App Store、好游快爆等 → 分析 → TapTap")
    with BUSY:
        # 启动（包括更新后重启）先用已有数据按新规则算一遍，面板马上是新结果，不用等抓完
        _safe(analyze.run, allow_network=False, send_notify=False)
    # 历史对照：后台单独一个线程慢慢补 Steam 评论曲线，断点续抓，不占主循环
    from . import history
    _safe(history.start)
    from . import video
    _safe(video.start, stop_event)
    from . import tapwatch
    _safe(tapwatch.start, stop_event)
    # 地图（v54）：后台单独一个线程，每天抓一次各国 App Store 游戏榜，算各地口味相似度
    from . import worldmap
    _safe(worldmap.start)
    # 机会雷达：B站早期信号（OPP_ENABLED 关掉就不启动）
    if getattr(config, "OPP_ENABLED", False):
        from . import opp
        _safe(opp.start, stop_event)
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
    from . import single
    if not single.guard():
        return
    activity.install()
    db.init()
    _, url = server.start()
    print("热度雷达已启动，看板地址：", url)
    print("关闭这个窗口就会停止。")
    if config.OPEN_BROWSER if open_browser is None else open_browser:
        webbrowser.open(url)
    loop()
