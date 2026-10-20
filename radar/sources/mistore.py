"""小米应用商店网页版 · 游戏排行（app.mi.com，公开）。只有名次，大多是老牌大游戏，主要看新上榜和名次上升。"""
import html as _html
import re
import time

from ..net import get_text

URL = "https://app.mi.com/catTopList/15"


def _name(s):
    return _html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def ranking(pages=5):
    out, rank, errors = {}, 0, []
    for page in range(1, pages + 1):
        try:
            text = get_text(URL, params={"page": page})
        except Exception as e:
            errors.append(f"第 {page} 页：{e}")
            break
        ul = re.search(r'<ul class="applist">(.*?)</ul>', text, re.S)
        lis = re.findall(r"<li>(.*?)</li>", ul.group(1), re.S) if ul else []
        if not lis:
            if page == 1:
                snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or ""))[:120]
                raise RuntimeError("小米商店网页里没找到排行列表（网页改版或被拦）：" + snippet)
            break
        new = 0
        for li in lis:
            pkg = re.search(r'/details\?id=([\w.]+)', li)
            h5 = re.search(r"<h5>(.*?)</h5>", li, re.S)
            if not pkg or pkg.group(1) in out:
                continue
            name = _name(h5.group(1)) if h5 else ""
            if not name:
                alt = re.search(r'alt="([^"]+)"', li)
                name = _name(alt.group(1)) if alt else pkg.group(1)
            cat = re.search(r'class="app-desc"><a[^>]*>([^<]+)</a>', li)
            img = re.search(r'(?:data-src|src)="(https?://[^"]+)"', li)
            rank += 1
            new += 1
            out[pkg.group(1)] = {"entity": pkg.group(1), "name": name, "metrics": {"rank_game": rank},
                                 "extra": {"genre": _name(cat.group(1)) if cat else "",
                                           "tags": [_name(cat.group(1))] if cat else [],
                                           "image": img.group(1) if img else "",
                                           "lists": [f"小米商店游戏排行第{rank}"]}}
        if not new:
            break
        time.sleep(1)
    if not out:
        raise RuntimeError("小米商店排行一个都没拿到：" + "；".join(errors))
    return list(out.values())
