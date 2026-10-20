"""B站 wbi 签名。

B站从 2023 年起给一部分接口加了 wbi 校验：请求要带 wts（时间戳）和 w_rid（签名）。
签名算法：拿 nav 接口返回的两个 key 拼起来，按固定表重排，取前 32 位当密钥；
把参数按名字排序拼成 query，接上密钥做 md5。

两个 key 大约一天换一次，这里缓存 10 小时。
"""
import hashlib
import time
import urllib.parse

from ..net import get_json_strict

# 固定重排表（B站前端代码里的 mixinKeyEncTab，64 项）
_MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9,
          42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1,
          60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]

_TTL = 10 * 3600
_cache = {"key": "", "ts": 0}

_NAV = "https://api.bilibili.com/x/web-interface/nav"
_HEAD = {"Referer": "https://www.bilibili.com/"}


def _filename(url):
    """从 https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png 里取文件名。"""
    name = (url or "").rsplit("/", 1)[-1]
    return name.split(".", 1)[0]


def _mix(key):
    return "".join(key[i] for i in _MIXIN)[:32] if len(key) >= 64 else ""


def get_key(force=False):
    """拿到（并缓存）mixin key。取不到就抛 RuntimeError，交给调用方写进运行情况。"""
    now = time.time()
    if not force and _cache["key"] and now - _cache["ts"] < _TTL:
        return _cache["key"]
    d = get_json_strict(_NAV, headers=_HEAD, retries=1, timeout=12)
    if not isinstance(d, dict):
        raise RuntimeError("wbi 取 key：返回的不是数据")
    data = d.get("data") or {}
    img = (data.get("wbi_img") or {})
    key = _mix(_filename(img.get("img_url")) + _filename(img.get("sub_url")))
    if not key:
        # 未登录时 code = -101，但 wbi_img 一般照样给；真没有就是接口变了
        raise RuntimeError("wbi 取 key 失败：nav 返回里没有 wbi_img（code=%s）" % d.get("code"))
    _cache.update(key=key, ts=now)
    return key


def sign(params):
    """返回加了 wts / w_rid 的新参数字典。"""
    p = dict(params or {})
    p["wts"] = int(time.time())
    items = sorted(p.items())
    # B站会去掉值里的 !'()* 再算签名，顺序不能变
    clean = [(k, "".join(ch for ch in str(v) if ch not in "!'()*")) for k, v in items]
    query = urllib.parse.urlencode(clean)
    p["w_rid"] = hashlib.md5((query + get_key()).encode("utf-8")).hexdigest()
    return p


def get_signed(url, params, retries=1, timeout=12, headers=None):
    """带签名请求一个接口，返回 data 部分。签名过期（-403）时自动换一次 key 重试。"""
    h = dict(_HEAD)
    h.update(headers or {})
    d = get_json_strict(url, sign(params), headers=h, retries=retries, timeout=timeout)
    if isinstance(d, dict) and d.get("code") == -403:
        get_key(force=True)
        d = get_json_strict(url, sign(params), headers=h, retries=retries, timeout=timeout)
    return d
