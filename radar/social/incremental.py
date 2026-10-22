"""Deduplicated publication evidence and explicitly scoped observation windows."""
import time

from .. import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS pub_records(
 platform TEXT, vid TEXT, title TEXT, author_id TEXT, published INTEGER,
 first_seen INTEGER, last_seen INTEGER, url TEXT, PRIMARY KEY(platform,vid));
CREATE INDEX IF NOT EXISTS idx_pub_time ON pub_records(platform,published);
CREATE TABLE IF NOT EXISTS pub_topics(
 platform TEXT, vid TEXT, topic TEXT, method TEXT, PRIMARY KEY(platform,vid,topic));
CREATE TABLE IF NOT EXISTS pub_runs(
 source TEXT, started INTEGER, ended INTEGER, ok INTEGER, n INTEGER, added INTEGER,
 oldest INTEGER, newest INTEGER, coverage TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS idx_pub_runs ON pub_runs(source,ended);
"""


def ensure():
    with db.session() as c:
        c.executescript(SCHEMA)


def ingest(platform, items, topic=None, now=None):
    """Content and topic rows are idempotent, including retries and overlapping scans."""
    ensure()
    now = int(now or time.time())
    added = 0
    with db.session() as c:
        for it in items:
            vid = it.get("vid")
            if not vid:
                continue
            pub = it.get("ts")
            pub = int(pub) if pub and 0 < pub <= now else None
            cur = c.execute("INSERT OR IGNORE INTO pub_records VALUES (?,?,?,?,?,?,?,?)",
                            (platform, vid, it.get("text") or "", it.get("author_id") or "",
                             pub, now, now, it.get("url") or ""))
            added += cur.rowcount
            c.execute("UPDATE pub_records SET last_seen=?,title=?,"
                      "published=COALESCE(?,published),author_id=CASE WHEN ?!='' THEN ? ELSE author_id END "
                      "WHERE platform=? AND vid=?",
                      (now, it.get("text") or "", pub, it.get("author_id") or "",
                       it.get("author_id") or "", platform, vid))
            if topic:
                c.execute("INSERT OR IGNORE INTO pub_topics VALUES (?,?,?,?)",
                          (platform, vid, topic, "search_candidate"))
    return added


def record_run(source, started, items, added, coverage="search_sample", error=""):
    ensure()
    dates = [it["ts"] for it in items if it.get("ts")]
    with db.session() as c:
        c.execute("INSERT INTO pub_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (source, int(started), int(time.time()), int(not error), len(items), added,
                   min(dates) if dates else None, max(dates) if dates else None, coverage, error[:500]))


def report(now=None):
    ensure()
    now = int(now or time.time())
    with db.session() as c:
        records = [dict(r) for r in c.execute("SELECT * FROM pub_records WHERE published>=? OR published IS NULL",
                                             (now - 9 * 86400,))]
        topics = {}
        for p, v, t, method in c.execute("SELECT * FROM pub_topics"):
            topics.setdefault((p, v), set()).add((t, method))
        runs = [dict(r) for r in c.execute("SELECT * FROM pub_runs WHERE ended>=? ORDER BY ended",
                                         (now - 9 * 86400,))]
    latest = {}
    for r in runs:
        latest[r["source"]] = r
    counts, unknown = {}, {}
    for r in records:
        p = r["platform"]
        if not r["published"]:
            unknown[p] = unknown.get(p, 0) + 1
            continue
        for topic, method in topics.get((p, r["vid"]), set()):
            group = counts.setdefault((p, topic), {"dates": [], "authors": set(), "authors3": set(), "method": method})
            group["dates"].append(r["published"])
            if now - 86400 < r["published"] <= now and r["author_id"]:
                group["authors"].add(r["author_id"])
                if now - 3 * 3600 < r["published"]:
                    group["authors3"].add(r["author_id"])
    rows = []
    for (p, topic), g in counts.items():
        windows = {}
        for h in (1, 3, 6, 24):
            span = h * 3600
            current = sum(now - span < t <= now for t in g["dates"])
            previous = sum(now - 2 * span < t <= now - span for t in g["dates"])
            windows[str(h)] = {"current": current, "previous_observed": previous}
        # A sampling failure/absence must never become an asserted zero baseline.
        rows.append({"platform": p, "topic": topic, "windows": windows,
                     "authors_24h": len(g["authors"]), "method": g["method"],
                     "baseline": "unverified", "score": None,
                     "signal": "待核起量" if windows["3"]["current"] >= 8 and len(g["authors3"]) >= 3 else "观察"})
    rows.sort(key=lambda r: (-r["windows"]["3"]["current"], -r["windows"]["24"]["current"]))
    totals = {}
    for p in {r["platform"] for r in records}:
        sample = [r for r in records if r["platform"] == p]
        totals[p] = {"observed_24h": sum(bool(r["published"] and now - 86400 < r["published"] <= now) for r in sample),
                     "missing_publication": unknown.get(p, 0), "full_platform": False}
    return {"ts": now, "scope": "已采集样本，非全平台总量；搜索词归属待核", "rows": rows[:200],
            "totals": totals, "sources": latest}
