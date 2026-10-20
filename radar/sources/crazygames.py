"""CrazyGames「Hot」（过去 24 小时里在爆的网页游戏）。

先找网页里 Next.js 自带的数据（有游戏名）；找不到就按网页里游戏链接出现的顺序，名字用链接里的英文。
"""
import json
import re

from ..net import get_text

URL = "https://www.crazygames.com/hot"


def _title(slug):
    return " ".join(w.capitalize() for w in slug.split("-"))


def _game_lists(node, out):
    if isinstance(node, list):
        games = [x for x in node if isinstance(x, dict) and isinstance(x.get("slug"), str) and x.get("name")]
        if len(games) >= 10:
            out.append(games)
        for x in node:
            _game_lists(x, out)
    elif isinstance(node, dict):
        for v in node.values():
            if isinstance(v, (list, dict)):
                _game_lists(v, out)


def hot():
    text = get_text(URL, headers={"Accept-Language": "en-US,en;q=0.9"})
    rows, covers = [], {}
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    if m:
        try:
            lists = []
            _game_lists(json.loads(m.group(1)), lists)
            if lists:
                for g in max(lists, key=len):
                    slug = g["slug"].strip("/").split("/")[-1]
                    if slug and slug not in [r[0] for r in rows]:
                        rows.append((slug, g["name"]))
                        cov = g.get("cover") or g.get("thumbnail") or g.get("image") or ""
                        if isinstance(cov, str) and cov:
                            # v51：网页数据里的封面多半是相对路径，图片放在 imgs.crazygames.com
                            covers[slug] = cov if cov.startswith("http") else "https://imgs.crazygames.com/" + cov.lstrip("/")
        except ValueError:
            pass
    if not rows:
        for slug in re.findall(r'(?:https://www\.crazygames\.com)?/game/([a-z0-9][a-z0-9-]*)["?#]', text):
            if slug not in [r[0] for r in rows]:
                rows.append((slug, _title(slug)))
    if not rows:
        snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or ""))[:120]
        raise RuntimeError("CrazyGames 网页里没找到游戏列表（网页改版或被拦）：" + snippet)
    out = []
    for rank, (slug, name) in enumerate(rows, 1):
        out.append({"entity": slug, "name": name, "metrics": {"rank_hot": rank},
                    "extra": {"url": f"https://www.crazygames.com/game/{slug}", "image": covers.get(slug, ""),
                              "lists": [f"CrazyGames 24 小时热门第{rank}"]}})
    return out
