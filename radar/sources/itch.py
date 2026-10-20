"""itch.io「New & Popular」榜（网页加 ?format=json 就是数据，公开，不用登录）。只有名次，没有下载数。"""
import html as _html
import re
import time

from ..net import get_json_strict

BOARDS = {
    "rank_all": ("https://itch.io/games/new-and-popular", "itch.io 新而热门"),
    "rank_web": ("https://itch.io/games/new-and-popular/platform-web", "itch.io 网页游戏新而热门"),
}


def _t(rx, s):
    m = re.search(rx, s, re.S)
    if not m:
        return ""
    return _html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()


def _parse(ch):
    gid = re.match(r"(\d+)", ch)
    a = re.search(r'<a([^>]*class="title game_link"[^>]*)>(.*?)</a>', ch, re.S)
    if not gid or not a:
        return None
    href = re.search(r'href="([^"]+)"', a.group(1))
    img = re.search(r'data-lazy_src="([^"]+)"', ch)
    return {"id": gid.group(1), "name": _html.unescape(re.sub(r"<[^>]+>", "", a.group(2))).strip(),
            "url": href.group(1) if href else "",
            "text": _t(r'class="game_text"[^>]*>(.*?)</div>', ch)[:160],
            "author": _t(r'class="game_author"[^>]*>(.*?)</div>', ch),
            "genre": _t(r'class="game_genre"[^>]*>(.*?)</div>', ch),
            "price": _t(r'class="price_value"[^>]*>(.*?)</div>', ch),
            "web": "web_flag" in ch, "image": img.group(1) if img else ""}


def boards(pages=3):
    """返回 (游戏列表, 出错说明)。每页 36 个。"""
    out, errors = {}, []
    for metric, (url, label) in BOARDS.items():
        rank, seen = 0, set()
        for page in range(1, pages + 1):
            try:
                d = get_json_strict(url, {"format": "json", "page": page}, retries=1)
            except Exception as e:
                errors.append(f"{label} 第 {page} 页：{e}")
                break
            chunks = (d.get("content") or "").split('data-game_id="')[1:]
            if not chunks:
                break
            for ch in chunks:
                g = _parse(ch)
                if not g or g["id"] in seen:
                    continue
                seen.add(g["id"])
                rank += 1
                e = out.get(g["id"])
                if not e:
                    e = out[g["id"]] = {"entity": g["id"], "name": g["name"], "metrics": {},
                                        "extra": {k: g[k] for k in ("url", "text", "author", "genre", "price",
                                                                     "web", "image")}}
                    e["extra"]["developers"] = [g["author"]] if g["author"] else []
                    e["extra"]["tags"] = [g["genre"]] if g["genre"] else []
                    e["extra"]["lists"] = []
                e["metrics"][metric] = rank
                e["extra"]["lists"].append(f"{label}第{rank}")
            time.sleep(1)
    return list(out.values()), errors
