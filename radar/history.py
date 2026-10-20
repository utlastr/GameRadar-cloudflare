"""历史对照（v37 起）：从 Steam 补近两年上线的小游戏的评论曲线，回答
"起步和它差不多的游戏，后来有多少成功了"。

数据从哪来（都不用登录）
  1. Steam 搜索结果（按上线时间从新到旧，每页 50 款）：名字、上线日期、原价、前 7 个标签、评论数。
  2. 每款游戏的评论直方图（appreviewhistogram）：从第一条评论起，每周多少条评论（好评 + 差评）。
     上线一两年内的游戏 Steam 按周给；更老的只按月给，只按月的不用（算不出第一周）。

收哪些游戏（只看上线时就能确定的条件，不看后来火没火，避免幸存者偏差）
  近 HISTORY_DAYS 天上线、原价不超过 HISTORY_MAX_PRICE 美元或免费、评论至少 10 条，
  不是 视觉小说 / 剧情 RPG / MMO / 4X / 体育 / 赛车 / 模拟飞行 / 成人内容 / 软件。
  类型（GROUPS）只用来分组统计，不用来挑游戏。没归进任何一组的算"其他"，也收。

"成功" = 第一条评论起 13 周（约 90 天）内评论数达到 HISTORY_SUCCESS_REVIEWS（默认 1000）。
"热度留得住" = 第 4 周的评论数还有第 1 周的一半以上。

断点续抓：翻页进度存在 kv "hist_scan"，每款游戏的状态存在 hist_steam 表，程序重启后接着抓。
后台单独一个线程慢慢抓（每次请求之间停 HISTORY_PAUSE 秒），主程序正在抓 Steam 时让开。
"""
import calendar
import html
import itertools
import json
import math
import random
import re
import threading
import time
import traceback

from . import activity, config, db
from .net import get_text

DAY = 86400
WEEK = 7 * DAY
WEEKS = 13                 # 看前 13 周（约 90 天）
MIN_REVIEWS = 10           # Steam 评论不到 10 条时搜索页不显示评论数，这些游戏不补曲线
MIN_PEERS = 15             # 对照的历史游戏少于这个数就不下结论
RESCAN_PAGES = 40          # 全部翻完以后，每天重翻最新的 40 页（约 2000 款，三周多），补新游戏、更新评论数
SEARCH_URL = "https://store.steampowered.com/search/results/"

SCHEMA = """
CREATE TABLE IF NOT EXISTS hist_steam (
    appid INTEGER PRIMARY KEY, name TEXT, released INTEGER, price REAL, tags TEXT, reviews INTEGER,
    grp TEXT, why TEXT, status TEXT, sample INTEGER DEFAULT 1, prio INTEGER DEFAULT 9,
    start INTEGER, weeks TEXT, total INTEGER, fetched INTEGER DEFAULT 0, tries INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_hist_status ON hist_steam(status, prio);
"""
# status：few 评论不到 10 条 / skip 不收（why 写原因）/ todo 待补曲线 / young 补了但还不满 13 周（3 天后再补）
#         done 满 13 周 / coarse Steam 只给按月的数据 / old 第一条评论在样本时间之前 / nodata 没有评论 / err 出错
# sample：1 = 在样本里；0 = 只是必看里的游戏，补曲线给它自己画图用，不参与统计

# Steam 标签 ID（来自 store.steampowered.com/tagdata/populartags/english）
TAG_ID = {
    "Party Game": 7178, "Party": 7108, "Social Deduction": 745697, "Minigames": 8093, "Local Multiplayer": 7368,
    "4 Player Local": 4840, "Split Screen": 10816,
    "Horror": 1667, "Psychological Horror": 1721, "Survival Horror": 3978, "Jump Scare": 42089,
    "Physics": 3968, "Funny": 4136, "Comedy": 1719, "Memes": 10397, "Intentionally Awkward Controls": 14906,
    "Destruction": 5363, "Dark Humor": 5923,
    "Co-op": 1685, "Online Co-Op": 3843, "Multiplayer": 3859, "Co-op Campaign": 4508,
    "Roguelike": 1716, "Roguelite": 3959, "Action Roguelike": 42804, "Bullet Heaven": 723991,
    "Roguelike Deckbuilder": 1091588, "Deckbuilding": 32322, "Auto Battler": 1084988, "Traditional Roguelike": 454187,
    "Puzzle": 1664, "Logic": 6129, "Idler": 615955, "Incremental": 560542, "Match 3": 1665, "Hidden Object": 1738,
    "Escape Room": 769306, "Desktop Companion": 1320952, "Puzzle Platformer": 5537, "Word Game": 24003,
    "Trivia": 10437, "Sokoban": 1730, "Typing": 1674,
    "Simulation": 599, "Management": 12472, "Job Simulator": 35079, "Shop Keeper": 91114, "Cooking": 3920,
    "Survival": 1662, "Crafting": 1702, "Open World Survival Craft": 1100689, "Base Building": 7332,
    "Sandbox": 3810, "Automation": 255534, "Cozy": 97376, "Farming Sim": 87918, "Life Sim": 10235,
    "Cleaning": 23491, "Time Management": 16689, "Colony Sim": 220585, "City Builder": 4328, "Tower Defense": 1645,
    "Arcade": 1773, "Score Attack": 5154, "Runner": 8666, "Bullet Hell": 4885, "Twin Stick Shooter": 4758,
    "Platformer": 1625, "Precision Platformer": 3877, "Fast-Paced": 1734, "Competitive": 3878, "PvP": 1775,
    "Battle Royale": 176981, "Arena Shooter": 5547, "Extraction Shooter": 1199779, "Shoot 'Em Up": 4255,
    "Beat 'em up": 4158, "Rhythm": 1752, "Hero Shooter": 620519, "2D Fighter": 4736,
    # 不收的
    "Visual Novel": 3799, "Dating Sim": 9551, "Otome": 31579, "Interactive Fiction": 11014,
    "Choose Your Own Adventure": 4486, "JRPG": 4434, "CRPG": 4474, "Party-Based RPG": 10695, "MMORPG": 1754,
    "Massively Multiplayer": 128, "4X": 1670, "Grand Strategy": 4364, "Sports": 701, "Racing": 699,
    "Automobile Sim": 1100687, "Flight": 15045, "Space Sim": 16598, "Sexual Content": 12095, "Nudity": 6650,
    "Hentai": 9130, "Software": 8013, "Utilities": 87, "Design & Illustration": 84, "Animation & Modeling": 872,
    "Video Production": 784, "Audio Production": 1027, "Photo Editing": 809, "Game Development": 13906,
    "Software Training": 1445, "Education": 1036, "Benchmark": 5407, "Soundtrack": 7948,
    "Football (Soccer)": 1254546, "Football (American)": 1254552, "Basketball": 1746, "Baseball": 5727,
    "Golf": 7038, "Tennis": 5914, "Hockey": 324176,
    "RPG": 122, "Story Rich": 1742,
}

# 分组：按顺序，第一个对上的就是它的组
GROUPS = [
    ("party", "派对 / 社交推理", ["Party Game", "Party", "Social Deduction", "Minigames", "Local Multiplayer",
                               "4 Player Local", "Split Screen"]),
    ("horror", "恐怖", ["Horror", "Psychological Horror", "Survival Horror", "Jump Scare"]),
    ("funny", "物理搞笑 / 整活", ["Physics", "Funny", "Comedy", "Memes", "Intentionally Awkward Controls",
                              "Destruction", "Dark Humor"]),
    ("coop", "多人合作 / 联机", ["Co-op", "Online Co-Op", "Multiplayer", "Co-op Campaign"]),
    ("rogue", "肉鸽 / 短局", ["Roguelike", "Roguelite", "Action Roguelike", "Bullet Heaven", "Roguelike Deckbuilder",
                           "Deckbuilding", "Auto Battler", "Traditional Roguelike"]),
    ("puzzle", "解谜 / 放置 / 休闲", ["Puzzle", "Logic", "Idler", "Incremental", "Match 3", "Hidden Object",
                                 "Escape Room", "Desktop Companion", "Puzzle Platformer", "Word Game", "Trivia",
                                 "Sokoban", "Typing"]),
    ("sim", "模拟经营 / 生存", ["Simulation", "Management", "Job Simulator", "Shop Keeper", "Cooking", "Survival",
                              "Crafting", "Open World Survival Craft", "Base Building", "Sandbox", "Automation",
                              "Cozy", "Farming Sim", "Life Sim", "Cleaning", "Time Management", "Colony Sim",
                              "City Builder", "Tower Defense"]),
    ("action", "动作 / 街机 / 竞技", ["Arcade", "Score Attack", "Runner", "Bullet Hell", "Twin Stick Shooter",
                                  "Platformer", "Precision Platformer", "Fast-Paced", "Competitive", "PvP",
                                  "Battle Royale", "Arena Shooter", "Extraction Shooter", "Shoot 'Em Up",
                                  "Beat 'em up", "Rhythm", "Hero Shooter", "2D Fighter"]),
]
GROUP_LABEL = {k: t for k, t, _ in GROUPS}
GROUP_LABEL.update({"other": "其他", "all": "全部类型"})
PRIO = {k: i for i, (k, _, _) in enumerate(GROUPS)}
PRIO["other"] = len(GROUPS)
EXCLUDE = ["Visual Novel", "Dating Sim", "Otome", "Interactive Fiction", "Choose Your Own Adventure", "JRPG", "CRPG",
           "Party-Based RPG", "MMORPG", "Massively Multiplayer", "4X", "Grand Strategy", "Sports", "Racing",
           "Automobile Sim", "Flight", "Space Sim", "Sexual Content", "Nudity", "Hentai", "Software", "Utilities",
           "Design & Illustration", "Animation & Modeling", "Video Production", "Audio Production", "Photo Editing",
           "Game Development", "Software Training", "Education", "Benchmark", "Soundtrack", "Football (Soccer)",
           "Football (American)", "Basketball", "Baseball", "Golf", "Tennis", "Hockey"]
EXCLUDE_CN = {"Visual Novel": "视觉小说", "Dating Sim": "恋爱模拟", "Otome": "乙女", "Interactive Fiction": "互动小说",
              "Choose Your Own Adventure": "文字冒险", "JRPG": "日式 RPG", "CRPG": "CRPG", "Party-Based RPG": "队伍 RPG",
              "MMORPG": "MMO", "Massively Multiplayer": "MMO", "4X": "4X 策略", "Grand Strategy": "大战略",
              "Sports": "体育", "Racing": "赛车", "Automobile Sim": "汽车模拟", "Flight": "模拟飞行",
              "Space Sim": "太空模拟", "Sexual Content": "成人内容", "Nudity": "成人内容", "Hentai": "成人内容"}
_ID_NAME = {v: k for k, v in TAG_ID.items()}


def _classify(names):
    """names：英文标签名（按重要程度排序）。返回 (组, 不收的原因)。"""
    s = set(names)
    why = ""
    bad = [n for n in EXCLUDE if n in s]
    if bad:
        why = EXCLUDE_CN.get(bad[0], "软件 / 体育等非目标类型")
    elif "RPG" in s and "Story Rich" in s:
        why = "剧情 RPG"
    for k, _, tags in GROUPS:
        if s & set(tags):
            return k, why
    return "other", why


# ---------------- 数据库 ----------------
_ready = False


def _init():
    global _ready
    if not _ready:
        with db.session() as c:
            c.executescript(SCHEMA)
        _ready = True


# ---------------- Steam 搜索页 ----------------
_ROW_SPLIT = re.compile(r'(?=<a\s+href="https://store\.steampowered\.com/(?:app|sub|bundle)/)')
_RE_APPID = re.compile(r'data-ds-appid="(\d+)"')
_RE_TITLE = re.compile(r'<span class="title">(.*?)</span>', re.S)
_RE_TAGS = re.compile(r'data-ds-tagids="\[([\d,\s]*)\]"')
_RE_REL = re.compile(r'search_released[^"]*">\s*(.*?)\s*</div>', re.S)
_RE_REV = re.compile(r'of the ([\d,]+) user reviews')
_RE_ORIG = re.compile(r'\$([\d,]+(?:\.\d+)?) normally')
_RE_FINAL = re.compile(r'data-price-final="(\d+)"')
_MON = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                    "dec"), 1)}


def _parse_date(s):
    """'Mar 27, 2026' 或 '27 Mar, 2026' → 时间戳（UTC 0 点）。'Coming soon'、'Q1 2026' 这类返回 None。"""
    parts = (s or "").replace(",", " ").split()
    if len(parts) != 3 or not parts[2].isdigit():
        return None
    a, b, y = parts
    if a[:3].lower() in _MON and b.isdigit():
        mon, d = _MON[a[:3].lower()], int(b)
    elif b[:3].lower() in _MON and a.isdigit():
        mon, d = _MON[b[:3].lower()], int(a)
    else:
        return None
    if not (1 <= d <= 31 and 1990 <= int(y) <= 2100):
        return None
    return int(calendar.timegm((int(y), mon, d, 0, 0, 0)))


def _parse_page(text):
    rows = []
    for chunk in _ROW_SPLIT.split(text or ""):
        m = _RE_APPID.search(chunk)
        if not m:
            continue
        appid = int(m.group(1))
        t = _RE_TITLE.search(chunk)
        tg = _RE_TAGS.search(chunk)
        rl = _RE_REL.search(chunk)
        rv = _RE_REV.search(chunk)
        tags = [int(x) for x in (tg.group(1).split(",") if tg else []) if x.strip().isdigit()]
        price = None
        o = _RE_ORIG.search(chunk)
        if o:
            price = float(o.group(1).replace(",", ""))
        else:
            f = _RE_FINAL.search(chunk)
            if f:
                price = int(f.group(1)) / 100.0
            elif "discount_final_price free" in chunk or ">Free<" in chunk:
                price = 0.0
        rows.append({"appid": appid, "name": html.unescape(t.group(1)).strip() if t else str(appid),
                     "released": _parse_date(html.unescape(rl.group(1))) if rl else None,
                     "tags": tags, "reviews": int(rv.group(1).replace(",", "")) if rv else 0, "price": price})
    return rows


def _upsert(rows):
    if not rows:
        return 0
    fetched_states = ("young", "done", "coarse", "old", "nodata", "err")
    added = 0
    with db.session() as c:
        ids = [r["appid"] for r in rows]
        old = {a: s for a, s in c.execute(
            f"SELECT appid, status FROM hist_steam WHERE appid IN ({','.join('?' * len(ids))})", ids)}
        for r in rows:
            names = [_ID_NAME[t] for t in r["tags"] if t in _ID_NAME]
            grp, why = _classify(names)
            if not why and r["price"] is not None and r["price"] > config.HISTORY_MAX_PRICE:
                why = f"原价 ${r['price']:.2f}，超过 ${config.HISTORY_MAX_PRICE}"
            prev = old.get(r["appid"])
            if prev in fetched_states:
                status = prev                      # 曲线已经补过，状态不变
            elif why:
                status = "skip"
            elif r["reviews"] < MIN_REVIEWS:
                status = "few"
            else:
                status = "todo"
            if prev is None:
                added += 1
            c.execute(
                """INSERT INTO hist_steam(appid, name, released, price, tags, reviews, grp, why, status, sample, prio)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(appid) DO UPDATE SET name = excluded.name, released = excluded.released,
                     price = excluded.price, tags = excluded.tags, reviews = excluded.reviews, grp = excluded.grp,
                     why = excluded.why, status = excluded.status, sample = excluded.sample, prio = excluded.prio""",
                (r["appid"], r["name"], r["released"], r["price"], json.dumps(r["tags"]), r["reviews"], grp, why,
                 status, 0 if why else 1, PRIO.get(grp, 9)))
    return added


def _scan(st, rescan):
    """翻一页 Steam 搜索（按上线时间从新到旧）。st 是 kv "hist_scan" 里的进度，翻完一页就存。"""
    now = int(time.time())
    key = "rescan_start" if rescan else "start"
    start = int(st.get(key) or 0)
    text = get_text(SEARCH_URL, {"infinite": 1, "start": start, "count": 50, "sort_by": "Released_DESC",
                                 "category1": 998, "cc": "us", "l": "english", "ndl": 1}, retries=2, timeout=30)
    try:
        d = json.loads(text or "")
    except ValueError:
        raise RuntimeError("Steam 搜索返回的不是数据（可能被限流，稍后自动重试）")
    raw = d.get("results_html") or ""
    rows = _parse_page(raw)
    if not rows and "data-ds-appid" in raw:
        raise RuntimeError("Steam 搜索页的格式变了，解析不出游戏（把这条发给助手）")
    if not rows and int(st.get("empty") or 0) < 4:
        st["empty"] = int(st.get("empty") or 0) + 1
        db.kv_set("hist_scan", st)
        raise RuntimeError(f"Steam 搜索第 {start} 款起返回空结果（第 {st['empty']} 次），稍后重试")
    cutoff = now - int(config.HISTORY_DAYS) * DAY
    keep = [r for r in rows if r["released"] and r["released"] >= cutoff]
    added = _upsert(keep)
    dated = sorted(r["released"] for r in rows if r["released"])
    if d.get("total_count"):
        st["total"] = int(d["total_count"])
    if dated and not rescan:
        st["oldest"] = dated[0]
    st[key] = start + len(rows)
    st["pages"] = int(st.get("pages") or 0) + (0 if rescan else 1)
    st["added"] = int(st.get("added") or 0) + added
    # 这一页八成以上都早于样本开始时间：翻完了
    past = dated and sum(1 for x in dated if x < cutoff) >= 0.8 * len(dated)
    ended = not rows                      # 连着 5 次都是空的：Steam 不再给更早的结果了
    st["empty"] = 0
    done = past or ended or (rescan and st[key] >= RESCAN_PAGES * 50)
    if done:
        if rescan:
            st["rescan_start"] = None
            activity.say(f"历史对照：每天补新游戏完成，新收 {st.get('r_added', 0) + added} 款")
            st["r_added"] = 0
        else:
            st["full"] = True
            st["full_ts"] = now
            if not past:
                st["note"] = (f"Steam 搜索翻到第 {start} 款就不再给结果了（最早到 "
                              f"{time.strftime('%Y-%m-%d', time.gmtime(st.get('oldest') or now))}）")
            activity.say(f"历史对照：Steam 列表翻完，共 {st['pages']} 页，收进 {_count_listed()} 款近 "
                         f"{config.HISTORY_DAYS} 天上线的游戏；接下来逐个补评论曲线" + (f"。{st['note']}" if st.get("note") else ""))
        st["last_rescan"] = now
    elif rescan:
        st["r_added"] = int(st.get("r_added") or 0) + added
    elif st["pages"] % 50 == 0:
        activity.say(f"历史对照：Steam 列表翻到 {time.strftime('%Y-%m-%d', time.gmtime(st.get('oldest') or now))}"
                     f"（第 {st['pages']} 页），已收 {_count_listed()} 款")
    db.kv_set("hist_scan", st)


def _count_listed():
    with db.session() as c:
        return c.execute("SELECT COUNT(*) FROM hist_steam WHERE released IS NOT NULL").fetchone()[0]


# ---------------- 评论曲线 ----------------
class _BadData(Exception):
    pass


def _histogram(appid):
    text = get_text(f"https://store.steampowered.com/appreviewhistogram/{int(appid)}",
                    {"l": "english", "review_score_preference": 0}, retries=1, timeout=20)
    try:
        d = json.loads(text or "")
    except ValueError:
        raise RuntimeError("Steam 评论数据返回的不是数据（可能被限流，稍后自动重试）")
    if not isinstance(d, dict) or d.get("success") != 1:
        raise _BadData("Steam 没给这款的评论数据")
    return d.get("results") or {}


def _weeks(res, now):
    """直方图 → (第一条评论时间, 已满的每周评论数列表（最多 13 周）, 总评论数, 是不是只有按月的数据)。"""
    start = int(res.get("start_date") or 0)
    rolls = res.get("rollups") or []
    if not start or not rolls:
        return None
    n = lambda x: int(x.get("recommendations_up") or 0) + int(x.get("recommendations_down") or 0)
    total = sum(n(x) for x in rolls)
    if res.get("rollup_type") not in ("week", "day"):
        return start, None, total, True
    # 按周汇总时 Steam 的周可能从第一条评论之前的某天算起：以较早的那个为第 1 周的开始
    start = min([start] + [int(x.get("date") or start) for x in rolls])
    w = [0] * WEEKS
    for x in rolls:
        i = int((int(x.get("date") or 0) - start + DAY // 2) // WEEK)
        if 0 <= i < WEEKS:
            w[i] += n(x)
    full = min(WEEKS, max(0, int((now - start) // WEEK)))
    return start, w[:full], total, False


def _fetch(appid):
    now = int(time.time())
    try:
        res = _histogram(appid)
    except _BadData as e:
        _mark_err(appid, now, str(e), hard=True)       # 这一款的问题：记下来，6 小时后再试
        return
    except Exception as e:
        _mark_err(appid, now, str(e)[:200], hard=False)   # 网络问题：先不改状态，连着 5 次才算这一款出错
        raise
    r = _weeks(res, now)
    if r is None:
        status, start, weeks, total = "nodata", None, None, 0
    else:
        start, weeks, total, coarse = r
        if coarse:
            status = "coarse"
        elif start < now - (int(config.HISTORY_DAYS) + 30) * DAY:
            status = "old"
        elif len(weeks) >= WEEKS:
            status = "done"
        else:
            status = "young"
    with db.session() as c:
        c.execute(
            """INSERT INTO hist_steam(appid, status, sample, start, weeks, total, fetched, tries)
               VALUES (?, ?, 0, ?, ?, ?, ?, 0)
               ON CONFLICT(appid) DO UPDATE SET status = excluded.status, start = excluded.start,
                 weeks = excluded.weeks, total = excluded.total, fetched = excluded.fetched, tries = 0""",
            (int(appid), status, start, json.dumps(weeks) if weeks is not None else None, total, now))
    _stat["curves"] += 1


def _mark_err(appid, now, why, hard):
    h = 1 if hard else 0
    with db.session() as c:
        c.execute(
            """INSERT INTO hist_steam(appid, status, sample, fetched, tries, why) VALUES (?, 'err', 0, ?, 1, ?)
               ON CONFLICT(appid) DO UPDATE SET
                 status = CASE WHEN ? OR hist_steam.tries + 1 >= 5 THEN 'err' ELSE hist_steam.status END,
                 fetched = CASE WHEN ? OR hist_steam.tries + 1 >= 5 THEN excluded.fetched ELSE hist_steam.fetched END,
                 tries = hist_steam.tries + 1""",
            (int(appid), now, why, h, h))


# ---------------- 后台线程 ----------------
_thread = None
_stat = {"curves": 0, "steps": 0, "busy_s": 0.0, "error": "", "err_ts": 0, "phase": "", "started": 0}


def start():
    """主程序启动时调用一次。重复调用没关系。"""
    global _thread
    if not config.HISTORY_ENABLED or (_thread and _thread.is_alive()):
        return
    _init()
    _thread = threading.Thread(target=_loop, name="history", daemon=True)
    _thread.start()


def _main_on_steam():
    s = activity.state()
    return bool(s.get("busy")) and "Steam" in (s.get("task") or "")


def _loop():
    time.sleep(90)          # 让主程序先启动、先抓第一轮
    _stat["started"] = time.time()
    activity.say("历史对照：后台开始补 Steam 近两年小游戏的评论曲线（每一步都存进数据库，重启后接着抓，不重来）")
    errs = 0
    while True:
        while _main_on_steam():
            time.sleep(5)       # 主程序在抓 Steam：让开，免得一起请求被 Steam 限流
        t0 = time.time()
        try:
            did = _step()
            errs = 0
        except Exception as e:
            errs += 1
            wait = min(1800, 60 * errs)
            _stat.update(error=f"{type(e).__name__}：{e}"[:300], err_ts=int(time.time()))
            if errs in (1, 3, 10) or errs % 30 == 0:
                activity.say(f"历史对照：请求 Steam 出错（{e}），{wait // 60} 分钟后接着抓")
            if errs == 1:
                traceback.print_exc()
            time.sleep(wait)
            continue
        if not did:
            _stat["phase"] = "idle"
            time.sleep(600)
            continue
        _stat["steps"] += 1
        _stat["busy_s"] += time.time() - t0
        if _stat["curves"] and _stat["curves"] % 300 == 0 and _stat.get("_said") != _stat["curves"]:
            _stat["_said"] = _stat["curves"]
            s = summary(force=True)
            activity.say(f"历史对照：这次运行已补 {_stat['curves']} 款曲线；样本里满 90 天的 {s['counts'].get('done', 0)} 款，"
                         f"还剩 {s['counts'].get('todo', 0)} 款" + (f"，预计还要约 {s['eta_h']} 小时" if s.get("eta_h") else ""))
        time.sleep(max(0.5, float(config.HISTORY_PAUSE)) + random.random())


def _step():
    """做一步（一个网络请求）。没事可做返回 False。"""
    _init()
    now = int(time.time())
    # 1. 必看里的 Steam 游戏：它自己的曲线（12 小时更新一次）
    watch = db.kv_get("hist_watch", []) or []
    if watch:
        with db.session() as c:
            have = {a: f for a, f in c.execute(
                f"SELECT appid, fetched FROM hist_steam WHERE appid IN ({','.join('?' * len(watch))}) "
                f"AND fetched > 0", watch)}
        for a in watch:
            if now - (have.get(a) or 0) > 12 * 3600:
                _stat["phase"] = "watch"
                _fetch(a)
                return True
    # 2. 翻 Steam 列表（第一次全部翻完；之后每天翻最新的 40 页）
    st = db.kv_get("hist_scan", {}) or {}
    if not st.get("full"):
        # v39：翻列表和补曲线交替做，不用等列表翻完才出结果。翻列表时先补已经上线满 98 天的（补完就能直接对照）
        _stat["tick"] = _stat.get("tick", 0) + 1
        if _stat["tick"] % 2 == 0:
            with db.session() as c:
                row = c.execute("SELECT appid FROM hist_steam WHERE status = 'todo' AND released < ? "
                                "ORDER BY (appid * 7919) % 10007 LIMIT 1", (now - 98 * DAY,)).fetchone()
            if row:
                _stat["phase"] = "scan+curve"
                _fetch(row[0])
                return True
        _stat["phase"] = "scan"
        _scan(st, rescan=False)
        return True
    if st.get("rescan_start") is None and now - int(st.get("last_rescan") or 0) > DAY:
        st["rescan_start"] = 0
    if st.get("rescan_start") is not None:
        _stat["phase"] = "rescan"
        _scan(st, rescan=True)
        return True
    # 3. 还不满 13 周的：3 天补一次，直到满 13 周
    # 4. 待补的：所有类型混在一起打乱顺序（v40 起；v37–v39 按组的顺序补，结果先补完派对、恐怖，其他类型一直是 0），
    #    上线满 98 天的先补（补完就能进对照表）
    # 5. 出错的：6 小时后再试
    with db.session() as c:
        row = c.execute("SELECT appid FROM hist_steam WHERE status = 'young' AND fetched < ? ORDER BY fetched LIMIT 1",
                        (now - 3 * DAY,)).fetchone()
        phase = "young"
        if not row:
            row = c.execute("SELECT appid FROM hist_steam WHERE status = 'todo' "
                            "ORDER BY (COALESCE(released, 0) >= ?), (appid * 7919) % 10007 LIMIT 1",
                            (now - 98 * DAY,)).fetchone()
            phase = "curve"
        if not row:
            row = c.execute("SELECT appid FROM hist_steam WHERE status = 'err' AND tries < 8 AND fetched < ? "
                            "AND released IS NOT NULL ORDER BY fetched LIMIT 1",
                            (now - 6 * 3600,)).fetchone()
            phase = "retry"
    if not row:
        return False
    _stat["phase"] = phase
    _fetch(row[0])
    return True


# ---------------- 统计 ----------------
_peer_cache = {"key": None, "rows": []}
_sum_cache = {"ts": 0, "data": None}


def _peers():
    """样本里满 13 周的游戏：[(appid, 组, 每周累计评论数 13 个)]。"""
    _init()
    with db.session() as c:
        key = tuple(c.execute("SELECT COUNT(*), MAX(fetched) FROM hist_steam "
                              "WHERE sample = 1 AND status = 'done'").fetchone())
        if _peer_cache["key"] == key:
            return _peer_cache["rows"]
        rows = []
        for appid, grp, weeks in c.execute("SELECT appid, grp, weeks FROM hist_steam "
                                           "WHERE sample = 1 AND status = 'done' AND weeks IS NOT NULL"):
            try:
                w = json.loads(weeks)
            except ValueError:
                continue
            if len(w) >= WEEKS:
                rows.append((appid, grp or "other", list(itertools.accumulate(w[:WEEKS]))))
    _peer_cache.update(key=key, rows=rows)
    return rows


def _succ(cum):
    return cum[WEEKS - 1] >= config.HISTORY_SUCCESS_REVIEWS


def _hold(cum):
    w1, w4 = cum[0], cum[3] - cum[2]
    return w1 > 0 and w4 >= 0.5 * w1


def _q(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    return s[int(round(q * (len(s) - 1)))]


# 第 1 周评论数分档。0–9：评论总数过 10 条，但第 1 周不到 10 条的（v40 前漏了这一档，各档加起来比总数少）
BUCKETS = [(0, 10), (10, 30), (30, 100), (100, 300), (300, 1000), (1000, None)]


def _table(rows):
    out = []
    for key in ["all"] + [k for k, _, _ in GROUPS] + ["other"]:
        sel = rows if key == "all" else [r for r in rows if r[1] == key]
        cells = []
        for lo, hi in BUCKETS:
            b = [r for r in sel if r[2][0] >= lo and (hi is None or r[2][0] < hi)]
            n = len(b)
            cells.append({"n": n,
                          "succ": round(sum(1 for r in b if _succ(r[2])) / n, 3) if n else None,
                          "hold": round(sum(1 for r in b if _hold(r[2])) / n, 3) if n else None,
                          "win": sum(1 for r in b if _succ(r[2])),
                          "med": _q([r[2][-1] for r in b], 0.5)})
        out.append({"key": key, "label": GROUP_LABEL[key], "n": len(sel), "win": sum(1 for r in sel if _succ(r[2])),
                    "succ": round(sum(1 for r in sel if _succ(r[2])) / len(sel), 3) if sel else None,
                    "cells": cells})
    return out


def _check():
    """自检：曲线里加起来的评论总数，和 Steam 列表上显示的评论数对不对得上。
    对得上（比值接近 1）说明每周的数是全的；明显偏小说明曲线漏了（例如只算了某种语言）。"""
    with db.session() as c:
        rows = c.execute("SELECT total, reviews FROM hist_steam WHERE sample = 1 AND status IN ('done', 'young') "
                         "AND reviews >= 50 AND total > 0").fetchall()
    if not rows:
        return None
    r = [t / v for t, v in rows]
    return {"n": len(r), "med": round(_q(r, 0.5), 2), "p10": round(_q(r, 0.1), 2), "p90": round(_q(r, 0.9), 2),
            "bad": sum(1 for x in r if x < 0.8 or x > 1.25)}


def _quick(now):
    """不用每周曲线、只用列表里的评论总数就能算的初步统计：上线满 90 天的样本里，现在评论数过成功线的有多少。
    现在的评论数 ≥ 90 天时的评论数，所以这里的成功比例是上限（偏高），有曲线以后以正式表为准。"""
    S = config.HISTORY_SUCCESS_REVIEWS
    with db.session() as c:
        rows = c.execute("SELECT appid, name, grp, reviews, released, price FROM hist_steam "
                         "WHERE sample = 1 AND reviews >= ? AND released IS NOT NULL AND released < ?",
                         (MIN_REVIEWS, now - 90 * DAY)).fetchall()
        n_listed = c.execute("SELECT COUNT(*) FROM hist_steam WHERE released IS NOT NULL").fetchone()[0]
        n_young = c.execute("SELECT COUNT(*) FROM hist_steam WHERE sample = 1 AND reviews >= ? "
                            "AND released >= ?", (MIN_REVIEWS, now - 90 * DAY)).fetchone()[0]
    out = []
    for key in ["all"] + [k for k, _, _ in GROUPS] + ["other"]:
        sel = rows if key == "all" else [r for r in rows if (r[2] or "other") == key]
        if not sel and key != "all":
            continue
        win = sorted([r for r in sel if r[3] >= S], key=lambda r: -r[3])
        out.append({"key": key, "label": GROUP_LABEL[key], "n": len(sel), "n_win": len(win),
                    "succ": round(len(win) / len(sel), 3) if sel else None,
                    "med": _q([r[3] for r in sel], 0.5),
                    "n10k": sum(1 for r in sel if r[3] >= 10000),
                    "top": [{"appid": r[0], "name": r[1], "reviews": r[3], "released": r[4], "price": r[5]}
                            for r in win[:5 if key == "all" else 3]]})
    oldest = min((r[4] for r in rows), default=None)
    return {"groups": out, "n_listed": n_listed, "n_young": n_young, "oldest": oldest}


def summary(force=False):
    """面板用：进度 + 对照表。1 分钟内重复调用直接用上次的结果。"""
    now = time.time()
    if not force and _sum_cache["data"] and now - _sum_cache["ts"] < 60:
        return _sum_cache["data"]
    _init()
    st = db.kv_get("hist_scan", {}) or {}
    counts, skip = {}, 0
    with db.session() as c:
        for s, sm, n in c.execute("SELECT status, sample, COUNT(*) FROM hist_steam "
                                  "WHERE released IS NOT NULL GROUP BY status, sample"):
            if s == "skip":
                skip += n
            elif sm:
                counts[s] = counts.get(s, 0) + n
    counts["skip"] = skip
    rows = _peers()
    # 还要多久：剩下的请求数 × 平均每步用时（含停顿）
    per = (_stat["busy_s"] / _stat["steps"] + float(config.HISTORY_PAUSE) + 0.5) if _stat["steps"] else 3.5
    left = counts.get("todo", 0) + counts.get("young", 0) // 10
    if not st.get("full"):
        # 列表还没翻完：剩下的页数 + 按已翻部分的比例估算后面还会冒出多少待补的
        span = int(config.HISTORY_DAYS) * DAY
        frac = min(1.0, (now - (st.get("oldest") or now)) / span) if st.get("oldest") else 0.0
        pages = int(st.get("pages") or 0)
        if frac > 0.02:
            left = int(pages * (1 / frac - 1)) + int(left / frac)
        else:
            left = 1500 + 20000         # 刚开始，按 Steam 两年约 6 万款、其中约 1/3 要补算
    data = {
        "enabled": bool(config.HISTORY_ENABLED), "running": bool(_thread and _thread.is_alive()),
        "phase": _stat["phase"], "scan": {k: st.get(k) for k in ("full", "pages", "oldest", "total", "full_ts",
                                                                   "last_rescan", "note", "start")},
        "counts": counts, "this_run": _stat["curves"],
        "eta_h": round(left * per / 3600, 1) if left else 0,
        "error": _stat["error"] if _stat["err_ts"] and now - _stat["err_ts"] < 3600 else "",
        "err_ts": _stat["err_ts"],
        "def": f"上线 90 天内评论过 {config.HISTORY_SUCCESS_REVIEWS} 条", "S": config.HISTORY_SUCCESS_REVIEWS,
        "days": config.HISTORY_DAYS, "max_price": config.HISTORY_MAX_PRICE,
        "buckets": [f"{lo}–{hi - 1}" if hi else f"{lo}+" for lo, hi in BUCKETS],
        "table": _table(rows), "n_done": len(rows), "quick": _quick(now), "check": _check(),
    }
    _sum_cache.update(ts=now, data=data)
    return data


# ---------------- 给必看卡片用 ----------------
def attach(cards):
    """分析时调用（不联网）：给有 Steam 的卡片加 card["hist"]，并把这些游戏记进 hist_watch，后台去补它们的曲线。"""
    _init()
    now = int(time.time())
    rows = _peers()
    pick = {}
    for card in cards:
        if (card.get("age_w") or 0) <= 0:
            continue
        rv = card.get("_reviews") or {}
        steam = [m for m in card["members"] if m.startswith("steam:") and m.split(":", 1)[1].isdigit()]
        if not steam:
            continue
        m = max(steam, key=lambda k: rv.get(k) or 0)
        pick[card["gid"]] = (int(m.split(":", 1)[1]), rv.get(m))
    if not pick:
        db.kv_set("hist_watch", [])
        return
    ids = sorted({a for a, _ in pick.values()})
    with db.session() as c:
        own = {r[0]: r for r in c.execute(
            f"SELECT appid, grp, start, weeks, total, status, fetched FROM hist_steam "
            f"WHERE appid IN ({','.join('?' * len(ids))})", ids)}
    watch = []
    for card in cards:
        if card["gid"] not in pick:
            continue
        appid, reviews_now = pick[card["gid"]]
        info = card.get("_info") or {}
        rel = info.get("release_ts")
        if not rel or now - rel < 120 * DAY:
            watch.append(appid)
        try:
            h = _judge(appid, own.get(appid), reviews_now, rel, info.get("tags") or [], info.get("price_usd"), now, rows)
        except Exception as e:
            h = {"note": f"算历史对照时出错：{type(e).__name__}：{e}"[:120]}
        if not h:
            continue
        card["hist"] = h
        if h.get("n"):
            p = round(h["succ"] * 100)
            po = round(h["opt"]["succ"] * 100) if h.get("opt") else None
            rng = f"{min(p, po)}%–{max(p, po)}%" if po is not None and po != p else f"{p}%"
            card["flags"].append({"t": f"同起步 {rng} 成功（N={h['n']}）", "c": "hot" if max(p, po or 0) >= 50 else ""})
        elif h.get("done"):
            card["flags"].append({"t": f"上线 90 天共 {h['total90']:,} 条评论，"
                                       f"{'达到' if h['succ'] else '没达到'}历史对照的成功线 {h['S']:,}", "c": ""})
    db.kv_set("hist_watch", watch[:100])


def for_appid(appid, reviews_now=None, rel=None, price=None):
    """v51：各平台里点开的任何一款 Steam 游戏（不在必看里也行）都算历史对照。纯读库，不联网。

    返回 (hist, meta)。meta 是历史库里记的名字 / 上线时间 / 评论数，给只在历史库里有的游戏用。
    """
    _init()
    now = int(time.time())
    with db.session() as c:
        r = c.execute("SELECT appid, grp, start, weeks, total, status, fetched, name, released, price, reviews "
                      "FROM hist_steam WHERE appid = ?", (int(appid),)).fetchone()
    row, meta = None, {}
    if r:
        row = tuple(r[:7])
        meta = {"name": r[7], "released": r[8], "price": r[9], "reviews": r[10]}
        rel = rel or r[8]
        price = r[9] if price is None else price
        reviews_now = reviews_now if reviews_now is not None else r[10]
    if rel and now - rel > 365 * DAY:
        return {"note": "上线超过一年，不和历史比（历史样本都是上线 90 天内的曲线）"}, meta
    try:
        h = _judge(int(appid), row, reviews_now, rel, [], price, now, _peers())
    except Exception as e:
        h = {"note": f"算历史对照时出错：{type(e).__name__}：{e}"[:120]}
    return h, meta


def _judge(appid, row, reviews_now, rel, tag_names, price, now, rows):
    S = config.HISTORY_SUCCESS_REVIEWS
    grp, why = _classify(tag_names)
    if row and row[1]:
        grp = row[1]
    base = {"S": S, "def": f"上线 90 天内评论过 {S:,} 条", "grp_own": GROUP_LABEL.get(grp, grp)}
    notes = []
    if why:
        notes.append(f"这款是{why}，历史样本里不收这类，下面按其他小游戏比，只能参考")
    if price is not None and price > config.HISTORY_MAX_PRICE:
        notes.append(f"原价超过 ${config.HISTORY_MAX_PRICE}，历史样本里都是更便宜的小游戏，只能参考")
    base["warn"] = notes
    cum, approx = [], False
    if row and row[3] is not None and row[2]:
        start = row[2]
        try:
            w = json.loads(row[3])
        except ValueError:
            w = []
        cum = list(itertools.accumulate(w[:WEEKS]))
        age = (now - start) / DAY
        if len(cum) >= WEEKS:
            return dict(base, done=True, total90=cum[WEEKS - 1], succ=cum[WEEKS - 1] >= S, own=cum)
        if cum:
            wk, x = len(cum), cum[-1]
        else:
            # 不满一周：用到现在的总数和"第 1 周"比，偏保守。
            # v52：row[4] 是后台补曲线那一刻的评论数，之后不再更新；和现在抓到的取大的（以前一直用旧数，例如 27 vs 41）
            wk, x, approx = 1, max(int(row[4] or 0), int(reviews_now or 0)), True
    elif rel and reviews_now:
        age = (now - rel) / DAY
        if age >= WEEKS * 7:
            return dict(base, note="上线超过 90 天，这款的评论曲线后台还没补到")
        wk, x, approx = max(1, min(WEEKS, math.ceil(age / 7))), int(reviews_now), True
    else:
        return dict(base, note="这款的评论曲线后台还没补到，补到后自动出结果")
    base.update(own=cum, wk=wk, x=x, approx=approx, age=round(age, 1))
    if x < MIN_REVIEWS:
        return dict(base, note=f"目前只有 {x} 条评论，不到 {MIN_REVIEWS} 条，先不比")
    if not rows:
        return dict(base, note="历史库还在补（运行情况页能看到进度），暂时没有能对照的游戏")
    found = _match(rows, grp, appid, x, lambda r: r[2][wk - 1])
    if found[0] is None:
        return dict(base, note=f"历史库里起步和它差不多的游戏还不够（{found[1]} 款，至少要 {MIN_PEERS} 款），先不下结论")
    res = dict(base, **_stats(*found))
    # v52：这一周还没过完时，再按"历史游戏上线同样天数时有多少条"比一次（把那一周的评论平均摊到每天估的；
    # 新游戏头几天评论最多，所以这样估偏乐观）。两个结果之间就是它真实成功率大概的范围
    if approx:
        f = (age - 7 * (wk - 1)) / 7
        if 0 < f < 0.95:
            def at_age(r, k=wk, f=f):
                prev = r[2][k - 2] if k >= 2 else 0
                return prev + (r[2][k - 1] - prev) * f
            fo = _match(rows, grp, appid, x, at_age)
            if fo[0] is not None:
                st = _stats(*fo)
                res["opt"] = {k: st[k] for k in ("succ", "hold", "n", "n_win", "ratio", "grp", "med90")}
    return res


def _match(rows, grp, appid, x, val):
    """找"起步像它"的历史游戏：val(r) 在 x/1.5–x×1.5 之间（不够再放宽到 2.5 倍、再放到全部类型）。
    返回 (peers, scope, ratio)；不够 MIN_PEERS 款时返回 (None, 找到的款数, None)。"""
    peers = []
    for scope in (grp, "all"):
        sel = rows if scope == "all" else [r for r in rows if r[1] == scope]
        for ratio in (1.5, 2.5):
            lo, hi = max(MIN_REVIEWS, x / ratio), x * ratio
            peers = [r for r in sel if r[0] != appid and lo <= val(r) <= hi]
            if len(peers) >= MIN_PEERS:
                return peers, scope, ratio
    return None, len(peers), None


def _stats(peers, scope, ratio):
    wins = [r for r in peers if _succ(r[2])]
    holds = [r for r in peers if r[2][0] > 0]
    return dict(grp=GROUP_LABEL.get(scope, scope), ratio=ratio, n=len(peers),
                succ=round(len(wins) / len(peers), 3),
                hold=round(sum(1 for r in holds if _hold(r[2])) / len(holds), 3) if holds else None,
                med90=_q([r[2][-1] for r in peers], 0.5),
                p25=[_q([r[2][i] for r in peers], 0.25) for i in range(WEEKS)],
                p50=[_q([r[2][i] for r in peers], 0.5) for i in range(WEEKS)],
                p75=[_q([r[2][i] for r in peers], 0.75) for i in range(WEEKS)],
                win=[_q([r[2][i] for r in wins], 0.5) for i in range(WEEKS)] if len(wins) >= 5 else None,
                n_win=len(wins))
