"""后台模式：没有黑色窗口，托盘图标，开机自动启动（默认开启），自动更新。由 GameRadar.pyw 启动。"""
import ctypes
import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

from . import activity, config, db, main, notify, server, updater

APP_NAME = "GameRadar"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_k32 = None
_mutex = None
_srv = None
_tray = None
_update_lock = threading.Lock()


def _pythonw():
    exe = sys.executable
    w = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return w if os.path.exists(w) else exe


def _launcher_path():
    return os.path.join(config.BASE_DIR, "GameRadar.pyw")


def _launcher_cmd():
    return f'"{_pythonw()}" "{_launcher_path()}"'


# ---------------- 开机自启 ----------------
def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _launcher_cmd())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


# ---------------- 工具 ----------------
def _redirect_output():
    """后台模式没有窗口，输出写到 radar_data/radar.log。"""
    os.makedirs(config.DATA_DIR, exist_ok=True)
    path = os.path.join(config.DATA_DIR, "radar.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 5 * 1024 * 1024:
            os.replace(path, path + ".old")
    except OSError:
        pass
    f = open(path, "a", encoding="utf-8", buffering=1)
    activity.install(f)       # 同时写日志文件，并显示在面板的"运行情况"里


def _msgbox(text):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, "热度雷达", 0x10)
    except Exception:
        pass


def _log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a)


# ---------------- 自动更新 ----------------
def _restart():
    """释放端口、单实例锁、托盘图标，启动新版本，然后退出自己。"""
    _log("重启以使用新版本")
    try:
        _srv.shutdown()
        _srv.server_close()
    except Exception:
        pass
    try:
        _k32.CloseHandle(ctypes.c_void_p(_mutex))
    except Exception:
        pass
    try:
        _tray.remove_icon()
    except Exception:
        pass
    subprocess.Popen([_pythonw(), _launcher_path()], cwd=config.BASE_DIR,
                     env=dict(os.environ, GAMERADAR_RESTART="1"),
                     creationflags=0x00000008 | 0x00000200)   # 独立进程，不跟随当前进程退出
    os._exit(0)


def _update_error_text(e):
    msg = str(e)
    if config.SETTINGS_ERROR:
        return config.SETTINGS_ERROR
    if not (config.GITHUB_TOKEN or "").strip():
        return "没有读到 GITHUB_TOKEN。填在 radar_data/settings.json 里，改完右键托盘图标退出，再双击 GameRadar.pyw"
    if "401" in msg:
        return "GitHub 拒绝了这个令牌：令牌填错、已过期或已删除。请新建一个再填"
    if "404" in msg:
        return "令牌没有 GameRadar 仓库的权限。新建令牌时 Resource owner 选 utlastr，仓库选 GameRadar，Contents 设为 Read-only"
    if "403" in msg:
        return "GitHub 暂时限制了访问（请求太多或令牌权限不够），过一会儿再试"
    return "连不上 GitHub：" + msg + "。网络不稳定的话在 settings.json 里填 PROXY"


_apply_lock = threading.Lock()


def _apply_and_restart(remote, note):
    with _apply_lock:                    # 同一时间只有一个在替换文件
        updater.apply()
        try:
            db.kv_set("just_updated", {"version": remote, "note": note})
        except Exception:
            _log("记录更新说明失败（不影响更新）：\n" + traceback.format_exc())
        _restart()                       # 文件已经换好，无论如何都重启


def _try_update(manual=False):
    """manual=True（右键点“检查更新”）：有新版本立刻更新重启，不管正在做什么。
    正在抓的这一轮会中断，已经存好的数据不受影响（数据库每次写入都是完整的），重启后重新抓。
    自动检查：等当前这轮抓取做完再更新。"""
    if manual:
        _tray.balloon("热度雷达", "正在检查更新…")
        try:
            has, remote, note = updater.check()
        except Exception as e:
            _log("检查更新失败：", e)
            _tray.balloon("检查更新失败", _update_error_text(e)[:250])
            return
        if not has:
            _tray.balloon("热度雷达", f"已经是最新版本（版本 {updater.local_version()[0]}）")
            return
        _log(f"手动检查：发现新版本 {remote}，立刻更新")
        activity.say(f"发现新版本 {remote}，立刻更新并重启（正在抓的这一轮会中断，重启后重新抓）")
        _tray.balloon("热度雷达", f"发现新版本 {remote}，正在下载，十几秒后自动重启")
        try:
            _apply_and_restart(remote, note)
        except Exception as e:
            _log("更新失败：\n" + traceback.format_exc())
            _tray.balloon("更新失败", (str(e) or "详细错误见 radar_data/radar.log")[:200])
        return

    if not _update_lock.acquire(blocking=False):
        return
    try:
        try:
            has, remote, note = updater.check()
        except Exception as e:
            _log("检查更新失败：", e)
            return
        if not has:
            return
        _log(f"发现新版本 {remote}，等当前这轮抓取做完后更新")
        with main.BUSY:                      # 自动更新：等当前这轮抓取/分析做完
            _apply_and_restart(remote, note)
    except Exception:
        _log("更新失败：\n" + traceback.format_exc())
    finally:
        if _update_lock.locked():
            _update_lock.release()


def _startup_update():
    """启动时先查一次更新，有新版本就在开始抓取之前更新好，不用等。"""
    if not config.AUTO_UPDATE:
        return
    try:
        has, remote, note = updater.check()
    except Exception as e:
        _log("启动时检查更新失败：", e)
        return
    if has:
        _log(f"启动时发现新版本 {remote}，先更新再开始抓取")
        try:
            _apply_and_restart(remote, note)
        except Exception:
            _log("更新失败：\n" + traceback.format_exc())


def _tip_loop():
    """每几秒把"正在做什么"写到托盘图标的悬停文字上。"""
    while True:
        try:
            st = activity.state()
            if st["busy"]:
                d = st["detail"]
                text = f"热度雷达：正在{st['task']}" + (f"\n{d}" if d else "")
            elif st["task"] == "空闲" and st.get("next_live"):
                nxt = min(x for x in (st["next_live"], st["next_slow"]) if x)
                text = f"热度雷达：空闲\n下次抓取 {time.strftime('%H:%M', time.localtime(nxt))}"
            else:
                text = f"热度雷达：{st['task']}"
            _tray.set_tip(text)
        except Exception:
            pass
        time.sleep(3)


def _open_log():
    path = os.path.join(config.DATA_DIR, "radar.log")
    try:
        os.startfile(path)
    except OSError:
        subprocess.Popen(["notepad.exe", path])


def _update_loop():
    time.sleep(90)
    while True:
        if config.AUTO_UPDATE:
            _try_update()
        time.sleep(max(1, float(config.UPDATE_CHECK_HOURS)) * 3600)


# ---------------- 入口 ----------------
def run():
    global _k32, _mutex, _srv, _tray
    if sys.platform != "win32":
        main.run_forever()
        return
    _redirect_output()
    from . import single                       # 只允许登记过的那一份在跑
    if not single.guard():
        _log("检测到非正式安装目录，已拒绝启动：", config.BASE_DIR)
        return
    single.sweep_async()                       # 后台清掉别处的重复副本
    _log("启动（后台模式），版本", updater.local_version()[0])
    if config.SETTINGS_ERROR:
        _log(config.SETTINGS_ERROR)

    # 只允许运行一个：已经在运行时，直接打开面板
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateMutexW.restype = ctypes.c_void_p
    _k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    restarting = os.environ.get("GAMERADAR_RESTART") == "1"
    got = False
    for _ in range(30 if restarting else 1):   # 自动更新重启时，旧进程可能还没完全退出，等一会儿
        _mutex = _k32.CreateMutexW(None, 0, "Local\\GameRadarSingleInstance")
        if ctypes.get_last_error() != 183:
            got = True
            break
        _k32.CloseHandle(ctypes.c_void_p(_mutex))
        time.sleep(0.5)
    if not got:
        webbrowser.open(f"http://{config.SERVER_HOST}:{config.SERVER_PORT}/")
        return

    db.init()
    for attempt in range(10):
        try:
            _srv, url = server.start()
            break
        except OSError as e:
            if attempt == 9:
                _msgbox(f"面板端口 {config.SERVER_PORT} 被占用。\n请打开 radar_data/settings.json，"
                        f"把 SERVER_PORT 改成别的数字（例如 8766）。\n\n{e}")
                return
            time.sleep(1)

    first = db.kv_get("autostart_initialized") is None
    try:
        if first:
            set_autostart(True)                 # 默认开启开机自启
            db.kv_set("autostart_initialized", True)
        elif autostart_enabled():
            set_autostart(True)                 # 文件夹换过位置时更新路径
    except OSError:
        traceback.print_exc()

    from . import tray
    _tray = tray.Tray(url=url, on_refresh=server.RUN_NOW.set,
                      autostart_get=autostart_enabled, autostart_set=set_autostart,
                      on_check_update=lambda: threading.Thread(target=_try_update, args=(True,), daemon=True).start(),
                      version_text=f"版本 {updater.local_version()[0]}",
                      on_status=lambda: webbrowser.open(url + "#status"),
                      on_log=_open_log)
    notify.set_tray(_tray)

    def ready():
        upd = db.kv_get("just_updated")
        if upd:
            db.kv_set("just_updated", None)
            _tray.balloon(f"热度雷达已更新到版本 {upd.get('version')}", (upd.get("note") or "")[:200] or "已自动完成更新。")
        elif first:
            _tray.balloon("热度雷达已在后台运行",
                          "单击右下角托盘图标打开面板；右键可以立即刷新、检查更新、关闭开机自启或退出。")

    _tray.on_ready = ready
    stop = threading.Event()
    _startup_update()                       # 有新版本就先更新，再开始抓取
    threading.Thread(target=main.loop, args=(stop,), daemon=True).start()
    threading.Thread(target=_update_loop, daemon=True).start()
    threading.Thread(target=_tip_loop, daemon=True).start()
    _tray.run()
    stop.set()
    _log("退出")
    os._exit(0)
