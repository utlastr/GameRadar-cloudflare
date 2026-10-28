# -*- coding: utf-8 -*-
"""关键词漏斗：热榜上的词 → 是不是游戏 → 老不老 → 新游戏候选 → 去各平台查。

判断顺序（从便宜、准的开始）：
  1. 你在面板上点过的（是游戏 / 玩法词 / 不是游戏 / 老游戏）——永远以你为准，存在 soc_terms.label。
  2. 老牌大游戏名单（config.KW_BIG_GAMES）：游戏，老。
  3. 跟雷达已有的游戏名对比（entities 表：Steam、TapTap、App Store、小游戏榜……）：
     名字完全一样 → 游戏；上线时间 > 1 年 → 老；词里包含某个已有游戏名（≥4 个字）→ 游戏相关。
  4. 词本身带游戏字样（手游、攻略、开服、联机、关卡……）→ 可能和游戏有关。
  5. 地名、直播里的非游戏分区名 → 不是游戏。
  6. 剩下判不出来的、以及"可能有关"的，有 AI 密钥就批量问 AI（一天跑一次，判过的词不再问），
     没有就留在"待判断"，等你点。
老不老另外看指数曲线：曲线开头 30 天的平均已经有最近 7 天的一半以上 → 老（早就火过）。

分出来的几栏：新游戏、玩法词、游戏相关（待核）、老游戏、不是游戏、待判断。
新游戏 + 玩法词 + 游戏相关 按热度排进关键词查询队列。
"""
import json
import re
import time

from .. import config, db
from ..sources.bili import normalize
from . import extract, settings, store

HINT = re.compile(r"游戏|手游|端游|页游|小游戏|网游|单机|steam|联机|开黑|通关|关卡|攻略|开服|新服|公测|内测|首测|"
                  r"测试服|抽卡|副本|皮肤|段位|上分|模拟器|大作战|消消|合成|塔防|肉鸽|roguelike|吃鸡|电竞|"
                  r"二次元|开放世界|mmorpg|moba|fps|rpg|slg|三消|放置|挂机|割草|搭子|玩法|主播|实况|速通|彩蛋|"
                  r"boss|npc|版本更新|赛季", re.I)
NOT_HINT = re.compile(r"回应|官宣|离婚|结婚|去世|逝世|警方|通报|地震|台风|暴雨|降温|高考|考研|股市|A股|楼市|"
                      r"外交部|总统|总理|演唱会|电视剧|综艺|电影|票房|奥运|世界杯|NBA|CBA|国足|天气|疫情", re.I)
YEAR = 365 * 86400

_names = {"ts": 0, "exact": {}, "long": []}


def _load_names():
    """雷达已有的游戏名（归一化后）→ [(平台:编号, 上线时间)]。30 分钟刷新一次。"""
    if time.time() - _names["ts"] < 1800 and _names["exact"]:
        return _names
    exact = {}
    with db.session() as c:
        rows = c.execute("SELECT source, entity, name, extra FROM entities").fetchall()
    for s, e, n, x in rows:
        try:
            xx = json.loads(x) if x else {}
        except ValueError:
            xx = {}
        if xx.get("generic"):
            continue
        rel = xx.get("release_ts") or xx.get("released_time") or xx.get("created_ts") or None
        for nm in (n, xx.get("name_cn"), xx.get("full_name")):
            k = normalize(nm or "")
            if len(k) >= 2:
                exact.setdefault(k, []).append(("%s:%s" % (s, e), rel))
    longs = sorted((k for k in exact if len(k) >= 4), key=len, reverse=True)[:20000]
    _names.update(ts=time.time(), exact=exact, long=longs)
    return _names


def _big():
    return [(normalize(b), b) for b in getattr(config, "KW_BIG_GAMES", []) if normalize(b)]


def _places():
    return set(getattr(config, "KW_PLACES", set())) | set(getattr(config, "NON_GAME_CATEGORIES", set()))


def _age_from_series(term):
    """指数曲线：开头 30 天平均 ≥ 最近 7 天的一半 → 老（早就有热度了）；曲线太短返回 None。"""
    return None


def _unverified_age_from_series(term):
    with db.session() as c:
        rows = c.execute("SELECT platform, metric, day, value FROM soc_series WHERE term = ? ORDER BY day",
                         (term,)).fetchall()
    by = {}
    for p, m, d, v in rows:
        by.setdefault((p, m), []).append(v)
    for (p, m), vals in by.items():
        if len(vals) < 60:
            continue
        head = sum(vals[:30]) / 30
        tail = sum(vals[-7:]) / 7
        if tail > 0 and head >= 0.5 * tail:
            return "old", "%s 曲线开头 30 天就已经有最近 7 天的 %.0f%%（早就火过）" % (p, 100 * head / tail)
        if tail > 0:
            return "new", "%s 曲线是最近才起来的（开头 30 天只有最近 7 天的 %.0f%%）" % (p, 100 * head / tail)
    return None


def classify(term, norm, label=None, ai=None):
    """返回 (cls, cls_why, age, age_why)。cls: game / gameplay / related / not / unknown；age: old / new / ''。"""
    if label in ("game", "gameplay", "not", "old"):
        if label == "old":
            return "game", "你标的", "old", "你标的老游戏"
        cls, why = label, "你标的"
    else:
        cls, why = None, ""
    age, age_why = "", ""
    for b, raw in _big():
        if b and b in norm:
            return cls or "game", why or "老牌大游戏名单（%s）" % raw, "old", "老牌大游戏（%s）" % raw
    names = _load_names()
    hits = names["exact"].get(norm)
    if hits:
        if not cls:
            cls, why = "game", "和雷达已有的游戏同名（%s）" % hits[0][0]
        rels = [r for _, r in hits if r]
        if rels:
            rel = min(rels)
            days = (time.time() - rel) / 86400
            age = "old" if days > 365 else "new"
            age_why = "上线 %d 天（%s）" % (days, hits[0][0])
    elif not cls and len(norm) >= 4:
        for k in names["long"]:
            if len(k) < len(norm) and k in norm:
                cls, why = "related", "词里包含已有游戏名「%s」" % k
                break
    if not cls and isinstance(ai, dict) and ai.get("c") in ("game", "gameplay", "related", "not"):
        cls, why = ai["c"], "AI 判断" + ("：%s" % ai["name"] if ai.get("name") else "")
        if ai.get("old") is True and not age:
            age, age_why = "old", "AI 认为是一年以上的老游戏"
    if not cls:
        if term in _places():
            cls, why = "not", "地名或非游戏分区名"
        elif NOT_HINT.search(term):
            cls, why = "not", "像新闻/娱乐话题"
        elif HINT.search(term):
            cls, why = "related", "词里有游戏字样"
        else:
            cls, why = "unknown", "判不出来"
    if cls in ("game", "gameplay", "related") and not age:
        s = _age_from_series(term)
        if s:
            age, age_why = s
    return cls, why, age, age_why


def refresh(hours=36):
    """把最近的热榜词并进 soc_terms，重新分类。"""
    since = int(time.time() - hours * 3600)
    with db.session() as c:
        rows = c.execute("SELECT rowid, platform, job, text, rank, value, ts FROM soc_hot h WHERE ts >= ? "
                         f"AND {store.trusted_hot_sql()}",
                         (since,)).fetchall()
        old = {r["norm"]: dict(r) for r in c.execute("SELECT * FROM soc_terms")}
    agg = {}
    for _, p, job, text, rank, value, ts in rows:
        t = (text or "").strip().lstrip("#").strip()
        if not t or len(t) > 40 or extract.junk_text(t):
            continue
        k = normalize(t)
        if len(k) < 2:
            continue
        a = agg.setdefault(k, {"term": t, "sources": set(), "rank": rank or 999, "value": value, "ts": ts,
                               "first": ts})
        a["sources"].add(job)
        a["rank"] = min(a["rank"], rank or 999)
        if value is not None and (a["value"] is None or value > a["value"]):
            a["value"] = value
        a["ts"] = max(a["ts"], ts)
        a["first"] = min(a["first"], ts)
    for w in settings.get("SOCIAL_WATCH_TERMS") or []:
        k = normalize(w)
        if k:
            a = agg.setdefault(k, {"term": w, "sources": set(), "rank": 0, "value": None, "ts": int(time.time()),
                                   "first": int(time.time())})
            a["sources"].add("watch")
    now = int(time.time())
    out = []
    for k, a in agg.items():
        o = old.get(k) or {}
        try:
            srcs = a["sources"]
        except ValueError:
            srcs = a["sources"]
        try:
            ai = json.loads(o["ai"]) if o.get("ai") else None
        except ValueError:
            ai = None
        cls, why, age, age_why = classify(a["term"], k, o.get("label"), ai)
        out.append((k, o.get("term") or a["term"], min(o.get("first_ts") or a["first"], a["first"]),
                    max(o.get("last_ts") or 0, a["ts"]), (o.get("seen") or 0) + 1, json.dumps(sorted(srcs)),
                    a["rank"], a["value"],
                    cls, why, age, age_why, o.get("label"), o.get("ai"), o.get("ai_ts"), now))
    if out:
        with db.session() as c:
            c.executemany("INSERT OR REPLACE INTO soc_terms VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", out)
    return len(out)


def set_label(term, label):
    """你在面板上点的：game / gameplay / not / old / ''（清掉）。"""
    k = normalize(term)
    if not k:
        return {"ok": False}
    with db.session() as c:
        r = c.execute("SELECT norm FROM soc_terms WHERE norm = ?", (k,)).fetchone()
        if not r:
            c.execute("INSERT INTO soc_terms(norm, term, first_ts, last_ts, seen, sources, best_rank, updated) "
                      "VALUES (?,?,?,?,?,?,?,?)", (k, term, int(time.time()), int(time.time()), 0, "[]", 999,
                                                   int(time.time())))
        c.execute("UPDATE soc_terms SET label = ? WHERE norm = ?", (label or None, k))
        row = dict(c.execute("SELECT * FROM soc_terms WHERE norm = ?", (k,)).fetchone())
    try:
        ai = json.loads(row["ai"]) if row.get("ai") else None
    except ValueError:
        ai = None
    cls, why, age, age_why = classify(row["term"], k, label or None, ai)
    with db.session() as c:
        c.execute("UPDATE soc_terms SET cls = ?, cls_why = ?, age = ?, age_why = ?, updated = ? WHERE norm = ?",
                  (cls, why, age, age_why, int(time.time()), k))
    return {"ok": True, "cls": cls, "age": age}


# ---------------- AI ----------------
AI_PROMPT = ("你是手机游戏行业分析师。下面是中国和海外短视频平台热榜上的词。逐个判断：\n"
             "c 取值：game（是某个具体游戏的名字或简称）、gameplay（是一种游戏玩法/小游戏类型，如\"抓大鹅\"\"合成大西瓜\"）、"
             "related（和游戏有关但不是游戏名，如游戏角色、赛事、主播、游戏梗）、not（和游戏无关）。\n"
             "old：如果是游戏且据你所知上线已超过一年，填 true，否则 false。name：如果是游戏，写它的正式名字，否则空。\n"
             "只输出 JSON：{\"items\":[{\"w\":\"原词\",\"c\":\"game\",\"old\":false,\"name\":\"\"}]}")


def ai_round():
    """把"待判断""游戏相关"里还没问过 AI 的词一批批问完。返回问了几个。"""
    if not str(getattr(config, "AI_KEY", "") or "").strip():
        raise RuntimeError("没填 AI_KEY，判不出来的词留在“待判断”，可以自己点")
    # v80：一天跑一次（什么时候跑由 social/__init__.py 管），把还没判过的词一批一批问完；判过的永远不再问
    total = 0
    for _ in range(max(1, int(settings.get("SOCIAL_AI_BATCHES") or 20))):
        with db.session() as c:
            rows = c.execute("SELECT norm, term FROM soc_terms WHERE label IS NULL AND ai IS NULL "
                             "AND cls IN ('unknown', 'related') AND last_ts >= ? "
                             "ORDER BY best_rank, best_value DESC",
                             (int(time.time() - 3 * 86400),)).fetchall()
        trusted = trusted_norms()
        rows = [r for r in rows if r["norm"] in trusted][:40]
        if not rows:
            break
        total += _ai_batch(rows)
    return total


def _ai_batch(rows):
    """问一批（≤40 个词），结果写回 soc_terms（没回的也记成问过，不重复问）。返回这批几个词。"""
    from .. import ai as ai_mod
    words = [t for _, t in rows]
    body = {"messages": [{"role": "system", "content": AI_PROMPT},
                         {"role": "user", "content": "\n".join(words)}],
            "temperature": 0.1, "max_tokens": 8192}
    try:
        d = ai_mod._post(body)
    except ValueError:
        raise RuntimeError("AI 接口回的不是 JSON（AI_URL 可能填成了网页地址，或者中转站这次出错了）；"
                           "看看必看页的 AI 打分是不是也失败")
    # v79：和必看的 AI 一样把各种"没回正文"的情况说清楚（以前只报 Expecting value）
    if not isinstance(d, dict):
        raise RuntimeError("AI 接口返回的不是数据（检查 AI_URL）：%s" % str(d)[:80])
    if d.get("error") and not d.get("choices"):
        er = d["error"]
        raise RuntimeError("AI 接口报错：" + str(er.get("message") if isinstance(er, dict) else er)[:150])
    choice = (d.get("choices") or [{}])[0]
    msg = (choice.get("message") or {}).get("content") or ""
    if not msg.strip():
        why = "回答被截断（token 上限）" if choice.get("finish_reason") == "length" else "返回了空正文"
        raise RuntimeError("AI %s；模型 %s" % (why, body.get("model")))
    m = re.search(r"\{.*\}", msg, re.S)
    try:
        res = json.loads(m.group(0) if m else msg)
    except ValueError:
        # 有的模型不认 JSON 格式要求，按行回"词 / 类别"也接受
        res = {"items": []}
        for line in msg.splitlines():
            parts = re.split(r"[\t|,，：:]+", line.strip())
            if len(parts) >= 2 and parts[1].strip() in ("game", "gameplay", "related", "not"):
                res["items"].append({"w": parts[0].strip(), "c": parts[1].strip()})
        if not res["items"]:
            raise RuntimeError("AI 回的不是规定格式：" + msg[:80])
    if isinstance(res, list):
        res = {"items": res}
    by = {}
    for it in res.get("items") or []:
        w = str(it.get("w") or "")
        if w:
            by[normalize(w)] = {"c": str(it.get("c") or ""), "old": bool(it.get("old")),
                                "name": str(it.get("name") or "")[:60]}
    now = int(time.time())
    with db.session() as c:
        for k, t in rows:
            a = by.get(k) or {"c": "", "old": None, "name": ""}
            c.execute("UPDATE soc_terms SET ai = ?, ai_ts = ? WHERE norm = ?", (json.dumps(a, ensure_ascii=False), now, k))
    return len(rows)


# ---------------- 给面板和查询队列用 ----------------

BUCKETS = [("new", "新游戏"), ("gameplay", "玩法词"), ("related", "游戏相关（待核）"), ("old", "老游戏"),
           ("unknown", "待判断"), ("not", "不是游戏")]


def bucket_of(r):
    if r["cls"] == "not":
        return "not"
    if r["cls"] == "unknown":
        return "unknown"
    if r["age"] == "old":
        return "old"
    if r["cls"] == "game":
        return "new" if r["age"] == "new" else "related"
    return r["cls"] or "unknown"


def buckets(days=3, limit=80):
    since = int(time.time() - days * 86400)
    with db.session() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM soc_terms WHERE last_ts >= ? OR label IS NOT NULL ORDER BY best_rank, best_value DESC",
            (since,))]
    trusted = trusted_norms()
    out = {k: [] for k, _ in BUCKETS}
    for r in rows:
        if not r.get("label") and r["norm"] not in trusted:
            continue
        b = bucket_of(r)
        if len(out[b]) >= limit:
            continue
        try:
            r["sources"] = json.loads(r.get("sources") or "[]")
        except ValueError:
            r["sources"] = []
        r["source_names"] = [store.JOB_NAMES.get(s, "你加的" if s == "watch" else s) for s in r["sources"]]
        r.pop("ai", None)
        out[b].append(r)
    return out


def queue():
    """要去各平台查的词，按优先级：你加的 → 新游戏 → 玩法词；同一栏按上了几个榜、名次。

    v79：「游戏相关（待核）」不再去查（"新赛季 我要认真了"这种只是带了"赛季"两个字的话题，查了白花额度），
    你点成"是游戏/玩法词"或 AI 判成游戏后才查。超过 20 个字的也不查（是句子不是名字）。
    """
    watch = [w for w in (settings.get("SOCIAL_WATCH_TERMS") or []) if w]
    since = int(time.time() - 3 * 86400)
    with db.session() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM soc_terms WHERE last_ts >= ?", (since,))]
    pri = {"new": 0, "gameplay": 1}
    trusted = trusted_norms()
    cand = []
    for r in rows:
        if not r.get("label") and r["norm"] not in trusted:
            continue
        b = bucket_of(r)
        if b not in pri or len(r["term"] or "") > 20:
            continue
        try:
            n_src = len(json.loads(r.get("sources") or "[]"))
        except ValueError:
            n_src = 1
        cand.append((pri[b], -n_src, r.get("best_rank") or 999, -(r.get("last_ts") or 0), r["term"]))
    cand.sort()
    # v81：雷达排名靠前的游戏名（analyze 写的）和漏斗里的词一个一个交替排
    radar = [t for t in (db.kv_get("radar_social_terms", []) or []) if t]
    fun = [c[-1] for c in cand]
    mixed = [x for pair in zip(radar, fun) for x in pair] + radar[len(fun):] + fun[len(radar):]
    seen, out = set(), []
    for t in watch + mixed:
        k = normalize(t)
        if k and k not in seen:
            seen.add(k)
            out.append(t)
    return out


def trusted_norms():
    with db.session() as c:
        words = c.execute("SELECT DISTINCT text FROM soc_hot h WHERE ts>=? "
                          f"AND {store.trusted_hot_sql()}",
                          (time.time()-3*86400,)).fetchall()
    return {normalize(row[0]) for row in words} | {
        normalize(t) for t in settings.get("SOCIAL_WATCH_TERMS") or []}
