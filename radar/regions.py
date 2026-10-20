"""地图（v69）：目标市场（settings.json 的 MARKETS，不填用 DEF_MARKETS）的省 / 州级口味。

1. 边界：Natural Earth 10m 一级行政区（公有领域），后台下载一次，只留目标市场，简化后存 radar_data/regions.json，
   同时算出每个行政区覆盖哪些人口格子（人口、县级收入按人口加权）
2. 每个目标市场取本国榜单份额最高的 N_GAMES 款游戏 + 几个平台词，查各省 / 州最近 12 个月的搜索热度：
   Google Trends（游戏优先用"主题"，不同语言的名字能对上）；中国大陆用百度指数（settings.json 填 BAIDU_COOKIE）
3. 偏好倍数 = 该省热度 / 全国按人口加权的平均热度。worldmap.compute 用它把国家份额拆到省 / 州
"""
import json
import math
import os
import random
import re
import struct
import threading
import time
import traceback
import unicodedata
import urllib.parse
from array import array
from collections import defaultdict

from . import config, db
from .net import get_json, get_text

NE_URLS = [
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_admin_1_states_provinces.geojson",
    "https://cdn.jsdelivr.net/gh/nvkelso/natural-earth-vector@master/geojson/ne_10m_admin_1_states_provinces.geojson",
]
GEO = os.path.join(config.DATA_DIR, "regions.json")
NE_TMP = os.path.join(config.DATA_DIR, "ne_admin1.geojson")
GEO_V = 1
N_GAMES = 25
PLATFORMS = ["Steam", "PlayStation", "Xbox", "Nintendo Switch"]
PLAT_CN = {"Steam": "steam", "PlayStation": "ps5", "Xbox": "xbox", "Nintendo Switch": "switch"}
ENT = [
    ("TikTok / Douyin", "抖音", "TikTok", None),
    ("Kuaishou / Kwai", "快手", "Kwai", {"br", "mx", "id"}),
    ("Bilibili", "哔哩哔哩", None, None),
    ("Xiaohongshu", "小红书", None, None),
    ("Weibo", "微博", None, None),
    ("Huya", "虎牙", None, None),
    ("Douyu", "斗鱼", None, None),
    ("YouTube", None, "YouTube", None),
    ("Instagram", None, "Instagram", None),
    ("Twitch", None, "Twitch", None),
    ("Netflix", None, "Netflix", None),
    ("Anime", "动漫", "anime", None),
    ("Football", "足球", "football", None),
    ("Basketball", "篮球", "NBA", None),
    ("Esports", "电竞", "esports", None),
    ("Short dramas", "短剧", None, None),
    ("Web novels", "小说", None, None),
]
REFRESH = 14 * 86400
TOL = 0.04
PAUSE = (8, 14)
SUB = 4
MIN_COVER = 0.4

SCHEMA = """
CREATE TABLE IF NOT EXISTS region_trends (cc TEXT, term TEXT, appid TEXT, src TEXT, ts INTEGER, data TEXT,
  PRIMARY KEY (cc, term));
"""

LOCAL_FIELD = {"ru": "name_ru", "cn": "name_zh", "tw": "name_zht", "hk": "name_zht", "mo": "name_zht", "jp": "name_ja",
               "kr": "name_ko", "fr": "name_fr", "de": "name_de", "br": "name_pt", "mx": "name_es", "vn": "name_vi",
               "tr": "name_tr", "sa": "name_ar", "ae": "name_ar", "id": "name_id", "es": "name_es", "it": "name_it",
               "pl": "name_pl", "nl": "name_nl", "ua": "name_uk", "ar": "name_es", "cl": "name_es", "co": "name_es",
               "pe": "name_es", "pt": "name_pt", "se": "name_sv", "gr": "name_el", "hu": "name_hu", "eg": "name_ar"}

BAIDU_PROV = {
    "901": ("CN-SD", "Shandong"), "902": ("CN-GZ", "Guizhou"), "903": ("CN-JX", "Jiangxi"), "904": ("CN-CQ", "Chongqing"),
    "905": ("CN-NM", "Inner Mongolia|Nei Mongol"), "906": ("CN-HB", "Hubei"), "907": ("CN-LN", "Liaoning"),
    "908": ("CN-HN", "Hunan"), "909": ("CN-FJ", "Fujian"), "910": ("CN-SH", "Shanghai"), "911": ("CN-BJ", "Beijing"),
    "912": ("CN-GX", "Guangxi"), "913": ("CN-GD", "Guangdong"), "914": ("CN-SC", "Sichuan"), "915": ("CN-YN", "Yunnan"),
    "916": ("CN-JS", "Jiangsu"), "917": ("CN-ZJ", "Zhejiang"), "918": ("CN-QH", "Qinghai"), "919": ("CN-NX", "Ningxia"),
    "920": ("CN-HE", "Hebei"), "921": ("CN-HL", "Heilongjiang"), "922": ("CN-JL", "Jilin"), "923": ("CN-TJ", "Tianjin"),
    "924": ("CN-SN", "Shaanxi"), "925": ("CN-GS", "Gansu"), "926": ("CN-XJ", "Xinjiang"), "927": ("CN-HA", "Henan"),
    "928": ("CN-AH", "Anhui"), "929": ("CN-SX", "Shanxi"), "930": ("CN-HI", "Hainan"), "932": ("CN-XZ", "Tibet|Xizang"),
}

_GENERIC = re.compile(r"\b(province|provincia|provinsi|state|estado|region|oblast|krai|kray|republic|respublika|of|the|"
                      r"autonomous|avtonomnyy|okrug|prefecture|metropolitan|city|special|administrative|governorate|"
                      r"district|territory|capital|federal|national|municipality|county|department|ken|to|fu|do|shi|"
                      r"sheng|zizhiqu|daerah|khusus|istimewa|emirate|land)\b")

_status = {}
_geo_cache = None
_lock = threading.Lock()


def _say(msg):
    print(f"{time.strftime('%H:%M:%S')}  地区口味：{msg}")


def _set_status(**kw):
    _status.update(kw, ts=int(time.time()))
    try:
        db.kv_set("region_status", _status)
    except Exception:
        pass


DEF_MARKETS = ["ru", "cn", "us", "jp", "kr", "tw", "de", "fr", "gb", "br", "mx", "id", "th", "vn", "ph", "my", "sa",
               "ae", "tr", "ca", "au"]
_opt_cache = [None, {}]


def _opt(name, default):
    """地图自己的设置直接读 settings.json（不改宿主 config）。"""
    try:
        m = os.path.getmtime(config.USER_SETTINGS)
        if m != _opt_cache[0]:
            with open(config.USER_SETTINGS, encoding="utf-8-sig") as f:
                d = json.load(f)
            _opt_cache[0], _opt_cache[1] = m, d if isinstance(d, dict) else {}
        v = _opt_cache[1].get(name)
    except Exception:
        v = None
    return default if v is None else v


def markets():
    v = _opt("MARKETS", DEF_MARKETS)
    return [str(c).lower() for c in (v if isinstance(v, list) else DEF_MARKETS)]


def _cookie():
    return str(_opt("BAIDU_COOKIE", "") or "").strip()


def _deacc(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _norm(s):
    return re.sub(r"[^0-9a-z\u0400-\u04ff\u4e00-\u9fff]+", "", _deacc(s))


def _strip(s):
    return re.sub(r"[^0-9a-z\u0400-\u04ff\u4e00-\u9fff]+", "", _GENERIC.sub(" ", re.sub(r"[-_']", " ", _deacc(s))))


# ---------------- 边界 ----------------

def _simplify(pts, tol):
    n = len(pts)
    if n < 5:
        return pts
    keep = bytearray(n)
    keep[0] = keep[-1] = 1
    stack = [(0, n - 1)]
    t2 = tol * tol
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a][0], pts[a][1]
        dx, dy = pts[b][0] - ax, pts[b][1] - ay
        L = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i][0] - ax, pts[i][1] - ay
            if L:
                t = (px * dx + py * dy) / L
                t = 0.0 if t < 0 else 1.0 if t > 1 else t
                px -= t * dx
                py -= t * dy
            d = px * px + py * py
            if d > best:
                best, bi = d, i
        if best > t2:
            keep[bi] = 1
            stack.append((a, bi))
            stack.append((bi, b))
    return [p for p, k in zip(pts, keep) if k]


def _popgrid():
    import base64
    from .incgrid import POPGRID
    with open(POPGRID, encoding="ascii") as f:
        b = base64.b64decode(f.read())
    w = struct.unpack_from("<H", b, 4)[0]
    lat_top, cell = struct.unpack_from("<ff", b, 8)
    n = struct.unpack_from("<I", b, 16)[0]
    idx, pop = [0] * n, [0] * n
    p, cur = 20, -1
    for i in range(n):
        for k in (0, 1):
            x = s = 0
            while True:
                c = b[p]
                p += 1
                x |= (c & 0x7F) << s
                s += 7
                if c < 128:
                    break
            if k == 0:
                cur += x
                idx[i] = cur
            else:
                pop[i] = x
    return w, lat_top, cell, idx, pop


def _cells(rings, w, top, cell, h):
    s = cell / SUB
    nj, nc = h * SUB, w * SUB
    fl, ce = math.floor, math.ceil
    rows = {}
    for ring in rings:
        n = len(ring)
        for i in range(n):
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
            if y1 == y2:
                continue
            lo, hi = (y1, y2) if y1 < y2 else (y2, y1)
            j0 = max(0, fl((top - hi) / s - .5) + 1)
            j1 = min(nj - 1, fl((top - lo) / s - .5))
            if j1 < j0:
                continue
            k = (x2 - x1) / (y2 - y1)
            for r in range(j0, j1 + 1):
                yc = top - (r + .5) * s
                rows.setdefault(r, []).append(x1 + (yc - y1) * k)
    out = defaultdict(int)
    for r, xl in rows.items():
        xl.sort()
        base = (r // SUB) * w
        for q in range(0, len(xl) - 1, 2):
            c0 = max(0, ce((xl[q] + 180) / s - .5))
            c1 = min(nc - 1, ce((xl[q + 1] + 180) / s - .5) - 1)
            if c1 < c0:
                continue
            for C in range(c0 // SUB, c1 // SUB + 1):
                a, z = max(c0, C * SUB), min(c1, C * SUB + SUB - 1)
                out[base + C] += z - a + 1
    return out


def _download_ne(say):
    from .incgrid import _fetch_file
    last = None
    for u in NE_URLS:
        try:
            say("正在下载省 / 州边界（约 40MB，只下载一次）")
            _fetch_file(u, NE_TMP)
            with open(NE_TMP, "rb") as f:
                if f.read(1) == b"{":
                    return
            last = RuntimeError("下载到的不是 GeoJSON")
        except Exception as e:
            last = e
    raise last


def _build_geo(say):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    if not os.path.exists(NE_TMP):
        _download_ne(say)
    say("正在整理省 / 州边界")
    with open(NE_TMP, encoding="utf-8") as f:
        d = json.load(f)
    mk = set(markets())
    units = []
    for ft in d.get("features") or []:
        p = ft.get("properties") or {}
        cc = (p.get("iso_a2") or "").lower()
        g = ft.get("geometry") or {}
        if cc not in mk or g.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        out, raw = [], []
        for poly in polys:
            rings = []
            for k, ring in enumerate(poly):
                raw.append(ring)
                sr = _simplify(ring, TOL)
                if len(sr) >= 4:
                    rings.append([[round(x, 3), round(y, 3)] for x, y in ((q[0], q[1]) for q in sr)])
                elif k == 0:
                    break
            if rings:
                out.append(rings)
        if not out:
            big = max((poly[0] for poly in polys), key=len)
            sr = _simplify(big, TOL / 8)
            if len(sr) < 4:
                continue
            out = [[[[round(q[0], 3), round(q[1], 3)] for q in sr]]]
        names = [p.get(k) for k in ("name", "name_en", "woe_name", "gn_name", "gns_name", "name_local")]
        names += (p.get("name_alt") or "").split("|")
        names = [re.sub(r",.*$", "", x) for x in names if x]
        loc = p.get(LOCAL_FIELD.get(cc, "")) or ""
        units.append({"cc": cc, "iso": (p.get("iso_3166_2") or "").upper(), "en": p.get("name_en") or p.get("name") or "",
                      "loc": loc, "keys": sorted({_norm(x) for x in names if _norm(x)}),
                      "skeys": sorted({_strip(x) for x in names if _strip(x)}),
                      "grp": sorted({_norm(x) for x in (p.get("region"), p.get("geonunit")) if x and _norm(x)}),
                      "g": out, "_raw": raw})
    if not units:
        raise RuntimeError("边界文件里没有目标市场")
    say(f"正在把 {len(units)} 个省 / 州换算到人口格子")
    w, top, cell, idx, pop = _popgrid()
    h = int(round((top + 90) / cell))
    pos = {ci: i for i, ci in enumerate(idx)}
    best_n, best_u = {}, {}
    for ui, u in enumerate(units):
        for ci, n in _cells(u.pop("_raw"), w, top, cell, h).items():
            if ci in pos and n > best_n.get(ci, 0):
                best_n[ci], best_u[ci] = n, ui
    cells = [[] for _ in units]
    for ci, ui in best_u.items():
        cells[ui].append(pos[ci])
    for ui, u in enumerate(units):
        u["cells"] = sorted(cells[ui])
        u["pop"] = sum(pop[i] for i in u["cells"])
    data = {"v": GEO_V, "markets": sorted(mk), "units": units}
    with open(GEO + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(GEO + ".tmp", GEO)
    try:
        os.remove(NE_TMP)
    except OSError:
        pass
    return len(units)


def _ig2():
    from .incgrid import PATH2
    try:
        with open(PATH2, "rb") as f:
            b = f.read()
    except OSError:
        return None
    if b[:4] != b"IG2\0":
        return None
    n = struct.unpack_from("<I", b, 14)[0]
    q = array("H")
    q.frombytes(b[18:18 + 2 * n])
    import sys
    if sys.byteorder != "little":
        q.byteswap()
    return q


def load_geo():
    """{units: [...]}，每个单位加上 ppp（县级人均收入按人口加权，没有县级数据时为空）。"""
    global _geo_cache
    try:
        mt = os.path.getmtime(GEO)
    except OSError:
        return None
    from .incgrid import PATH2
    try:
        mt2 = os.path.getmtime(PATH2)
    except OSError:
        mt2 = 0
    with _lock:
        if _geo_cache and _geo_cache[0] == (mt, mt2):
            return _geo_cache[1]
        try:
            with open(GEO, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return None
        q = _ig2()
        _, _, _, _, pop = _popgrid()
        for u in d["units"]:
            u["ppp"] = None
            if q is not None:
                s = wt = 0.0
                for i in u["cells"]:
                    if i < len(q) and q[i]:
                        s += pop[i] * q[i] * 10
                        wt += pop[i]
                u["ppp"] = s / wt if wt else None
        nat = defaultdict(lambda: [0.0, 0.0])
        for u in d["units"]:
            if u["ppp"]:
                nat[u["cc"]][0] += u["ppp"] * u["pop"]
                nat[u["cc"]][1] += u["pop"]
        for u in d["units"]:
            a = nat.get(u["cc"])
            u["rf"] = round(u["ppp"] / (a[0] / a[1]), 3) if u["ppp"] and a and a[1] else None
        pub = {"units": [{"cc": u["cc"], "en": u["en"], "loc": u["loc"], "pop": u["pop"], "rf": u["rf"], "g": u["g"]}
                         for u in d["units"]]}
        d["public"] = json.dumps(pub, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        _geo_cache = ((mt, mt2), d)
        return d


def public_json():
    d = load_geo()
    return d["public"] if d else None


def ensure_geo(say=_say):
    head = load_geo()
    if head and head.get("v") == GEO_V and head.get("markets") == sorted(set(markets())):
        return True
    k = db.kv_get("regions_geo_try") or {}
    if time.time() - k.get("ts", 0) < 1800:
        return False
    db.kv_set("regions_geo_try", {"ts": int(time.time())})
    _set_status(phase="borders", geo_error=None)
    try:
        n = _build_geo(say)
        _set_status(phase="idle")
        say(f"省 / 州边界已就绪（{n} 个）")
        db.kv_set("regions_ver", int(time.time()))
        return True
    except Exception as e:
        say(f"省 / 州边界准备失败（半小时后再试）：{type(e).__name__}：{e}")
        _set_status(phase="idle", geo_error=f"{type(e).__name__}：{e}"[:200])
        return False


# ---------------- 搜索热度 ----------------

TR = "https://trends.google.com/trends/api/"
_warm = [0]


def _tjson(text):
    i = (text or "").find("{")
    if i < 0:
        raise RuntimeError("Google Trends 返回的不是数据：" + re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or ""))[:100])
    return json.loads(text[i:])


def _tget(path, params):
    if time.time() - _warm[0] > 1800:
        try:
            get_text("https://trends.google.com/trends/explore", {"geo": "US", "hl": "en-US"}, retries=0, timeout=30)
        except Exception:
            pass
        _warm[0] = time.time()
    return _tjson(get_text(TR + path, dict(params, hl="en-US", tz="0"), headers={"Accept-Language": "en-US,en;q=0.9"},
                           retries=1, timeout=40))


def _topic(term):
    cache = db.kv_get("trends_topics") or {}
    if term in cache:
        return cache[term]
    mid = None
    d = _tget("autocomplete/" + urllib.parse.quote(term), {})
    t0 = _norm(term)
    for t in (d.get("default") or {}).get("topics") or []:
        if "game" in (t.get("type") or "").lower() and t0[:6] == _norm(t.get("title"))[:6]:
            mid = t.get("mid")
            break
    cache[term] = mid
    db.kv_set("trends_topics", cache)
    return mid


def _google_geo(kw, cc):
    req = {"comparisonItem": [{"keyword": kw, "geo": cc.upper(), "time": "today 12-m"}], "category": 0, "property": ""}
    ex = _tget("explore", {"req": json.dumps(req, separators=(",", ":"))})
    wd = next((w for w in ex.get("widgets") or [] if str(w.get("id", "")).startswith("GEO_MAP")), None)
    if not wd:
        raise RuntimeError("Google Trends 没有地区数据")
    time.sleep(1.5)
    d = _tget("widgetdata/comparedgeo", {"req": json.dumps(wd["request"], separators=(",", ":")), "token": wd["token"]})
    out = []
    for r in (d.get("default") or {}).get("geoMapData") or []:
        has = (r.get("hasData") or [False])[0]
        v = (r.get("value") or [None])[0]
        out.append([r.get("geoCode") or "", r.get("geoName") or "", v if has else None])
    return out


def _baidu_geo(word):
    ck = _cookie()
    if not ck:
        raise RuntimeError("settings.json 没有填 BAIDU_COOKIE")
    if "=" not in ck:
        ck = "BDUSS=" + ck
    d = get_json("https://index.baidu.com/api/SearchApi/region", {"region": 0, "word": word, "days": 30},
                 headers={"Cookie": ck, "Referer": "https://index.baidu.com/v2/main/index.html", "Accept": "application/json"},
                 retries=1, timeout=30)
    if not isinstance(d, dict):
        raise RuntimeError("百度指数返回的不是数据")
    if d.get("status") not in (0, None):
        raise RuntimeError(f"百度指数：{d.get('status')} {d.get('message') or ''}（登录过期就重新复制 Cookie）")
    reg = (d.get("data") or {}).get("region") or []
    prov = (reg[0] or {}).get("prov") if reg else None
    if not prov:
        return []
    return [[BAIDU_PROV[k][0], BAIDU_PROV[k][1], v] for k, v in prov.items() if k in BAIDU_PROV]


def _clean(n):
    n = re.sub(r"[™®©]", "", n or "")
    n = re.split(r"\s[-–—|:]\s|[:：|（(【\[]|\s-\s", n)[0]
    return re.sub(r"\s+", " ", n).strip()


def _cn_names(ids):
    cache = db.kv_get("cn_names") or {}
    miss = [a for a in ids if a not in cache]
    if miss:
        try:
            from .worldmap import LOOKUP
            d = get_json(LOOKUP, {"id": ",".join(miss[:150]), "country": "cn"}, retries=1, timeout=40) or {}
            for r in d.get("results") or []:
                if r.get("trackName"):
                    cache[str(r.get("trackId"))] = r["trackName"]
            for a in miss:
                cache.setdefault(a, "")
            db.kv_set("cn_names", cache)
        except Exception as e:
            _say(f"查中文游戏名失败：{e}")
    return cache


def country_share(cc):
    from .worldmap import FEED_W, RANK_POW
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 10 * 86400))
    with db.session() as c:
        rows = c.execute("SELECT day, feed, rank, appid FROM world_charts WHERE cc = ? AND day >= ?", (cc, cutoff)).fetchall()
    latest = {}
    for day, feed, _, _ in rows:
        latest[feed] = max(latest.get(feed, ""), day)
    d = {}
    for feed in latest:
        lst = [(r, a) for day, f, r, a in rows if f == feed and day == latest[feed]]
        tot = sum(r ** -RANK_POW for r, _ in lst) or 1
        for r, a in lst:
            d[a] = d.get(a, 0.0) + r ** -RANK_POW / tot * FEED_W.get(feed, .5)
    t = sum(d.values()) or 1
    return {a: v / t for a, v in d.items()}


def _terms(cc):
    sh = country_share(cc)
    top = [a for a, _ in sorted(sh.items(), key=lambda x: -x[1])]
    if not top:
        return []
    with db.session() as c:
        nm = {r[0]: (r[1], r[2]) for r in c.execute(
            f"SELECT appid, name, en_name FROM world_apps WHERE appid IN ({','.join('?' * len(top))})", top)}
    cn = _cn_names(top[:N_GAMES * 2]) if cc == "cn" else {}
    out, seen = [], set()
    for a in top:
        name, en = nm.get(a, ("", ""))
        q = _clean(cn.get(a) or name) if cc == "cn" else _clean(en or name)
        k = _norm(q)
        if len(k) < 2 or k in seen:
            continue
        seen.add(k)
        out.append(("g:" + a, a, q))
        if len(out) >= N_GAMES:
            break
    for p in PLATFORMS:
        out.append(("p:" + p, None, PLAT_CN[p] if cc == "cn" else p))
    for key, q_cn, q, only in ENT:
        qq = q_cn if cc == "cn" else q
        if qq and (not only or cc in only or cc == "cn"):
            out.append(("e:" + key, None, qq))
    return out


def _fetch_one(cc, term, query):
    if cc == "cn":
        return "baidu", _baidu_geo(query)
    kw = query
    if term.startswith("g:"):
        try:
            kw = _topic(query) or query
        except Exception:
            kw = query
        time.sleep(1.5)
    return "google", _google_geo(kw, cc)


def scan():
    """每轮最多抓 60 个词，返回成功个数。"""
    mk = markets()
    if not mk or not load_geo():
        return 0
    now = int(time.time())
    with db.session() as c:
        have = {(r[0], r[1]): r[2] for r in c.execute("SELECT cc, term, ts FROM region_trends")}
    skip = db.kv_get("region_skip") or {}
    todo, prog, nodata = [], {}, []
    for cc in mk:
        if now - skip.get(cc, 0) < REFRESH:
            nodata.append(cc)
            continue
        if cc == "cn" and not _cookie():
            continue
        try:
            terms = _terms(cc)
        except Exception as e:
            _say(f"{cc} 取游戏名失败：{e}")
            continue
        need = [(cc, t, a, q) for t, a, q in terms if now - have.get((cc, t), 0) >= REFRESH]
        prog[cc] = [len(terms) - len(need), len(terms)]
        todo += need
    _set_status(prog=prog, nodata=nodata, total=sum(v[1] for v in prog.values()))
    if not todo:
        _set_status(phase="idle", left=0)
        return 0
    got, streak, empty = 0, 0, defaultdict(int)
    for i, (cc, term, appid, q) in enumerate(todo[:60]):
        _set_status(phase="fetch", cc=cc, done=i, left=len(todo) - i)
        if empty[cc] >= 3:
            continue
        try:
            src, geo = _fetch_one(cc, term, q)
            vals = [g for g in geo if g[2] is not None]
            if not geo:
                empty[cc] += 1
                if empty[cc] >= 3:
                    skip[cc] = now
                    db.kv_set("region_skip", skip)
                    _say(f"{cc} 没有省 / 州级数据，两周内不再抓")
            with db.session() as c:
                c.execute("INSERT OR REPLACE INTO region_trends(cc, term, appid, src, ts, data) VALUES (?, ?, ?, ?, ?, ?)",
                          (cc, term, appid, src, now, json.dumps({"q": q, "geo": geo}, ensure_ascii=False)))
            got += 1 if vals else 0
            streak = 0
            if cc in prog:
                prog[cc][0] += 1
                _set_status(prog=prog, last_error=None)
        except Exception as e:
            msg = f"{type(e).__name__}：{e}"[:200]
            errs = dict(_status.get("errors") or {})
            errs[f"{cc}:{q}"] = msg
            _set_status(errors=dict(list(errs.items())[-20:]), last_error=f"{cc.upper()} {q}：{msg}"[:240])
            with db.session() as c:
                c.execute("INSERT OR REPLACE INTO region_trends(cc, term, appid, src, ts, data) VALUES (?, ?, ?, ?, ?, ?)",
                          (cc, term, appid, "err", now - REFRESH + 86400, json.dumps({"q": q, "err": msg}, ensure_ascii=False)))
            if cc != "cn":
                streak += 1
                if streak >= 3:
                    _set_status(phase="blocked", blocked=msg)
                    _say(f"Google Trends 连续失败，一小时后再试：{msg}")
                    return got
        time.sleep(random.uniform(*PAUSE))
    _set_status(phase="idle", left=max(0, len(todo) - 60), last=int(time.time()), blocked=None)
    if got:
        db.kv_set("regions_ver", int(time.time()))
        from .worldmap import RUN_NOW
        RUN_NOW.set()
        _say(f"抓到 {got} 个词的省 / 州热度，还剩 {max(0, len(todo) - 60)} 个")
    return got


# ---------------- 给 worldmap.compute 用 ----------------

def _match(units, codes):
    """Google / 百度的地区 → 边界单位编号。先按 ISO 代码，再按名字，再按上级大区（英格兰、法兰西岛等）。"""
    by_iso, by_key, by_skey, by_grp = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    for i, u in units:
        if u["iso"]:
            by_iso[u["iso"]].append(i)
        for k in u["keys"]:
            by_key[k].append(i)
        for k in u["skeys"]:
            by_skey[k].append(i)
        for k in u["grp"]:
            by_grp[k].append(i)
    res, used = {}, set()
    for stage in range(4):
        for code, name in codes:
            if code in res:
                continue
            alts = [x for x in (name or "").split("|") if x]
            hit = []
            if stage == 0:
                hit = by_iso.get((code or "").upper(), [])
            else:
                tbl, f = ((by_key, _norm), (by_grp, _norm), (by_skey, _strip))[stage - 1]
                for a in alts:
                    hit = tbl.get(f(a), [])
                    if hit:
                        break
            hit = [i for i in hit if i not in used]
            if hit:
                res[code] = hit
                used.update(hit)
    return res


def split(order):
    """{cc: {"places": {code: {name, units, pop, lift: {appid: x}, plat: {平台: x}, ent: {娱乐: x}}}}}，只含数据够的目标市场。"""
    geo = load_geo()
    if not geo:
        return {}
    by_cc = defaultdict(list)
    for i, u in enumerate(geo["units"]):
        by_cc[u["cc"]].append((i, u))
    with db.session() as c:
        rows = c.execute("SELECT cc, term, appid, data FROM region_trends WHERE src != 'err'").fetchall()
    terms = defaultdict(list)
    for cc, term, appid, data in rows:
        try:
            g = json.loads(data).get("geo") or []
        except ValueError:
            continue
        if g and cc in by_cc and cc in order:
            terms[cc].append((term, appid, g))
    out = {}
    for cc, lst in terms.items():
        codes = {}
        for _, _, g in lst:
            for code, name, _ in g:
                codes.setdefault(code, name)
        m = _match(by_cc[cc], list(codes.items()))
        units = geo["units"]
        pop = {code: sum(units[i]["pop"] for i in ix) for code, ix in m.items()}
        pl = {code: {"name": codes[code].split("|")[0], "units": ix, "pop": pop[code], "lift": {}, "plat": {}, "ent": {}}
              for code, ix in m.items() if pop[code] > 0}
        n_game = 0
        for term, appid, g in lst:
            vals = {code: v for code, _, v in g if v is not None and code in pl}
            wt = sum(pl[c_]["pop"] for c_ in vals)
            mean = sum(pl[c_]["pop"] * v for c_, v in vals.items()) / wt if wt else 0
            if mean <= 0:
                continue
            is_game = term.startswith("g:")
            n_game += is_game
            for c_, v in vals.items():
                x = round(min(5.0, max(0.2, v / mean)), 3)
                if is_game:
                    pl[c_]["lift"][appid] = x
                else:
                    pl[c_]["ent" if term.startswith("e:") else "plat"][term[2:]] = x
        pl = {c_: p for c_, p in pl.items() if n_game and len(p["lift"]) >= MIN_COVER * n_game}
        if len(pl) >= 2:
            out[cc] = {"places": pl, "n_game": n_game}
    return out


def changed_since(ts):
    return (db.kv_get("regions_ver") or 0) > (ts or 0)


def status():
    st = dict(db.kv_get("region_status") or {})
    st["markets"] = markets()
    st["baidu"] = bool(_cookie())
    return st


def _loop():
    time.sleep(40)
    while True:
        try:
            if config.source_on("world") and markets() and ensure_geo():
                scan()
        except Exception as e:
            traceback.print_exc()
            _set_status(phase="error", error=f"{type(e).__name__}：{e}"[:200])
        time.sleep(3600 if _status.get("phase") == "blocked" else 120 if (_status.get("left") or 0) > 0 else 3600)


def start():
    with db.session() as c:
        c.executescript(SCHEMA)
    _status.update(db.kv_get("region_status") or {})
    threading.Thread(target=_loop, daemon=True, name="regions").start()
