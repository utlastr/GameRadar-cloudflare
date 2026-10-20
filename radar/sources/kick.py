"""Kick 直播：每个游戏分类现在有多少人在看（公开接口，不用登录）。Kick 只给分类观看人数，不给主播数。"""
import time

from ..images import url_of
from ..net import get_json, get_json_strict

URL = "https://kick.com/api/v1/subcategories"


def categories(max_pages=15, progress=None):
    out = {}
    for page in range(1, max_pages + 1):
        if progress:
            progress(page, max_pages)
        params = {"limit": 20, "page": page}
        d = get_json_strict(URL, params, headers={"Accept": "application/json"}) if page == 1 \
            else get_json(URL, params, headers={"Accept": "application/json"})
        data = (d or {}).get("data") or []
        if not data:
            break
        still_big = False
        for c in data:
            v = c.get("viewers") or 0
            if v >= 50:
                still_big = True
            if ((c.get("category") or {}).get("slug") or "") != "games":
                continue          # IRL、Gambling、Music 这些不是游戏
            slug = c.get("slug") or str(c.get("id"))
            out[slug] = {"entity": slug, "name": c.get("name") or slug, "metrics": {"viewers": v},
                         "extra": {"id": c.get("id"), "url": f"https://kick.com/category/{slug}",
                                   "image": url_of(c.get("banner")) or url_of(c.get("image"))}}
        if not still_big:
            break
        time.sleep(1)
    if not out:
        raise RuntimeError("Kick 返回了数据，但里面没有游戏分类（接口可能改了）")
    return list(out.values())
