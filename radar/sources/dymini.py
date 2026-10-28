"""Official Douyin mini-game charts through the existing signed-in CDP page."""
import json
from urllib.parse import parse_qs, urlsplit

from .. import cdp, config

PAGE_URL = "https://developer.open-douyin.com/game-console/creative-accelerator"
API_PATH = "/bff_api_game_console/v2/PlatBaseSettingServices/GetWorkbenchGameRankList"
CHART_SIZE = 200
BOARDS = (
    (3, "rank_new", "新游榜"),
    (1, "rank_popular", "热门榜"),
    (2, "rank_grossing", "畅销榜"),
    (4, "rank_publisher", "发行人热度榜"),
)


def candidate(rank):
    return rank is not None and config.DYMINI_MIN_RANK <= rank <= config.DYMINI_MAX_RANK


def _rows(payload):
    if not isinstance(payload, dict) or payload.get("error") != 0:
        message = payload.get("message") if isinstance(payload, dict) else ""
        raise RuntimeError(message or "抖音榜单请求失败，需要已登录的开发者页面")
    data = payload.get("data") or {}
    rows = data.get("gameInfo") or []
    parsed, ids, ranks = [], set(), set()
    for row in rows:
        aid = (parse_qs(urlsplit(row.get("schema") or "").query).get("app_id") or [""])[0]
        name, rank = (row.get("name") or "").strip(), row.get("index")
        if not aid.startswith("tt") or not name or type(rank) is not int or not 1 <= rank <= CHART_SIZE:
            raise RuntimeError("抖音榜单记录缺少真实 app_id、名称或有效名次")
        if aid in ids or rank in ranks:
            raise RuntimeError("抖音榜单出现重复游戏或重复名次")
        ids.add(aid)
        ranks.add(rank)
        parsed.append((aid, name, rank, row))
    if ranks != set(range(1, CHART_SIZE + 1)) or data.get("hasMore"):
        raise RuntimeError(f"抖音榜单只拿到 {len(parsed)}/{CHART_SIZE} 名，未完成前 200 名采集")
    return parsed


def boards(progress=None):
    tab = cdp.attach(PAGE_URL)
    if tab is None:
        raise RuntimeError("CDP 9222 中缺少已登录的抖音小游戏创意加速器页面")
    out, errors = {}, []
    try:
        for i, (rank_type, metric, label) in enumerate(BOARDS, 1):
            if progress:
                progress(i, len(BOARDS))
            body = {"onlyMine": False, "page": 1, "pageSize": CHART_SIZE, "rankType": rank_type}
            script = """(async () => {
                const r = await fetch(%s, {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(%s)
                });
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return await r.json();
            })()""" % (json.dumps(API_PATH), json.dumps(body))
            try:
                parsed = _rows(tab.js(script))
            except Exception as exc:
                if metric == "rank_new":
                    raise RuntimeError(f"新游榜采集失败：{exc}") from exc
                errors.append(f"{label}：{exc}")
                continue
            for aid, name, rank, row in parsed:
                entry = out.setdefault(aid, {
                    "entity": aid, "name": name, "metrics": {},
                    "extra": {"mini_app_id": aid, "lists": [], "marks": {},
                              "rank_history": {}, "collector": "douyin_official",
                              "source_url": PAGE_URL, "new_candidate": False},
                })
                entry["metrics"][metric] = rank
                extra = entry["extra"]
                extra["lists"].append(f"抖音小游戏{label}第{rank}")
                extra["rank_history"][metric] = row.get("rankData") or []
                extra["image"] = row.get("iconUrl") or extra.get("image", "")
                extra["tags"] = row.get("keyWords") or extra.get("tags", [])
                change = row.get("rankingChange")
                if type(change) is int and change > 0:
                    extra["marks"][metric] = change
                if metric == "rank_new":
                    extra["new_candidate"] = candidate(rank)
    finally:
        tab.close()
    return list(out.values()), errors
