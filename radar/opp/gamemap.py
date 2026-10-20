"""视频对应到游戏（方案第 4 节）。

词典来自 Steam 中文名/英文名、TapTap 名称、用户手填的别名。
对应关系不并进 video.py 的原则（BV 号不按标题并进游戏），这里单独建表、记方法和可信度。
"""
import json
import re
import time

from .. import db

# 归一化时去掉的尾巴
_NOISE = ("中文版", "官方中文版", "试玩版", "试玩", "demo", "Demo", "正式版", "完整版",
          "steam版", "Steam版", "手机版", "手游", "游戏", "pc版", "PC版")

_BRACKET = re.compile(r"[《【\[]([^》】\]]{2,40})[》】\]]")
_STEAM_LINK = re.compile(r"store\.steampowered\.com/app/(\d+)")
_PUNCT = re.compile(r"[\s\u3000!-/:-@\[-`{-~·—…“”‘’《》【】「」、，。！？；：（）()]+")


def normalize(s):
    """转小写、全角转半角、去标点、去"中文版/试玩"这类尾巴。"""
    s = (s or "").strip()
    if not s:
        return ""
    out = []
    for ch in s:
        o = ord(ch)
        if o == 0x3000:
            out.append(" ")
        elif 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))       # 全角转半角
        else:
            out.append(ch)
    s = "".join(out).lower()
    s = _PUNCT.sub("", s)
    for w in _NOISE:
        if s.endswith(w.lower()) and len(s) > len(w):
            s = s[: -len(w)]
    return s.strip()


def _gid(name, appid=None):
    if appid:
        return "steam:%s" % appid
    return "name:%s" % normalize(name)


def build_dict():
    """返回 {归一化名字: {"gid":…, "name":…, "appid":…}}。"""
    idx = {}

    def put(name, appid=None, cn=None):
        n = normalize(name)
        if len(n) < 2:
            return
        gid = _gid(cn or name, appid)
        idx.setdefault(n, {"gid": gid, "name": cn or name, "appid": appid})

    ents = db.load_entities()
    for (src, eid), v in ents.items():
        extra = v.get("extra") or {}
        if src == "steam":
            put(v.get("name"), appid=eid, cn=extra.get("name_cn") or v.get("name"))
            if extra.get("name_cn"):
                put(extra.get("name_cn"), appid=eid, cn=extra.get("name_cn"))
        elif src in ("taptap", "tapmaker", "haoyou", "appcn"):
            put(v.get("name"), cn=v.get("name"))

    # 用户手动加的别名
    for member, lab in db.load_labels().items():
        for a in lab.get("aliases") or []:
            put(a, cn=member.split(":", 1)[-1] if ":" in member else a)
    return idx


def candidates(title, tags):
    """从标题的《》和标签里抽出候选游戏名。"""
    out = []
    for m in _BRACKET.finditer(title or ""):
        name = m.group(1).strip()
        if 2 <= len(name) <= 40:
            out.append(name)
    for t in tags or []:
        t = (t or "").strip()
        if 2 <= len(t) <= 20:
            out.append(t)
    # 去重但保序
    seen, uniq = set(), []
    for x in out:
        k = normalize(x)
        if k and k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq


def match(title, descr, tags, idx=None):
    """按优先级给对应关系。返回 [(gid, name, method, conf)]，可能为空。"""
    idx = idx if idx is not None else build_dict()
    hits = []
    seen = set()

    def add(gid, name, method, conf):
        if gid not in seen:
            seen.add(gid)
            hits.append((gid, name, method, conf))

    # 1. 简介里的 Steam 商店链接：最硬
    m = _STEAM_LINK.search(descr or "")
    if m:
        appid = m.group(1)
        for v in idx.values():
            if str(v.get("appid")) == appid:
                add(v["gid"], v["name"], "steam_link", 1.0)
                break
        else:
            add("steam:%s" % appid, "steam:%s" % appid, "steam_link", 1.0)

    # 2. 标签完全等于词典名字
    for t in tags or []:
        v = idx.get(normalize(t))
        if v:
            add(v["gid"], v["name"], "tag", 0.9)

    # 3. 标题里《》括起来的名字
    for m in _BRACKET.finditer(title or ""):
        v = idx.get(normalize(m.group(1)))
        if v:
            add(v["gid"], v["name"], "title_bracket", 0.8)

    # 4. 标题直接包含词典名字（至少 3 个字，避免"原神"类短词误伤）
    norm_title = normalize(title)
    if norm_title:
        for n, v in idx.items():
            if len(n) >= 3 and n in norm_title:
                add(v["gid"], v["name"], "title_contains", 0.6)
    return hits


def ensure_game(gid, name, appid=None):
    """有对应关系就把游戏登记进 opp_games。"""
    with db.session() as c:
        row = c.execute("SELECT gid FROM opp_games WHERE gid = ?", (gid,)).fetchone()
        if row:
            return
        c.execute("INSERT INTO opp_games(gid, name_cn, name_en, steam_appid, aliases, t0, state, state_ts, "
                  "source, big) VALUES (?, ?, '', ?, '[]', 0, '观察', ?, 'auto', 0)",
                  (gid, name, str(appid or ""), int(time.time())))


def save_map(bvid, hits):
    if not hits:
        return 0
    with db.session() as c:
        for gid, name, method, conf in hits:
            c.execute("INSERT OR REPLACE INTO opp_vmap(bvid, gid, method, conf) VALUES (?, ?, ?, ?)",
                      (bvid, gid, method, conf))
    return len(hits)


def apply_all(limit=3000):
    """给还没对应过的视频补上对应关系。返回处理条数。"""
    idx = build_dict()
    with db.session() as c:
        rows = c.execute(
            "SELECT v.bvid, v.title, v.tags, v.descr FROM opp_videos v "
            "LEFT JOIN opp_vmap m ON m.bvid = v.bvid WHERE m.bvid IS NULL LIMIT ?", (limit,)).fetchall()
    n = 0
    for r in rows:
        try:
            tags = json.loads(r["tags"]) if r["tags"] else []
        except ValueError:
            tags = []
        hits = match(r["title"], r["descr"], tags, idx)
        if hits:
            save_map(r["bvid"], hits)
            for gid, name, _m, _c in hits:
                ensure_game(gid, name)
            n += 1
    return n


# ---------------- 自动发现没识别出来的游戏名 ----------------

UNKNOWN_HOURS = 48
UNKNOWN_MIN_UP = 3


def update_unknown():
    """48 小时内，同一个《名字》或同一个标签出现在 ≥3 个不同 UP 主的跟踪视频里，就进待确认。"""
    idx = build_dict()
    since = int(time.time()) - UNKNOWN_HOURS * 3600
    with db.session() as c:
        rows = c.execute(
            "SELECT bvid, title, tags, mid FROM opp_videos WHERE first_seen >= ?", (since,)).fetchall()
    pool = {}
    for r in rows:
        for name in candidates(r["title"], _loads(r["tags"])):
            k = normalize(name)
            if not k or k in idx or len(k) < 2:
                continue
            e = pool.setdefault(k, {"name": name, "mids": set(), "bvids": []})
            e["mids"].add(str(r["mid"]))
            if len(e["bvids"]) < 20:
                e["bvids"].append(r["bvid"])
    n = 0
    for k, e in pool.items():
        if len(e["mids"]) < UNKNOWN_MIN_UP:
            continue
        with db.session() as c:
            c.execute("INSERT OR IGNORE INTO opp_unknown(name, first_ts, n_up, bvids, status) "
                      "VALUES (?, ?, ?, ?, 'pending')",
                      (e["name"], int(time.time()), len(e["mids"]),
                       json.dumps(e["bvids"], ensure_ascii=False)))
            c.execute("UPDATE opp_unknown SET n_up = ? WHERE name = ? AND status = 'pending'",
                      (len(e["mids"]), e["name"]))
        n += 1
    return n


def _loads(s):
    try:
        return json.loads(s) if s else []
    except ValueError:
        return []


def unknown_list():
    with db.session() as c:
        return [dict(r) for r in c.execute(
            "SELECT name, first_ts, n_up, status FROM opp_unknown ORDER BY n_up DESC LIMIT 100")]
