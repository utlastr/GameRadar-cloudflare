"""微信小游戏、抖音小游戏榜单：数据来自引力引擎小游戏排行榜（rank.gravity-engine.com，公开网页，不用登录）。

它的接口每次请求要带签名，返回的数据是加密的。算法是从它网页代码里找到的（v46 探测），助手实测过：
  session   = base64("etg" + 5 位随机十六进制)
  signature = md5(毫秒时间戳[3:8] + "11" + session + 请求体 JSON)
  返回 data.text = base64(AES-128-ECB 密文)，密钥 = "etg…"(8 位) + "gv" + 时间戳[7:11] + "00"
请求体必须和签名用的字符串一字不差（JSON 里不能有空格），所以自己拼好字符串再发。
每个榜 100 名，接口一页最多 20 个，所以翻 5 页。榜单是按天出的，几小时抓一次就够。
对方哪天改了算法，这里会报"签名不对"或"解密失败"，到时把新的网页代码发给助手。
"""
import base64
import hashlib
import json
import random
import time

from .. import aes
from ..net import post_json

URL = "https://api-insight.gravity-engine.com/apprank/api/v1/rank/public_list/"
HEADERS = {"Origin": "https://rank.gravity-engine.com", "Referer": "https://rank.gravity-engine.com/",
           "Accept": "application/json, text/plain, */*"}
# 平台 → (接口里的名字, [(接口榜单名, 本程序指标名, 榜单中文名)])。接口允许的榜：popularity / bestseller /
# most_played / fresh_game（free 两个平台都是空的；微信没有新游榜，抖音没有最多人玩榜，实测为空）
GENRES = {
    "wxmini": ("wx_minigame", [("popularity", "rank_popular", "人气榜"), ("most_played", "rank_played", "最多人玩榜"),
                               ("bestseller", "rank_grossing", "畅销榜")]),
    "dymini": ("dy_minigame", [("popularity", "rank_popular", "人气榜"), ("fresh_game", "rank_new", "新游榜"),
                               ("bestseller", "rank_grossing", "畅销榜")]),
}
PLAT_NAME = {"wxmini": "微信小游戏", "dymini": "抖音小游戏"}
PAGES = 5
PAGE_SIZE = 20


def _call(genre, rank_type, page):
    g = "etg" + "".join(random.choice("0123456789abcdef") for _ in range(5))
    session = base64.b64encode(g.encode()).decode()
    ts = str(int(time.time() * 1000))
    body = json.dumps({"page": page, "page_size": PAGE_SIZE,
                       "extra_fields": {"change_label": True, "app_genre_ranking": True},
                       "filters": [{"field": "period_type", "operator": 1, "values": ["day"]},
                                   {"field": "rank_type", "operator": 1, "values": [rank_type]},
                                   {"field": "rank_genre", "operator": 1, "values": [genre]}]},
                      separators=(",", ":"), ensure_ascii=False)
    sig = hashlib.md5((ts[3:8] + "11" + session + body).encode("utf-8")).hexdigest()
    h = dict(HEADERS, **{"gravity-timestamp": ts, "gravity-session": session, "gravity-signature": sig})
    d = post_json(URL, body, headers=h, timeout=40)
    if d.get("code") != 0:
        err = d.get("msg") or "未知错误"
        if (d.get("extra") or {}).get("error"):
            err += "：" + str(d["extra"]["error"])
        if d.get("code") == 1004:
            err += "（签名不对，可能引力引擎改了算法）"
        raise RuntimeError(f"引力引擎返回 {d.get('code')}：{err}")
    text = (d.get("data") or {}).get("text")
    if not text:
        return {}
    plain = aes.ecb_decrypt((g + "gv" + ts[7:11] + "00").encode(), base64.b64decode(text))
    return json.loads(plain.decode("utf-8"))


def boards(src, progress=None):
    """返回 (游戏列表, 出错说明)。src = wxmini / dymini。"""
    genre, lists = GENRES[src]
    plat = PLAT_NAME[src]
    out, errors = {}, []
    total = len(lists) * PAGES
    done = 0
    for rank_type, metric, label in lists:
        got = 0
        for page in range(1, PAGES + 1):
            done += 1
            if progress:
                progress(done, total)
            try:
                d = _call(genre, rank_type, page)
            except Exception as e:
                errors.append(f"{label}第 {page} 页：{e}")
                break
            rows = d.get("list") or []
            for a in rows:
                info = a.get("app_info") or {}
                aid = str(info.get("app_id") or a.get("app_id") or "")
                name = (info.get("app_name") or "").strip()
                rank = a.get("ranking")
                if not aid or not name or not rank:
                    continue
                e = out.get(aid)
                if not e:
                    typ = "·".join(x for x in (str(info.get("game_type_main_name") or ""),
                                               str(info.get("game_type_sub_name") or "")) if x and not x.isdigit())
                    e = out[aid] = {"entity": aid, "name": name, "metrics": {},
                                    "extra": {"developers": [info.get("publisher_name") or ""], "genre": typ,
                                              "image": info.get("icon_url") or "",
                                              "mini_app_id": info.get("mini_app_id") or "",
                                              "stat_date": a.get("stat_datetime") or "", "lists": [], "marks": {}}}
                e["metrics"][metric] = int(rank)
                e["extra"]["lists"].append(f"{plat}{label}第{rank}")
                ch = a.get("change")
                if isinstance(ch, int) and ch > 0:
                    e["extra"]["marks"][metric] = ch          # 比昨天往前了几名（还没有自己的 24 小时前数据时用）
                msg = ((a.get("change_label") or {}).get("first_msg") or "")
                if msg:
                    e["extra"].setdefault("labels", {})[metric] = msg
                got += 1
            if len(rows) < PAGE_SIZE:
                break
            time.sleep(1)
        if not got and not any(x.startswith(label) for x in errors):
            errors.append(f"{label}：榜单是空的")
    return list(out.values()), errors
