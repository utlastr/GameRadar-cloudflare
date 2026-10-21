# -*- coding: utf-8 -*-
"""用 CDP 驱动本地浏览器（只用标准库，不装任何东西）。

浏览器要在 127.0.0.1:9222 开着调试端口（Edge / Chrome 都行）。
本模块负责三件事：
  1. 找到一个页面（或新开一个）并连上它的 WebSocket；
  2. 在页面里执行 JS、取回结果；
  3. 直接打开网址、等页面加载完，把渲染后的 HTML 拿回来。

比直接打接口强的地方：带着浏览器的登录状态和 cookie，百度指数、抖音、
快手这些要登录的页面能直接读；被风控时换个页面重新访问即可。
"""
import base64
import json
import os
import socket
import struct
import time
import urllib.request

DEFAULT_PORT = int(os.environ.get("CDP_PORT") or 9222)


class CDPError(RuntimeError):
    pass


# ---------------- 最小 WebSocket 客户端 ----------------

class _WS:
    def __init__(self, url, timeout=30):
        assert url.startswith("ws://"), "只支持 ws://"
        rest = url[5:]
        host_port, self.path = (rest.split("/", 1) + [""])[:2]
        self.path = "/" + self.path
        host, _, port = host_port.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (self.path, host_port, key))
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("握手失败：连接被关闭")
            buf += chunk
        head, _, self._rest = buf.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise CDPError("握手失败：" + head.decode("latin-1")[:200])
        self._id = 0
        self._buf = self._rest

    def _recv(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CDPError("连接断开")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, payload):
        data = json.dumps(payload).encode()
        head = bytearray([0x81])
        n = len(data)
        mask = os.urandom(4)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        head += mask
        self.sock.sendall(bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        while True:
            b0, b1 = self._recv(2)
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv(8))[0]
            mk = self._recv(4) if masked else None
            data = self._recv(n)
            if mk:
                data = bytes(b ^ mk[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise CDPError("浏览器关闭了连接")
            if opcode == 0x9:                     # ping
                continue
            if opcode in (0x1, 0x2):
                return data.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Tab:
    """一个浏览器页面。"""

    def __init__(self, ws_url):
        self.ws = _WS(ws_url)
        self._seq = 0

    def call(self, method, params=None, timeout=40):
        self._seq += 1
        mid = self._seq
        self.ws.send({"id": mid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") != mid:
                continue                          # 事件，跳过
            if "error" in msg:
                raise CDPError("%s 失败：%s" % (method, msg["error"].get("message")))
            return msg.get("result") or {}
        raise CDPError("%s 超时" % method)

    # 页面操作
    def goto(self, url, wait=2.5, timeout=45):
        self.call("Page.enable")
        self.call("Page.navigate", {"url": url}, timeout=timeout)
        time.sleep(wait)
        return self

    def js(self, expr, timeout=40):
        """在页面里执行 JS，返回它的值（对象会被转成 JSON 再解回来）。"""
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True},
                      timeout=timeout)
        if r.get("exceptionDetails"):
            raise CDPError("JS 出错：" + str(r["exceptionDetails"].get("text"))[:200])
        return (r.get("result") or {}).get("value")

    def html(self):
        return self.js("document.documentElement.outerHTML") or ""

    def text(self):
        return self.js("document.body ? document.body.innerText : ''") or ""

    def title(self):
        return self.js("document.title") or ""

    def cookies(self, urls=None):
        p = {"urls": urls} if urls else {}
        out = self.call("Network.getCookies", p).get("cookies") or []
        return {c["name"]: c["value"] for c in out}

    def close(self):
        self.ws.close()


# ---------------- 找浏览器、开页面 ----------------

def _http(path, port=None, method="GET"):
    url = "http://127.0.0.1:%d%s" % (port or DEFAULT_PORT, path)
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def available(port=None):
    try:
        _http("/json/version", port)
        return True
    except Exception:                             # noqa: BLE001
        return False


def version(port=None):
    return _http("/json/version", port)


def targets(port=None):
    return _http("/json/list", port)


def _browser_url(port=None):
    return _http("/json/version", port)["webSocketDebuggerUrl"]


def create_target(url="about:blank", port=None, background=True):
    """在浏览器层面建一个标签。

    background=True 时不抢焦点（Chrome 的 Target.createTarget 支持），
    你的当前页面不会被切走。注意：页面不能冻结，冻结了里面的 JS 就不跑了。
    """
    b = Tab(_browser_url(port))
    try:
        r = b.call("Target.createTarget", {"url": url, "background": bool(background)}, timeout=30)
        tid = r.get("targetId")
    finally:
        b.close()
    if not tid:
        raise CDPError("建标签失败")
    deadline = time.time() + 15
    while time.time() < deadline:
        for t in targets(port):
            if t.get("id") == tid and t.get("webSocketDebuggerUrl"):
                return Tab(t["webSocketDebuggerUrl"])
        time.sleep(0.2)
    raise CDPError("建了标签但连不上")


def open_tab(url="about:blank", port=None, wait=2.0, background=True):
    """新开一个页面并连上。background=True 时不抢焦点（默认）。"""
    try:
        tab = create_target(url, port, background)
    except Exception:                             # noqa: BLE001
        path = "/json/new?" + urllib.parse.quote(url, safe="")
        t = None
        for method in ("PUT", "GET"):
            try:
                t = _http(path, port, method=method)
                break
            except Exception:                     # noqa: BLE001
                continue
        if not t:
            raise CDPError("开不了新页面")
        tab = Tab(t["webSocketDebuggerUrl"])
    if wait:
        time.sleep(wait)
    return tab


def find_tab(match="", port=None):
    """找已打开、网址里含 match 的页面；没有就新开一个。"""
    for t in targets(port):
        if t.get("type") == "page" and match in (t.get("url") or ""):
            return Tab(t["webSocketDebuggerUrl"])
    return open_tab("about:blank", port)


def attach(match, port=None, exclude=("127.0.0.1",)):
    """挂到已经打开的页面上，不新开、不跳转、不刷新。

    match 是网址里的片段（比如 "douyin.com"）。找不到返回 None。
    这是最不打扰人的用法：只在你已经打开的页面上读数据。
    """
    for t in targets(port):
        if t.get("type") != "page":
            continue
        url = t.get("url") or ""
        if match and match not in url:
            continue
        if any(x in url for x in exclude):
            continue
        if not t.get("webSocketDebuggerUrl"):
            continue
        return Tab(t["webSocketDebuggerUrl"])
    return None


def close_tab(tab):
    """用完把页面关掉，别在人家标签栏里留垃圾。"""
    try:
        tab.call("Page.close")
    except Exception:                             # noqa: BLE001
        pass
    tab.close()


def close_target(target_id, port=None):
    """按 targetId 关页面。没有 Tab 对象时（比如清理残留标签）用这个。"""
    try:
        _http("/json/close/" + target_id, port)
        return True
    except Exception:                             # noqa: BLE001
        return False


# ---------------- 无头浏览器：看得见的是零 ----------------

_HEADLESS = {}


def launch_headless(port=9223, timeout=25):
    """起一个不带窗口的 Edge/Chrome，专门跑公开数据的抓取。

    用独立的临时 profile，不碰你正在用的那个，窗口一个都不开。
    已经起过就直接复用。
    """
    if port in _HEADLESS and _HEADLESS[port].poll() is None:
        return port
    if available(port):
        return port
    exe = _find_browser()
    if not exe:
        raise CDPError("找不到 Edge 或 Chrome，没法起无头浏览器")
    prof = os.path.join(os.environ.get("TEMP") or ".", "radar_headless_%d" % port)
    os.makedirs(prof, exist_ok=True)
    args = [exe, "--headless=new", "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=%d" % port, "--user-data-dir=%s" % prof,
            "--no-first-run", "--no-default-browser-check", "--disable-gpu",
            "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
            "--disable-features=CalculateNativeWinOcclusion", "about:blank"]
    import subprocess
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=0x08000000)  # 无窗口控制台
    _HEADLESS[port] = p
    deadline = time.time() + timeout
    while time.time() < deadline:
        if available(port):
            return port
        time.sleep(0.5)
    raise CDPError("无头浏览器起不来（端口 %d）" % port)


def _find_browser():
    cands = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def _fill_domain(cookies):
    """Network.getCookies 返回的可能没有 domain，补上，否则 setCookie 会失败。"""
    for c in cookies:
        if not c.get("domain"):
            c["domain"] = ".bilibili.com"
    return cookies


def copy_cookies(src_port, dst_port, domains=("bilibili.com",)):
    """把登录态从一个浏览器搬到另一个。

    无头浏览器是全新 profile、没有登录，B站会按匿名严格限流；
    把已登录那份的 cookie 搬过去，既不出窗口又有登录态。
    返回搬了多少条。
    """
    got = []
    tab = open_tab("https://www.bilibili.com/", src_port, wait=3.5, background=True)
    try:
        try:
            tab.call("Network.enable")
        except Exception:                          # noqa: BLE001
            pass
        got = tab.call("Network.getCookies", {"urls": ["https://www.bilibili.com"]}).get("cookies") or []
        if not got:
            got = tab.call("Network.getCookies", {}).get("cookies") or []
    finally:
        close_tab(tab)
    pick = [c for c in got if any(d in (c.get("domain") or ".bilibili.com") for d in domains)]
    if not pick:
        return 0

    n = 0
    dst = open_tab("https://www.bilibili.com/", dst_port, wait=2.0, background=True)
    try:
        try:
            dst.call("Network.enable")
        except Exception:                          # noqa: BLE001
            pass
        for c in pick:
            p = {"name": c["name"], "value": c["value"],
                 "domain": c.get("domain") or ".bilibili.com",
                 "path": c.get("path") or "/",
                 "secure": bool(c.get("secure")), "httpOnly": bool(c.get("httpOnly"))}
            if c.get("expires") and c["expires"] > 0:
                p["expires"] = c["expires"]
            if c.get("sameSite"):
                p["sameSite"] = c["sameSite"]
            try:
                r = dst.call("Network.setCookie", p, timeout=10)
                if r.get("success"):
                    n += 1
            except Exception:                     # noqa: BLE001
                pass
    finally:
        close_tab(dst)
    return n


def stop_headless(port=9223):
    p = _HEADLESS.pop(port, None)
    if p and p.poll() is None:
        try:
            p.terminate()
        except Exception:                         # noqa: BLE001
            pass


def eval_on(url, expr, port=None, wait=3.0, background=True):
    """打开网址、等一会、执行 JS。"""
    tab = open_tab(url, port, wait, background=background)
    try:
        return tab.js(expr)
    finally:
        tab.close()
