"""Fortnite 创意地图（UEFN 岛）：Epic 官方 Fortnite Data API，公开，不用 Key。

每个玩法分类（派对小游戏、恐怖、模拟经营……）的排行榜每小时更新；
再给上榜的岛查最近一小时的最高在线人数（官方数据，和 Steam 在线人数一样能算涨幅）。
岛名和作者查一次就记住，不用每次查。
"""
import time

from .. import db
from ..net import HttpError, get_json_strict

API = "https://api.fortnite.com/ecosystem/v1"
GENRE_CN = {"survival": "生存", "strategy": "策略", "sports-racing": "体育竞速", "simulation-tycoon": "模拟经营",
            "shooter": "射击", "roleplaying-social": "角色扮演社交", "roguelike": "肉鸽",
            "party-mini-games": "派对小游戏", "music-rhythm": "音乐节奏", "horror": "恐怖",
            "deathrun-platformer": "跑酷闯关", "battle-royale": "大逃杀", "adventure-rpg": "冒险RPG"}


def _get(path, params=None):
    for i in range(3):
        try:
            return get_json_strict(API + path, params, retries=0)
        except HttpError as e:
            if e.code == 429 and i < 2:
                time.sleep(10 * (i + 1))
                continue
            raise
        except RuntimeError as e:
            if "429" in str(e) and i < 2:
                time.sleep(10 * (i + 1))
                continue
            raise


def islands(per_genre=30, progress=None):
    """返回 (岛列表, 出错说明)。"""
    errors = []
    try:
        genres = [g.get("slug") if isinstance(g, dict) else g for g in (_get("/genres").get("data") or [])]
        genres = [g for g in genres if g]
    except Exception as e:
        errors.append(f"分类列表：{e}，用内置的 13 个分类")
        genres = []
    genres = genres or list(GENRE_CN)
    ranks = {}
    for i, g in enumerate(genres, 1):
        if progress:
            progress(i, len(genres) + 1, f"分类排行 {i} / {len(genres)}")
        try:
            d = _get(f"/genres/{g}/rankings", {"size": per_genre})
        except Exception as e:
            errors.append(f"{GENRE_CN.get(g, g)}排行：{e}")
            continue
        for row in d.get("data") or []:
            code, r = row.get("islandCode"), row.get("rank")
            if code and r:
                ranks.setdefault(code, {})["rank_" + g.replace("-", "")] = int(r)
                ranks[code].setdefault("_genres", []).append((GENRE_CN.get(g, g), int(r)))
        time.sleep(0.3)
    if not ranks:
        raise RuntimeError("Fortnite 排行一个都没拿到：" + "；".join(errors[:3]))

    cache = db.kv_get("fortnite_islands", {}) or {}
    out, ccu_fail = [], 0
    codes = sorted(ranks, key=lambda c: min(v for k, v in ranks[c].items() if k != "_genres"))
    for j, code in enumerate(codes, 1):
        if progress and j % 10 == 1:
            progress(j, len(codes), f"岛的在线人数 {j} / {len(codes)}")
        info = cache.get(code)
        if not info:
            try:
                d = _get(f"/islands/{code}")
                info = {"title": d.get("title") or code, "creator": d.get("creatorCode") or "",
                        "tags": d.get("tags") or [], "made": d.get("createdIn") or ""}
                cache[code] = info
            except Exception:
                info = {"title": code, "creator": "", "tags": []}
            time.sleep(0.2)
        m = {k: v for k, v in ranks[code].items() if k != "_genres"}
        if ccu_fail < 5:
            try:
                iv = [x for x in (_get(f"/islands/{code}/metrics/hour/peak-ccu").get("intervals") or [])
                      if x.get("value") is not None]
                if iv:
                    m["players"] = iv[-1]["value"]
                    m["peak_daily"] = max(x["value"] for x in iv)
                ccu_fail = 0
            except Exception as e:
                ccu_fail += 1
                if ccu_fail == 5:
                    errors.append(f"在线人数连续失败，后面的岛这一轮不查了：{e}")
            time.sleep(0.25)
        gl = sorted(ranks[code]["_genres"], key=lambda x: x[1])
        creator = info.get("creator") or ""
        out.append({"entity": code, "name": info.get("title") or code, "metrics": m,
                    "extra": {"creator": creator, "developers": [creator] if creator else [],
                              "tags": info.get("tags") or [], "made": info.get("made") or "",
                              "url": f"https://www.fortnite.com/@{creator}/{code}" if creator
                              else f"https://www.fortnite.com/creative/island-codes/{code}",
                              "lists": [f"Fortnite {g}榜第{r}" for g, r in gl]}})
    if len(cache) > 5000:
        cache = {c: cache[c] for c in list(cache)[-4000:]}
    db.kv_set("fortnite_islands", cache)
    return out, errors
