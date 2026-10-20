"""Steam 数据源。全部是公开接口，不需要 API Key。"""
import re
import time
from datetime import datetime

from .. import config
from ..net import get_json, get_text


def discovery_candidates(known, now):
    """Import releases even if they never enter popular charts."""
    from .. import db
    with db.session() as c:
        exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hist_steam'").fetchone()
        if not exists:
            return []
        rows = c.execute(
            "SELECT appid,name,released FROM hist_steam WHERE released BETWEEN ? AND ? "
            "ORDER BY reviews DESC, released DESC",
            (now - config.STEAM_DISCOVERY_DAYS * 86400, now)).fetchall()
    rows = [dict(appid=r[0], name=r[1], released=r[2]) for r in rows
            if str(r[0]) not in known]
    return rows[:config.STEAM_DISCOVERY_PER_RUN]


def seed_recent_releases(now=None):
    """Publish native IDs to the live pool even before their first metric."""
    from .. import db
    now = int(time.time() if now is None else now)
    known = {e: v["extra"] for (s, e), v in db.load_entities().items() if s == "steam"}
    rows = discovery_candidates(known, now)
    items = [{"entity": str(r["appid"]), "name": r["name"], "metrics": {},
              "extra": {"name": r["name"], "release_ts": r["released"], "type": "game",
                        "coming_soon": False, "discovered_by": "release_scan",
                        "lists": ["近期发行扫描"], "metrics_pending": True}} for r in rows]
    if items:
        with db.session() as c:
            db.upsert_entities(c, "steam", now, items)
    return len(items)


def scan_recent_releases():
    """Independently scan release-date pages; no popularity requirement."""
    from .. import history
    from .. import db
    with db.session() as c:
        c.executescript(history.SCHEMA)
    total = 0
    cutoff = time.time() - config.STEAM_DISCOVERY_DAYS * 86400
    cursor = int(db.kv_get("steam_release_cursor", 0) or 0)
    for page in range(config.STEAM_DISCOVERY_PAGES):
        start = 0 if page == 0 else cursor
        if page > 0 and start == 0:
            start = 50
        text = get_text(history.SEARCH_URL,
                        {"infinite": 1, "start": start, "count": 50,
                         "sort_by": "Released_DESC", "category1": 998,
                         "cc": config.STEAM_CC, "l": "english", "ndl": 1},
                        retries=0, timeout=20)
        import json
        payload = json.loads(text)
        if not payload.get("success"):
            raise RuntimeError("Steam 近期发行扫描未成功")
        rows = history._parse_page(payload.get("results_html") or "")
        if not rows:
            raise RuntimeError("Steam 近期发行页没有解析到游戏，保留扫描进度等待重试")
        history._upsert(rows)
        total += len(rows)
        dates = [r["released"] for r in rows if r.get("released")]
        if not rows or (dates and min(dates) < cutoff):
            db.kv_set("steam_release_cursor", 0)
            break
        if page > 0:
            cursor = start + len(rows)
            db.kv_set("steam_release_cursor", cursor)
    return total


def refresh_discovered():
    """Sample discovered IDs without waiting for the long full-platform crawl."""
    import json
    from .. import collect, db
    now = int(time.time())
    with db.session() as c:
        rows = c.execute(
            """SELECT e.entity,e.name,e.extra,h.reviews,MAX(m.ts) sampled FROM entities e
            JOIN hist_steam h ON h.appid=CAST(e.entity AS INTEGER)
            LEFT JOIN metrics m ON m.source='steam' AND m.entity=e.entity
            WHERE e.source='steam' AND h.released BETWEEN ? AND ?
            AND json_extract(e.extra,'$.discovered_by')='release_scan'
            GROUP BY e.entity HAVING sampled IS NULL OR sampled<?
            ORDER BY sampled IS NOT NULL,sampled ASC,h.reviews DESC LIMIT ?""",
            (now - config.STEAM_DISCOVERY_DAYS * 86400, now, now - 1800,
             config.STEAM_DISCOVERY_METRICS_PER_RUN)).fetchall()
    done, errors = 0, []
    for entity, name, extra, _, _ in rows:
        metrics = {}
        try:
            reviews = review_total(entity)
            if reviews is not None:
                metrics["reviews"] = reviews
        except Exception as e:
            errors.append(f"{entity} 评论：{e}")
        try:
            players = current_players(entity)
            if players is not None:
                metrics["players"] = players
        except Exception as e:
            errors.append(f"{entity} 在线：{e}")
        if metrics:
            x = json.loads(extra)
            x.pop("metrics_pending", None)
            collect._save("steam", int(time.time()),
                          [{"entity": entity, "name": name, "extra": x, "metrics": metrics}])
            done += 1
        time.sleep(.3)
    return done, errors


def release_pool():
    from .. import db
    now = int(time.time())
    with db.session() as c:
        rows = [dict(r) for r in c.execute(
            """SELECT e.entity id,e.name,e.first_seen,
            json_extract(e.extra,'$.release_ts') released,
            (SELECT value FROM metrics m WHERE m.source='steam' AND m.entity=e.entity
              AND m.metric='reviews' ORDER BY ts DESC LIMIT 1) reviews,
            (SELECT value FROM metrics m WHERE m.source='steam' AND m.entity=e.entity
              AND m.metric='players' ORDER BY ts DESC LIMIT 1) players,
            (SELECT MAX(ts) FROM metrics m WHERE m.source='steam' AND m.entity=e.entity) sampled
            FROM entities e WHERE source='steam'
            AND json_extract(extra,'$.discovered_by')='release_scan'
            AND json_extract(extra,'$.release_ts') BETWEEN ? AND ?
            ORDER BY first_seen DESC, name LIMIT 300""",
            (now - config.STEAM_DISCOVERY_DAYS * 86400, now))]
    return rows


def most_played():
    """Steam 官方在线人数榜前 100（含上周排名；peak 是当天峰值在线）。"""
    d = get_json("https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/")
    ranks = ((d or {}).get("response") or {}).get("ranks") or []
    return [{"appid": int(r["appid"]), "rank": r.get("rank"),
             "last_week_rank": r.get("last_week_rank"), "peak": r.get("peak_in_game")}
            for r in ranks]


def featured_lists():
    """Steam 首页的热销、新品、即将推出。"""
    d = get_json("https://store.steampowered.com/api/featuredcategories",
                 {"cc": config.STEAM_CC, "l": "english"}) or {}
    out = {}
    for key in ("top_sellers", "new_releases", "coming_soon"):
        ids = []
        for it in (d.get(key) or {}).get("items") or []:
            if it.get("type") == 0 and it.get("id") and int(it["id"]) not in ids:
                ids.append(int(it["id"]))
        out[key] = ids
    return out


_APPID_RE = re.compile(r"/apps/(\d+)/")


def search_list(filter_name, count=50):
    """商店搜索页榜单：popularnew（热门新品）/ topsellers（热销）/ popularcomingsoon（热门即将推出）"""
    d = get_json("https://store.steampowered.com/search/results/",
                 {"filter": filter_name, "json": 1, "count": count,
                  "cc": config.STEAM_CC, "l": "english"}) or {}
    ids = []
    for it in d.get("items") or []:
        m = _APPID_RE.search(it.get("logo") or "")
        if m and int(m.group(1)) not in ids:
            ids.append(int(m.group(1)))
    return ids


def current_players(appid):
    """当前在线人数。未上线或查不到返回 None（缺失，不是 0）。"""
    d = get_json("https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/",
                 {"appid": appid}, retries=1)
    r = (d or {}).get("response") or {}
    if r.get("result") == 1:
        return int(r.get("player_count") or 0)
    return None


def app_details(appid, lang="english"):
    d = get_json("https://store.steampowered.com/api/appdetails",
                 {"appids": appid, "cc": config.STEAM_CC, "l": lang}) or {}
    item = d.get(str(appid)) or {}
    return (item.get("data") or None) if item.get("success") else None


_TAG_RE = re.compile(r'class="app_tag"[^>]*>\s*([^<]+?)\s*</a>')
_COOKIE = "birthtime=0; lastagecheckage=1-January-1990; wants_mature_content=1; mature_content=1"


def store_tags(appid):
    """玩家标签（Casual、2D、Pixel Graphics 这类），从商店页面读取。"""
    html = get_text(f"https://store.steampowered.com/app/{appid}/",
                    {"l": "english", "cc": config.STEAM_CC}, headers={"Cookie": _COOKIE})
    tags = []
    for t in _TAG_RE.findall(html or ""):
        t = t.strip()
        if t and t != "+" and t not in tags:
            tags.append(t)
    return tags[:20]


def review_total(appid, language="all"):
    d = get_json(f"https://store.steampowered.com/appreviews/{appid}",
                 {"json": 1, "language": language, "purchase_type": "all",
                  "num_per_page": 0, "filter": "summary"})
    q = (d or {}).get("query_summary") or {}
    return q.get("total_reviews")


def prices(appids):
    """批量查价格和折扣（一次最多 50 个）。返回 {appid: {"price_usd", "discount"}}。"""
    out = {}
    ids = [str(a) for a in appids]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            d = get_json("https://store.steampowered.com/api/appdetails",
                         {"appids": ",".join(chunk), "cc": config.STEAM_CC, "filters": "price_overview"}) or {}
        except Exception:
            continue
        for a in chunk:
            item = d.get(a) or {}
            data = item.get("data") if item.get("success") else None
            if isinstance(data, dict):
                po = data.get("price_overview") or {}
                out[a] = {"price_usd": (po.get("final") or 0) / 100.0, "discount": po.get("discount_percent") or 0}
            elif item.get("success"):
                out[a] = {"price_usd": 0.0, "discount": 0}     # 免费游戏没有价格信息
        time.sleep(0.5)
    return out


def reviews_text(appid, n=50):
    """最有用的 n 条评论（所有语言），给 AI 读。"""
    d = get_json(f"https://store.steampowered.com/appreviews/{appid}",
                 {"json": 1, "language": "all", "purchase_type": "all", "filter": "all",
                  "day_range": 365, "num_per_page": min(100, n)}) or {}
    out = []
    for r in d.get("reviews") or []:
        t = re.sub(r"\[/?[a-z0-9]+[^\]]*\]", "", r.get("review") or "")
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) < 15:
            continue
        out.append({"up": bool(r.get("voted_up")), "text": t[:400],
                    "hours": round(((r.get("author") or {}).get("playtime_at_review") or 0) / 60, 1)})
    return out, ((d.get("query_summary") or {}).get("total_reviews"))


def basic_info(appid):
    """商店简介、分类、类型（给 AI 读）。"""
    d = get_json("https://store.steampowered.com/api/appdetails",
                 {"appids": appid, "cc": config.STEAM_CC, "l": "english",
                  "filters": "basic,categories,genres"}) or {}
    item = d.get(str(appid)) or {}
    data = (item.get("data") or {}) if item.get("success") else {}
    desc = re.sub(r"<[^>]+>", " ", data.get("about_the_game") or data.get("detailed_description") or "")
    return {"short": data.get("short_description") or "",
            "about": re.sub(r"\s+", " ", desc).strip()[:1500],
            "categories": [g.get("description") for g in data.get("categories") or [] if g.get("description")],
            "genres": [g.get("description") for g in data.get("genres") or [] if g.get("description")]}


_SIZE_RE = re.compile(
    r"(?:Storage|Hard Drive|Hard Disk Space|Disk Space|HDD)\s*:?\s*</strong>\s*([\d.,]+)\s*(GB|MB|TB)",
    re.I)


def _size_gb(data):
    req = data.get("pc_requirements")
    text = req.get("minimum", "") if isinstance(req, dict) else ""
    m = _SIZE_RE.search(text or "")
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    unit = m.group(2).upper()
    return v / 1024 if unit == "MB" else (v * 1024 if unit == "TB" else v)


def _parse_date(s):
    for fmt in ("%b %d, %Y", "%d %b, %Y", "%B %d, %Y", "%d %B, %Y", "%b %Y", "%Y"):
        try:
            return int(datetime.strptime(s.strip(), fmt).timestamp())
        except (ValueError, AttributeError):
            pass
    return None


def detail(appid):
    """抓一个游戏的全部详情（3–4 次请求）。查不到返回 None。"""
    d = app_details(appid, "english")
    if not d:
        return None
    time.sleep(0.5)
    try:
        cn = app_details(appid, "schinese") or {}
    except Exception:
        cn = {}
    time.sleep(0.5)
    try:
        tags = store_tags(appid)
    except Exception:
        tags = []
    rel = d.get("release_date") or {}
    price = (d.get("price_overview") or {}).get("final")
    return {
        "name": d.get("name"),
        "name_cn": cn.get("name"),
        "type": d.get("type"),
        "is_free": bool(d.get("is_free")),
        "price_usd": (price / 100.0) if price else 0.0,
        "developers": d.get("developers") or [],
        "publishers": d.get("publishers") or [],
        "genres": [g.get("description") for g in d.get("genres") or [] if g.get("description")],
        "categories": [g.get("description") for g in d.get("categories") or [] if g.get("description")],
        "tags": tags,
        "release": rel.get("date"),
        "release_ts": _parse_date(rel.get("date") or ""),
        "coming_soon": bool(rel.get("coming_soon")),
        "size_gb": _size_gb(d),
        "image": d.get("header_image"),
        "fetched_at": int(time.time()),
    }
