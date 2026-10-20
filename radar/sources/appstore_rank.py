"""App Store 游戏榜（苹果官方 RSS，免费，不用 Key）：免费榜、畅销榜、新游免费榜，各 200 名。

注意：苹果新版 RSS（rss.marketingtools.apple.com）不能只看游戏，所以用旧版 itunes.apple.com 的 RSS。
开发时在海外服务器上测这个地址返回 403，没法验证；在你电脑上跑一次后看"运行情况"。
"""
import time
from datetime import datetime

from ..net import get_json_strict

URL = "https://itunes.apple.com/{cc}/rss/{feed}/limit=200/genre=6014/json"
FEEDS = {"rank_free": ("topfreeapplications", "免费游戏榜"),
         "rank_grossing": ("topgrossingapplications", "畅销游戏榜"),
         "rank_newfree": ("newfreeapplications", "新游免费榜")}


def _label(x):
    return (x or {}).get("label") if isinstance(x, dict) else None


def short_name(name):
    for sep in (" - ", "-", "－", "—", "–", "：", ":"):
        if sep in name:
            head = name.split(sep, 1)[0].strip()
            if len(head) >= 2:
                return head
    return name.strip()


def _ts(s):
    try:
        return int(datetime.fromisoformat(str(s)).timestamp())
    except (ValueError, TypeError):
        try:
            return int(datetime.strptime(str(s)[:10], "%Y-%m-%d").timestamp())
        except (ValueError, TypeError):
            return None


def charts(cc, market_name):
    """返回 (游戏列表, 出错说明)。"""
    out, errors = {}, []
    for metric, (feed, label) in FEEDS.items():
        try:
            d = get_json_strict(URL.format(cc=cc, feed=feed), retries=1)
        except Exception as e:
            errors.append(f"{label}：{e}")
            continue
        entries = (d.get("feed") or {}).get("entry") or []
        if isinstance(entries, dict):
            entries = [entries]
        if not entries:
            errors.append(f"{label}：榜单是空的")
        for rank, en in enumerate(entries, 1):
            aid = ((en.get("id") or {}).get("attributes") or {}).get("im:id")
            name = _label(en.get("im:name"))
            if not aid or not name:
                continue
            e = out.get(aid)
            if not e:
                imgs = en.get("im:image") or []
                rel = ((en.get("im:releaseDate") or {}).get("label")) if isinstance(en.get("im:releaseDate"), dict) else None
                cat = ((en.get("category") or {}).get("attributes") or {}).get("label") or ""
                e = out[aid] = {"entity": aid, "name": short_name(name), "metrics": {},
                                "extra": {"full_name": name, "developers": [_label(en.get("im:artist")) or ""],
                                          "genre": cat, "release_ts": _ts(rel),
                                          "image": _label(imgs[-1]) if imgs else "", "lists": []}}
            e["metrics"][metric] = rank
            e["extra"]["lists"].append(f"App Store {market_name}{label}第{rank}")
        time.sleep(1)
    return list(out.values()), errors
