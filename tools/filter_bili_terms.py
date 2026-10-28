"""Repair derived term evidence and run bounded, cached entity-typing batches."""
import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from radar import bili_daily, bili_term_filter as filtering, bili_terms, config, db
from radar.sources import bili


def snapshot(c):
    return {
        "videos": c.execute("SELECT COUNT(*) FROM bd_videos").fetchone()[0],
        "terms": c.execute("SELECT COUNT(*) FROM bd_terms").fetchone()[0],
        "status": {r["status"]: r["n"] for r in c.execute(
            "SELECT status,COUNT(*) n FROM bd_terms GROUP BY status")},
    }


def backup(path):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("Backup destination already exists")
    with sqlite3.connect(config.DB_PATH, timeout=60) as source:
        with sqlite3.connect(str(destination)) as target:
            source.backup(target, pages=1000, sleep=0.02)


def rebuild():
    """Only replace derived hits; retain every raw video, title, Tag and counter."""
    bili_daily.init()
    with db.session() as c:
        c.execute("DELETE FROM bd_word_hits")
        c.execute("UPDATE bd_terms SET scanned=0,scan_after=''")
        c.execute("UPDATE bd_videos SET title_scanned=0")
    last, count = "", 0
    while True:
        with db.session() as c:
            rows = c.execute("""SELECT bvid,day,title,tags,owner_id FROM bd_videos
                WHERE bvid>? ORDER BY bvid LIMIT 1000""", (last,)).fetchall()
            for row in rows:
                try:
                    tags = json.loads(row["tags"] or "[]")
                except (ValueError, TypeError):
                    tags = []
                bili_terms.seed(c, {"bvid": row["bvid"], "date": row["day"],
                                   "title": row["title"], "tags": tags})
            if rows:
                last = rows[-1]["bvid"]
                count += len(rows)
        if not rows:
            break
    with db.session() as c:
        bili_terms.refresh(c)
        c.execute("INSERT OR REPLACE INTO kv VALUES('bili_term_rebuilt_version',?)",
                  (json.dumps(filtering.VERSION),))
    return count


def run(args):
    started = time.monotonic()
    if args.backup:
        backup(args.backup)
    bili_daily.init()
    with db.session() as c:
        before = snapshot(c)
    rebuilt = rebuild() if args.rebuild else 0
    with db.session() as c:
        bili_terms.refresh(c)
    batches = []
    for _ in range(max(0, args.ai_batches)):
        result = filtering.ai_round(args.batch_size)
        batches.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if result["status"] != "ok":
            break
    if args.rescan:
        with db.session() as c:
            bili_terms.rescan(c, datetime.now(bili.BEIJING).date().isoformat(),
                              game_only=True, title_limit=2000)
    with db.session() as c:
        after = snapshot(c)
        stats = filtering.summary(c)
        games = bili_terms.topics(c, limit=30, game_only=True)
        excluded = [dict(r) for r in c.execute("""SELECT name,kind,kind_reason,seed_videos
            FROM bd_terms WHERE status='blocked' ORDER BY seed_videos DESC LIMIT 45""")]
        examples = [dict(r) for r in c.execute("""SELECT name,status,kind,seed_videos
            FROM bd_terms WHERE norm IN
            ('三国','dlc','roblox','mmorpg','铁道','突围','传说','剧情','手机','真的')
            ORDER BY norm""")]
    report = {"version": filtering.VERSION, "generated": datetime.now(bili.BEIJING).isoformat(),
              "before": before, "after": after, "rebuilt_videos": rebuilt, "filter": stats,
              "ai_batches": batches, "game_examples": games, "excluded_examples": excluded,
              "exact_examples": examples, "seconds": round(time.monotonic() - started, 2)}
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items()
                      if k not in ("game_examples", "excluded_examples")}, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--rescan", action="store_true")
    parser.add_argument("--ai-batches", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--report")
    run(parser.parse_args())
