"""全机器只允许一个 GameRadar-main。

规则（v70 起）：
- 规范路径记在注册表 HKCU\\Software\\GameRadar\\Root，另存一份 radar_data/canonical.txt 兜底。
- 从任何非规范目录启动的副本都会被拒绝，并尝试打开规范路径那一份。
- 启动时顺手清扫：把别处的同名目录/安装包挪进 _quarantine，保证"永远只有一个"。

规范路径只在第一次运行时自动认领（当前目录即规范路径）。
要真的搬家，见 _tools/move_install.ps1 —— 它先改注册表再搬文件，顺序不能反。
"""
import os
import re
import sys
import ctypes
import threading
import shutil
import time

REG_KEY = r"Software\GameRadar"
REG_VALUE = "Root"
MARKER = "canonical.txt"

# 这些目录里的同名文件夹会被清扫（都不该放雷达）
SCAN_ROOTS = ("%USERPROFILE%", "%USERPROFILE%\\Desktop", "%USERPROFILE%\\Downloads",
              "%USERPROFILE%\\Documents")
SKIP_DIRS = {"AppData", ".git", "node_modules", "__pycache__", ".cache", "Local Settings",
             "Application Data", "My Documents", "Start Menu", "SendTo", "NetHood", "PrintHood",
             "Recent", "Templates", "Cookies", "Favorites", "Links", "Saved Games", "Searches",
             "Contacts", "3D Objects", "OneDrive"}


def base_dir():
    """程序根目录（含 GameRadar.pyw 的那层）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _norm(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(p))).rstrip("\\")


def _marker_path():
    return os.path.join(base_dir(), "radar_data", MARKER)


def _reg_read():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
            v, _ = winreg.QueryValueEx(k, REG_VALUE)
            return (v or "").strip() or None
    except Exception:
        return None


def _reg_write(path):
    try:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
            winreg.SetValueEx(k, REG_VALUE, 0, winreg.REG_SZ, path)
        return True
    except Exception:
        return False


def canonical():
    """当前被认定的唯一目录；None 表示还没认领过。"""
    p = _reg_read()
    if p:
        return p
    try:
        with open(_marker_path(), encoding="utf-8") as f:
            p = f.readline().strip()
        return p or None
    except OSError:
        return None


def claim(path=None):
    """把 path（默认当前目录）记成唯一目录。"""
    p = path or base_dir()
    _reg_write(p)
    try:
        os.makedirs(os.path.dirname(_marker_path()), exist_ok=True)
        with open(_marker_path(), "w", encoding="utf-8") as f:
            f.write(p + "\n")
    except OSError:
        pass
    return p


def is_canonical():
    root = canonical()
    return (root is None) or (_norm(root) == _norm(base_dir()))


def _msg(text, title="热度雷达"):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:
        try:
            sys.stderr.write(text + "\n")
        except Exception:
            pass


def _open_canonical(root):
    try:
        launcher = os.path.join(root, "GameRadar.pyw")
        if os.path.exists(launcher):
            os.startfile(launcher)          # noqa: S606 —— 只开自己
            return True
    except Exception:
        pass
    return False


def guard(quiet=False):
    """返回 True 表示允许运行；False 表示这是多余副本，已拦下。"""
    root = canonical()
    if root is None:                        # 第一次运行：当前目录就是这个程序的合法位置
        claim()
        return True
    if _norm(root) == _norm(base_dir()):
        return True
    if not os.path.isdir(root):
        # 登记的位置已经不存在了（搬过家、注册表没跟上）。再拦下去用户只能手动改注册表，
        # 所以直接把当前位置认领成唯一位置。
        claim()
        return True
    if not quiet:
        ok = _open_canonical(root)
        _msg("这份雷达是多余的副本，已阻止启动。\n\n"
             "唯一允许的位置：\n" + root +
             ("\n\n已经帮你打开那一份。" if ok else "\n\n但它现在不存在，请把它放回去。"))
    return False


# ---------------- 清扫 ----------------
def _quarantine_dir():
    return os.path.join(canonical() or base_dir(), "_quarantine")


_DUPE_RE = re.compile(r"^gameradarmain(\(\d+\))?$")


def _is_dupe_name(name):
    """GameRadar-main / Game Radar.Main / GameRadar-main (2) 这类都算重复。"""
    bare = re.sub(r"[\s._\-]", "", name).lower()
    return bool(_DUPE_RE.match(bare))


def sweep(verbose=False):
    """把规范路径之外的 GameRadar-main* 目录挪进 _quarantine，返回被挪走的列表。"""
    root = canonical()
    if not root:
        return []
    root_n = _norm(root)
    qdir = os.path.join(root, "_quarantine", time.strftime("%Y%m%d"))
    moved = []
    seen = set()

    def scan(path, depth):
        if depth <= 0 or not path or not os.path.isdir(path):
            return
        if _norm(path) in seen:
            return
        seen.add(_norm(path))
        try:
            entries = list(os.scandir(path))
        except OSError:
            return
        for e in entries:
            try:
                if not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            name = e.name
            if _norm(e.path) == root_n:
                continue                     # 规范路径本身
            if _is_dupe_name(name) and os.path.abspath(e.path) != os.path.abspath(root):
                try:
                    os.makedirs(qdir, exist_ok=True)
                    dst = os.path.join(qdir, name)
                    i = 1
                    while os.path.exists(dst):
                        i += 1
                        dst = os.path.join(qdir, f"{name}_{i}")
                    shutil.move(e.path, dst)
                    moved.append((e.path, dst))
                except Exception as exc:     # noqa: BLE001
                    if verbose:
                        print("跳过", e.path, exc)
                continue
            if name in SKIP_DIRS or name.startswith(".") or name.startswith("$"):
                continue
            scan(e.path, depth - 1)

    for r in SCAN_ROOTS:
        p = os.path.expandvars(r)
        if os.path.isdir(p):
            scan(p, 8)

    if moved:
        try:
            os.makedirs(qdir, exist_ok=True)
            with open(os.path.join(qdir, "sweep.log"), "a", encoding="utf-8") as f:
                for src, dst in moved:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{src}\t->\t{dst}\n")
        except OSError:
            pass
    return moved


def sweep_async():
    threading.Thread(target=lambda: _safe_sweep(), daemon=True).start()


def _safe_sweep():
    try:
        sweep()
    except Exception:
        pass
