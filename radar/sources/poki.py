"""Poki「Popular this week」（本周热门）。网页里自带一份按名次排好的游戏列表（给搜索引擎看的 ItemList）。"""
import json
import re

from ..net import get_text

URL = "https://poki.com/en/popular"


def _slug(url):
    m = re.search(r"/g/([a-z0-9][a-z0-9-]*)", url or "")
    return m.group(1) if m else ""


def _title(slug):
    return " ".join(w.capitalize() for w in slug.split("-"))


def _itemlists(node):
    if isinstance(node, list):
        for x in node:
            yield from _itemlists(x)
    elif isinstance(node, dict):
        if node.get("@type") == "ItemList":
            yield node
        for v in node.values():
            if isinstance(v, (list, dict)):
                yield from _itemlists(v)


def popular():
    text = get_text(URL, headers={"Accept-Language": "en-US,en;q=0.9"})
    best = []
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', text, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        for lst in _itemlists(d):
            rows = []
            for el in lst.get("itemListElement") or []:
                it = el.get("item") if isinstance(el.get("item"), dict) else el
                slug = _slug(it.get("url") or it.get("@id") or "")
                if slug:
                    rows.append((int(el.get("position") or len(rows) + 1), slug, it.get("name") or _title(slug),
                                 it.get("image") if isinstance(it.get("image"), str) else ""))
            if len(rows) > len(best):
                best = rows
    if not best:
        # 备用：按网页里游戏链接出现的顺序
        seen = []
        for slug in re.findall(r'href="/en/g/([a-z0-9][a-z0-9-]*)"', text):
            if slug not in seen:
                seen.append(slug)
        best = [(i, s, _title(s), "") for i, s in enumerate(seen, 1)]
    if not best:
        snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or ""))[:120]
        raise RuntimeError("Poki 网页里没找到游戏列表（网页改版或被拦）：" + snippet)
    best.sort()
    out = []
    for rank, (_, slug, name, img) in enumerate(best, 1):
        out.append({"entity": slug, "name": name, "metrics": {"rank_popular": rank},
                    "extra": {"url": f"https://poki.com/en/g/{slug}", "image": img or "",
                              "lists": [f"Poki 本周热门第{rank}"]}})
    return out
