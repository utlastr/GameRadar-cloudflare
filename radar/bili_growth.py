"""Observed Tag growth, reversible co-occurrence groups and playback sampling."""
import hashlib
import json
import math
import re
import statistics
import threading
import time
import unicodedata
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from itertools import combinations

from . import bili_queue, config, db
from . import bili_realtime_client as bili

REVISION = "2026-10-27-tag-growth-v1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS bg_tags(norm TEXT PRIMARY KEY,name TEXT);
CREATE TABLE IF NOT EXISTS bg_hits(
 norm TEXT,bvid TEXT,PRIMARY KEY(norm,bvid));
CREATE INDEX IF NOT EXISTS bg_hits_video ON bg_hits(bvid);
CREATE TABLE IF NOT EXISTS bg_indexed(bvid TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS bg_samples(
 bvid TEXT,ts INTEGER,views INTEGER,PRIMARY KEY(bvid,ts));
CREATE INDEX IF NOT EXISTS bg_samples_time ON bg_samples(ts);
CREATE TABLE IF NOT EXISTS bg_refresh(
 bvid TEXT PRIMARY KEY,next_due REAL DEFAULT 0,last_attempt REAL DEFAULT 0,
 attempts INTEGER DEFAULT 0,error TEXT DEFAULT '',active INTEGER DEFAULT 0,
 priority INTEGER DEFAULT 1000000000,tracked_until REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS bg_refresh_due ON bg_refresh(active,next_due);
CREATE TABLE IF NOT EXISTS bg_edges(
 a TEXT,b TEXT,blocked INTEGER DEFAULT 0,updated REAL,PRIMARY KEY(a,b));
"""
_ready = set()
_init_lock = threading.Lock()
_build_lock = threading.Lock()
_sample_lock = threading.Lock()
_started = False


def norm(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or "").casefold().strip().lstrip("#"))


def init():
    from . import bili_daily
    bili_daily.init()
    with _init_lock:
        if config.DB_PATH not in _ready:
            with db.session() as c:
                c.executescript(SCHEMA)
                columns = {r["name"] for r in c.execute("PRAGMA table_info(bg_refresh)")}
                for name, kind in (("priority", "INTEGER DEFAULT 1000000000"),
                                   ("tracked_until", "REAL DEFAULT 0")):
                    if name not in columns:
                        c.execute(f"ALTER TABLE bg_refresh ADD COLUMN {name} {kind}")
                c.execute("CREATE INDEX IF NOT EXISTS bg_refresh_priority ON bg_refresh(active,priority,next_due)")
            _ready.add(config.DB_PATH)


def record_sample(c, bvid, views, now=None):
    if isinstance(views, bool) or not isinstance(views, int) or views < 0:
        return
    stamp = int(time.time() if now is None else now)
    c.execute("INSERT OR REPLACE INTO bg_samples VALUES(?,?,?)", (bvid, stamp, views))
    c.execute("""INSERT INTO bg_refresh(bvid,next_due) VALUES(?,?)
        ON CONFLICT(bvid) DO UPDATE SET next_due=excluded.next_due,attempts=0,error=''""",
              (bvid, stamp + max(3600, config.BVIDEO_GROWTH_SAMPLE_HOURS * 3600)))


def record(c, record, now=None):
    """Raw Tags bypass legacy word judgments; no new exclusion dictionary."""
    c.execute("DELETE FROM bg_hits WHERE bvid=?", (record["bvid"],))
    c.execute("INSERT OR IGNORE INTO bg_indexed VALUES(?)", (record["bvid"],))
    for tag in record.get("tags") or []:
        name = tag.get("name", tag.get("tag_name", "")) if isinstance(tag, dict) else tag
        key = norm(name)
        if key:
            c.execute("INSERT OR IGNORE INTO bg_tags VALUES(?,?)", (key, name.strip()))
            c.execute("INSERT OR IGNORE INTO bg_hits VALUES(?,?)", (key, record["bvid"]))
    record_sample(c, record["bvid"], record.get("views"), now)


def backfill(limit=1000):
    """A durable local cursor imports existing Tags and actual last-observed counters."""
    init()
    cursor = db.kv_get("bili_growth_import_cursor", "")
    with db.session() as c:
        rows = c.execute("""SELECT bvid,tags,views,detail_updated FROM bd_videos
            WHERE bvid>? AND detail_state='ok' ORDER BY bvid LIMIT ?""", (cursor, limit)).fetchall()
        if not rows:
            rows = c.execute("""SELECT bvid,tags,views,detail_updated FROM bd_videos v
                WHERE detail_state='ok' AND NOT EXISTS
                    (SELECT 1 FROM bg_indexed i WHERE i.bvid=v.bvid)
                ORDER BY bvid LIMIT ?""", (limit,)).fetchall()
        for row in rows:
            try:
                tags = json.loads(row["tags"] or "[]")
            except (ValueError, TypeError):
                tags = []
            record(c, {"bvid": row["bvid"], "tags": tags}, now=0)
            # Never assign an old cumulative counter to the video's publication date.
            if row["detail_updated"] and row["views"] is not None:
                record_sample(c, row["bvid"], row["views"], row["detail_updated"])
        if rows:
            c.execute("INSERT OR REPLACE INTO kv VALUES('bili_growth_import_cursor',?)",
                      (json.dumps(max(cursor, rows[-1]["bvid"])),))
    return len(rows)


def growth_index(heat, smoothing):
    """D is position 10: today/last 3 versus the preceding seven days."""
    if len(heat) != 10 or any(value is None for value in heat):
        return None
    numerator = max(heat[-1], statistics.mean(heat[-3:]))
    baseline = statistics.median(heat[:7])
    return (numerator + smoothing) / (baseline + smoothing)


def boundary(points, stamp, max_gap):
    """Interpolate between real observations only; never extrapolate a missing endpoint."""
    stamps = [p[0] for p in points]
    index = bisect_left(stamps, stamp)
    if index < len(points) and points[index][0] == stamp:
        return float(points[index][1]), False
    if index == 0 or index == len(points):
        return None, False
    left, right = points[index - 1], points[index]
    if right[0] - left[0] > max_gap or right[1] < left[1]:
        return None, False
    return left[1] + (right[1] - left[1]) * (stamp - left[0]) / (right[0] - left[0]), True


def daily_play(video, points, day, max_gap):
    if video["day"] > day:
        return {"views": 0.0, "estimated": False, "state": "not_published"}
    start = int(datetime.fromisoformat(day).replace(tzinfo=bili.BEIJING).timestamp())
    end = start + 86400
    before, estimated_before = (0.0, False) if video["day"] == day else boundary(points, start, max_gap)
    after, estimated_after = boundary(points, end, max_gap)
    if before is None or after is None or after < before:
        return {"views": None, "estimated": False, "state": "missing_snapshot"}
    # Reject a counter reset within the window, even if its endpoints later recover.
    within = [p[1] for p in points if start <= p[0] <= end]
    if any(b < a for a, b in zip(within, within[1:])):
        return {"views": None, "estimated": False, "state": "counter_reset"}
    return {"views": after - before, "estimated": estimated_before or estimated_after,
            "state": "estimated" if estimated_before or estimated_after else "measured"}


def metric(video_ids, videos, plays, days, smoothing):
    series = []
    for day in days:
        ids = [key for key in video_ids if videos[key]["day"] <= day]
        missing = [key for key in ids if plays[key][day]["views"] is None or not videos[key]["owner_id"]]
        by_author = defaultdict(float)
        for key in ids:
            value = plays[key][day]["views"]
            owner = videos[key]["owner_id"]
            if value is not None and owner:
                by_author[owner] += value
        active = {owner: views for owner, views in by_author.items() if views > 0}
        top = max(active, key=lambda owner: (active[owner], owner)) if active else None
        total = sum(active.values())
        remaining = max(0.0, total - active.get(top, 0))
        remaining_authors = max(0, len(active) - 1)
        heat = math.sqrt(remaining_authors * remaining) if not missing else None
        series.append({
            "day": day, "views": round(total, 3) if not missing else None,
            "observed_views": round(total, 3), "authors": len(active),
            "remaining_authors": remaining_authors, "remaining_views": round(remaining, 3),
            "heat": heat, "top_author": top,
            "top_share": active[top] / total if top and total else None,
            "videos": len(ids), "measured_videos": len(ids) - len(missing),
            "missing_videos": len(missing),
            "estimated_videos": sum(plays[key][day]["estimated"] for key in ids),
            "new_videos": sum(videos[key]["day"] == day for key in video_ids),
            "posting_authors": len({videos[key]["owner_id"] for key in video_ids
                                    if videos[key]["day"] == day and videos[key]["owner_id"]}),
        })
    today = series[-1]
    score = growth_index([item["heat"] for item in series], smoothing)
    single_index = growth_index([
        math.sqrt(item["views"]) if item["views"] is not None else None for item in series
    ], smoothing)
    state = ("baseline_pending" if score is None else "single_author" if today["authors"] == 1
             else "growing" if score > 1 else "stable")
    return {"series": series, "today": today, "index": score, "state": state,
            "single_index": single_index, "single_source": today["authors"] == 1,
            "estimated": any(item["estimated_videos"] for item in series),
            "baseline": statistics.median([s["heat"] for s in series[:7]]) if score is not None else None,
            "recent_heat": max(series[-1]["heat"], statistics.mean([s["heat"] for s in series[-3:]]))
            if score is not None else None}


def _components(keys, edges):
    parent = {key: key for key in keys}
    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key
    for edge in edges:
        a, b = root(edge["a"]), root(edge["b"])
        if a != b:
            parent[max(a, b)] = min(a, b)
    groups = defaultdict(list)
    for key in sorted(keys):
        groups[root(key)].append(key)
    return list(groups.values())


def build(day=None, wait=False):
    init()
    if not _build_lock.acquire(blocking=wait):
        return {"busy": True}
    try:
        today = datetime.now(bili.BEIJING).date()
        target = date.fromisoformat(day) if day else today - timedelta(days=1)
        if target >= today:
            raise ValueError("增长榜只比较已结束的北京时间日期")
        days = [(target - timedelta(days=offset)).isoformat() for offset in range(9, -1, -1)]
        smoothing = max(0.001, float(db.kv_get("bili_growth_smoothing", config.BVIDEO_GROWTH_SMOOTHING)))
        with db.session() as c:
            cutoff = bili_queue.collection_policy(c).get("started", 0)
            videos = {r["bvid"]: dict(r) for r in c.execute(
                "SELECT v.bvid,v.day,v.owner_id FROM bd_videos v WHERE v.day<=?"
                " AND v.detail_state!='archived'" +
                (" AND EXISTS(SELECT 1 FROM bs_latest_seen s WHERE s.bvid=v.bvid"
                 " AND s.first_seen>=? AND s.published>=?)" if cutoff else ""),
                (days[-1], cutoff, cutoff) if cutoff else (days[-1],))}
            names = {r["norm"]: r["name"] for r in c.execute("SELECT * FROM bg_tags")}
            tag_videos = defaultdict(set)
            for row in c.execute("SELECT norm,bvid FROM bg_hits"):
                if row["bvid"] in videos:
                    tag_videos[row["norm"]].add(row["bvid"])
            samples = defaultdict(list)
            low = int(datetime.fromisoformat(days[0]).replace(tzinfo=bili.BEIJING).timestamp()) - 108000
            high = int(datetime.fromisoformat(days[-1]).replace(tzinfo=bili.BEIJING).timestamp()) + 194400
            for row in c.execute("SELECT bvid,ts,views FROM bg_samples WHERE ts BETWEEN ? AND ? ORDER BY ts",
                                 (max(low, cutoff), high)):
                samples[row["bvid"]].append((row["ts"], row["views"]))
            blocked = {tuple((r["a"], r["b"])) for r in c.execute("SELECT a,b FROM bg_edges WHERE blocked=1")}
            first_observed = c.execute("SELECT MIN(ts) FROM bg_samples WHERE ts>=?",
                                       (cutoff,)).fetchone()[0]
        # Count-based provisional discovery remains available during playback warm-up.
        candidates = []
        positive_sample_ids = {
            bvid for bvid, points in samples.items()
            if len(points) > 1 and any(b[1] > a[1] for a, b in zip(points, points[1:]))
        }
        for key, ids in tag_videos.items():
            counts = Counter(videos[bvid]["day"] for bvid in ids)
            recent = max(counts[days[-1]], statistics.mean([counts[d] for d in days[-3:]]))
            baseline = statistics.median([counts[d] for d in days[:7]])
            count_rising = recent > baseline and recent > 0
            observed_play = bool(ids & positive_sample_ids)
            if not count_rising and not observed_play:
                continue
            candidates.append({
                "key": key, "name": names[key], "seed_index": (recent + 1) / (baseline + 1),
                "new_videos": counts[days[-1]], "recent_videos": sum(counts[d] for d in days[-3:]),
                "seed_baseline": baseline, "videos": len(ids),
                "authors": len({videos[b]["owner_id"] for b in ids if videos[b]["owner_id"]}),
                "count_rising": count_rising, "observed_play": observed_play,
            })
        candidates.sort(key=lambda r: (-r["seed_index"], -r["recent_videos"], r["key"]))
        # Bound expensive playback aggregation, not the underlying saved keyword catalogue.
        provisional = candidates[:max(1, int(config.BVIDEO_GROWTH_CANDIDATES)) * 2]
        pool_keys = {r["key"] for r in provisional}
        pool = provisional + [r for r in candidates if r["observed_play"] and r["key"] not in pool_keys]
        pool_ids = set().union(*(tag_videos[r["key"]] for r in pool)) if pool else set()
        gap = max(3600, float(config.BVIDEO_GROWTH_MAX_GAP_HOURS) * 3600)
        plays = {bvid: {d: daily_play(videos[bvid], samples[bvid], d, gap) for d in days}
                 for bvid in pool_ids}
        for item in pool:
            item.update(metric(tag_videos[item["key"]], videos, plays, days, smoothing))
            if first_observed is None or first_observed > (
                    datetime.fromisoformat(days[0]).replace(tzinfo=bili.BEIJING).timestamp()):
                item.update(index=None, single_index=None, state="baseline_pending", baseline=None, recent_heat=None)
        pool = [r for r in pool if (r["index"] is not None and r["index"] > 1)
                or (r["single_source"] and (r["single_index"] or 0) > 1)
                or (r["index"] is None and r["count_rising"])]
        pool.sort(key=lambda r: (r["index"] is None, -(r["index"] or 0), -r["seed_index"],
                                 -r["recent_videos"], r["key"]))
        selected = pool[:max(1, int(config.BVIDEO_GROWTH_CANDIDATES))]
        keys = {r["key"] for r in selected}
        by_video = defaultdict(set)
        for key in keys:
            for bvid in tag_videos[key]:
                if days[0] <= videos[bvid]["day"] <= days[-1] or bvid in positive_sample_ids:
                    by_video[bvid].add(key)
        pair_ids, pair_authors = defaultdict(set), defaultdict(set)
        for bvid, tags in by_video.items():
            for pair in combinations(sorted(tags), 2):
                pair_ids[pair].add(bvid)
                if videos[bvid]["owner_id"]:
                    pair_authors[pair].add(videos[bvid]["owner_id"])
        edges = [{
            "a": a, "b": b, "left": names[a], "right": names[b],
            "videos": len(ids), "authors": len(pair_authors[a, b]),
            "examples": sorted(ids)[:5], "blocked": (a, b) in blocked,
        } for (a, b), ids in pair_ids.items() if len(pair_authors[a, b]) >= 3]
        edges.sort(key=lambda e: (-e["authors"], -e["videos"], e["a"], e["b"]))
        # A rejected pair is a cannot-link constraint, including indirect paths.
        selected_map = {r["key"]: r for r in selected}
        accepted, conflicts, deferred = [], [], []
        members = {key: {key} for key in keys}
        for edge in edges:
            if edge["blocked"]:
                continue
            if any(selected_map[key]["index"] is None for key in (edge["a"], edge["b"])):
                deferred.append(dict(edge, deferred=True))
                continue
            left, right = members[edge["a"]], members[edge["b"]]
            if left is right:
                accepted.append(edge)
                continue
            merged = left | right
            if any(a in merged and b in merged for a, b in blocked):
                conflicts.append(edge)
                continue
            for key in merged:
                members[key] = merged
            accepted.append(edge)
        groups = []
        for component in _components(keys, accepted):
            ids = set().union(*(tag_videos[key] for key in component))
            ordered = sorted(component, key=lambda key: (-len({
                videos[b]["owner_id"] for b in tag_videos[key] if videos[b]["owner_id"]}),
                -len(tag_videos[key]), key))
            chosen = ordered[0]
            component_edges = [edge for edge in edges if edge["a"] in component and edge["b"] in component]
            component_edges += [edge for edge in deferred if edge["a"] in component or edge["b"] in component]
            group_metric = metric(ids, videos, plays, days, smoothing)
            if first_observed is None or first_observed > (
                    datetime.fromisoformat(days[0]).replace(tzinfo=bili.BEIJING).timestamp()):
                group_metric.update(index=None, single_index=None, state="baseline_pending", baseline=None, recent_heat=None)
            groups.append({
                "id": hashlib.sha256("\0".join(component).encode()).hexdigest()[:16],
                "name": names[chosen], "aliases": [names[key] for key in ordered if key != chosen],
                "keys": component, "videos": len(ids),
                "authors": len({videos[b]["owner_id"] for b in ids if videos[b]["owner_id"]}),
                "seed_index": max(selected_map[key]["seed_index"] for key in component),
                "edges": component_edges, "candidate": True,
                **group_metric,
            })
        groups.sort(key=lambda g: (g["index"] is None, -(g["index"] or 0),
                                   -g["seed_index"], -g["videos"], g["id"]))
        sample_ids = set().union(*(tag_videos[key] for key in keys)) if keys else set()
        sample_cutoff = (target - timedelta(days=config.BVIDEO_GROWTH_TRACK_DAYS)).isoformat()
        priorities = {}
        for key in keys:
            for bvid in tag_videos[key]:
                priorities[bvid] = min(priorities.get(bvid, len(tag_videos[key])), len(tag_videos[key]))
        with db.session() as c:
            now = time.time()
            c.execute("UPDATE bg_refresh SET active=0 WHERE active=1 AND tracked_until<=?", (now,))
            c.executemany("""INSERT INTO bg_refresh(bvid,active,priority,tracked_until) VALUES(?,1,?,?)
                ON CONFLICT(bvid) DO UPDATE SET active=1,priority=excluded.priority,
                tracked_until=MAX(bg_refresh.tracked_until,excluded.tracked_until)""",
                          [(bvid, priorities[bvid], now + 11 * 86400) for bvid in sample_ids
                           if videos[bvid]["day"] >= sample_cutoff])
            snapshot = {
                "revision": REVISION, "updated": time.time(), "day": days[-1], "days": days,
                "smoothing": smoothing, "candidate_limit": config.BVIDEO_GROWTH_CANDIDATES,
                "min_pair_authors": 3, "tag_count": len(names), "rising_tag_count": len(candidates),
                "selected_count": len(selected), "groups": groups, "tags": selected,
                "blocked_edges": [e for e in edges if e["blocked"]], "conflict_edges": conflicts,
                "deferred_edges": len(deferred),
                "decisions": [{"a": a, "b": b, "left": names.get(a, a),
                               "right": names.get(b, b), "blocked": True} for a, b in sorted(blocked)],
                "first_snapshot": first_observed,
                "baseline_started": cutoff or None,
                "coverage": {
                    "source": "已保存的B站样本，不是全站全量",
                    "play_basis": "真实累计播放快照；非整点边界只在前后快照间线性估算，不外推",
                    "max_snapshot_gap_hours": gap / 3600,
                    "selection_basis": "播放基线不足时按已保存投稿数的增长预选，不视为播放增长",
                    "ready_groups": sum(g["index"] is not None for g in groups),
                    "baseline_pending_groups": sum(g["index"] is None for g in groups),
                    "missing_tag_videos": c.execute(
                        """SELECT COUNT(*) FROM bd_videos v WHERE detail_state IN ('pending','error')""" +
                        (" AND EXISTS(SELECT 1 FROM bs_latest_seen s WHERE s.bvid=v.bvid"
                         " AND s.first_seen>=? AND s.published>=?)" if cutoff else ""),
                        (cutoff, cutoff) if cutoff else ()).fetchone()[0],
                },
            }
            c.execute("INSERT OR REPLACE INTO kv VALUES('bili_growth_dashboard',?)",
                      (json.dumps(snapshot, ensure_ascii=False),))
            c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)",
                      ("bili_growth_dashboard:" + days[-1], json.dumps(snapshot, ensure_ascii=False)))
        return snapshot
    finally:
        _build_lock.release()


def edge_decision(a, b, blocked=True, day=None):
    init()
    if not isinstance(a, str) or not isinstance(b, str):
        raise ValueError("需要两个Tag名称")
    if day is not None and date.fromisoformat(day) >= datetime.now(bili.BEIJING).date():
        raise ValueError("增长榜只比较已结束的北京时间日期")
    a, b = sorted((norm(a), norm(b)))
    if not a or not b or a == b:
        raise ValueError("需要两个不同的Tag")
    if not isinstance(blocked, bool):
        raise ValueError("blocked 必须是布尔值")
    with db.session() as c:
        for key in (a, b):
            if not c.execute("SELECT 1 FROM bg_tags WHERE norm=?", (key,)).fetchone():
                raise ValueError("Tag不存在")
        c.execute("INSERT OR REPLACE INTO bg_edges VALUES(?,?,?,?)", (a, b, int(blocked), time.time()))
    cached = db.kv_get("bili_growth_dashboard" + (":" + day if day else ""), {}) or {}
    build(cached.get("day"), wait=True)
    return {"ok": True}


def payload(day=None):
    init()
    if day:
        parsed = date.fromisoformat(day)
        if parsed >= datetime.now(bili.BEIJING).date():
            raise ValueError("增长榜只比较已结束的北京时间日期")
    result = db.kv_get("bili_growth_dashboard" + (":" + day if day else ""), {}) or {
        "revision": REVISION, "groups": [], "tags": [], "status": "building", "day": day}
    with db.session() as c:
        result["baseline_started"] = bili_queue.collection_policy(c).get("started")
        sample = c.execute("SELECT COUNT(*) n,MIN(ts) first,MAX(ts) last FROM bg_samples").fetchone()
        due = c.execute("SELECT COUNT(*) FROM bg_refresh WHERE active=1 AND next_due<=?",
                        (time.time(),)).fetchone()[0]
        tracked = c.execute("SELECT COUNT(*) FROM bg_refresh WHERE active=1").fetchone()[0]
    result["sampling"] = {**dict(sample), "due": due,
                          "tracked": tracked,
                          "minimum_sweep_hours": tracked * config.BVIDEO_GROWTH_REQUEST_SECONDS / 3600,
                          "pause_seconds": max(bili.pause_left(), bili.pause_left("视频详情")),
                          "interval_hours": config.BVIDEO_GROWTH_SAMPLE_HOURS,
                          **(db.kv_get("bili_growth_worker", {}) or {})}
    return result


def sample_once(now=None):
    init()
    if not config.source_on("bvideo") or not config.BVIDEO_GROWTH_ENABLED:
        return False
    if bili.paused() or bili.paused("视频详情") or bili.paused("视频详情及标签"):
        return False
    if not _sample_lock.acquire(blocking=False):
        return False
    try:
        now = time.time() if now is None else now
        with db.session() as c:
            if bili_queue.recent_pressure(c, now) > 120:
                return False
            cutoff = bili_queue.collection_policy(c).get("started", 0)
            row = c.execute("""SELECT r.* FROM bg_refresh r JOIN bd_videos v ON v.bvid=r.bvid
                WHERE r.active=1 AND r.next_due<=? AND v.detail_state='ok'
                AND NOT EXISTS(SELECT 1 FROM bd_lease l
                    WHERE l.name='detail:'||r.bvid AND l.expires>?)""" +
                (" AND EXISTS(SELECT 1 FROM bs_latest_seen s WHERE s.bvid=v.bvid"
                 " AND s.first_seen>=? AND s.published>=?)" if cutoff else "") +
                " ORDER BY r.priority,r.next_due,r.bvid LIMIT 1",
                (now, now, cutoff, cutoff) if cutoff else (now, now)).fetchone()
        if row is None:
            return False
        # Uses the same per-BV lease and anonymous transport/rate/risk gate as enrichment.
        from . import bili_daily
        with bili_daily.detail_claim(row["bvid"]) as claimed:
            if not claimed:
                return False
            try:
                data = bili.check(bili._plain(
                    "https://api.bilibili.com/x/web-interface/view", {"bvid": row["bvid"]},
                    authenticated=False, timeout=12, persistent=True), "播放快照", "视频详情")
                if data.get("bvid") != row["bvid"]:
                    raise RuntimeError("播放快照BV不匹配")
                views = (data.get("stat") or {}).get("view")
                if isinstance(views, bool) or not isinstance(views, int) or views < 0:
                    raise RuntimeError("播放快照缺少有效播放量")
                stamp = time.time()
                with db.session() as c:
                    record_sample(c, row["bvid"], views, stamp)
                    c.execute("UPDATE bd_videos SET views=? WHERE bvid=?", (views, row["bvid"]))
                db.kv_set("bili_growth_worker", {"last_success": stamp, "error": ""})
                return True
            except Exception as exc:
                retry = max(60, bili.pause_left(), bili.pause_left("视频详情"),
                            min(3600, 60 * 2 ** min(row["attempts"], 6)))
                with db.session() as c:
                    c.execute("""UPDATE bg_refresh SET next_due=?,last_attempt=?,
                        attempts=attempts+1,error=? WHERE bvid=?""",
                              (time.time() + retry, time.time(), str(exc)[:300], row["bvid"]))
                db.kv_set("bili_growth_worker", {"last_attempt": time.time(), "error": str(exc)[:300]})
                return False
    finally:
        _sample_lock.release()


def start(stop_event=None):
    global _started
    if _started or not config.BVIDEO_GROWTH_ENABLED:
        return
    init()
    _started = True
    stop = stop_event or threading.Event()
    def run():
        last_build = 0
        while not stop.is_set():
            try:
                imported = backfill(limit=25)
                if not imported and time.time() - last_build >= 300:
                    build()
                    last_build = time.time()
                if imported:
                    stop.wait(0.1)
                    continue
                sample_once()
            except Exception as exc:
                db.kv_set("bili_growth_worker", {"error": f"{type(exc).__name__}: {exc}"[:300],
                                               "last_attempt": time.time()})
            stop.wait(max(3, float(config.BVIDEO_GROWTH_REQUEST_SECONDS)))
    threading.Thread(target=run, name="bili-growth", daemon=True).start()
