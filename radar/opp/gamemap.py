"""视频 → 游戏档案成员。

v72 起不再自己建游戏编号（以前的 steam: / name: 编号和"先到先得"的名字词典都去掉了）：
- 简介里有 Steam 商店链接 → 成员 steam:appid（硬证据）；
- 标题书名号里的名字 → 成员 btopic:<归一化名字>（"B站 上叫这个名字的视频"）。
B站 话题和哪个商店游戏是同一个，由游戏档案（radar/archive.py）自动判断，连上以后
所有叫这个名字的视频都算进去。"标题里包含某个名字"只当线索，不算对应。
"""
import json
import time

from .. import config, db
from ..sources import bili

normalize = bili.normalize


def topic_member(name):
    n = normalize(name)
    return ("btopic:" + n) if len(n) >= 2 else None


def candidates(title, tags=None):
    """标题书名号 + 标签里的候选名字（去重保序）。"""
    out, seen = [], set()
    for x in bili.bracket_names(title) + [t for t in (tags or []) if t and 2 <= len(t.strip()) <= 20]:
        k = normalize(x)
        if k and k not in seen:
            seen.add(k)
            out.append(x.strip())
    return out


def match(title, descr, tags=None):
    """返回 [(成员, 显示名, 方法)]。方法：steam_link（硬）/ tag / title_bracket（话题）。"""
    hits, seen = [], set()

    def add(member, name, method):
        if member and member not in seen:
            seen.add(member)
            hits.append((member, name, method))

    for appid in bili.STEAM_LINK.findall(descr or ""):
        add("steam:%s" % appid, "steam:%s" % appid, "steam_link")
    for t in tags or []:
        add(topic_member(t), (t or "").strip(), "tag")
    for name in bili.bracket_names(title):
        add(topic_member(name), name, "title_bracket")
    return hits


def ensure_game(member, name):
    with db.session() as c:
        c.execute("INSERT OR IGNORE INTO opp_games(gid, name_cn, name_en, steam_appid, aliases, t0, state, state_ts, "
                  "source, big) VALUES (?, ?, '', ?, '[]', 0, '观察', ?, 'auto', 0)",
                  (member, name, member.split(":", 1)[1] if member.startswith("steam:") else "", int(time.time())))


def save_map(bvid, hits):
    with db.session() as c:
        for member, _name, method in hits:
            c.execute("INSERT OR REPLACE INTO opp_vmap(bvid, gid, method, conf) VALUES (?, ?, ?, NULL)",
                      (bvid, member, method))
    return len(hits)


def apply_all(limit=20000):
    """给还没对应过的视频补对应。v71 的对应（名字词典先到先得、标题包含）升级时整体重做一次，原始视频不动。"""
    if not db.kv_get("opp_map_v72"):
        with db.session() as c:
            c.execute("DELETE FROM opp_vmap")
            c.execute("DELETE FROM opp_games")
            c.execute("DELETE FROM opp_features")
        db.kv_set("opp_map_v72", 1)
    with db.session() as c:
        rows = c.execute(
            "SELECT v.bvid, v.title, v.tags, v.descr FROM opp_videos v "
            "LEFT JOIN opp_vmap m ON m.bvid = v.bvid WHERE m.bvid IS NULL LIMIT ?", (limit,)).fetchall()
    n = 0
    for r in rows:
        hits = match(r["title"], r["descr"], _loads(r["tags"]))
        if hits:
            save_map(r["bvid"], hits)
            n += 1
    _register_topics()
    return n


def _min_up():
    return int(getattr(config, "BILI_TOPIC_MIN_UP", 3) or 3)


def _register_topics():
    """同一个名字有足够多不同 UP 主在做，才当成一个 B站 话题登记（门槛 BILI_TOPIC_MIN_UP）。"""
    with db.session() as c:
        rows = c.execute(
            "SELECT m.gid, COUNT(DISTINCT v.mid) AS n_up, MIN(v.title) AS t FROM opp_vmap m "
            "JOIN opp_videos v ON v.bvid = m.bvid GROUP BY m.gid").fetchall()
    for r in rows:
        if r["gid"].startswith("steam:") or r["n_up"] >= _min_up():
            name = r["gid"].split(":", 1)[1]
            for b in bili.bracket_names(r["t"] or ""):
                if topic_member(b) == r["gid"]:
                    name = b
                    break
            ensure_game(r["gid"], name)


def update_unknown(hours=48):
    """最近 hours 小时里够多 UP 主在做、但还没并进任何商店游戏档案的 B站 名字。"""
    from .. import archive
    since = int(time.time()) - hours * 3600
    with db.session() as c:
        rows = c.execute(
            "SELECT m.gid, COUNT(DISTINCT v.mid) AS n_up, GROUP_CONCAT(v.bvid) AS bvids FROM opp_vmap m "
            "JOIN opp_videos v ON v.bvid = m.bvid WHERE m.gid LIKE 'btopic:%' AND v.first_seen >= ? "
            "GROUP BY m.gid", (since,)).fetchall()
    n = 0
    for r in rows:
        if r["n_up"] < _min_up() or archive.is_linked(r["gid"]):
            continue
        name = r["gid"].split(":", 1)[1]
        with db.session() as c:
            g = c.execute("SELECT name_cn FROM opp_games WHERE gid = ?", (r["gid"],)).fetchone()
            if g and g[0]:
                name = g[0]
            c.execute("INSERT OR IGNORE INTO opp_unknown(name, first_ts, n_up, bvids, status) "
                      "VALUES (?, ?, ?, ?, 'pending')",
                      (name, int(time.time()), r["n_up"], json.dumps((r["bvids"] or "").split(",")[:20])))
            c.execute("UPDATE opp_unknown SET n_up = ? WHERE name = ? AND status = 'pending'", (r["n_up"], name))
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
