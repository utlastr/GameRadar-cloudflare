"""TapTap 数据源。做法完全照参考网站作者 as167888 公开的爬虫（只看网页，不用接口）：

- TapTap 制造（taptap-maker-tracker/1.爬取带TapTap制造tag的游戏/taptap_spider.py）：
  翻 https://www.taptap.cn/tag/TapTap%E5%88%B6%E9%80%A0 第 1–50 页（第 1 页不带 page 参数），
  每张卡片 div.app-title：父级 <a> 的 href 是详情链接，里面 meta[itemprop=name] 是名字。
  没解析到的页跳过继续翻，不提前停。每页成功后停 1.5 秒。
- TapTap 榜单：仅新品榜，使用当前榜单页面实际请求的 app-top/v2/hits 分页。
  参考 TapTrack 的发现、去重、逐游戏详情及历史持久化流程，不再使用失效的旧 HTML 分页参数。
- 游戏详情（taptap_mk_detail.py / crawl_detail.py）：打开 https://www.taptap.cn/app/ID?os=android，
  JSON-LD（@type=VideoGame）读名字、上线日期、热度（userInteractionCount）、评分、评价数，
  __NUXT_DATA__ 里 fans_count 指向的值是关注数。404 = 已下线。每款之间随机停 1–3 秒（由调用方停）。

网络和解析用 requests + BeautifulSoup（和他一样，第一次运行自动安装）；装不上时退回 Python 自带的库。
"""
import json
import re
import time
import urllib.parse
from datetime import datetime
from html import unescape

from .. import deps
from ..images import url_of
from ..net import get_text as _urllib_get, is_waf

MAKER_TAG = "TapTap制造"
WORKSHOP_TAG = "创意工坊"
BASE_URL = "https://www.taptap.cn"

# 请求头：每一处都和他对应的脚本一样
TAG_HEADERS = {      # taptap_spider.py
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.taptap.cn/",
}
BOARD_HEADERS = {    # crawl_bangdan.py
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
DETAIL_HEADERS = {   # taptap_mk_detail.py / crawl_detail.py
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# 他的 榜单链接.txt（分类名, 网址），原样照抄
_BOARD_PAGES = ([("热门榜", "download", 15), ("热玩榜", "played", 15), ("新品榜", "download/new", 10),
                 ("独家榜", "exclusive", 10)]
                + [(n, "download/" + s, 5) for n, s in (
                    ("动作榜", "action"), ("策略榜", "strategy"), ("放置榜", "idle"), ("单机榜", "single"),
                    ("休闲榜", "casual"), ("沙盒生存榜", "sandbox_survival"), ("模拟经营榜", "management"),
                    ("解谜榜", "unriddle"), ("射击榜", "shooter"), ("多人对战榜", "multiplayer"),
                    ("二次元榜", "acgn"), ("音乐节奏榜", "music"), ("剧情榜", "scenario"),
                    ("武侠榜", "swordsman"), ("女性向榜", "otome"), ("独立游戏榜", "independent"),
                    ("肉鸽榜", "roguelike"))])
BOARD_LINKS = [(cat, f"{BASE_URL}/top/{path}?page={p}") for cat, path, n in _BOARD_PAGES for p in range(1, n + 1)]
# 记名次的几个主榜（其他分类榜只记"在哪个榜第几"）
BOARD_RANK_KEYS = {"热门榜": "hot", "热玩榜": "played", "新品榜": "new", "独家榜": "exclusive"}

_mode_said = False


def _say(msg):
    print(f"{time.strftime('%H:%M:%S')}  {msg}")


def _use_requests():
    global _mode_said
    ok = deps.ensure()
    if not _mode_said:
        _mode_said = True
        _say("TapTap 抓取：" + ("用 requests + BeautifulSoup（和参考网站一样）" if ok
                               else "requests 没装上，暂用 Python 自带的网络库"))
    return ok


_direct_mode = False      # v49：走系统代理连 TapTap 断过一次后，这次运行一直不走代理（TapTap 是国内网站，不需要代理）
_direct_sess = None


def _direct():
    global _direct_sess
    if _direct_sess is None:
        import requests
        _direct_sess = requests.Session()
        _direct_sess.trust_env = False      # 不读系统代理 / 环境变量里的代理，直接连
    return _direct_sess


def _req(url, headers, timeout):
    """requests 抓一页。连接被掐断、读超时：换成不走代理、隔几秒再试，最多 3 次。"""
    global _direct_mode
    import requests
    last = None
    for attempt in range(3):
        direct = _direct_mode or attempt >= 1
        try:
            resp = (_direct() if direct else requests).get(url, headers=headers, timeout=timeout)
            if direct and not _direct_mode:
                _direct_mode = True
                _say("TapTap：走代理时连接被掐断（SSLEOFError），改成不走代理直接连，成功了；这次运行之后都直接连")
            return resp
        except requests.exceptions.Timeout as e:              # 读超时：TapTap 自己慢，等一下再试
            last = e
            time.sleep(2 * attempt + 1)
        except requests.exceptions.ConnectionError as e:      # 包括 SSLError、ProxyError、连不上
            last = e
            time.sleep(2 * attempt + 1)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"{type(e).__name__}: {e}")
    return last


def _get(url, headers, timeout):
    """返回 (状态码, 网页文本)。网络出错抛出 RuntimeError。"""
    if _use_requests():
        resp = _req(url, headers, timeout)
        if isinstance(resp, Exception):
            # requests 走代理、不走代理都失败：最后用 Python 自带网络库再试一次
            try:
                text = _urllib_get(url, headers=headers, timeout=timeout)
            except Exception:
                raise RuntimeError(f"{type(resp).__name__}: {resp}（走代理、不走代理各试过，都连不上 TapTap）")
            status = 404 if re.search(r"<title>\s*404\s*</title>", text or "") else 200
            return status, text
        resp.encoding = "utf-8"
        text = resp.text
        if not is_waf(text):
            return resp.status_code, text
        # 被阿里云防爬验证拦下（他在家里电脑上跑时没有这一步）：交给自带网络库算 cookie 再请求
    text = _urllib_get(url, headers=headers, timeout=timeout)
    status = 404 if re.search(r"<title>\s*404\s*</title>", text or "") else 200
    return status, text


def _soup(text, parser="html.parser"):
    from bs4 import BeautifulSoup
    if parser == "lxml":
        try:
            import lxml  # noqa: F401
        except ImportError:
            parser = "html.parser"
    return BeautifulSoup(text, parser)


def _have_bs4():
    try:
        import bs4  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------- TapTap 制造标签页 ----------------
def _tag_cards(page_html):
    """[(详情链接, 名字)]，照 taptap_spider.py"""
    out = []
    if _have_bs4():
        soup = _soup(page_html)
        for title_div in soup.find_all("div", class_="app-title"):
            parent_a = title_div.parent
            link = ""
            if parent_a and parent_a.name == "a" and parent_a.get("href"):
                link = f"{BASE_URL}{parent_a.get('href')}"
            name = ""
            meta_name = title_div.find("meta", itemprop="name")
            if meta_name and meta_name.get("content"):
                name = meta_name.get("content")
            if name and link:
                out.append((link, name))
        return out
    for m in re.finditer(r'<div[^>]*class="[^"]*\bapp-title\b[^"]*"[^>]*>', page_html):
        before = page_html[max(0, m.start() - 800):m.start()]
        a = re.findall(r'href="(/app/\d+[^"]*)"', before)
        name = _meta_name(page_html[m.end():m.end() + 800])
        if a and name:
            out.append((BASE_URL + unescape(a[-1]), name))
    return out


def _meta_name(chunk):
    mm = re.search(r'<meta[^>]*itemprop="name"[^>]*>', chunk)
    if not mm:
        return ""
    c = re.search(r'content="([^"]*)"', mm.group(0))
    return unescape(c.group(1)).strip() if c else ""


def _app_id(link):
    m = re.search(r"/app/(\d+)", link or "")
    return int(m.group(1)) if m else None


def tag_list(tag=MAKER_TAG, pages=50, progress=None):
    """照 taptap_spider.py：第 1 页到第 pages 页。返回 [(名次, {"id", "title"})]，按链接去重。"""
    base_url = BASE_URL + "/tag/" + urllib.parse.quote(tag)
    out, seen, errs, empty = [], set(), [], []
    for page in range(1, pages + 1):
        url = base_url if page == 1 else f"{base_url}?page={page}"
        if progress:
            progress(page, pages)
        try:
            status, text = _get(url, TAG_HEADERS, 10)
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            games = _tag_cards(text)
            if not games:
                empty.append(page)      # 他的做法：这一页没数据，跳过，继续下一页
                continue
            for link, name in games:
                aid = _app_id(link)
                if aid is None or aid in seen:
                    continue
                seen.add(aid)
                out.append((len(out) + 1, {"id": aid, "title": name}))
            time.sleep(1.5)
        except Exception as e:
            errs.append(f"第 {page} 页：{e}")
    msg = f"{tag}·网页：翻了 {pages} 页，拿到 {len(out)} 款"
    if empty:
        msg += f"；{len(empty)} 页没有游戏（第 {empty[0]} 页起）"
    if errs:
        msg += f"；{len(errs)} 页出错，第一个：{errs[0]}"
    _say(msg)
    if not out:
        raise RuntimeError(errs[0] if errs else "标签页里找不到游戏卡片（页面结构可能变了）")
    return out


# ---------------- TapTap 榜单网页 ----------------
def _board_games(page_html):
    """[(名字, app_id)]，照 crawl_bangdan.py 的 extract_games"""
    games = []
    if _have_bs4():
        soup = _soup(page_html, "lxml")
        modern = soup.select('a.ranking-card[data-scroll-restore-anchor^="new:"]')
        if modern:
            for item in modern:
                title = item.select_one(".ranking-card__title")
                aid = _app_id(item.get("href"))
                if title and aid:
                    games.append((title.get_text(strip=True), aid))
            return games
        for item in soup.find_all("div", class_="list-item"):
            name_meta = item.find("meta", itemprop="name")
            if not name_meta:
                continue
            name = (name_meta.get("content") or "").strip()
            if not name:
                continue
            app_link = None
            for a in item.find_all("a", href=True):
                if re.match(r"^/app/\d+", a["href"]):
                    app_link = a["href"]
                    break
            if app_link:
                games.append((name, int(re.search(r"/app/(\d+)", app_link).group(1))))
        return games
    starts = [m.start() for m in re.finditer(r'<div[^>]*class="[^"]*\blist-item\b', page_html)] + [len(page_html)]
    for s, e in zip(starts, starts[1:]):
        chunk = page_html[s:e]
        name = _meta_name(chunk)
        aa = re.search(r'href="/app/(\d+)', chunk)
        if name and aa:
            games.append((name, int(aa.group(1))))
    return games


def board_lists(progress=None):
    """Read only the new-release chart, following its actual offset pagination."""
    from .. import db
    apps, seen_pages = {}, set()
    report = {"scope": "new", "pages": 0, "count": 0, "complete": False}
    offset = 0
    for idx in range(1, 201):
        if progress:
            progress(idx, 0)
        try:
            query = urllib.parse.urlencode({"from": offset, "limit": 10, "platform": "android",
                                            "type_name": "new", "X-UA": _XUA})
            status, text = _get(BASE_URL + "/webapiv2/app-top/v2/hits?" + query, TAG_HEADERS, 20)
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            payload = json.loads(text)
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise RuntimeError("新品榜响应缺少 data.list")
            items = data["list"]
            fingerprint = tuple(str((r.get("app") or {}).get("id")) for r in items)
            if items and fingerprint in seen_pages:
                raise RuntimeError("新品榜重复分页，未将重复页算作新增游戏")
            seen_pages.add(fingerprint)
            for position, row in enumerate(items, offset + 1):
                app = row.get("app") or {}
                if app.get("id") and app.get("title"):
                    apps[int(app["id"])] = {"name": app["title"],
                                            "lists": [("新品榜", position)]}
            report.update(pages=idx, count=len(apps))
            total = data.get("total")
            report["declared_total"] = total
            if len(items) < 10 or ("next_page" in data and not data["next_page"]):
                report["complete"] = True
                break
            offset += len(items)
        except Exception as e:
            report["error"] = str(e)[:400]
            db.kv_set("taptap_new_coverage", report)
            raise RuntimeError(f"TapTap 新品榜第 {idx} 页失败，已发现 {len(apps)} 款：{e}") from e
        time.sleep(0.5)
    db.kv_set("taptap_new_coverage", report)
    if not report["complete"]:
        raise RuntimeError("TapTap 新品榜超过分页安全上限，未标记为完整")
    if not apps:
        raise RuntimeError("TapTap 新品榜返回空列表")
    _say(f"TapTap 新品榜：{report['pages']} 页，{len(apps)} 款")
    return apps


# ---------------- 游戏详情页 ----------------
GONE = "gone"
DETAIL_TIMEOUT = 20   # 详情页偶尔很慢，15 秒时经常还没回包就被判失败


def _ld_and_nuxt(page_html):
    lds, nuxt = [], None
    if _have_bs4():
        soup = _soup(page_html)
        lds = [s.string for s in soup.find_all("script", type="application/ld+json")]
        ns = soup.find("script", id="__NUXT_DATA__")
        nuxt = ns.string if ns else None
    else:
        lds = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page_html, re.S)
        m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', page_html, re.S)
        nuxt = m.group(1) if m else None
    return lds, nuxt


_OG_IMG = (re.compile(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"'),
           re.compile(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"'))


def _og_image(page_html):
    for r in _OG_IMG:
        m = r.search(page_html or "")
        if m:
            return unescape(m.group(1))
    return ""


def detail(app_id, strict=False, timeout=DETAIL_TIMEOUT):
    """Fetch this native ID; strict mode preserves failures for monitoring."""
    try:
        status, page_html = _get(f"{BASE_URL}/app/{app_id}?os=android", DETAIL_HEADERS, timeout)
        if status == 404:
            return GONE
        if status != 200:
            raise RuntimeError(f"TapTap {app_id} HTTP {status}")
        return parse_detail_html(app_id, page_html)
    except Exception:
        if strict:
            raise
        return None


def parse_detail_html(app_id, page_html):
    lds, nuxt = _ld_and_nuxt(page_html)
    game = None
    for s in lds:
        try:
            data = json.loads(s or "")
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "VideoGame":
            canonical = data.get("@id") or data.get("url")
            if canonical and _app_id(canonical) != int(app_id):
                continue
            game = data
            break
    if not game:
        raise RuntimeError(f"TapTap {app_id} 页面未包含该 ID 的游戏资料（可能返回验证页）")
    fans = None
    hits = None
    try:
        nd = json.loads(nuxt) if nuxt else None
        if isinstance(nd, list):
            def deref(index):
                return nd[index] if type(index) is int and 0 <= index < len(nd) else None
            for item in nd:
                if not isinstance(item, dict) or str(deref(item.get("id"))) != str(app_id):
                    continue
                stat = deref(item.get("stat"))
                if isinstance(stat, dict):
                    fans = deref(stat.get("fans_count"))
                    hits = deref(stat.get("hits_total"))
                    break
    except ValueError:
        pass
    if type(fans) not in (int, float) or fans < 0:
        fans = None
    if type(hits) not in (int, float) or hits < 0:
        hits = None
    inter = game.get("interactionStatistic") or {}
    if isinstance(inter, list):
        inter = inter[0] if inter else {}
    agg = game.get("aggregateRating") or {}
    rel = 0
    raw_date = str(game.get("datePublished") or "")
    if raw_date:
        try:
            rel = int(datetime.fromisoformat(raw_date.replace("Z", "+00:00")).timestamp())
        except ValueError:
            try:
                rel = int(datetime.strptime(raw_date.split("T")[0].split(" ")[0], "%Y-%m-%d").timestamp())
            except ValueError:
                rel = 0
    genre = game.get("applicationSubCategory") or game.get("genre") or []
    genres = [genre] if isinstance(genre, str) else list(genre)
    author = game.get("author") or {}
    return {
        "id": int(app_id), "title": game.get("name") or str(app_id), "released_time": rel,
        "description": str(game.get("description") or "")[:800],
        "stat": {"hits_total": hits if hits is not None else inter.get("userInteractionCount"), "fans_count": fans,
                 "review_count": agg.get("ratingCount"), "rating": {"score": str(agg.get("ratingValue") or "")}},
        "tags": [{"value": g} for g in genres if g],
        "information": [{"title": "厂商", "text": author.get("name") if isinstance(author, dict) else ""}],
        # v51：JSON-LD 里的 image 有时是列表或 {"url": ...}，统一取成地址；没有就用网页的 og:image
        "icon": {"small_url": url_of(game.get("image")) or _og_image(page_html)},
        "source_url": f"{BASE_URL}/app/{app_id}",
    }


def kind_of(app):
    """maker = 带 TapTap制造 标签；workshop = 带创意工坊标签、不带 TapTap制造；其他返回空。"""
    tags = {t.get("value") for t in app.get("tags") or []}
    if MAKER_TAG in tags:
        return "maker"
    if WORKSHOP_TAG in tags:
        return "workshop"
    return ""


def _info(app, title):
    for it in app.get("information") or []:
        if it.get("title") == title:
            return it.get("text") or ""
    return ""


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def parse_app(app):
    st = app.get("stat") or {}
    released = app.get("released_time") or (app.get("seo") or {}).get("release_time") or 0
    preorder = app.get("button_label") == "预约"
    metrics = {
        "fans": _num(st.get("fans_count")),
        "reviews": _num(st.get("review_count")),
    }
    hits = _num(st.get("hits_total"))
    if hits is not None:      # 0 也是有效数字（刚上线的游戏热度就是 0），不能当成"没抓到"
        metrics["downloads"] = hits
    if st.get("reserve_count") is not None:
        metrics["reserves"] = st.get("reserve_count")
    return {
        "entity": str(app["id"]),
        "name": app.get("title") or str(app["id"]),
        "metrics": metrics,
        "extra": {
            "tags": [t.get("value") for t in app.get("tags") or [] if t.get("value")],
            "rec_text": app.get("rec_text") or "",
            "desc": (app.get("description") or "")[:800] if isinstance(app.get("description"), str) else "",
            "released_time": released,
            "preorder": bool(preorder),
            "labels": app.get("title_labels") or [],
            "hints": app.get("hints") or [],
            "expected_launch": app.get("expected_launch_time") or "",
            "itunes_id": app.get("itunes_id") or "",
            "package": app.get("identifier") or "",
            "miniapp": bool(app.get("miniapp")),
            "score": ((st.get("rating") or {}).get("score")) or "",
            "icon": url_of(app.get("icon")),
            "dev": _info(app, "厂商"),
            "kind": kind_of(app),
            "lists": [],
            "url": app.get("source_url") or f"{BASE_URL}/app/{app['id']}",
        },
    }


# ---------------- 玩家评价（给 AI 读） ----------------
_XUA = ("V=1&PN=WebApp&LANG=zh_CN&VN_CODE=102&LOC=CN&PLT=PC&DS=Android"
        "&UID=15c5ceed-8775-4826-a70f-204a04505034&DT=PC&OS=Windows&OSV=10")


def reviews_text(app_id, n=50):
    """TapTap 网页版评价列表（不用登录）。返回 (评价列表, 总评价数)；拿不到抛出 RuntimeError。

    每条：{"score": 1-5 星, "hours": 游戏时长, "text": 内容}。先按热门取，不够再按最新补。
    """
    out, seen, total = [], set(), None
    for sort in ("hot", "new"):
        start = 0
        while len(out) < n and start < 60:
            q = urllib.parse.urlencode({"app_id": app_id, "limit": 10, "from": start, "sort": sort, "X-UA": _XUA})
            text = _urllib_get(f"{BASE_URL}/webapiv2/review/v2/list-by-app?{q}", headers=TAG_HEADERS, timeout=15)
            try:
                d = json.loads(text or "")
            except ValueError:
                raise RuntimeError("TapTap 评价接口返回的不是数据" + ("（被防爬验证拦下）" if is_waf(text or "") else ""))
            lst = ((d.get("data") or {}).get("list")) or []
            if not lst:
                break
            for it in lst:
                mo = it.get("moment") or {}
                rv = mo.get("review") or {}
                if total is None:
                    total = ((((mo.get("app") or {}).get("stat")) or {}).get("review_count"))
                t = re.sub(r"<[^>]+>", " ", ((rv.get("contents") or {}).get("text")) or "")
                t = re.sub(r"\s+", " ", unescape(t)).strip()
                rid = rv.get("id")
                if len(t) < 8 or rid in seen:
                    continue
                seen.add(rid)
                out.append({"score": rv.get("score"), "text": t[:300],
                            "hours": round((rv.get("played_spent") or 0) / 3600, 1)})
            start += 10
            time.sleep(0.6)
        if len(out) >= n:
            break
    return out[:n], total
