"""Offline bootstrap/rebuild of the installed Bilibili growth dashboard."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radar import bili_growth


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--day")
    parser.add_argument("--output")
    args = parser.parse_args()
    started, imported = time.time(), 0
    while True:
        count = bili_growth.backfill(1000)
        imported += count
        if not count:
            break
        if imported % 10000 == 0:
            print(f"Imported {imported} saved video Tag sets", flush=True)
    result = bili_growth.build(args.day)
    summary = {
        "revision": result["revision"], "day": result["day"],
        "imported": imported, "tag_count": result["tag_count"],
        "selected": result["selected_count"], "groups": len(result["groups"]),
        "coverage": result["coverage"], "seconds": round(time.time() - started, 2),
    }
    if args.output:
        Path(args.output).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
