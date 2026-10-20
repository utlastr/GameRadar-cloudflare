"""网络请求。只用 Python 自带的库，不需要 pip 安装任何东西。"""
import gzip
import http.cookiejar
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_opener = None
_jar = http.cookiejar.CookieJar()     # 记住网站发的 cookie（TapTap 的防爬验证需要）
_direct = None
_switched = set()

# 有些软件会设置 SSLKEYLOGFILE 环境变量，指向的文件夹不存在时，所有加密连接都会报 "No such file or directory"
_kl = os.environ.get("SSLKEYLOGFILE")
if _kl and not os.path.isdir(os.path.dirname(os.path.abspath(_kl)) or "."):
    os.environ.pop("SSLKEYLOGFILE", None)


def _get_direct():
    """不走系统代理、用一个新建的加密设置的连接（普通连接出系统级错误时用）。"""
    global _direct
    if _direct is None:
        try:
            ctx = ssl.create_default_context()
        except OSError:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            try:
                ctx.load_default_certs()
            except OSError:
                pass
        _direct = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar),
                                              urllib.request.ProxyHandler({}),
                                              urllib.request.HTTPSHandler(context=ctx))
    return _direct


def _errtext(e):
    r = getattr(e, "reason", None)
    if isinstance(r, OSError):
        fn = getattr(r, "filename", None)
        return f"{type(r).__name__} errno={r.errno} {r.strerror or r}" + (f" 文件={fn}" if fn else "")
    return str(e)


def _get_opener():
    global _opener
    if _opener is None:
        handlers = [urllib.request.HTTPCookieProcessor(_jar)]
        if config.PROXY:
            handlers.append(urllib.request.ProxyHandler(
                {"http": config.PROXY, "https": config.PROXY}))
        _opener = urllib.request.build_opener(*handlers)
    return _opener


def _decode(data, headers):
    if (headers.get("Content-Encoding") or "").lower() == "gzip":
        data = gzip.decompress(data)
    return data.decode("utf-8", "replace")


# ---------------- 阿里云 WAF 的 JS 验证（TapTap 用） ----------------
# 不是浏览器的请求会先收到一段脚本，脚本用页面里的 arg1 算出 acw_sc__v2 cookie，带上 cookie 再请求就正常了。
# 这里用 Python 做同样的计算，不需要装浏览器。
_WAF_ARG = re.compile(r"arg1\s*=\s*'([0-9A-Fa-f]{40})'")
_WAF_POS = [0xf, 0x23, 0x1d, 0x18, 0x21, 0x10, 0x1, 0x26, 0xa, 0x9, 0x13, 0x1f, 0x28, 0x1b, 0x16, 0x17, 0x19, 0xd,
            0x6, 0xb, 0x27, 0x12, 0x14, 0x8, 0xe, 0x15, 0x20, 0x1a, 0x2, 0x1e, 0x7, 0x4, 0x11, 0x5, 0x3, 0x1c, 0x22,
            0x25, 0xc, 0x24]
_WAF_KEY = "3000176000856006061501533003690027800375"


def is_waf(text):
    return bool(text) and ("aliyun_waf" in text or "acw_sc__v2" in text or "renderData" in text) and "arg1" in text


def _waf_cookie(arg1):
    out = [""] * len(_WAF_POS)
    for i, ch in enumerate(arg1):
        for j, p in enumerate(_WAF_POS):
            if p == i + 1:
                out[j] = ch
    s = "".join(out)
    return "".join("%02x" % (int(s[i:i + 2], 16) ^ int(_WAF_KEY[i:i + 2], 16))
                   for i in range(0, min(len(s), len(_WAF_KEY)), 2))


def _solve_waf(url, text):
    m = _WAF_ARG.search(text or "")
    if not m:
        return False
    host = urllib.parse.urlparse(url).hostname
    c = http.cookiejar.Cookie(0, "acw_sc__v2", _waf_cookie(m.group(1)), None, False, host, False, False,
                              "/", True, False, int(time.time()) + 1800, False, None, None, {})
    _jar.set_cookie(c)
    return True


def get_text(url, params=None, headers=None, retries=2, timeout=20):
    """GET 一个网址，返回文本。失败会重试，最后还失败就抛出 RuntimeError。"""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    h = {"User-Agent": _UA, "Accept": "*/*", "Accept-Encoding": "gzip",
         "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        h.update(headers)
    last_err = None
    waf_tries = 0
    attempt = 0
    direct = False
    opener = _get_opener()
    while attempt <= retries:
        try:
            req = urllib.request.Request(url, headers=h)
            with opener.open(req, timeout=timeout) as resp:
                text = _decode(resp.read(), resp.headers)
            if is_waf(text) and waf_tries < 2:
                waf_tries += 1
                if _solve_waf(url, text):
                    time.sleep(1)
                    continue          # 带上算好的 cookie 再请求一次
            return text
        except urllib.error.HTTPError as e:
            last_err = e
            body = ""
            try:
                body = _decode(e.read(), e.headers)
            except Exception:
                pass
            if is_waf(body) and waf_tries < 2:
                waf_tries += 1
                if _solve_waf(url, body):
                    time.sleep(1)
                    continue
            if e.code == 404:
                return body
            if e.code in (403, 429):
                time.sleep(15 * (attempt + 1))
                attempt += 1
                continue
        except Exception as e:
            last_err = e
            reason = getattr(e, "reason", None)
            if (not direct and isinstance(reason, OSError)
                    and not isinstance(reason, (socket.timeout, TimeoutError, ConnectionResetError))):
                direct = True
                opener = _get_direct()
                host = urllib.parse.urlparse(url).hostname
                if host not in _switched:
                    _switched.add(host)
                    print(f"{time.strftime('%H:%M:%S')}  {host} 普通连接出错（{_errtext(e)}），改用直连（不走系统代理）再试")
                continue
        time.sleep(2 * (attempt + 1))
        attempt += 1
    host = urllib.parse.urlparse(url)
    raise RuntimeError(f"{_errtext(last_err)}{'（直连也失败）' if direct else ''}（{host.hostname}{host.path}）")


def get_json(url, params=None, headers=None, retries=2, timeout=20):
    text = get_text(url, params, headers, retries, timeout)
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


class HttpError(RuntimeError):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}：{body[:200]}")
        self.code = code
        self.body = body


def post_json(url, body, headers=None, timeout=90):
    """POST 一段 JSON，返回解析后的 JSON。HTTP 错误抛出 HttpError（带状态码），其他错误抛出 RuntimeError。"""
    h = {"User-Agent": _UA, "Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        h.update(headers)
    # body 是字符串时原样发（引力引擎的签名要和请求体一字不差）
    data = (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode("utf-8")
    last = None
    for opener in (_get_opener(), _get_direct()):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method="POST")
            with opener.open(req, timeout=timeout) as resp:
                return json.loads(_decode(resp.read(), resp.headers))
        except urllib.error.HTTPError as e:
            try:
                txt = _decode(e.read(), e.headers)
            except Exception:
                txt = ""
            raise HttpError(e.code, txt)
        except Exception as e:
            last = e
            reason = getattr(e, "reason", None)
            if not isinstance(reason, OSError) or isinstance(reason, (socket.timeout, TimeoutError)):
                break
    raise RuntimeError(_errtext(last) if last else "未知错误")


def post_form(url, fields, headers=None, timeout=30):
    """POST 表单（application/x-www-form-urlencoded），返回解析后的 JSON。HTTP 错误抛出 HttpError。"""
    h = {"User-Agent": _UA, "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    if headers:
        h.update(headers)
    data = urllib.parse.urlencode(fields).encode("utf-8")
    last = None
    for opener in (_get_opener(), _get_direct()):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method="POST")
            with opener.open(req, timeout=timeout) as resp:
                return json.loads(_decode(resp.read(), resp.headers))
        except urllib.error.HTTPError as e:
            try:
                txt = _decode(e.read(), e.headers)
            except Exception:
                txt = ""
            raise HttpError(e.code, txt)
        except Exception as e:
            last = e
            reason = getattr(e, "reason", None)
            if not isinstance(reason, OSError) or isinstance(reason, (socket.timeout, TimeoutError)):
                break
    raise RuntimeError(_errtext(last) if last else "未知错误")


def get_json_strict(url, params=None, headers=None, retries=2, timeout=20):
    """和 get_json 一样，但拿不到 JSON 时抛出写明原因的错误（面板上能看到）。"""
    text = get_text(url, params, headers, retries, timeout)
    if not text:
        raise RuntimeError("返回内容是空的")
    try:
        return json.loads(text)
    except ValueError:
        pass
    if is_waf(text):
        raise RuntimeError("被 TapTap 的防爬验证（阿里云 WAF）拦下，自动验证没有通过。把这条错误发给助手")
    snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))[:120]
    raise RuntimeError("返回的不是数据而是网页：" + snippet)
