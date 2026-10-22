"""全机器只允许一个 GameRadar-main。

规则（v70 起）：
- 规范路径记在注册表 HKCU\\Software\\GameRadar\\Root，另存一份 radar_data/canonical.txt 兜底。
- 从任何非规范目录启动的副本都会被拒绝，并尝试打开规范路径那一份。
- 只拦启动，不碰别处的文件夹（v72 去掉了"把别处同名目录挪进 _quarantine"的清扫：程序不该动你的文件）。
- 只在托盘版（app.run）里拦；命令行 / 开发运行不拦。

规范路径只在第一次运行时自动认领（当前目录即规范路径）。
要真的搬家，见 _tools/move_install.ps1 —— 它先改注册表再搬文件，顺序不能反。
"""
import os
import sys
import ctypes

REG_KEY = r"Software\GameRadar"
REG_VALUE = "Root"
MARKER = "canonical.txt"


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
