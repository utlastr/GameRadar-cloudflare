"""好游快爆（3839.com）排行榜。网页自己用的接口，公开，不用登录。

每页返回一段 HTML，用正则解析。好游快爆没有公开下载数，只有名次；
榜单上自带"升了几名""新上榜"的标记，程序跑满一天前先用它判断。
"""
import html as _html
import re
import time
from datetime import datetime

from ..net import get_json_strict

URL = "https://www.3839.com/app/hykb_web/index.php"
# 榜单类型: (存成哪个指标, 名字)
BOARDS = {
    "hot3": ("rank_new", "新品榜"),
    "hot2": ("rank_week", "本周热门榜"),
    "hot": ("rank_hot", "热门榜"),
    "expectnum": ("rank_expect", "预约热度榜"),
    "tag80": ("rank_diy", "玩家自制榜"),
}
DAY = 86400


def _t(rx, s):
    m = re.search(rx, s, re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", m.group(1)))).strip()


def short_name(name):
    """"武士零-赛博动作斩击神作" → "武士零"（横杠后面是宣传语）。"""
    for sep in ("-", "－", "—", "–"):
        if sep in name:
            head = name.split(sep, 1)[0].strip()
            if len(head) >= 2:
                return head
    return name.strip()


def parse_release(text, now=None):
    """从"4天前正式上线""已于9月23日上午10点正式上线""9.24正式上线"里算出上线时间；没写上线的返回 None。"""
    if not text or "上线" not in text:
        return None
    now = now or time.time()
    m = re.search(r"(\d+)\s*天前[^；;，,。]*?上线", text)
    if m:
        return int(now - int(m.group(1)) * DAY)
    m = re.search(r"(\d+)\s*小时前[^；;，,。]*?上线", text)
    if m:
        return int(now - int(m.group(1)) * 3600)
    if re.search(r"(今天|今日)[^；;，,。]*?上线", text):
        return int(now)
    m = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号][^；;，,。]*?上线", text) \
        or re.search(r"(?<![\d.])()(\d{1,2})\.(\d{1,2})(?![\d.])[^；;，,。]*?上线", text)
    if not m:
        return None
    y = int(m.group(1)) if m.group(1) else datetime.now().year
    try:
        ts = datetime(y, int(m.group(2)), int(m.group(3))).timestamp()
    except ValueError:
        return None
    if not m.group(1) and ts > now + DAY:
        if ts - now > 180 * DAY:
            ts = datetime(y - 1, int(m.group(2)), int(m.group(3))).timestamp()
        else:
            return None           # 写的是以后的日期：还没上线
    return int(ts) if ts <= now + DAY else None


def _parse(ch):
    gid = re.search(r'/a/(\d+)\.htm', ch)
    name = _t(r'class="sp-name">(.*?)</span>', ch)
    if not gid or not name:
        return None
    mark = None
    up = re.search(r'class="st-up">\s*<i>\s*</i>\s*(\d+)', ch)
    if up:
        mark = int(up.group(1))
    elif 'class="st-new"' in ch:
        mark = "new"
    elif 'class="st-down"' in ch:
        mark = -1
    tags_html = re.search(r'class="sp-tags">(.*?)</div>', ch, re.S)
    tags = [_html.unescape(x).strip() for x in re.findall(r"<span>([^<]+)</span>", tags_html.group(1))] \
        if tags_html else []
    img = re.search(r'(?:lz_src|src)="(https?://img\.[^"]+)"', ch)
    score = _t(r'class="sp-score">(.*?)</div>', ch)
    return {"id": gid.group(1), "name": name, "mark": mark, "tags": tags,
            "pay": _t(r'class="sp-tag">(.*?)</span>', ch),
            "score": float(score) if re.fullmatch(r"\d+(\.\d+)?", score or "") else None,
            "text": _t(r'class="game-text">(.*?)</div>\s*</div>', ch)[:80],
            "image": img.group(1) if img else ""}


def boards(max_pages=200, progress=None):
    """返回 (游戏列表, 出错的榜单说明)。"""
    out, errors = {}, []
    jobs = list(BOARDS.items())
    for i, (typ, (metric, label)) in enumerate(jobs, 1):
        rank, seen = 0, set()
        for page in range(1, max_pages + 1):
            if progress:
                progress(i, len(jobs))
            try:
                d = get_json_strict(URL, {"m": "top", "ac": "rank_list", "type": typ, "page": page, "offset": 0},
                                    headers={"Referer": "https://www.3839.com/top/"}, retries=1)
            except Exception as e:
                errors.append(f"{label}第 {page} 页：{e}")
                break
            if str(d.get("code")) != "100":
                if page == 1:
                    errors.append(f"{label}：返回 code={d.get('code')}")
                break
            chunks = (d.get("html") or "").split('<div class="game-item')[1:]
            if not chunks:
                break
            before = rank
            for ch in chunks:
                g = _parse(ch)
                if not g or g["id"] in seen:
                    continue
                seen.add(g["id"])
                rank += 1
                e = out.get(g["id"])
                if not e:
                    e = out[g["id"]] = {
                        "entity": g["id"], "name": short_name(g["name"]), "metrics": {},
                        "extra": {"full_name": g["name"], "tags": g["tags"], "pay": g["pay"], "score": g["score"],
                                  "text": g["text"], "image": g["image"], "lists": [], "marks": {}}}
                e["metrics"][metric] = rank
                e["extra"]["lists"].append(f"{label}第{rank}")
                if g["mark"] is not None:
                    e["extra"]["marks"][metric] = g["mark"]
                if g["text"] and not e["extra"]["text"]:
                    e["extra"]["text"] = g["text"]
            if rank == before or str(d.get("nextpage", 1)) == "0":
                break             # 这一页全是重复的 / 好游快爆说没有下一页了
            time.sleep(1)
        else:
            errors.append(f"{label}达到 {max_pages} 页安全上限，覆盖尚未完整")
    return list(out.values()), errors
