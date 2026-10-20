"""Roblox 数据源：首页各个榜单（热门趋势、上升最快等）里的游戏和实时在线人数。公开接口。"""
import time
import uuid
from datetime import datetime

from ..net import get_json


def _iso(s):
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def details(universe_ids):
    """games.roblox.com 批量查（每次 50 个）：原始英文名、简介、创建时间、累计访问、作者。"""
    out = {}
    ids = [str(u) for u in universe_ids]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            d = get_json("https://games.roblox.com/v1/games", {"universeIds": ",".join(chunk)}) or {}
        except Exception:
            continue
        for g in d.get("data") or []:
            out[str(g.get("id"))] = {
                "name_en": g.get("sourceName") or g.get("name") or "",
                "desc": (g.get("sourceDescription") or g.get("description") or "")[:800],
                "created_ts": _iso(g.get("created")),
                "visits": g.get("visits"),
                "creator": (g.get("creator") or {}).get("name") or "",
            }
        time.sleep(0.5)
    return out


def icons(universe_ids):
    """v51：游戏图标（thumbnails.roblox.com 公开接口，PNG，每次 50 个）。拿不到就没有图，不影响别的。"""
    out = {}
    ids = [str(u) for u in universe_ids]
    for i in range(0, len(ids), 50):
        try:
            d = get_json("https://thumbnails.roblox.com/v1/games/icons",
                         {"universeIds": ",".join(ids[i:i + 50]), "size": "150x150", "format": "Png",
                          "isCircular": "false"}, retries=1) or {}
        except Exception:
            continue
        for x in d.get("data") or []:
            if x.get("state") == "Completed" and x.get("imageUrl"):
                out[str(x.get("targetId"))] = x["imageUrl"]
        time.sleep(0.3)
    return out


def trending(max_pages=3):
    out = {}
    token = None
    session_id = str(uuid.uuid4())
    for _ in range(max_pages):
        params = {"sessionId": session_id, "device": "computer", "country": "all"}
        if token:
            params["sortsPageToken"] = token
        d = get_json("https://apis.roblox.com/explore-api/v1/get-sorts", params) or {}
        for s in d.get("sorts") or []:
            if s.get("contentType") != "Games":
                continue
            sort_name = s.get("sortDisplayName") or s.get("sortId") or ""
            for g in s.get("games") or []:
                uid = g.get("universeId")
                if not uid:
                    continue
                e = out.setdefault(str(uid), {
                    "entity": str(uid), "name": g.get("name") or str(uid),
                    "metrics": {"players": g.get("playerCount") or 0},
                    "extra": {"genre": g.get("genreL1") or "", "sorts": [],
                              "up": g.get("totalUpVotes"), "down": g.get("totalDownVotes"),
                              "place": g.get("rootPlaceId")}})
                if sort_name and sort_name not in e["extra"]["sorts"]:
                    e["extra"]["sorts"].append(sort_name)
        token = d.get("nextSortsPageToken")
        if not token:
            break
        time.sleep(1)
    try:
        info = details(list(out))
    except Exception:
        info = {}
    try:
        pics = icons(list(out))
    except Exception:
        pics = {}
    for uid, url in pics.items():
        if uid in out:
            out[uid]["extra"]["image"] = url
    for uid, e in out.items():
        x = info.get(uid)
        if not x:
            continue
        if x["visits"] is not None:
            e["metrics"]["visits"] = x["visits"]
        e["extra"].update({"name_en": x["name_en"], "desc": x["desc"], "created_ts": x["created_ts"],
                           "developers": [x["creator"]] if x["creator"] else []})
        if x["name_en"]:
            e["extra"]["name_local"] = e["name"]
            e["name"] = x["name_en"]          # 用原始英文名（探索页给的是翻译过的中文名，拿去搜 App Store 搜不到）
    return list(out.values())
