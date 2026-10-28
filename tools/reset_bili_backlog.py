"""Offline, backed-up cutover to new-post-only Bilibili collection."""
import argparse
import json
import socket
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radar import bili_queue, config


def counts():
    with sqlite3.connect(config.DB_PATH) as c:
        return {
            "videos": c.execute("SELECT COUNT(*) FROM bd_videos").fetchone()[0],
            "completed": c.execute("SELECT COUNT(*) FROM bd_videos WHERE detail_state='ok'").fetchone()[0],
            "samples": c.execute("SELECT COUNT(*) FROM bg_samples").fetchone()[0],
            "pending": c.execute("SELECT COUNT(*) FROM bd_videos WHERE detail_state IN ('pending','error')").fetchone()[0],
            "queue": c.execute("SELECT COUNT(*) FROM bs_pending").fetchone()[0],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        parser.error("Use --apply only after stopping GameRadar")
    try:
        with socket.create_connection(("127.0.0.1", config.SERVER_PORT), timeout=2):
            raise SystemExit("GameRadar is still listening; stop it before the cutover")
    except (ConnectionRefusedError, TimeoutError):
        pass
    backup_dir = Path(config.DATA_DIR) / "backup"
    if not backup_dir.is_dir():
        raise SystemExit("Expected existing radar_data/backup directory")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"before-bili-reset-{stamp}.db"
    with sqlite3.connect(config.DB_PATH) as source:
        with sqlite3.connect(backup) as target:
            source.backup(target)
    print(json.dumps({"backup": str(backup), "bytes": backup.stat().st_size}), flush=True)
    before = counts()
    result = bili_queue.archive_pending()
    after = counts()
    if any(before[key] != after[key] for key in ("videos", "completed", "samples")):
        raise RuntimeError("Preservation check failed; retain backup and do not restart")
    if after["pending"] or after["queue"]:
        raise RuntimeError("Old pending jobs remain; inspect before restarting")
    print(json.dumps({"policy": result, "before": before, "after": after},
                     ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
