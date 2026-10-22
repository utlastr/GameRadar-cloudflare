"""Observed Douyin hashtags and comparable counter windows, never search terms."""
import math
import time
from collections import defaultdict

from .. import db, match

SCHEMA = """
CREATE TABLE IF NOT EXISTS dy_tag_videos(
 vid TEXT PRIMARY KEY, author TEXT, source TEXT, scope TEXT, last_seen INTEGER);
CREATE TABLE IF NOT EXISTS dy_video_tags(
 vid TEXT, tag TEXT, PRIMARY KEY(vid,tag));
CREATE TABLE IF NOT EXISTS dy_counter_samples(
 vid TEXT, ts INTEGER, plays REAL, likes REAL, comments REAL, shares REAL, favs REAL,
 PRIMARY KEY(vid,ts));
CREATE INDEX IF NOT EXISTS dy_counter_time ON dy_counter_samples(ts);
"""
FIELDS = ("plays", "likes", "comments", "shares", "favs")
COUNTERS = ("play_count", "digg_count", "comment_count", "share_count", "collect_count")


def ensure():
    with db.session() as c:
        c.executescript(SCHEMA)


def hashtags(record):
    """Explicit platform topic objects only; no title tokenization or AI inference."""
    tags = set()
    for field, key in (("cha_list", "cha_name"), ("text_extra", "hashtag_name")):
        for item in record.get(field) or []:
            if not isinstance(item, dict):
                continue
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                tags.add(value.strip().lstrip("#"))
    return sorted(tags - {""})


def ingest(caps, now=None):
    ensure()
    now = int(time.time() if now is None else now)
    records = {}

    def visit(obj, source, depth=0):
        if depth > 18:
            return
        if isinstance(obj, list):
            for value in obj:
                visit(value, source, depth + 1)
        elif isinstance(obj, dict):
            vid = str(obj.get("aweme_id") or "")
            if vid.isdecimal() and any(k in obj for k in ("video", "statistics", "cha_list", "text_extra")):
                previous, _ = records.get(vid, ({}, source))
                merged = {**previous, **obj}
                merged["cha_list"] = [{"cha_name": t} for t in set(hashtags(previous) + hashtags(obj))]
                merged["statistics"] = {**(previous.get("statistics") or {}), **(obj.get("statistics") or {})}
                records[vid] = merged, source
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    visit(value, source, depth + 1)

    from .extract import junk_src
    for cap in caps:
        if not junk_src(cap.get("src")):
            visit(cap.get("obj"), cap.get("src") or "unknown")
    with db.session() as c:
        for vid, (record, source) in records.items():
            author = record.get("author") or {}
            author = str(author.get("uid") or author.get("sec_uid") or "") if isinstance(author, dict) else ""
            c.execute("INSERT OR REPLACE INTO dy_tag_videos VALUES (?,?,?,?,?)",
                      (vid, author, source, "unverified_sample", now))
            for tag in hashtags(record):
                c.execute("INSERT OR IGNORE INTO dy_video_tags VALUES (?,?)", (vid, tag))
            stats = record.get("statistics") or {}
            values = []
            for key in COUNTERS:
                value = stats.get(key)
                values.append(value if isinstance(value, (int, float)) and not isinstance(value, bool)
                              and math.isfinite(value) and value >= 0 else None)
            c.execute("INSERT OR REPLACE INTO dy_counter_samples VALUES (?,?,?,?,?,?,?)", (vid, now, *values))
    return len(records)


def _windows(points, now, field):
    """Two adjacent hour intervals, allowing <=5 minute sampling skew."""
    slots = []
    for target in (now - 7200, now - 3600, now):
        candidates = [p for p in points if abs(p[0] - target) <= 300 and p[field + 1] is not None]
        if not candidates:
            return None
        slots.append(min(candidates, key=lambda p: abs(p[0] - target)))
    a, b, c = slots
    if not a[0] < b[0] < c[0] or not a[field + 1] <= b[field + 1] <= c[field + 1]:
        return None
    return ((b[field + 1] - a[field + 1]) * 3600 / (b[0] - a[0]),
            (c[field + 1] - b[field + 1]) * 3600 / (c[0] - b[0]))


def report(now=None, names=None):
    ensure()
    now = int(time.time() if now is None else now)
    with db.session() as c:
        meta = {r[0]: dict(r) for r in c.execute("SELECT * FROM dy_tag_videos")}
        tags = defaultdict(set)
        for vid, tag in c.execute("SELECT vid,tag FROM dy_video_tags"):
            tags[tag].add(vid)
        series = defaultdict(list)
        for row in c.execute("SELECT * FROM dy_counter_samples WHERE ts>=? ORDER BY ts", (now - 7500,)):
            series[row[0]].append(tuple(row)[1:])
        try:
            from .incremental import ensure as ensure_publications
            ensure_publications()
            publication_meta = c.execute(
                "SELECT DISTINCT p.vid,p.author_id,p.url,p.last_seen FROM pub_records p "
                "JOIN pub_topics t ON p.platform=t.platform AND p.vid=t.vid "
                "WHERE p.platform='douyin'").fetchall()
            publication_tags = c.execute(
                "SELECT DISTINCT p.vid,t.topic FROM pub_topics t JOIN pub_records p "
                "ON p.platform=t.platform AND p.vid=t.vid WHERE p.platform='douyin'").fetchall()
            for vid, author, url, last_seen in publication_meta:
                meta.setdefault(vid, {"vid": vid, "author": author or "", "source": url or "publication",
                                      "scope": "unverified_sample", "last_seen": last_seen})
        except Exception:
            pass
        for vid, tag in publication_tags if 'publication_tags' in locals() else ():
            if vid in meta:
                tags[tag].add(vid)
        try:
            included = {r[0] for r in c.execute("SELECT vid FROM douyin_game_evidence WHERE decision='included'")}
        except Exception:
            included = set()
        if names is None:
            names = defaultdict(set)
            for gid, name in c.execute("SELECT gid,name FROM games"):
                names[match.normalize(name or "")].add(gid)
            for gid, name in c.execute("SELECT gid,name FROM game_members"):
                names[match.normalize(name or "")].add(gid)
    rows = []
    # Use the same measured video cohort on both sides; missing counters never become zero.
    for field, unit in enumerate(FIELDS):
        measured = {vid: pair for vid, pts in series.items() if (pair := _windows(pts, now, field)) is not None}
        for scope, allowed in (("observed_sample", set(meta)), ("verified_game_content", included)):
            cohort = {vid: pair for vid, pair in measured.items() if vid in allowed}
            total_before = sum(p[0] for p in cohort.values())
            total_after = sum(p[1] for p in cohort.values())
            for tag, vids in tags.items():
                selected = vids & allowed
                if not selected:
                    continue
                comparable = selected & cohort.keys()
                before = sum(cohort[v][0] for v in comparable) if comparable else None
                after = sum(cohort[v][1] for v in comparable) if comparable else None
                linked = names.get(match.normalize(tag), set())
                rows.append({"tag": tag, "scope": scope, "metric": unit, "videos": len(selected),
                             "authors": len({meta[v]["author"] for v in selected if meta[v]["author"]}),
                             "measured_videos": len(comparable), "previous_hour_rate": before,
                             "hour_rate": after, "growth_ratio": after / before if before else None,
                             "share": after / total_after if comparable and total_after else None,
                             "previous_share": before / total_before if comparable and total_before else None,
                             "acceleration": math.log1p(after) - math.log1p(before) if comparable else None,
                             "state": "measured" if comparable else "cold_start",
                             "archive": next(iter(linked)) if len(linked) == 1 else None,
                             "ambiguous_archives": len(linked) if len(linked) > 1 else 0,
                             "video_ids": sorted(selected)})
    rows.sort(key=lambda r: (r["acceleration"] is not None, r["acceleration"] or 0, r["hour_rate"] or 0), reverse=True)
    return {"rows": rows, "videos": len(meta), "tags": len(tags), "gaming_audience_available": False,
            "official_growth_chart_connected": False, "denominator": "matched_monitored_sample",
            "ranking_metric": "log_hour_rate_change", "full_platform": False}


def archive_videos(now, names):
    """Only verified gaming-content evidence can enter the archive score."""
    ensure()
    with db.session() as c:
        try:
            rows = c.execute("SELECT t.vid,t.tag FROM dy_video_tags t JOIN douyin_game_evidence e "
                             "ON e.vid=t.vid WHERE e.decision='included'").fetchall()
        except Exception:
            return {}, {}
        gids = defaultdict(set)
        for vid, tag in rows:
            matches = names.get(match.normalize(tag), set())
            if len(matches) == 1:
                gids[vid].update(matches)
        points = defaultdict(list)
        for vid, ts, plays, likes, comments, shares, favs in c.execute(
                "SELECT * FROM dy_counter_samples WHERE ts>=? ORDER BY ts", (now - 14 * 86400,)):
            if vid in gids:
                points[vid].append((ts, [plays, comments, likes, shares, favs]))
    return {v: (pts, None) for v, pts in points.items()}, gids
