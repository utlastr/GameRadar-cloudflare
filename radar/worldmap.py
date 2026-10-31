"""地图（v54）：每天抓各国 App Store 游戏榜，算各地玩家口味有多像，分出玩家圈。

做法（README「地图」一节有说明）：
1. 每个国家 / 地区抓畅销榜、免费榜各前 100 名（苹果旧版 iTunes RSS，genre=6014 只要游戏）
2. 名次换算成份额：第 r 名权重 r^-0.75，每个国家内归一化。只看"钱和下载花在哪"，和市场大小无关
3. 全球爆款不算：每款游戏、每个发行商乘 log(国家数 / 上榜国家数)，到处都上榜的接近 0（类似 TF-IDF / 区位商）
4. 相似度 = 0.5 × 游戏偏爱的余弦 + 0.25 × 发行商偏爱的余弦 + 0.25 × 类型偏爱（减去全球平均后）的余弦
   发行商这一项是为了同一个游戏在各国用不同应用编号的情况（Garena 在泰国、越南、台湾各发一个《传说对决》）
5. 两条客观的线（README「地图」有说明）：
   噪声地板 = 同一个地方自己和自己比的相似度（当天榜单重抽两份比 / 今天和前一天比，取低的）；两地相似度达到地板 = 盲测分不出来
   背景水平 = 所有两两相似度的中位数 + 3 个稳健标准差；平均联结聚类，组间平均相似度低于它就停，圈数由数据决定
   圈名 = 最核心的两个成员（"台湾·香港圈"），按成员重合度沿用前一次的编号和名字；谁都不够像的标"（独立）"
不加"地理上必须相连"的限制：隔得很远但口味像的地方也会互相点亮。
"""
import math
import re
import threading
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import config, db
from .net import get_json, get_json_strict

URL = "https://itunes.apple.com/{cc}/rss/{feed}/limit={n}/genre=6014/json"
LOOKUP = "https://itunes.apple.com/lookup"
INCOME_URL = "https://api.worldbank.org/v2/country/all/indicator/NY.GNP.PCAP.CD"
# v100：地图改看人均居民最终消费支出 = 住户最终消费支出总额（现价美元）÷ 总人口
SPEND_URL = "https://api.worldbank.org/v2/country/all/indicator/NE.CON.PRVT.CD"
POP_URL = "https://api.worldbank.org/v2/country/all/indicator/SP.POP.TOTL"
# 世界银行没有台湾：2024 年民间消费约 11 兆新台币 ≈ 3,500 亿美元，÷ 2,340 万人
SPEND_EXTRA = {"tw": [14800, "2024"]}
FEEDS = {"g": "topgrossingapplications", "f": "topfreeapplications"}
FEED_NAME = {"g": "畅销榜", "f": "免费榜"}
FEED_W = {"g": 0.6, "f": 0.4}
LIMIT = 100
SLEEP = 1.5
RANK_POW = 0.75
W_GAME, W_DEV, W_GENRE = 0.5, 0.25, 0.25
KEEP_DAYS = 120
RESULT_V = 6
BOOT = 12
BG_SIGMA = 3.0

RUN_NOW = threading.Event()

SCHEMA = """
CREATE TABLE IF NOT EXISTS world_charts (day TEXT, cc TEXT, feed TEXT, rank INTEGER, appid TEXT,
  PRIMARY KEY (day, cc, feed, rank));
CREATE TABLE IF NOT EXISTS world_apps (appid TEXT PRIMARY KEY, name TEXT, artist TEXT, icon TEXT,
  genres TEXT, looked INTEGER DEFAULT 0, first_cc TEXT);
"""

# 代码: (名字, 地理区)。地理区按联合国的分区。苹果没有这个国家的商店时抓取会失败，自动跳过
COUNTRIES = {
    "cn": ("中国大陆", "东亚"), "hk": ("香港", "东亚"), "mo": ("澳门", "东亚"), "tw": ("台湾", "东亚"),
    "jp": ("日本", "东亚"), "kr": ("韩国", "东亚"), "mn": ("蒙古", "东亚"),
    "sg": ("新加坡", "东南亚"), "my": ("马来西亚", "东南亚"), "th": ("泰国", "东南亚"), "vn": ("越南", "东南亚"),
    "id": ("印度尼西亚", "东南亚"), "ph": ("菲律宾", "东南亚"), "kh": ("柬埔寨", "东南亚"), "la": ("老挝", "东南亚"),
    "mm": ("缅甸", "东南亚"), "bn": ("文莱", "东南亚"),
    "in": ("印度", "南亚"), "pk": ("巴基斯坦", "南亚"), "bd": ("孟加拉国", "南亚"), "lk": ("斯里兰卡", "南亚"),
    "np": ("尼泊尔", "南亚"), "mv": ("马尔代夫", "南亚"),
    "kz": ("哈萨克斯坦", "中亚"), "uz": ("乌兹别克斯坦", "中亚"), "kg": ("吉尔吉斯斯坦", "中亚"),
    "tj": ("塔吉克斯坦", "中亚"), "tm": ("土库曼斯坦", "中亚"),
    "ae": ("阿联酋", "西亚"), "sa": ("沙特阿拉伯", "西亚"), "qa": ("卡塔尔", "西亚"), "kw": ("科威特", "西亚"),
    "bh": ("巴林", "西亚"), "om": ("阿曼", "西亚"), "jo": ("约旦", "西亚"), "lb": ("黎巴嫩", "西亚"),
    "il": ("以色列", "西亚"), "tr": ("土耳其", "西亚"), "iq": ("伊拉克", "西亚"), "ye": ("也门", "西亚"),
    "am": ("亚美尼亚", "西亚"), "az": ("阿塞拜疆", "西亚"), "ge": ("格鲁吉亚", "西亚"), "cy": ("塞浦路斯", "西亚"),
    "gb": ("英国", "北欧"), "ie": ("爱尔兰", "北欧"), "se": ("瑞典", "北欧"), "no": ("挪威", "北欧"),
    "dk": ("丹麦", "北欧"), "fi": ("芬兰", "北欧"), "is": ("冰岛", "北欧"), "ee": ("爱沙尼亚", "北欧"),
    "lv": ("拉脱维亚", "北欧"), "lt": ("立陶宛", "北欧"),
    "fr": ("法国", "西欧"), "de": ("德国", "西欧"), "at": ("奥地利", "西欧"), "ch": ("瑞士", "西欧"),
    "be": ("比利时", "西欧"), "nl": ("荷兰", "西欧"), "lu": ("卢森堡", "西欧"),
    "it": ("意大利", "南欧"), "es": ("西班牙", "南欧"), "pt": ("葡萄牙", "南欧"), "mt": ("马耳他", "南欧"),
    "gr": ("希腊", "南欧"), "hr": ("克罗地亚", "南欧"), "si": ("斯洛文尼亚", "南欧"), "rs": ("塞尔维亚", "南欧"),
    "ba": ("波黑", "南欧"), "me": ("黑山", "南欧"), "mk": ("北马其顿", "南欧"), "al": ("阿尔巴尼亚", "南欧"),
    "xk": ("科索沃", "南欧"),
    "pl": ("波兰", "东欧"), "cz": ("捷克", "东欧"), "sk": ("斯洛伐克", "东欧"), "hu": ("匈牙利", "东欧"),
    "ro": ("罗马尼亚", "东欧"), "bg": ("保加利亚", "东欧"), "ua": ("乌克兰", "东欧"), "by": ("白俄罗斯", "东欧"),
    "md": ("摩尔多瓦", "东欧"), "ru": ("俄罗斯", "东欧"),
    "eg": ("埃及", "北非"), "ma": ("摩洛哥", "北非"), "tn": ("突尼斯", "北非"), "dz": ("阿尔及利亚", "北非"),
    "ly": ("利比亚", "北非"),
    "ng": ("尼日利亚", "西非"), "gh": ("加纳", "西非"), "sn": ("塞内加尔", "西非"), "ci": ("科特迪瓦", "西非"),
    "ml": ("马里", "西非"), "ne": ("尼日尔", "西非"), "bf": ("布基纳法索", "西非"), "bj": ("贝宁", "西非"),
    "lr": ("利比里亚", "西非"), "gm": ("冈比亚", "西非"), "gw": ("几内亚比绍", "西非"), "mr": ("毛里塔尼亚", "西非"),
    "ke": ("肯尼亚", "东非"), "tz": ("坦桑尼亚", "东非"), "ug": ("乌干达", "东非"), "rw": ("卢旺达", "东非"),
    "zm": ("赞比亚", "东非"), "zw": ("津巴布韦", "东非"), "mz": ("莫桑比克", "东非"), "mw": ("马拉维", "东非"),
    "mg": ("马达加斯加", "东非"), "mu": ("毛里求斯", "东非"),
    "cm": ("喀麦隆", "中非"), "cg": ("刚果（布）", "中非"), "cd": ("刚果（金）", "中非"), "ga": ("加蓬", "中非"),
    "td": ("乍得", "中非"), "ao": ("安哥拉", "中非"),
    "za": ("南非", "南部非洲"), "na": ("纳米比亚", "南部非洲"), "bw": ("博茨瓦纳", "南部非洲"), "sz": ("斯威士兰", "南部非洲"),
    "us": ("美国", "北美"), "ca": ("加拿大", "北美"),
    "mx": ("墨西哥", "中美洲"), "gt": ("危地马拉", "中美洲"), "hn": ("洪都拉斯", "中美洲"), "sv": ("萨尔瓦多", "中美洲"),
    "ni": ("尼加拉瓜", "中美洲"), "cr": ("哥斯达黎加", "中美洲"), "pa": ("巴拿马", "中美洲"), "bz": ("伯利兹", "中美洲"),
    "do": ("多米尼加", "加勒比"), "jm": ("牙买加", "加勒比"), "tt": ("特立尼达和多巴哥", "加勒比"),
    "bs": ("巴哈马", "加勒比"), "bb": ("巴巴多斯", "加勒比"),
    "br": ("巴西", "南美"), "ar": ("阿根廷", "南美"), "cl": ("智利", "南美"), "co": ("哥伦比亚", "南美"),
    "pe": ("秘鲁", "南美"), "ec": ("厄瓜多尔", "南美"), "bo": ("玻利维亚", "南美"), "py": ("巴拉圭", "南美"),
    "uy": ("乌拉圭", "南美"), "ve": ("委内瑞拉", "南美"), "gy": ("圭亚那", "南美"), "sr": ("苏里南", "南美"),
    "au": ("澳大利亚", "大洋洲"), "nz": ("新西兰", "大洋洲"), "fj": ("斐济", "大洋洲"), "pg": ("巴布亚新几内亚", "大洋洲"),
    "sb": ("所罗门群岛", "大洋洲"), "vu": ("瓦努阿图", "大洋洲"),
}

GENRE_ZH = {
    "Action": "动作", "Adventure": "冒险", "Arcade": "街机", "Board": "桌游", "Card": "卡牌", "Casino": "博彩",
    "Casual": "休闲", "Dice": "骰子", "Educational": "教育", "Family": "家庭", "Music": "音乐", "Puzzle": "益智",
    "Racing": "赛车", "Role Playing": "角色扮演", "Simulation": "模拟", "Sports": "体育", "Strategy": "策略",
    "Trivia": "问答", "Word": "文字",
}
GENRE_ZH_EN = {v: k for k, v in GENRE_ZH.items()}
GENRE_ZH_EN["其他"] = "Other"

_DEV_SKIP = {"the", "beijing", "shanghai", "shenzhen", "guangzhou", "hangzhou", "chengdu", "xiamen", "nanjing",
             "suzhou", "wuhan", "tianjin", "fuzhou", "hong", "kong", "game", "games", "studio", "studios", "inc",
             "ltd", "co", "limited", "mobile", "digital", "interactive", "entertainment", "technology",
             "technologies", "network", "online", "pte", "llc", "corp", "corporation", "group", "holdings",
             "international", "global", "software", "media", "company", "joint", "stock", "sg", "hk", "jsc"}

_status = {}


def _say(msg):
    print(f"{time.strftime('%H:%M:%S')}  地图：{msg}")


def _set_status(**kw):
    _status.update(kw, ts=int(time.time()))
    try:
        db.kv_set("world_status", _status)
    except Exception:
        pass


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _label(x):
    return (x or {}).get("label") if isinstance(x, dict) else None


def dev_key(artist):
    for t in re.split(r"[^0-9a-z\u4e00-\u9fff]+", (artist or "").lower()):
        if len(t) >= 2 and t not in _DEV_SKIP:
            return t
    return (artist or "").strip().lower() or None


def _fetch(cc, key):
    d = get_json_strict(URL.format(cc=cc, feed=FEEDS[key], n=LIMIT), retries=0, timeout=30)
    entries = (d.get("feed") or {}).get("entry") or []
    if isinstance(entries, dict):
        entries = [entries]
    out = []
    for en in entries:
        aid = ((en.get("id") or {}).get("attributes") or {}).get("im:id")
        name = _label(en.get("im:name"))
        if not aid or not name:
            continue
        imgs = en.get("im:image") or []
        out.append((str(aid), name, _label(en.get("im:artist")) or "", _label(imgs[-1]) if imgs else ""))
    return out


def scan():
    """今天（世界标准时间）还没抓的国家 / 榜单抓一遍。返回抓到的榜单数。"""
    day = _today()
    with db.session() as c:
        have = {(r[0], r[1]) for r in c.execute("SELECT DISTINCT cc, feed FROM world_charts WHERE day = ?", (day,))}
    fails = _status.get("fails") if _status.get("day") == day else {}
    fails = dict(fails or {})
    todo = [(cc, k) for cc in COUNTRIES for k in FEEDS if (cc, k) not in have and fails.get(f"{cc}:{k}", 0) < 2]
    if not todo:
        return 0
    _say(f"开始抓各国 App Store 游戏榜，还剩 {len(todo)} 个榜单")
    got, blocked_streak = 0, 0
    errors = dict(_status.get("errors") or {}) if _status.get("day") == day else {}
    for i, (cc, k) in enumerate(todo):
        _set_status(phase="scan", day=day, done=i, total=len(todo), got=got, fails=fails, errors=errors)
        try:
            rows = _fetch(cc, k)
            if not rows:
                raise RuntimeError("Empty chart (no App Store in this country?)")
            with db.session() as c:
                c.executemany("INSERT OR REPLACE INTO world_charts(day, cc, feed, rank, appid) VALUES (?, ?, ?, ?, ?)",
                              [(day, cc, k, r, a[0]) for r, a in enumerate(rows, 1)])
                for aid, name, artist, icon in rows:
                    c.execute("""INSERT INTO world_apps(appid, name, artist, icon, first_cc) VALUES (?, ?, ?, ?, ?)
                                 ON CONFLICT(appid) DO UPDATE SET icon = COALESCE(NULLIF(excluded.icon, ''), world_apps.icon)""",
                              (aid, name, artist, icon, cc))
            got += 1
            blocked_streak = 0
            errors.pop(f"{cc}:{k}", None)
        except Exception as e:
            fails[f"{cc}:{k}"] = fails.get(f"{cc}:{k}", 0) + 1
            errors[f"{cc}:{k}"] = str(e)[:160]
            if "403" in str(e) or "Forbidden" in str(e):
                blocked_streak += 1
                if blocked_streak >= 4:
                    _set_status(phase="blocked", day=day, done=i + 1, total=len(todo), got=got, fails=fails, errors=errors,
                                blocked="苹果拒绝访问（403）。可能是代理或网络被苹果拦了，一小时后再试")
                    _say("苹果连续拒绝访问，这一轮先停")
                    return got
        time.sleep(SLEEP)
    _set_status(phase="idle", day=day, done=len(todo), total=len(todo), got=got, fails=fails, errors=errors,
                blocked=None, last_scan=int(time.time()))
    _say(f"各国榜单抓完：成功 {got} 个")
    return got


def lookup_genres():
    """给新出现的游戏查类型（苹果 lookup 接口，一次 150 个）。返回新查到的个数。"""
    with db.session() as c:
        first = [r[0] for r in c.execute("SELECT appid FROM world_apps WHERE looked = 0")]
        second = [(r[0], r[1]) for r in c.execute(
            "SELECT appid, first_cc FROM world_apps WHERE looked = 1 AND (genres IS NULL OR en_name IS NULL)")]
    found = 0

    def run(ids, country, level):
        nonlocal found
        d = get_json(LOOKUP, {"id": ",".join(ids), "country": country, "lang": "en_us"}, retries=1, timeout=40) or {}
        hit = {}
        for r in d.get("results") or []:
            gs = [g for g in (r.get("genres") or []) if g in GENRE_ZH]
            hit[str(r.get("trackId"))] = (",".join(dict.fromkeys(gs)) or "Other", r.get("trackName") or None, r.get("artistName") or None)
        with db.session() as c:
            for a in ids:
                if a in hit:
                    g, nm, ar = hit[a]
                    c.execute("UPDATE world_apps SET genres = ?, en_name = COALESCE(?, en_name), en_artist = COALESCE(?, en_artist), looked = ? WHERE appid = ?",
                              (g, nm, ar, level, a))
                    found += 1
                else:
                    c.execute("UPDATE world_apps SET looked = ? WHERE appid = ?", (level, a))
        time.sleep(SLEEP)

    for i in range(0, len(first), 150):
        _set_status(phase="genre")
        try:
            run(first[i:i + 150], "us", 1)
        except Exception as e:
            _say(f"查游戏类型失败：{e}")
            return found
    by_cc = defaultdict(list)
    for a, cc in second:
        by_cc[cc or "us"].append(a)
    for cc, ids in by_cc.items():
        for i in range(0, len(ids), 150):
            try:
                run(ids[i:i + 150], cc, 2)
            except Exception as e:
                _say(f"查游戏类型失败：{e}")
                return found
    if found:
        _say(f"查到 {found} 个游戏的类型")
    return found


def income(force=False):
    """人均国民收入（世界银行，美元，现价）。30 天更新一次。返回 {代码: 数值}。"""
    k = db.kv_get("world_income")
    if k and not force and time.time() - k.get("ts", 0) < 30 * 86400:
        return k.get("data") or {}
    try:
        d = get_json(INCOME_URL, {"format": "json", "per_page": 20000, "mrnev": 1}, retries=1, timeout=40)
        data = {}
        for r in (d[1] if isinstance(d, list) and len(d) > 1 else []) or []:
            cid = ((r.get("country") or {}).get("id") or "").lower()
            if len(cid) == 2 and r.get("value"):
                data[cid] = [round(r["value"]), r.get("date")]
        if data:
            db.kv_set("world_income", {"ts": int(time.time()), "data": data})
            _say(f"人均收入已更新（{len(data)} 个国家和地区）")
            return data
    except Exception as e:
        _say(f"人均收入下载失败：{e}")
    return (k or {}).get("data") or {}


def _wb_latest(url):
    """世界银行某个指标每个国家最近一年有值的数：{代码: [数值, 年份]}。"""
    d = get_json(url, {"format": "json", "per_page": 20000, "mrnev": 1}, retries=1, timeout=40)
    out = {}
    for r in (d[1] if isinstance(d, list) and len(d) > 1 else []) or []:
        cid = ((r.get("country") or {}).get("id") or "").lower()
        if len(cid) == 2 and r.get("value"):
            out[cid] = [r["value"], r.get("date")]
    return out


def spend(force=False):
    """人均居民最终消费支出（v100）：世界银行"住户和为住户服务的非营利机构最终消费支出（现价美元）"÷ 总人口。
    按市场汇率，不用购买力平价——要看的是当地人一年实际花出去多少美元。30 天更新一次。返回 {代码: [数值, 年份]}。"""
    k = db.kv_get("world_spend")
    if k and not force and time.time() - k.get("ts", 0) < 30 * 86400:
        return k.get("data") or {}
    try:
        tot = _wb_latest(SPEND_URL)
        pop = _wb_latest(POP_URL)
        data = {}
        for cid, tv in tot.items():
            p = (pop.get(cid) or [0])[0]
            if p and p > 0:
                data[cid] = [round(tv[0] / p), tv[1]]
        if data:
            for cid, v in SPEND_EXTRA.items():
                data.setdefault(cid, v)
            db.kv_set("world_spend", {"ts": int(time.time()), "data": data})
            _say(f"人均消费支出已更新（{len(data)} 个国家和地区）")
            return data
    except Exception as e:
        _say(f"人均消费支出下载失败：{e}")
    return (k or {}).get("data") or {}


def _cos(a, b):
    if len(a) > len(b):
        a, b = b, a
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def _idf_weight(vecs):
    n = len(vecs)
    df = Counter(k for v in vecs.values() for k in v)
    idf = {k: math.log((n + 1) / (d + 0.5)) for k, d in df.items()}
    return {cc: {k: s * idf[k] for k, s in v.items()} for cc, v in vecs.items()}, df, idf


def _clusters(sim, threshold):
    """完全联结：两组合并后任意两地都还高于门槛才合并（不会像平均联结那样串成超大圈）。圈数由数据决定。"""
    groups = [[i] for i in range(len(sim))]
    while len(groups) > 1:
        best, bi, bj = -1.0, 0, 1
        for i in range(len(groups)):
            gi = groups[i]
            for j in range(i + 1, len(groups)):
                gj = groups[j]
                s = min(sim[a][b] for a in gi for b in gj)
                if s > best:
                    best, bi, bj = s, i, j
        if best < threshold:
            break
        groups[bi] = groups[bi] + groups[bj]
        groups.pop(bj)
    return groups


def _share_of(lists):
    """{榜: [(名次, 编号)]} → 每款游戏的份额（名次 r 的权重 r^-0.75，畅销 0.6 + 免费 0.4，合计 1）。"""
    d = {}
    for feed, lst in lists.items():
        tot = sum(r ** -RANK_POW for r, _ in lst) or 1
        for r, aid in lst:
            d[aid] = d.get(aid, 0.0) + r ** -RANK_POW / tot * FEED_W[feed]
    t = sum(d.values()) or 1
    return {a: v / t for a, v in d.items()}


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2) if n else 0.0


def compute():
    rv = db.kv_get("regions_ver") or 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    with db.session() as c:
        rows = c.execute("SELECT day, cc, feed, rank, appid FROM world_charts WHERE day >= ?", (cutoff,)).fetchall()
        apps = {r[0]: {"name": r[5] or r[1], "artist": r[6] or r[2], "icon": r[3],
                       "genres": [GENRE_ZH_EN.get(g, g) for g in (r[4] or "").split(",")] if r[4] else []}
                for r in c.execute("SELECT appid, name, artist, icon, genres, en_name, en_artist FROM world_apps")}
        c.execute("DELETE FROM world_charts WHERE day < ?",
                  ((datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d"),))
    latest = {}
    for day, cc, feed, rank, aid in rows:
        if day > latest.get((cc, feed), ""):
            latest[(cc, feed)] = day
    charts = defaultdict(list)
    for day, cc, feed, rank, aid in rows:
        if latest[(cc, feed)] == day and cc in COUNTRIES:
            charts[(cc, feed)].append((rank, aid))
    order = sorted({cc for cc, _ in charts})
    if len(order) < 3:
        return None
    share, ranks, days = {}, defaultdict(dict), {}
    for (cc, feed), lst in charts.items():
        ws = [(aid, r ** -RANK_POW) for r, aid in lst]
        tot = sum(w for _, w in ws) or 1
        d = share.setdefault(cc, {})
        for (r, aid), (_, w) in zip(lst, ws):
            d[aid] = d.get(aid, 0.0) + w / tot * FEED_W[feed]
            ranks[cc][f"{feed}:{aid}"] = r
        days[cc] = max(days.get(cc, ""), latest[(cc, feed)])
    for cc, d in share.items():
        t = sum(d.values()) or 1
        for a in d:
            d[a] /= t
    n = len(order)
    game_pref, game_df, g_idf = _idf_weight(share)

    def dev_of(d):
        v = {}
        for a, s in d.items():
            key = dev_key((apps.get(a) or {}).get("artist"))
            if key:
                v[key] = v.get(key, 0.0) + s
        return v

    def genre_of(d):
        v = defaultdict(float)
        for a, s in d.items():
            gs = [g for g in (apps.get(a) or {}).get("genres", []) if g and g not in ("其他", "Other")]
            for g in gs:
                v[g] += s / len(gs)
        t = sum(v.values()) or 1
        return {g: x / t for g, x in v.items()}

    devs = {cc: dev_of(d) for cc, d in share.items()}
    dev_name = {}
    for cc, d in share.items():
        for a, s in d.items():
            key = dev_key((apps.get(a) or {}).get("artist"))
            if key:
                dev_name.setdefault(key, Counter())[(apps.get(a) or {}).get("artist")] += s
    dev_pref, _, d_idf = _idf_weight(devs)
    gshare = {cc: genre_of(d) for cc, d in share.items()}
    all_g = sorted({g for v in gshare.values() for g in v})
    gmean = {g: sum(gshare[cc].get(g, 0.0) for cc in order) / n for g in all_g}
    gdev = {cc: {g: gshare[cc].get(g, 0.0) - gmean[g] for g in all_g} for cc in order}

    def profile(d):
        dv = dev_of(d)
        gs = genre_of(d)
        return ({a: s * g_idf.get(a, 0.0) for a, s in d.items()}, {k_: s * d_idf.get(k_, 0.0) for k_, s in dv.items()},
                {g: gs.get(g, 0.0) - gmean[g] for g in all_g})

    def psim(p, q):
        return (W_GAME * _cos(p[0], q[0]) + W_DEV * _cos(p[1], q[1]) + W_GENRE * max(0.0, _cos(p[2], q[2])))

    # 噪声地板：同一个地方自己和自己比能有多像。两种算法取低的：
    # 1) 把当天榜单随机重抽（有放回）两份，比两份之间的相似度，重复 BOOT 次取平均
    # 2) 有前一天的榜单时，比今天和前一天
    import random
    rnd = random.Random(42)
    prev_lists = defaultdict(dict)
    for day, cc, feed, rank, aid in rows:
        if cc in COUNTRIES and day < latest[(cc, feed)]:
            prev_lists[(cc, feed)].setdefault(day, []).append((rank, aid))
    noise = {}
    for cc in order:
        lists = {f: charts[(cc, f)] for f in FEEDS if charts.get((cc, f))}
        vals = []
        for _ in range(BOOT):
            a = {f: [rnd.choice(l) for _ in l] for f, l in lists.items()}
            b = {f: [rnd.choice(l) for _ in l] for f, l in lists.items()}
            vals.append(psim(profile(_share_of(a)), profile(_share_of(b))))
        boot = sum(vals) / len(vals)
        day_s = None
        pl = {}
        for f in FEEDS:
            ds = prev_lists.get((cc, f))
            if ds:
                pl[f] = ds[max(ds)]
        if pl:
            day_s = psim(profile(share[cc]), profile(_share_of(pl)))
        noise[cc] = {"boot": round(boot, 3), "day": round(day_s, 3) if day_s is not None else None,
                     "floor": round(min(boot, day_s) if day_s is not None else boot, 3)}
    mean_share = defaultdict(float)
    for d in share.values():
        for a, s in d.items():
            mean_share[a] += s / n
    dmean = defaultdict(float)
    for d in devs.values():
        for k_, s in d.items():
            dmean[k_] += s / n

    sim = [[0.0] * n for _ in range(n)]
    for i in range(n):
        sim[i][i] = 1.0
        a = order[i]
        for j in range(i + 1, n):
            b = order[j]
            s = (W_GAME * _cos(game_pref[a], game_pref[b]) + W_DEV * _cos(dev_pref[a], dev_pref[b])
                 + W_GENRE * max(0.0, _cos(gdev[a], gdev[b])))
            sim[i][j] = sim[j][i] = round(s, 3)

    # 背景水平：随便两个地方通常有多像（所有两两相似度的中位数 + 3 个稳健标准差）。
    # 一群地方之间平均相似度高过这条线，才算一个圈；圈的门槛不能高过噪声地板（否则自己和自己都不算一伙）
    pairs = [sim[i][j] for i in range(n) for j in range(i + 1, n)]
    med = _median(pairs)
    mad = _median([abs(x - med) for x in pairs]) * 1.4826
    floor_med = _median([noise[cc]["floor"] for cc in order])
    threshold = round(min(med + BG_SIGMA * mad, floor_med * 0.9), 3)
    groups = _clusters(sim, threshold)

    old = db.kv_get("world_result") or {}
    old_cl = [c_ for c_ in (old.get("clusters") or []) if c_.get("members")] if old.get("v") == RESULT_V else []
    next_no = max([int(c_["id"][1:]) for c_ in old_cl if str(c_.get("id", ""))[1:].isdigit()] + [0]) + 1
    used = set()
    clusters = []
    for g in sorted(groups, key=lambda g: -len(g)):
        members = [order[i] for i in g]
        center = max(g, key=lambda i: sum(sim[i][j] for j in g))
        members.sort(key=lambda m: -sim[center][order.index(m)])
        cg = {gname: sum(gshare[m].get(gname, 0.0) for m in members) / len(members) for gname in all_g}
        traits = sorted(((x / gmean[gname], gname) for gname, x in cg.items() if gmean[gname] > 0 and x >= 0.02), reverse=True)
        traits = [[gname, round(lq, 1)] for lq, gname in traits[:4] if lq >= 1.1]
        if len(members) == 1:
            name = f"{COUNTRIES[members[0]][0]}（独立）"
        else:
            name = "·".join(COUNTRIES[m][0] for m in members[:2]) + "圈"
        mset, best, bj = set(members), None, 0.0
        for oc in old_cl:
            if oc["id"] in used:
                continue
            os_ = set(oc["members"])
            jac = len(mset & os_) / len(mset | os_)
            if jac > bj:
                best, bj = oc, jac
        core = members[:2]
        if best and bj >= 0.5 and (len(members) > 1) == (len(best["members"]) > 1):
            cid, name = best["id"], best["name"]
            if all(x in mset for x in best.get("core") or []):
                core = best.get("core") or core
            used.add(cid)
        else:
            cid = f"G{next_no}"
            next_no += 1
        clusters.append({"id": cid, "name": name, "core": core, "members": members, "center": order[center],
                         "traits": traits, "solo": len(members) == 1,
                         "cohesion": round(sum(sim[i][j] for i in g for j in g if i != j) / max(1, len(g) * (len(g) - 1)), 3)})
    cl_of = {}
    for cl in clusters:
        for m in cl["members"]:
            cl_of[m] = cl["id"]
        agg = defaultdict(float)
        for m in cl["members"]:
            for a, v in game_pref[m].items():
                agg[a] += v / len(cl["members"])
        cl["games"] = []
        for a, v in sorted(agg.items(), key=lambda x: -x[1])[:5]:
            ap = apps.get(a) or {}
            cl["games"].append({"id": a, "name": ap.get("name") or a, "icon": ap.get("icon") or "",
                                "in": sum(1 for m in cl["members"] if a in share[m])})
    pos = {cc: i for i, cc in enumerate(order)}
    aff = {}
    for cc in order:
        i = pos[cc]
        row = []
        for cl in clusters:
            ms = [pos[m] for m in cl["members"] if m != cc]
            if ms:
                row.append([cl["id"], round(sum(sim[i][j] for j in ms) / len(ms), 3)])
        aff[cc] = sorted(row, key=lambda x: -x[1])[:3]

    countries = {}
    for i, cc in enumerate(order):
        games = sorted(share[cc].items(), key=lambda x: -game_pref[cc][x[0]])[:10]
        top_games = []
        for a, s in games:
            ap = apps.get(a) or {}
            top_games.append({"id": a, "name": ap.get("name") or a, "artist": ap.get("artist") or "", "icon": ap.get("icon") or "",
                              "genres": ap.get("genres") or [], "lq": round(s / mean_share[a], 1) if mean_share[a] else None,
                              "spread": game_df[a], "rg": ranks[cc].get(f"g:{a}"), "rf": ranks[cc].get(f"f:{a}")})
        gen = sorted(((x / gmean[g], g, x) for g, x in gshare[cc].items() if gmean.get(g) and x >= 0.03), reverse=True)[:5]
        dv = sorted(devs[cc].items(), key=lambda x: -dev_pref[cc][x[0]])[:6]
        sims = sorted(((sim[i][j], order[j]) for j in range(n) if j != i), reverse=True)[:12]
        countries[cc] = {
            "name": COUNTRIES[cc][0], "sub": COUNTRIES[cc][1], "day": days[cc], "cluster": cl_of.get(cc),
            "n": {f: len(charts.get((cc, f), [])) for f in FEEDS},
            "games": top_games,
            "genres": [{"g": g, "lq": round(lq, 2), "share": round(x, 3)} for lq, g, x in gen],
            "devs": [{"name": dev_name[k_].most_common(1)[0][0], "lq": round(s / dmean[k_], 1) if dmean[k_] else None} for k_, s in dv],
            "similar": [[c2, s] for s, c2 in sims],
            "aff": aff[cc],
            "noise": noise[cc],
        }
    res = {"v": RESULT_V, "threshold": threshold, "bg": {"median": round(med, 3), "sd": round(mad, 3)},
           "updated": int(time.time()), "order": order, "sim": sim, "clusters": clusters, "countries": countries,
           "n_games": len(mean_share), "rv": rv}
    try:
        res.update(_place_layer(order, share, profile, genre_of, gmean, all_g, apps, floor_med, old))
    except Exception:
        traceback.print_exc()
    return res


def _fcos(a, b):
    (da, na), (db_, nb) = a, b
    if not na or not nb:
        return 0.0
    if len(da) > len(db_):
        da, db_ = db_, da
    g = db_.get
    return sum(v * g(k, 0.0) for k, v in da.items()) / (na * nb)


def _prep(v):
    return v, math.sqrt(sum(x * x for x in v.values()))


def _complete(S, thr):
    """完全联结（和 _clusters 一样），合并后按"取两者较小"更新相似度，几百上千个地方也算得动。"""
    n = len(S)
    M = [row[:] for row in S]
    alive = set(range(n))
    members = {i: [i] for i in range(n)}
    best = [(-2.0, -1)] * n

    def rb(i):
        row, b, bj = M[i], -2.0, -1
        for j in alive:
            if j != i and row[j] > b:
                b, bj = row[j], j
        best[i] = (b, bj)

    for i in range(n):
        rb(i)
    while len(alive) > 1:
        i = max(alive, key=lambda k: best[k][0])
        s, j = best[i]
        if j < 0 or s < thr:
            break
        Mi, Mj = M[i], M[j]
        alive.discard(j)
        for k in alive:
            if k != i:
                v = Mi[k] if Mi[k] < Mj[k] else Mj[k]
                Mi[k] = v
                M[k][i] = v
        members[i] += members.pop(j)
        rb(i)
        for k in alive:
            if k != i and best[k][1] in (i, j):
                rb(k)
    return list(members.values())


def _place_layer(order, share, profile, genre_of, gmean, all_g, apps, floor_med, old):
    """目标市场拆成省 / 州（regions.split），和其他国家一起分一层玩家圈。
    跨国的两地用全球相似度（门槛 = 背景水平）；同一国家的两个省用"偏离全国的方向"（游戏 + 平台 + 娱乐的对数倍数余弦，
    门槛 = 2/√维数，即比随机方向高两个标准差）。两种都换算成"超出门槛多少"，完全联结，圈里任意两地都要过各自的门槛。"""
    from . import regions
    reg = regions.split(set(order))
    if not reg:
        return {}
    pshare, pmeta = {}, {}
    for cc in order:
        rd = reg.get(cc)
        if rd:
            for code, pl in rd["places"].items():
                d = {a: s * pl["lift"].get(a, 1.0) for a, s in share[cc].items()}
                t = sum(d.values()) or 1
                key = f"{cc}:{code}"
                pshare[key] = {a: v / t for a, v in d.items()}
                pmeta[key] = (cc, pl)
        else:
            pshare[cc] = share[cc]
    keys = list(pshare)
    n = len(keys)
    profs = [[_prep(v) for v in profile(pshare[k])] for k in keys]
    S = [[0.0] * n for _ in range(n)]
    pairs = []
    for i in range(n):
        S[i][i] = 1.0
        p = profs[i]
        for j in range(i + 1, n):
            q = profs[j]
            s = round(W_GAME * _fcos(p[0], q[0]) + W_DEV * _fcos(p[1], q[1]) + W_GENRE * max(0.0, _fcos(p[2], q[2])), 3)
            S[i][j] = S[j][i] = s
            pairs.append(s)
    med = _median(pairs)
    mad = _median([abs(x - med) for x in pairs]) * 1.4826
    thr = round(min(med + BG_SIGMA * mad, floor_med * 0.9), 3)
    C = [[(S[i][j] - thr) / (1 - thr) if thr < 1 else 0.0 for j in range(n)] for i in range(n)]
    idx = {k: i for i, k in enumerate(keys)}
    wthr = {}
    for cc, rd in reg.items():
        codes = [c_ for c_ in rd["places"] if f"{cc}:{c_}" in idx]
        feats = sorted({("g", a) for c_ in codes for a in rd["places"][c_]["lift"]}
                       | {("p", a) for c_ in codes for a in rd["places"][c_]["plat"]}
                       | {("e", a) for c_ in codes for a in rd["places"][c_]["ent"]})
        if len(codes) < 2 or len(feats) < 5:
            continue
        src = {"g": "lift", "p": "plat", "e": "ent"}
        vec = [_prep({f: math.log(rd["places"][c_][src[f[0]]].get(f[1], 1.0)) for f in feats}) for c_ in codes]
        tw = 2 / math.sqrt(len(feats))
        wthr[cc] = round(tw, 3)
        for x in range(len(codes)):
            i = idx[f"{cc}:{codes[x]}"]
            for y in range(x + 1, len(codes)):
                j = idx[f"{cc}:{codes[y]}"]
                C[i][j] = C[j][i] = (_fcos(vec[x], vec[y]) - tw) / (1 - tw)
    for i in range(n):
        C[i][i] = 1.0
    groups = _complete(C, 0.0)

    old_cl = [c_ for c_ in (old.get("pclusters") or []) if c_.get("members")]
    next_no = max([int(c_["id"][1:]) for c_ in old_cl if str(c_.get("id", ""))[1:].isdigit()] + [0]) + 1
    used, pcl, cl_of = set(), [], {}
    gsh = {k: genre_of(pshare[k]) for k in keys}
    for g in sorted(groups, key=lambda g: -len(g)):
        center = max(g, key=lambda i: sum(C[i][j] for j in g))
        g = sorted(g, key=lambda i: -C[center][i])
        members = [keys[i] for i in g]
        cg = {gn: sum(gsh[m].get(gn, 0.0) for m in members) / len(members) for gn in all_g}
        traits = sorted(((x / gmean[gn], gn) for gn, x in cg.items() if gmean.get(gn) and x >= 0.02), reverse=True)
        traits = [[gn, round(lq, 1)] for lq, gn in traits[:4] if lq >= 1.1]
        mset, best, bj = set(members), None, 0.0
        for oc in old_cl:
            if oc["id"] in used:
                continue
            os_ = set(oc["members"])
            jac = len(mset & os_) / len(mset | os_)
            if jac > bj:
                best, bj = oc, jac
        core = members[:2]
        if best and bj >= 0.5:
            cid = best["id"]
            if all(x in mset for x in best.get("core") or []):
                core = best.get("core") or core
            used.add(cid)
        else:
            cid = f"P{next_no}"
            next_no += 1
        agg = defaultdict(float)
        for i in g:
            for a, v in profs[i][0][0].items():
                agg[a] += v / len(g)
        games = [{"id": a, "in": sum(1 for m in members if a in pshare[m])} for a, _ in sorted(agg.items(), key=lambda x: -x[1])[:5]]
        ent = defaultdict(list)
        for m in members:
            if m in pmeta:
                for e, x in pmeta[m][1]["ent"].items():
                    ent[e].append(x)
        ent = sorted(((math.exp(sum(math.log(x) for x in v) / len(v)), e) for e, v in ent.items()), reverse=True)
        pcl.append({"id": cid, "members": members, "core": core, "center": keys[center], "traits": traits, "games": games,
                    "solo": len(g) == 1, "pop": sum(pmeta[m][1]["pop"] for m in members if m in pmeta),
                    "ent": [[e, round(x, 2)] for x, e in ent[:3] if x >= 1.15],
                    "cohesion": round(sum(S[i][j] for i in g for j in g if i != j) / max(1, len(g) * (len(g) - 1)), 3)})
        for m in members:
            cl_of[m] = cid

    places, want = {}, set()
    for i, k in enumerate(keys):
        row = C[i]
        nb = sorted(((round(row[j], 3), keys[j]) for j in range(n) if j != i), reverse=True)[:30]
        p = {"cluster": cl_of.get(k), "near": [[kk, s] for s, kk in nb]}
        if k in pmeta:
            cc, pl = pmeta[k]
            likes = sorted(((x, a) for a, x in pl["lift"].items() if x >= 1.15), reverse=True)[:8]
            p.update(cc=cc, name=pl["name"], units=pl["units"], pop=pl["pop"],
                     likes=[[a, x] for x, a in likes], plat=pl["plat"], ent=pl["ent"])
            want.update(a for _, a in likes)
        else:
            p["cc"] = k
        places[k] = p
    for c_ in pcl:
        want.update(g["id"] for g in c_["games"])
    papps = {a: {"name": (apps.get(a) or {}).get("name") or a, "icon": (apps.get(a) or {}).get("icon") or ""} for a in want}
    return {"places": places, "pclusters": pcl, "pthreshold": thr, "pwthr": wthr, "papps": papps}


def compute_and_save():
    r = compute()
    if r:
        db.kv_set("world_result", r)
        _say(f"口味相似度已算好：{len(r['order'])} 个国家和地区，{len(r['clusters'])} 个玩家圈")
    return r


def payload():
    inc = (db.kv_get("world_income") or {}).get("data") or {}
    spd = (db.kv_get("world_spend") or {}).get("data") or {}
    st = db.kv_get("world_status") or {}
    st = {k: v for k, v in st.items() if k != "fails"}
    errs = st.get("errors") or {}
    st["errors"] = dict(list(errs.items())[:40])
    st["n_errors"] = len(errs)
    from . import regions
    return {"result": db.kv_get("world_result"), "status": st, "income": inc, "spend": spd, "regions": regions.status(),
            "names": {k: v[0] for k, v in COUNTRIES.items()}, "subs": {k: v[1] for k, v in COUNTRIES.items()}}


def _loop():
    time.sleep(15)
    while True:
        try:
            if config.source_on("world"):
                spend()  # 地图的消费数据先拿（有缓存时立刻返回），不用等抓完各国榜单
                got = scan()
                found = lookup_genres()
                income()
                from . import incgrid
                incgrid.ensure(_say)
                from . import regions
                last = db.kv_get("world_result") or {}
                if got or found or last.get("v") != RESULT_V or regions.changed_since(last.get("rv")):
                    compute_and_save()
                _set_status(phase=_status.get("phase") if _status.get("phase") == "blocked" else "idle")
        except Exception as e:
            traceback.print_exc()
            _set_status(phase="error", error=f"{type(e).__name__}：{e}"[:200])
        RUN_NOW.wait(3600)
        RUN_NOW.clear()


def start():
    with db.session() as c:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(world_apps)")}
        if "en_name" not in cols:
            c.execute("ALTER TABLE world_apps ADD COLUMN en_name TEXT")
            c.execute("ALTER TABLE world_apps ADD COLUMN en_artist TEXT")
            c.execute("UPDATE world_apps SET looked = 0")
    old = db.kv_get("world_status") or {}
    _status.update(old)
    threading.Thread(target=_loop, daemon=True, name="worldmap").start()
    from . import regions
    regions.start()
