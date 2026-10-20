"""自动更新：检查 GitHub 上的新版本，下载后替换程序文件，然后自动重启。

只替换程序文件（radar 文件夹和根目录的几个文件）。radar_data 里的数据、设置、备份都不会动。
版本号在仓库根目录的 version.txt 里，每次发布加 1。
"""
import io
import json
import os
import shutil
import tempfile
import urllib.request
import zipfile

from . import config

REPO = "utlastr/GameRadar"
BRANCH = "main"
LOCAL_VERSION_FILE = os.path.join(config.BASE_DIR, "version.txt")


def _open(url, accept="application/vnd.github+json", timeout=30):
    headers = {"User-Agent": "GameRadar-Updater", "Accept": accept}
    if config.GITHUB_TOKEN:
        headers["Authorization"] = "Bearer " + config.GITHUB_TOKEN.strip()
    handlers = []
    if config.PROXY:
        handlers.append(urllib.request.ProxyHandler({"http": config.PROXY, "https": config.PROXY}))
    return urllib.request.build_opener(*handlers).open(urllib.request.Request(url, headers=headers), timeout=timeout)


def _parse(text):
    """version.txt 第一行是版本号，第二行起是更新说明。"""
    lines = (text or "").replace("\ufeff", "").strip().splitlines()   # version.txt 带 BOM 时别读成 0
    try:
        num = int(lines[0].strip())
    except (IndexError, ValueError):
        num = 0
    return num, "\n".join(lines[1:]).strip()


def local_version():
    try:
        with open(LOCAL_VERSION_FILE, encoding="utf-8") as f:
            return _parse(f.read())
    except OSError:
        return 0, ""


def _read(url, accept="application/vnd.github+json", timeout=30, tries=3):
    """国内连 GitHub 时好时坏：超时重试几次。"""
    import time
    err = None
    for i in range(tries):
        try:
            with _open(url, accept=accept, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            err = e
            if "401" in str(e) or "404" in str(e):
                break
            time.sleep(3 * (i + 1))
    raise err


def remote_version():
    url = f"https://api.github.com/repos/{REPO}/contents/version.txt?ref={BRANCH}"
    return _parse(_read(url, accept="application/vnd.github.raw").decode("utf-8", "replace"))


def check():
    """返回 (有没有新版本, 远程版本号, 更新说明)。网络失败会抛出异常。"""
    remote, note = remote_version()
    local, _ = local_version()
    return remote > local, remote, note


def apply():
    """下载最新版并替换程序文件。"""
    data = _read(f"https://api.github.com/repos/{REPO}/zipball/{BRANCH}", timeout=120)
    tmp = tempfile.mkdtemp(prefix="gameradar_")
    try:
        zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
        tops = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
        if not tops:
            raise RuntimeError("下载的压缩包是空的")
        src = os.path.join(tmp, tops[0])
        if not os.path.isfile(os.path.join(src, "radar", "__init__.py")):
            raise RuntimeError("下载的文件不完整")
        base = config.BASE_DIR
        cur, new, old = (os.path.join(base, n) for n in ("radar", "radar.new", "radar.old"))
        for d in (new, old):
            shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(os.path.join(src, "radar"), new)
        try:
            os.replace(cur, old)
            try:
                os.replace(new, cur)
            except OSError:
                os.replace(old, cur)
                raise
            shutil.rmtree(old, ignore_errors=True)
        except OSError:
            # 文件夹被占用不能整体换掉时，逐个覆盖
            shutil.copytree(os.path.join(src, "radar"), cur, dirs_exist_ok=True)
            shutil.rmtree(new, ignore_errors=True)
        for name in os.listdir(src):
            p = os.path.join(src, name)
            if os.path.isfile(p):
                shutil.copy2(p, os.path.join(base, name))   # version.txt 也在这里一起更新
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
