"""Local, incremental topic discovery. Names remain candidates, not invented game IDs."""
import json
import re
import time
import unicodedata
from datetime import date, timedelta
from html import unescape

from . import bili_term_filter as term_filter, config, db
from .bili_realtime_client import normalize

SCHEMA = """
CREATE TABLE IF NOT EXISTS bd_terms(
 norm TEXT PRIMARY KEY,name TEXT,status TEXT DEFAULT 'pending',
 seed_videos INTEGER DEFAULT 0,seed_authors INTEGER DEFAULT 0,
 first_day TEXT,last_day TEXT,scanned INTEGER DEFAULT 0,known_ids TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS bd_word_hits(
 norm TEXT,bvid TEXT,source TEXT,PRIMARY KEY(norm,bvid,source));
CREATE INDEX IF NOT EXISTS bd_hits_bvid ON bd_word_hits(bvid);
CREATE INDEX IF NOT EXISTS bd_terms_catalog ON bd_terms(seed_videos DESC,norm);
CREATE TABLE IF NOT EXISTS bd_term_dirty(norm TEXT PRIMARY KEY);
"""
_BRACKETS = re.compile(
    r"《([^《》]{2,60})》|【([^【】]{2,60})】|\[([^\[\]]{2,60})\]|"
    r"（([^（）]{2,60})）|\(([^()]{2,60})\)|「([^「」]{2,60})」|"
    r"『([^『』]{2,60})』|〈([^〈〉]{2,60})〉")
_SPLIT = re.compile(r"[/|、，,;；]")
_DATE = re.compile(r"(?:20\d{2}[年./-]?)?\d{1,2}[月./-]\d{1,2}日?(?:\d{1,2}[:时]\d{1,2})?")
_NUMBER = re.compile(r"(?:第)?[\d零一二三四五六七八九十百]+(?:期|集|章|回|部|p|part)?", re.I)
_FORMAT = re.compile(r"直播|录播|录屏|回放|弹幕版|清屏版|创作激励|创作者|赛季|打卡|播放量")
_NOISE = set("""
游戏 单机游戏 网络游戏 手机游戏 主机游戏 电子竞技 steam steam游戏 pc pc游戏
独立游戏 单机联机 联机 游戏实况 实况解说 游戏解说 游戏试玩 游戏推荐 新游戏 新游
教程 攻略 教程攻略 试玩 测评 demo 原创 娱乐 搞笑 视频 日常 生活记录 记录
直播 直播录像 直播回放 录播 录屏 弹幕 必剪创作 热门 热门游戏 热门视频 推荐宝藏游戏
mod 修改器 演示 速通 通关 vup vtuber vr vtubers 虚拟主播 虚拟up主 虚拟偶像
动作游戏 策略游戏 模拟经营 steam平台 游戏内容分享 寻找宝藏游戏 之弈 行动 世界
""".split())


def aliases():
    out = {}
    for name, others in (getattr(config, "BVIDEO_NAME_ALIASES", {}) or {}).items():
        key = normalize(name)
        if not key:
            continue
        for value in [name] + (others if isinstance(others, list) else [others]):
            alias = normalize(str(value))
            if alias:
                out[alias] = (key, name)
    return out


def blacklisted():
    return {normalize(s) for s in list(getattr(config, "KW_BIG_GAMES", [])) +
            list(getattr(config, "BVIDEO_WORD_BLACKLIST", [])) if normalize(s)}


def candidates(title, tags, owner_name=""):
    """Keep complete names; a rejected word never vetoes the surrounding video."""
    blocked = blacklisted()
    people = {normalize(s) for s in getattr(config, "BVIDEO_PERSON_NAMES", [])}
    if owner_name:
        people.add(normalize(owner_name))
    alias_map = aliases()
    raw = []
    for match in _BRACKETS.finditer(unescape(title or "")):
        value = next(g for g in match.groups() if g)
        raw.extend((s, "bracket") for s in dict.fromkeys([value] + _SPLIT.split(value)))
    for tag in tags or []:
        if not tag.startswith("发现《"):
            raw.append((tag, "tag"))
    found = {}
    for value, source in raw:
        value = value.strip()
        key = normalize(value)
        if not 2 <= len(key) <= 40 or key in _NOISE or key in people or term_filter.exact_kind(key):
            continue
        if _DATE.fullmatch(value) or _NUMBER.fullmatch(key) or re.fullmatch(r"\d{6,}", key):
            continue
        if _FORMAT.fullmatch(value) or "http" in key:
            continue
        if key in blocked:
            continue
        if re.fullmatch(r"[a-z]+", key) and len(key) < 3:
            continue
        key, value = alias_map.get(key, (key, value))
        if key not in blocked and key not in people:
            found[(key, source)] = value
    return [(key, value, source) for (key, source), value in found.items()]


def seed(c, record):
    bvid, day = record["bvid"], record["date"]
    dirty = {row[0] for row in c.execute(
        "SELECT norm FROM bd_word_hits WHERE bvid=? AND source IN ('bracket','tag')", (bvid,))}
    c.execute("DELETE FROM bd_word_hits WHERE bvid=? AND source IN ('bracket','tag')", (bvid,))
    for key, name, source in candidates(record.get("title"), record.get("tags"), record.get("owner_name", "")):
        c.execute("""INSERT INTO bd_terms(norm,name,first_day,last_day) VALUES(?,?,?,?)
                     ON CONFLICT(norm) DO UPDATE SET first_day=MIN(first_day,excluded.first_day),
                     last_day=MAX(last_day,excluded.last_day)""", (key, name, day, day))
        c.execute("INSERT OR IGNORE INTO bd_word_hits VALUES(?,?,?)", (key, bvid, source))
        dirty.add(key)
    c.executemany("INSERT OR IGNORE INTO bd_term_dirty VALUES(?)", [(key,) for key in dirty])


def refresh(c, norms=None):
    values = tuple(dict.fromkeys(norms)) if norms is not None else ()
    if norms is not None and not values:
        return
    placeholders = ",".join("?" for _ in values)
    known = {}
    alias_map = aliases()
    for row in c.execute("SELECT source,entity,name,extra FROM entities WHERE source IN "
                         "('steam','taptap','tapmaker','itch','appcn','appus')"):
        try:
            extra = json.loads(row["extra"] or "{}")
        except (ValueError, TypeError):
            extra = {}
        if not isinstance(extra, dict):
            extra = {}
        if extra.get("generic") or extra.get("type") in ("dlc", "demo", "music", "video"):
            continue
        for name in (row["name"], extra.get("name_cn"), extra.get("full_name")):
            key = normalize(name)
            key = alias_map.get(key, (key, None))[0]
            if key:
                known.setdefault(key, set()).add(row["source"] + ":" + str(row["entity"]))
    counts = {r["norm"]: r for r in c.execute("""
        SELECT h.norm,COUNT(DISTINCT h.bvid) n,
        COUNT(DISTINCT CASE WHEN v.owner_id!='' THEN v.owner_id END) authors
        FROM bd_word_hits h JOIN bd_videos v ON v.bvid=h.bvid
        WHERE h.source IN ('bracket','tag') AND v.detail_state!='archived'""" +
        (f" AND h.norm IN ({placeholders})" if norms is not None else "") +
        " GROUP BY h.norm", values)}
    blocked = blacklisted()
    people = {normalize(n) for n in getattr(config, "BVIDEO_PERSON_NAMES", [])}
    judgments = {r["norm"]: r for r in c.execute(
        "SELECT * FROM bd_term_judgments WHERE version=?" +
        (f" AND norm IN ({placeholders})" if norms is not None else ""),
        (term_filter.VERSION,) + values)}
    rows = c.execute("SELECT * FROM bd_terms" +
                     (f" WHERE norm IN ({placeholders})" if norms is not None else ""), values).fetchall()
    for row in rows:
        key, count = row["norm"], counts.get(row["norm"])
        n, authors = (count["n"], count["authors"]) if count else (0, 0)
        ids = sorted(known.get(key, set()))
        state = "pending"
        kind, why, origin = "unknown", "", ""
        rule = term_filter.exact_kind(key)
        judged = judgments.get(key)
        if key in blocked or key in people or key in _NOISE or rule:
            state = "blocked"
            kind = rule or ("game" if key in blocked else
                            "person" if key in people else "common")
            why = "已有游戏排除名单" if key in blocked else "完整词过滤"
            origin = "rule"
        elif ids:
            state = "known"
            kind, why, origin = "game", "匹配已有游戏作品", "catalog"
        else:
            if judged:
                kind, why, origin = judged["kind"], judged["reason"], "ai"
            if kind not in ("game", "unknown"):
                state = "blocked"
            elif n >= config.BVIDEO_CANDIDATE_MIN_VIDEOS and authors >= config.BVIDEO_CANDIDATE_MIN_AUTHORS:
                state = "candidate"
        encoded_ids = json.dumps(ids)
        current = tuple(row[name] for name in (
            "status", "seed_videos", "seed_authors", "known_ids", "kind", "kind_reason", "kind_source"))
        if current == (state, n, authors, encoded_ids, kind, why, origin):
            continue
        c.execute("""UPDATE bd_terms SET status=?,seed_videos=?,seed_authors=?,known_ids=?,
                     scanned=CASE WHEN status!=? OR kind!=? THEN 0 ELSE scanned END,
                     scan_after=CASE WHEN status!=? OR kind!=? THEN '' ELSE scan_after END,
                     kind=?,kind_reason=?,kind_source=? WHERE norm=?""",
                  (state, n, authors, encoded_ids, state, kind, state, kind,
                   kind, why, origin, key))
    c.executemany("DELETE FROM bd_term_dirty WHERE norm=?", [(row["norm"],) for row in rows])


def refresh_dirty(limit=128, sweep=0):
    """Only changed evidence is recounted; each batch releases the shared DB lock."""
    with db.session() as c:
        norms = [row[0] for row in c.execute("SELECT norm FROM bd_term_dirty LIMIT ?", (limit,))]
        revisit = []
        if sweep:
            saved = c.execute("SELECT v FROM kv WHERE k='bili_terms_refresh_cursor'").fetchone()
            cursor = json.loads(saved[0]) if saved else ""
            revisit = [row[0] for row in c.execute(
                "SELECT norm FROM bd_terms WHERE norm>? ORDER BY norm LIMIT ?", (cursor, sweep))]
            c.execute("INSERT OR REPLACE INTO kv VALUES('bili_terms_refresh_cursor',?)",
                      (json.dumps(revisit[-1] if revisit else ""),))
        refresh(c, norms + revisit)
    return len(norms)


class Matcher:
    """Compiled multi-pattern regex batches, not a Python keyword loop per title."""
    def __init__(self, words):
        mapping = {word: word for word in words}
        mapping.update({a: target for a, (target, _) in aliases().items() if target in words})
        self.mapping = mapping
        self.patterns = []
        values = sorted(mapping, key=lambda w: (-len(w), w))
        for start in range(0, len(values), 256):
            branches = []
            for word in values[start:start + 256]:
                escaped = r"\x00*".join(re.escape(char) for char in word)
                if word.isascii():
                    escaped = r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])"
                branches.append(escaped)
            self.patterns.append(re.compile("(?=(" + "|".join(branches) + "))"))

    def find(self, title):
        raw = unicodedata.normalize("NFKC", unescape(title or "")).lower()
        # Preserve word boundaries while allowing spaces/punctuation inside game names.
        text = "\x00".join(normalize(part) for part in re.split(r"[\W_]+", raw))
        return {self.mapping[m.group(1).replace("\x00", "")]
                for pattern in self.patterns for m in pattern.finditer(text)}


def rescan(c, today, game_only=False, title_limit=None, refresh_terms=True):
    if refresh_terms:
        refresh(c)
    active = {r["norm"]: r for r in c.execute(
        "SELECT norm,scanned,scan_after FROM bd_terms WHERE status IN ('candidate','known')" +
        (" AND kind='game'" if game_only else ""))}
    if not active:
        return
    cutoff = (date.fromisoformat(today) - timedelta(days=config.BVIDEO_TITLE_KEEP_DAYS)).isoformat()
    # Fresh titles take priority; backfills have separate per-word durable cursors.
    budget = max(2, int(title_limit)) if title_limit is not None else None
    remaining = budget // 2 if budget is not None else None
    matcher = Matcher(set(active))
    while remaining is None or remaining > 0:
        size = min(500, remaining) if remaining is not None else 500
        rows = c.execute("""SELECT bvid,title FROM bd_videos WHERE day>=? AND detail_state!='archived'
            AND title_scanned=0 ORDER BY day DESC,bvid DESC LIMIT ?""", (cutoff, size)).fetchall()
        for row in rows:
            c.executemany("INSERT OR IGNORE INTO bd_word_hits VALUES(?,?,'title')",
                          [(key, row["bvid"]) for key in matcher.find(row["title"])])
        c.executemany("UPDATE bd_videos SET title_scanned=1 WHERE bvid=?",
                      [(row["bvid"],) for row in rows])
        if remaining is not None:
            remaining -= len(rows)
        if len(rows) < size:
            break
    groups = {}
    for key, row in active.items():
        if not row["scanned"]:
            groups.setdefault(row["scan_after"] or "", set()).add(key)
    remaining = budget - budget // 2 if budget is not None else None
    for cursor, words in sorted(groups.items()):
        matcher = Matcher(words)
        while remaining is None or remaining > 0:
            size = min(500, remaining) if remaining is not None else 500
            rows = c.execute("""SELECT bvid,title FROM bd_videos
                WHERE day>=? AND detail_state!='archived' AND bvid>? ORDER BY bvid LIMIT ?""",
                (cutoff, cursor, size)).fetchall()
            for row in rows:
                c.executemany("INSERT OR IGNORE INTO bd_word_hits VALUES(?,?,'title')",
                              [(key, row["bvid"]) for key in matcher.find(row["title"])])
            if rows:
                cursor = rows[-1]["bvid"]
            finished = len(rows) < size
            c.executemany("UPDATE bd_terms SET scan_after=?,scanned=? WHERE norm=?",
                          [(cursor, int(finished), key) for key in words])
            if remaining is not None:
                remaining -= len(rows)
            if finished:
                break


def topics(c, day=None, limit=200, game_only=False):
    where = "WHERE d.day=?" if day else ""
    values = [day] if day else []
    values.append(limit)
    return [dict(r) for r in c.execute("""
        SELECT t.norm,t.name,t.status,t.known_ids,t.kind,t.kind_reason,t.kind_source,
        MIN(v.day) first_day,MAX(v.day) last_day,
        COUNT(*) videos,COUNT(DISTINCT NULLIF(v.owner_id,'')) authors,
        SUM(CASE WHEN v.detail_state='ok' THEN 1 ELSE 0 END) tagged,
        SUM(v.views) views,SUM(v.likes) likes,SUM(v.coins) coins,
        SUM(v.favorites) favorites,SUM(v.shares) shares,SUM(v.replies) replies
        FROM (SELECT DISTINCT h.norm,h.bvid FROM bd_word_hits h
              JOIN bd_videos d ON d.bvid=h.bvid """ + where + """) h
        JOIN bd_terms t ON t.norm=h.norm JOIN bd_videos v ON v.bvid=h.bvid
        WHERE t.status IN ('candidate','known') AND v.detail_state!='archived' """ +
        ("AND t.kind='game' " if game_only else "") + """
        GROUP BY t.norm ORDER BY videos DESC,t.norm LIMIT ?""", values)]


_archive_cache = None


def _archive_index(c):
    from collections import defaultdict

    global _archive_cache
    stamp = time.monotonic()
    if _archive_cache and _archive_cache[0] == config.DB_PATH and stamp - _archive_cache[1] < 30:
        return _archive_cache[2:]
    names = defaultdict(set)
    representatives = {}
    for row in c.execute("""SELECT g.gid,g.name,m.member,m.name member_name,
            e.extra,l.aliases FROM games g JOIN game_members m ON m.gid=g.gid
            LEFT JOIN entities e ON e.source=substr(m.member,1,instr(m.member,':')-1)
                AND e.entity=substr(m.member,instr(m.member,':')+1)
            LEFT JOIN labels l ON l.member=m.member
            ORDER BY CASE WHEN e.source IN ('steam','taptap','tapmaker','itch','appcn','appus')
                THEN 0 WHEN e.source IS NOT NULL THEN 1 ELSE 2 END,m.member"""):
        gid = row["gid"]
        representatives.setdefault(gid, row["member"])
        values = [row["name"], row["member_name"]]
        for encoded, field in ((row["extra"], "extra"), (row["aliases"], "aliases")):
            try:
                parsed = json.loads(encoded or ("{}" if field == "extra" else "[]"))
            except (TypeError, ValueError):
                continue
            if field == "extra" and isinstance(parsed, dict):
                values.extend(parsed.get(k) for k in ("name_cn", "name_en", "full_name", "name_local"))
            elif field == "aliases" and isinstance(parsed, list):
                values.extend(parsed)
        for name in values:
            if isinstance(name, str) and normalize(name):
                names[normalize(name)].add(gid)
    configured = aliases()
    for alias, (key, _) in configured.items():
        if key in names:
            names[alias].update(names[key])
    _archive_cache = (config.DB_PATH, stamp, names, representatives)
    return names, representatives


def archive_links(c, rows):
    """Link exact archive names, never substrings or ambiguous homonyms."""
    names, representatives = _archive_index(c)
    for row in rows:
        matches = names.get(normalize(row.get("name", "")), set())
        if len(matches) == 1:
            gid = next(iter(matches))
            row["archive"] = {"gid": gid, "member": representatives[gid]}
    return rows


def matched_catalogue():
    """Existing exact game matches, independent of growth-history eligibility."""
    from . import bili_daily, db

    bili_daily.init()
    with db.session() as c:
        names, representatives = _archive_index(c)
        rows = []
        for record in c.execute(
                "SELECT norm,name,status,kind,seed_videos,seed_authors,first_day,last_day "
                "FROM bd_terms ORDER BY norm"):
            matches = names.get(normalize(record["name"]), set())
            if len(matches) != 1:
                continue
            gid = next(iter(matches))
            row = dict(record)
            row["archive"] = {"gid": gid, "member": representatives[gid]}
            rows.append(row)
    return {"rows": rows, "total": len(rows), "scope": "exact_archive_matches",
            "counters": "Discovery evidence, not daily playback growth"}


def catalogue(query="", status="all", page=1, page_size=100):
    """Browse saved extraction results without the daily game-only threshold."""
    from . import bili_daily, db
    if status not in ("all", "known", "candidate", "pending", "blocked"):
        raise ValueError("未知关键词状态")
    bili_daily.init()
    page_size = max(1, min(100, int(page_size)))
    page = max(1, int(page))
    query = (query or "").strip()[:100]
    clauses, values = [], []
    if query:
        # Search complete display names and normalized aliases; LIKE metacharacters are literal.
        def literal(value):
            return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        key = normalize(query)
        clauses.append("(name LIKE ? ESCAPE '\\'" +
                       (" OR norm LIKE ? ESCAPE '\\'" if key else "") + ")")
        values.append(literal(query))
        if key:
            values.append(literal(key))
    with db.session() as c:
        counts = {r["status"]: r["n"] for r in c.execute(
            "SELECT status,COUNT(*) n FROM bd_terms GROUP BY status")}
        if status != "all":
            clauses.append("status=?")
            values.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = c.execute("SELECT COUNT(*) FROM bd_terms" + where, values).fetchone()[0]
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        rows = [dict(r) for r in c.execute(
            "SELECT norm,name,status,kind,kind_reason,kind_source,seed_videos,seed_authors,"
            "first_day,last_day FROM bd_terms" + where +
            " ORDER BY seed_videos DESC,norm LIMIT ? OFFSET ?",
            values + [page_size, (page - 1) * page_size])]
        for row in rows:
            row["sources"] = {r["source"]: r["n"] for r in c.execute(
                "SELECT source,COUNT(*) n FROM bd_word_hits WHERE norm=? GROUP BY source",
                (row["norm"],))}
        archive_links(c, rows)
    return {"total": total, "saved_total": sum(counts.values()), "status_counts": counts,
            "page": page, "pages": pages, "page_size": page_size,
            "query": query, "status": status, "rows": rows}
