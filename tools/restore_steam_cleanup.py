"""Restore recent platform data lost by the October 31 retention cleanup."""
import argparse
import os
import sqlite3
import time


EXCLUDED_RELEASE_DAYS = ("2026-09-30", "2026-10-01")


def restore(target, backup):
    target = os.path.abspath(target)
    backup = os.path.abspath(backup)
    if not os.path.isfile(target) or not os.path.isfile(backup):
        raise FileNotFoundError("Both target and backup databases must exist")

    conn = sqlite3.connect(target, timeout=60)
    try:
        conn.execute("ATTACH DATABASE ? AS old", (backup,))
        conn.execute("CREATE TEMP TABLE eligible_members(source TEXT, entity TEXT, PRIMARY KEY(source, entity))")
        conn.execute(
            """INSERT INTO eligible_members(source, entity)
               SELECT e.source, e.entity FROM old.entities e
               LEFT JOIN old.hist_steam h ON h.appid=CAST(e.entity AS INTEGER)
               WHERE e.source<>'steam' OR COALESCE(
                     date(CAST(json_extract(e.extra, '$.release_ts') AS INTEGER), 'unixepoch'),
                     date(h.released, 'unixepoch')
                 ) NOT IN (?, ?)""",
            EXCLUDED_RELEASE_DAYS)
        conn.commit()

        cutoff = int(time.time()) - 30 * 86400
        conn.execute("BEGIN IMMEDIATE")
        before = {
            "entities": conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
            "metrics": conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0],
            "history": conn.execute("SELECT COUNT(*) FROM hist_steam").fetchone()[0],
        }
        conn.execute(
            """INSERT OR IGNORE INTO entities(source, entity, name, first_seen, last_seen, extra)
               SELECT e.source, e.entity, e.name, e.first_seen, e.last_seen, e.extra
               FROM old.entities e JOIN eligible_members i
                 ON i.source=e.source AND i.entity=e.entity""")
        conn.execute(
            """INSERT OR IGNORE INTO metrics(ts, source, entity, metric, value)
               SELECT m.ts, m.source, m.entity, m.metric, m.value
               FROM old.metrics m JOIN eligible_members i
                 ON i.source=m.source AND i.entity=m.entity
               WHERE m.entity<>'__index__' AND m.ts>=?""",
            (cutoff,))
        conn.execute(
            """INSERT OR IGNORE INTO metrics_hourly(
                   hour_ts, source, entity, metric, vmin, vavg, vmax)
               SELECT m.hour_ts, m.source, m.entity, m.metric, m.vmin, m.vavg, m.vmax
               FROM old.metrics_hourly m JOIN eligible_members i
                 ON i.source=m.source AND i.entity=m.entity
               WHERE m.hour_ts>=?""",
            (cutoff,))
        conn.execute(
            """INSERT OR IGNORE INTO hist_steam(
                   appid, name, released, price, tags, reviews, grp, why, status, sample,
                   prio, start, weeks, total, fetched, tries)
               SELECT h.appid, h.name, h.released, h.price, h.tags, h.reviews, h.grp, h.why,
                      h.status, h.sample, h.prio, h.start, h.weeks, h.total, h.fetched, h.tries
               FROM old.hist_steam h
               JOIN eligible_members i ON i.source='steam' AND i.entity=CAST(h.appid AS TEXT)""")
        conn.execute(
            """INSERT OR IGNORE INTO revisit(member, last_ts, interval_h, n, last_v, hist, sig, reason)
               SELECT r.member, r.last_ts, r.interval_h, r.n, r.last_v, r.hist, r.sig, r.reason
               FROM old.revisit r
               JOIN eligible_members i ON r.member=i.source || ':' || i.entity""")
        conn.execute(
            """INSERT OR REPLACE INTO kv(k, v)
               SELECT 'dashboard_recovered', v FROM old.kv WHERE k='dashboard'""")
        conn.execute(
            """INSERT OR REPLACE INTO kv(k, v)
               SELECT 'dashboard', v FROM old.kv WHERE k='dashboard'""")
        old_status = conn.execute("SELECT v FROM old.kv WHERE k='source_status'").fetchone()
        if old_status:
            current_status = conn.execute("SELECT v FROM kv WHERE k='source_status'").fetchone()
            merged = json_merge_status(old_status[0], current_status[0] if current_status else None)
            conn.execute("INSERT OR REPLACE INTO kv(k, v) VALUES ('source_status', ?)", (merged,))
        after = {
            "entities": conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
            "metrics": conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0],
            "history": conn.execute("SELECT COUNT(*) FROM hist_steam").fetchone()[0],
        }
        conn.commit()
        return {"before": before, "after": after,
                "eligible": conn.execute("SELECT COUNT(*) FROM eligible_members").fetchone()[0],
                "cutoff": cutoff}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def json_merge_status(backup_json, current_json):
    import json
    old = json.loads(backup_json or "{}")
    current = json.loads(current_json or "{}")
    old.update(current)
    return json.dumps(old, ensure_ascii=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--backup", required=True)
    args = parser.parse_args()
    print(restore(args.target, args.backup))
