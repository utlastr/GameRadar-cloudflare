"""供给检查：目标平台上有没有已上线的同类（跟风品或官方版本）。

只有能下载、能玩的才算"已上线"。预约、即将推出的单独计数，不算供给。
"""
import json
import time
from datetime import datetime

from . import config, db, match
from .sources import appstore

DAY = 86400


def _days_since(ts):
    return None if not ts else max(0.0, (time.time() - ts) / DAY)


def _appstore(country, terms, devs):
    term = terms[0]
    results = appstore.search(term, country, limit=25)
    time.sleep(3.5)  # 苹果接口每分钟约 20 次
    norms = [match.normalize(t) for t in terms if t]
    items = []
    for r in results:
        if "6014" not in [str(g) for g in r.get("genreIds") or []]:   # 6014 = 游戏
            continue
        n = match.normalize(r.get("trackName") or "")
        sim = max((match.similarity(n, t) for t in norms), default=0)
        if sim < config.SUPPLY_MATCH:
            continue
        rel = r.get("releaseDate") or ""
        try:
            rel_ts = int(datetime.strptime(rel[:19], "%Y-%m-%dT%H:%M:%S").timestamp())
        except ValueError:
            rel_ts = None
        launched = rel_ts is None or rel_ts <= time.time()
        dev = r.get("artistName") or ""
        items.append({
            "name": r.get("trackName"), "dev": dev,
            "status": "launched" if launched else "preorder",
            "age_days": _days_since(rel_ts) if launched else None,
            "strength": r.get("userRatingCount") or 0,
            "strength_label": f"{r.get('userRatingCount') or 0} 个评价",
            "maybe_official": False,
            "sim": round(sim, 2),
        })
    return items


def _taptap(terms, ctx):
    """在 TapTap（榜单 + TapTap制造 + 创意工坊，几千款）里找同名或近似名字的游戏。"""
    norms = [n for n in (match.normalize(t) for t in terms if t) if n]
    sets = [set(n) for n in norms]
    items = []
    for t in ctx.get("taptap", []):
        tc = t.get("chars") or set(t["norm"])
        if not tc:
            continue
        # 先粗筛：共同的字太少的直接跳过，省掉大量逐个比对
        if not any(len(tc & s) >= 0.5 * min(len(tc), len(s)) for s in sets):
            continue
        sim = max((match.similarity(t["norm"], n) for n in norms), default=0)
        if sim < config.SUPPLY_MATCH:
            continue
        pre = t["extra"].get("preorder")
        items.append({
            "key": t.get("key"), "name": t["name"], "dev": t["extra"].get("dev") or "",
            "status": "preorder" if pre else "launched",
            "age_days": None if pre else _days_since(t["extra"].get("released_time")),
            "strength": t.get("downloads") or 0,
            "strength_label": (f"预约 {int(t.get('reserves') or 0):,}" if pre
                               else f"热度 {int(t.get('downloads') or 0):,}"),
            "maybe_official": False,
            "sim": round(sim, 2),
        })
    items.sort(key=lambda i: (i["status"] != "launched", -(i["strength"] or 0)))
    return items


def check(target, terms, devs, ctx, allow_network=True, game_id=None):
    """返回 {"checked_at", "items": [...]}，或 None（还没检查过）。"""
    terms = [t for t in terms if t]
    if not terms:
        return None
    if target == "taptap":
        return {"checked_at": int(time.time()), "items": _taptap(terms, ctx)}
    if not game_id:
        return None
    key = game_id
    with db.session() as c:
        r = c.execute("SELECT checked_at, result FROM supply WHERE target = ? AND term = ?",
                      (target, key)).fetchone()
    if r and time.time() - r[0] < config.SUPPLY_HOURS * 3600:
        return json.loads(r[1])
    if not allow_network:
        return json.loads(r[1]) if r else None
    country = {"appstore_cn": "cn", "appstore_us": "us"}[target]
    try:
        items = _appstore(country, terms, devs)
    except Exception:
        return json.loads(r[1]) if r else None
    res = {"checked_at": int(time.time()), "items": items}
    with db.session() as c:
        c.execute("INSERT OR REPLACE INTO supply(target, term, checked_at, result) VALUES (?, ?, ?, ?)",
                  (target, key, res["checked_at"], json.dumps(res, ensure_ascii=False)))
    return dict(res, fresh=True)
