"""Windows 托盘图标。

左键单击：打开面板
右键菜单：打开面板 / 立即刷新数据 / 开机自动启动（可勾选）/ 退出

只用 Python 自带的 ctypes 调用 Windows 接口，不需要安装任何东西。
"""
import ctypes
import math
import os
import struct
import traceback
import webbrowser
from ctypes import wintypes

from . import config

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CONTEXTMENU = 0x007B
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO, NIIF_NOSOUND = 0x01, 0x10
MF_STRING, MF_CHECKED, MF_SEPARATOR = 0x0000, 0x0008, 0x0800
TPM_RIGHTBUTTON, TPM_BOTTOMALIGN, TPM_RETURNCMD = 0x0002, 0x0020, 0x0100
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
SM_CXSMICON = 49
IDI_APPLICATION = 32512
ID_OPEN, ID_REFRESH, ID_AUTOSTART, ID_EXIT, ID_UPDATE, ID_VERSION, ID_STATUS, ID_LOG = 1, 2, 3, 4, 5, 6, 7, 8
MF_GRAYED = 0x0001


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


def _sig(fn, restype, *argtypes):
    fn.restype = restype
    fn.argtypes = list(argtypes)


_sig(user32.DefWindowProcW, LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32.RegisterClassW, wintypes.ATOM, ctypes.POINTER(WNDCLASSW))
_sig(user32.CreateWindowExW, wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
     wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
_sig(user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.LoadImageW, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
     ctypes.c_int, ctypes.c_int, wintypes.UINT)
_sig(user32.LoadIconW, wintypes.HICON, wintypes.HINSTANCE, wintypes.LPVOID)
_sig(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int)
_sig(user32.CreatePopupMenu, wintypes.HMENU)
_sig(user32.AppendMenuW, wintypes.BOOL, wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR)
_sig(user32.SetMenuDefaultItem, wintypes.BOOL, wintypes.HMENU, wintypes.UINT, wintypes.UINT)
_sig(user32.TrackPopupMenu, ctypes.c_int, wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
     ctypes.c_int, wintypes.HWND, wintypes.LPVOID)
_sig(user32.DestroyMenu, wintypes.BOOL, wintypes.HMENU)
_sig(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
_sig(user32.SetForegroundWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
_sig(user32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
_sig(user32.DispatchMessageW, LRESULT, ctypes.POINTER(wintypes.MSG))
_sig(user32.PostQuitMessage, None, ctypes.c_int)
_sig(user32.RegisterWindowMessageW, wintypes.UINT, wintypes.LPCWSTR)
_sig(shell32.Shell_NotifyIconW, wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW))
_sig(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)


# ---------------- 图标（程序自己画，不用额外文件） ----------------
def _clamp(v):
    return max(0.0, min(1.0, v))


def _icon_pixels(size):
    """蓝色圆底 + 白色圆环 + 中心白点。返回自下而上的 BGRA 像素。"""
    px = bytearray()
    c = (size - 1) / 2.0
    R = size / 2.0 - 0.5
    ring_r, ring_w, dot_r = R * 0.58, max(1.0, size / 14.0), R * 0.2
    base = (37, 99, 235)
    for y in range(size - 1, -1, -1):
        for x in range(size):
            d = math.hypot(x - c, y - c)
            a = _clamp(R + 0.5 - d)
            if a <= 0:
                px += b"\x00\x00\x00\x00"
                continue
            w = max(_clamp(ring_w / 2 + 0.5 - abs(d - ring_r)), _clamp(dot_r + 0.5 - d))
            r, g, b = (int(base[i] * (1 - w) + 255 * w) for i in range(3))
            px += bytes((b, g, r, int(a * 255)))
    return bytes(px)


def write_icon(path, sizes=(16, 20, 24, 32, 40, 48, 64)):
    images = []
    for s in sizes:
        pixels = _icon_pixels(s)
        mask = bytes(((s + 31) // 32) * 4 * s)
        header = struct.pack("<IiiHHIIiiII", 40, s, s * 2, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0)
        images.append((s, header + pixels + mask))
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for s, data in images:
        out += struct.pack("<BBBBHHII", s, s, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _, data in images:
        out += data
    with open(path, "wb") as f:
        f.write(out)


# ---------------- 托盘 ----------------
class Tray:
    def __init__(self, url, on_refresh, autostart_get, autostart_set, on_check_update=None, version_text="",
                 on_status=None, on_log=None):
        self.url = url
        self.on_status = on_status
        self.on_log = on_log
        self.on_refresh = on_refresh
        self.on_check_update = on_check_update
        self.version_text = version_text
        self.autostart_get = autostart_get
        self.autostart_set = autostart_set
        self.on_ready = None
        self.hwnd = None
        self.hicon = None
        self._proc = None
        self._taskbar_created = 0
        self._tip = "热度雷达 - 后台运行中"

    def _nid(self, flags):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        return nid

    def _add(self):
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.hicon
        nid.szTip = self._tip[:127]
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

    def set_tip(self, text):
        self._tip = text
        if self.hwnd:
            nid = self._nid(NIF_TIP)
            nid.szTip = text[:127]
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def balloon(self, title, msg, sound=False):
        """右下角通知。"""
        if not self.hwnd:
            return False
        nid = self._nid(NIF_INFO)
        nid.szInfoTitle = title[:63]
        nid.szInfo = msg[:255]
        nid.dwInfoFlags = NIIF_INFO | (0 if sound else NIIF_NOSOUND)
        return bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))

    def open(self):
        webbrowser.open(self.url)

    def _menu(self):
        try:
            on = bool(self.autostart_get())
        except Exception:
            on = False
        m = user32.CreatePopupMenu()
        user32.AppendMenuW(m, MF_STRING, ID_OPEN, "打开面板")
        if self.on_status:
            user32.AppendMenuW(m, MF_STRING, ID_STATUS, "查看运行情况")
        user32.AppendMenuW(m, MF_STRING, ID_REFRESH, "立即刷新数据")
        if self.on_log:
            user32.AppendMenuW(m, MF_STRING, ID_LOG, "打开日志文件")
        user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(m, MF_STRING | (MF_CHECKED if on else 0), ID_AUTOSTART, "开机自动启动")
        if self.on_check_update:
            user32.AppendMenuW(m, MF_STRING, ID_UPDATE, "检查更新")
        user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        if self.version_text:
            user32.AppendMenuW(m, MF_STRING | MF_GRAYED, ID_VERSION, self.version_text)
        user32.AppendMenuW(m, MF_STRING, ID_EXIT, "退出")
        user32.SetMenuDefaultItem(m, ID_OPEN, 0)
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(m, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(m)
        if cmd == ID_OPEN:
            self.open()
        elif cmd == ID_REFRESH:
            self.on_refresh()
            self.balloon("热度雷达", "已开始刷新数据，几分钟后面板会自动更新。")
        elif cmd == ID_AUTOSTART:
            try:
                self.autostart_set(not on)
                self.balloon("热度雷达", "已关闭开机自动启动。" if on else "已开启开机自动启动。")
            except Exception as e:
                self.balloon("热度雷达", f"设置失败：{e}")
        elif cmd == ID_STATUS and self.on_status:
            self.on_status()
        elif cmd == ID_LOG and self.on_log:
            self.on_log()
        elif cmd == ID_UPDATE and self.on_check_update:
            self.on_check_update()
        elif cmd == ID_EXIT:
            user32.DestroyWindow(self.hwnd)

    def remove_icon(self):
        if self.hwnd:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event == WM_LBUTTONUP:
                    self.open()
                elif event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._menu()
                return 0
            if self._taskbar_created and msg == self._taskbar_created:
                self._add()          # 资源管理器重启后重新加图标
                return 0
            if msg == WM_DESTROY:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            traceback.print_exc()
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self):
        """在主线程调用，一直运行到点“退出”。"""
        hinst = kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = "GameRadarTrayWindow"
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, "GameRadarTrayWindow", "热度雷达", 0, 0, 0, 0, 0,
                                           None, None, hinst, None)
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        try:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            path = os.path.join(config.DATA_DIR, "radar.ico")
            write_icon(path)
            cx = user32.GetSystemMetrics(SM_CXSMICON) or 16
            self.hicon = user32.LoadImageW(None, path, IMAGE_ICON, cx, cx, LR_LOADFROMFILE)
        except Exception:
            traceback.print_exc()
        if not self.hicon:
            self.hicon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        self._add()
        if self.on_ready:
            self.on_ready()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
