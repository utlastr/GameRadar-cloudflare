"""TapTap 抓取用的第三方库：requests、beautifulsoup4、lxml（和参考网站 as167888 的爬虫用的一样）。

第一次用到时自动安装（走清华镜像，装不上再试官方源）。装不上时 TapTap 退回 Python 自带的网络库，
日志里会写明，手动安装的命令：python -m pip install requests beautifulsoup4
"""
import importlib
import os
import subprocess
import sys
import time

_MIRRORS = ["https://pypi.tuna.tsinghua.edu.cn/simple", "https://mirrors.aliyun.com/pypi/simple", ""]
_tried_at = 0
_ok = None


def _python():
    exe = sys.executable or "python"
    d, name = os.path.split(exe)
    if name.lower() == "pythonw.exe":
        p = os.path.join(d, "python.exe")
        if os.path.exists(p):
            return p
    return exe


def _have():
    try:
        importlib.invalidate_caches()
        import requests  # noqa: F401
        import bs4  # noqa: F401
        return True
    except Exception:
        return False


def _pip(args):
    flags = 0x08000000 if sys.platform == "win32" else 0      # 不弹黑色窗口
    try:
        r = subprocess.run([_python(), "-m", "pip", "install", "--disable-pip-version-check"] + args,
                           capture_output=True, text=True, timeout=180, creationflags=flags)
        return r.returncode == 0, (r.stderr or r.stdout or "").strip()[-300:]
    except Exception as e:
        return False, str(e)


def ensure():
    """requests 和 bs4 都能用返回 True。装不上返回 False（一天最多自动试一次）。"""
    global _tried_at, _ok
    if _ok:
        return True
    if time.time() - _tried_at < 86400:
        return False                     # 今天已经试过装不上，先不重试
    if _have():
        _ok = True
        return True
    _tried_at = time.time()
    print(f"{time.strftime('%H:%M:%S')}  正在安装 TapTap 抓取用的 requests、beautifulsoup4（只装一次，约 1 分钟）")
    err = ""
    for mirror in _MIRRORS:
        extra = ["-i", mirror] if mirror else []
        for user in ([], ["--user"]):
            ok, err = _pip(user + extra + ["requests", "beautifulsoup4"])
            if not ok:
                continue
            # --user 装的位置可能还不在搜索路径里
            try:
                import site
                p = site.getusersitepackages()
                if p not in sys.path:
                    sys.path.append(p)
            except Exception:
                pass
            if _have():
                _ok = True
                _pip(user + extra + ["lxml"])      # 榜单页他用 lxml 解析；装不上就用自带的解析器，结果一样
                print(f"{time.strftime('%H:%M:%S')}  requests、beautifulsoup4 安装完成")
                return True
    print(f"{time.strftime('%H:%M:%S')}  requests、beautifulsoup4 自动安装失败，TapTap 先用 Python 自带的网络库。"
          f"可以手动在命令行运行：python -m pip install requests beautifulsoup4 。错误：{err}")
    return False
