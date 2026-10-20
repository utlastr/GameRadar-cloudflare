"""地图 Income 模式用的州 / 市级人均 GDP 网格（Kummu 等 2025，Scientific Data，CC BY 4.0）。

v65 起优先用同一数据集的县 / 区级边界（43,501 个单位，gpkg 约 40MB），按人口格子（0.2°）每格 4×4 采样求平均，
存成 radar_data/incgrid2.bin：b"IG2\\0"，<HffI（人口格子宽、顶纬度、格子度数、格子个数），然后按人口格子顺序的 uint16（美元 ÷ 10）。
失败时退回 0.5° 网格：

下载一次 30 角分（0.5°）的 GeoTIFF（约 5MB），取最近一年那一层，存成 radar_data/incgrid.bin。
地图只用它的"国内相对高低"（格子值 ÷ 本国人口加权平均），国家总水平仍按世界银行人均国民收入。
文件格式：b"IG1\\0"，<HHfff（宽、高、左上经度、左上纬度、格子度数），然后宽×高个 uint16（美元 ÷ 10，0 = 没有数据）。
"""
import math
import os
import re
import struct
import sys
import time
import urllib.request
from array import array
from itertools import accumulate

from . import config, db
from .net import _UA, _get_direct, _get_opener

URL = "https://zenodo.org/records/18429133/files/rast_adm2_gdp_perCapita_1990_2024_30arcmin.tif?download=1"
PATH = os.path.join(config.DATA_DIR, "incgrid.bin")
_TYPES = {1: ("B", 1), 2: ("c", 1), 3: ("H", 2), 4: ("I", 4), 5: ("II", 8), 6: ("b", 1), 7: ("B", 1), 8: ("h", 2),
          9: ("i", 4), 10: ("ii", 8), 11: ("f", 4), 12: ("d", 8), 16: ("Q", 8), 17: ("q", 8), 18: ("Q", 8)}


def _download():
    h = {"User-Agent": _UA, "Accept": "*/*"}
    last = None
    for opener in (_get_opener(), _get_direct()):
        try:
            with opener.open(urllib.request.Request(URL, headers=h), timeout=120) as r:
                data = r.read()
            if data[:4] in (b"II*\0", b"MM\0*", b"II+\0", b"MM\0+"):
                return data
            last = RuntimeError("下载到的不是 GeoTIFF")
        except Exception as e:
            last = e
    raise last


def _lzw(data):
    out = bytearray()
    table = [bytes([i]) for i in range(256)] + [b"", b""]
    acc = nb = i = 0
    width, prev, n = 9, None, len(data)
    while True:
        while nb < width:
            if i >= n:
                return bytes(out)
            acc = ((acc & 0xFFFF) << 8) | data[i]
            i += 1
            nb += 8
        nb -= width
        code = (acc >> nb) & ((1 << width) - 1)
        if code == 256:
            del table[258:]
            width, prev = 9, None
            continue
        if code == 257:
            return bytes(out)
        if prev is None:
            entry = table[code]
        elif code < len(table):
            entry = table[code]
            table.append(prev + entry[:1])
        elif code == len(table):
            entry = prev + prev[:1]
            table.append(entry)
        else:
            return bytes(out)
        out += entry
        prev = entry
        t = len(table)
        if t == 511:
            width = 10
        elif t == 1023:
            width = 11
        elif t == 2047:
            width = 12


def _inflate(data, comp):
    if comp == 1:
        return data
    if comp == 5:
        return _lzw(data)
    if comp in (8, 32946):
        import zlib
        return zlib.decompress(data)
    raise RuntimeError(f"不支持的压缩方式 {comp}")


def _parse(buf):
    e = "<" if buf[:2] == b"II" else ">"
    big = struct.unpack(e + "H", buf[2:4])[0] == 43
    off = struct.unpack(e + "Q", buf[8:16])[0] if big else struct.unpack(e + "I", buf[4:8])[0]
    n = struct.unpack(e + ("Q" if big else "H"), buf[off:off + (8 if big else 2)])[0]
    p0, step, inl = off + (8 if big else 2), (20 if big else 12), (8 if big else 4)
    tags = {}
    for k in range(n):
        p = p0 + k * step
        tag, typ = struct.unpack(e + "HH", buf[p:p + 4])
        cnt = struct.unpack(e + ("Q" if big else "I"), buf[p + 4:p + (12 if big else 8)])[0]
        if typ not in _TYPES:
            continue
        fmt, size = _TYPES[typ]
        vp = p + (12 if big else 8)
        if size * cnt > inl:
            vp = struct.unpack(e + ("Q" if big else "I"), buf[vp:vp + inl])[0]
        raw = buf[vp:vp + size * cnt]
        if typ == 2:
            tags[tag] = raw.split(b"\0")[0].decode("ascii", "replace")
        else:
            tags[tag] = list(struct.unpack(e + fmt[0] * (cnt * len(fmt)), raw))
    return e, tags


def decode(buf):
    e, t = _parse(buf)
    g = lambda k, d=None: t.get(k, [d] if d is not None else None)
    w, h = g(256)[0], g(257)[0]
    spp, bps = g(277, 1)[0], g(258, 32)[0]
    comp, planar, pred, sfmt = g(259, 1)[0], g(284, 1)[0], g(317, 1)[0], g(339, 1)[0]
    if 322 in t:
        bw, bh, offs, cnts = g(322)[0], g(323)[0], g(324), g(325)
    else:
        bw, bh, offs, cnts = w, g(278, h)[0], g(273), g(279)
    nbx, nby = -(-w // bw), -(-h // bh)
    band = spp - 1
    B = bps // 8
    code = {(3, 4): "f", (3, 8): "d", (1, 1): "B", (1, 2): "H", (1, 4): "I", (2, 1): "b", (2, 2): "h", (2, 4): "i"}[(sfmt, B)]
    nod = t.get(42113)
    try:
        nod = float(nod) if nod else None
    except ValueError:
        nod = None
    lsp = 1 if planar == 2 else spp
    per_band = nbx * nby
    vals = [0.0] * (w * h)
    for by in range(nby):
        for bx in range(nbx):
            idx = by * nbx + bx + (band * per_band if planar == 2 else 0)
            raw = _inflate(bytes(buf[offs[idx]:offs[idx] + cnts[idx]]), comp)
            bs = lsp if planar == 2 else spp
            bsel = 0 if planar == 2 else band
            rowb = bw * bs * B
            rows = min(bh, len(raw) // rowb)
            for r in range(rows):
                y = by * bh + r
                if y >= h:
                    break
                row = raw[r * rowb:(r + 1) * rowb]
                if pred == 3:
                    ch = bytes(x & 255 for x in accumulate(row[bsel::bs]))
                    planes = [ch[k * bw:(k + 1) * bw] for k in range(B)]
                    be = bytes(planes[k][i] for i in range(bw) for k in range(B))
                    px = struct.unpack(">" + code * bw, be)
                else:
                    allv = struct.unpack(e + code * (bw * bs), row)
                    px = allv[bsel::bs]
                    if pred == 2:
                        px = list(accumulate(px))
                x0 = bx * bw
                for i in range(min(bw, w - x0)):
                    vals[y * w + x0 + i] = px[i]
    sx, sy = (t.get(33550) or [360 / w, 180 / h])[:2]
    tp = t.get(33922) or [0, 0, 0, -180, 90, 0]
    lon0, lat0 = tp[3] - tp[0] * sx, tp[4] + tp[1] * sy
    out = bytearray(b"IG1\0" + struct.pack("<HHfff", w, h, lon0, lat0, sx))
    q = []
    for v in vals:
        ok = v == v and v > 0 and v < 1e7 and (nod is None or abs(v - nod) > 1e-6)
        q.append(min(65535, max(1, int(round(v / 10)))) if ok else 0)
    out += struct.pack("<" + "H" * len(q), *q)
    return bytes(out), sum(1 for x in q if x)


GPKG_URL = "https://zenodo.org/records/18429133/files/polyg_adm2_gdp_perCapita_1990_2024.gpkg?download=1"
CSV_URL = "https://zenodo.org/records/18429133/files/tabulated_adm2_gdp_perCapita.csv?download=1"
PATH2 = os.path.join(config.DATA_DIR, "incgrid2.bin")
GPKG = os.path.join(config.DATA_DIR, "adm2.gpkg")
POPGRID = os.path.join(config.WEB_DIR, "map", "popgrid.b64")
_YEAR = re.compile(r"(?<!\d)(19[89]\d|20[0-4]\d)(?!\d)")
SUB = 4


def _fetch_file(url, path):
    h = {"User-Agent": _UA, "Accept": "*/*"}
    last = None
    for opener in (_get_opener(), _get_direct()):
        try:
            with opener.open(urllib.request.Request(url, headers=h), timeout=120) as r, open(path + ".part", "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
            os.replace(path + ".part", path)
            return
        except Exception as e:
            last = e
    raise last


def _popgrid():
    import base64
    with open(POPGRID, encoding="ascii") as f:
        b = base64.b64decode(f.read())
    w = struct.unpack_from("<H", b, 4)[0]
    lat_top, cell = struct.unpack_from("<ff", b, 8)
    n = struct.unpack_from("<I", b, 16)[0]
    idx = [0] * n
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
    return w, lat_top, cell, idx


def _wkb(b, o, out):
    e = "<" if b[o] == 1 else ">"
    t = struct.unpack_from(e + "I", b, o + 1)[0]
    o += 5
    lo = t & 0xFFFF
    z = bool(t & 0x80000000) or lo // 1000 in (1, 3)
    m = bool(t & 0x40000000) or lo // 1000 in (2, 3)
    if t & 0x20000000:
        o += 4
    base, d = lo % 1000, 2 + z + m
    if base == 3:
        nr = struct.unpack_from(e + "I", b, o)[0]
        o += 4
        for _ in range(nr):
            n = struct.unpack_from(e + "I", b, o)[0]
            o += 4
            v = struct.unpack_from(e + "d" * (n * d), b, o)
            o += 8 * n * d
            out.append((v[0::d], v[1::d]))
    elif base in (6, 7):
        k = struct.unpack_from(e + "I", b, o)[0]
        o += 4
        for _ in range(k):
            o = _wkb(b, o, out)
    else:
        raise ValueError(f"不支持的几何类型 {t}")
    return o


def _rings(blob):
    b = bytes(blob)
    o = 0
    if b[:2] == b"GP":
        fl = b[3]
        if fl & 0x10:
            return []
        o = 8 + (0, 32, 48, 48, 64, 0, 0, 0)[(fl >> 1) & 7]
    out = []
    _wkb(b, o, out)
    return out


class _Raster:
    def __init__(self, w, lat_top, cell, h):
        self.w, self.top, self.s = w, lat_top, cell / SUB
        self.nj, self.nc = h * SUB, w * SUB
        self.S = array("d", bytes(8 * w * h))
        self.N = array("I", bytes(4 * w * h))

    def add(self, rings, v):
        top, s, nj, nc, w = self.top, self.s, self.nj, self.nc, self.w
        fl, ce = math.floor, math.ceil
        S, N = self.S, self.N
        rows = {}
        for xs, ys in rings:
            n = len(xs)
            for i in range(n):
                j = i + 1 if i + 1 < n else 0
                x1, y1, y2 = xs[i], ys[i], ys[j]
                if y1 == y2:
                    continue
                lo, hi = (y1, y2) if y1 < y2 else (y2, y1)
                j0 = max(0, fl((top - hi) / s - .5) + 1)
                j1 = min(nj - 1, fl((top - lo) / s - .5))
                if j1 < j0:
                    continue
                k = (xs[j] - x1) / (y2 - y1)
                for r in range(j0, j1 + 1):
                    yc = top - (r + .5) * s
                    lst = rows.get(r)
                    if lst is None:
                        rows[r] = lst = []
                    lst.append(x1 + (yc - y1) * k)
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
                    S[base + C] += v * (z - a + 1)
                    N[base + C] += z - a + 1


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and 0 < v < 1e7 else None


def _key(x):
    if isinstance(x, float) and x.is_integer():
        x = int(x)
    return str(x).strip()


def _csv_vals(key):
    import csv
    p = os.path.join(config.DATA_DIR, "adm2.csv")
    if not os.path.exists(p):
        _fetch_file(CSV_URL, p)
    out = {}
    with open(p, encoding="utf-8", newline="") as f:
        rd = csv.reader(f)
        head = next(rd)
        ki = next(i for i, c in enumerate(head) if c.lower() == key.lower())
        yc = sorted(((int(c), i) for i, c in enumerate(head) if _YEAR.fullmatch(c)), reverse=True)
        for row in rd:
            for _, i in yc:
                v = _num(row[i]) if i < len(row) else None
                if v:
                    out[row[ki].strip()] = v
                    break
    return out


def _build2(say):
    import sqlite3
    os.makedirs(config.DATA_DIR, exist_ok=True)
    if not os.path.exists(GPKG):
        say("正在下载县 / 区级人均收入边界（约 40MB，只下载一次）")
        _fetch_file(GPKG_URL, GPKG)
    with open(GPKG, "rb") as f:
        if f.read(16) != b"SQLite format 3\x00":
            os.remove(GPKG)
            raise RuntimeError("下载到的不是 GeoPackage")
    say("正在把县 / 区边界换算到人口格子（几十秒到几分钟）")
    con = sqlite3.connect(GPKG)
    try:
        t, gcol = con.execute("SELECT table_name, column_name FROM gpkg_geometry_columns").fetchone()
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{t}")')]
        years = sorted(((int(m.group(1)), c) for c in cols if c != gcol for m in [_YEAR.search(c)] if m), reverse=True)
        ycol = None
        for _, c in years:
            if con.execute(f'SELECT COUNT(*) FROM "{t}" WHERE "{c}" IS NOT NULL').fetchone()[0] > 1000:
                ycol = c
                break
        vals = None
        if ycol:
            sql = f'SELECT "{gcol}", "{ycol}" FROM "{t}"'
        else:
            key = next((c for c in cols if c.lower() in ("gid_2", "adm2id")), None)
            if not key:
                raise RuntimeError("边界文件里找不到年份列，列名：" + ", ".join(cols)[:200])
            vals = _csv_vals(key)
            sql = f'SELECT "{gcol}", "{key}" FROM "{t}"'
        w, lat_top, cell, idx = _popgrid()
        h = int(round((lat_top + 90) / cell))
        ras = _Raster(w, lat_top, cell, h)
        nf = 0
        for g, x in con.execute(sql):
            v = _num(vals.get(_key(x)) if vals is not None else x)
            if g and v:
                ras.add(_rings(g), v)
                nf += 1
    finally:
        con.close()
    S, N = ras.S, ras.N
    val = {}
    for ci in idx:
        if ci < len(N) and N[ci]:
            val[ci] = S[ci] / N[ci]
    filled = {}
    for ci in idx:
        if ci in val:
            continue
        r, c = divmod(ci, w)
        nb = [val[k] for k in ((r + a) * w + (c + b) % w for a in (-1, 0, 1) for b in (-1, 0, 1) if a or b) if k in val]
        if nb:
            filled[ci] = sum(nb) / len(nb)
    val.update(filled)
    q = array("H", bytes(2 * len(idx)))
    for i, ci in enumerate(idx):
        v = val.get(ci)
        if v:
            q[i] = min(65535, max(1, int(round(v / 10))))
    if sys.byteorder != "little":
        q.byteswap()
    ok = sum(1 for x in q if x)
    if ok < 50000:
        raise RuntimeError(f"有效格子太少（{ok}，{nf} 个县 / 区）")
    with open(PATH2 + ".tmp", "wb") as f:
        f.write(b"IG2\0" + struct.pack("<HffI", w, lat_top, cell, len(idx)) + q.tobytes())
    os.replace(PATH2 + ".tmp", PATH2)
    for p in (GPKG, os.path.join(config.DATA_DIR, "adm2.csv")):
        try:
            os.remove(p)
        except OSError:
            pass
    return ok, nf


def ensure(say=print):
    if os.path.exists(PATH2):
        return True
    k = db.kv_get("incgrid2_try") or {}
    if time.time() - k.get("ts", 0) >= 86400:
        db.kv_set("incgrid2_try", {"ts": int(time.time())})
        try:
            ok, nf = _build2(say)
            say(f"县 / 区级人均收入已就绪（{nf} 个县 / 区，{ok} 个人口格子）")
            return True
        except Exception as e:
            say(f"县 / 区级人均收入准备失败（明天再试）：{type(e).__name__}：{e}")
    return _ensure1(say)


def _ensure1(say):
    if os.path.exists(PATH):
        return True
    k = db.kv_get("incgrid_try") or {}
    if time.time() - k.get("ts", 0) < 86400:
        return False
    db.kv_set("incgrid_try", {"ts": int(time.time())})
    try:
        say("正在下载州 / 市级人均收入网格（约 5MB，只下载一次）")
        blob, n = decode(_download())
        if n < 10000:
            raise RuntimeError(f"有效格子太少（{n}）")
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(PATH + ".tmp", "wb") as f:
            f.write(blob)
        os.replace(PATH + ".tmp", PATH)
        say(f"州 / 市级人均收入网格已就绪（{n} 个格子）")
        return True
    except Exception as e:
        say(f"州 / 市级人均收入网格下载失败（明天再试）：{type(e).__name__}：{e}")
        return False


def blob():
    for p in (PATH2, PATH):
        try:
            with open(p, "rb") as f:
                return f.read()
        except OSError:
            pass
    return None
