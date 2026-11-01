#!/usr/bin/env python3
"""Create a consistent SQLite snapshot and publish it to private R2 via Wrangler."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "container"))
from seed_format import build_manifest, split_file, valid_object_key, validate_manifest


def sqlite_backup(source: Path, destination: Path) -> None:
    source_conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    target_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(target_conn)
        result = target_conn.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise ValueError(f"SQLite backup failed quick_check: {result}")
        target_conn.commit()
    finally:
        target_conn.close()
        source_conn.close()


def upload(wrangler: list[str], bucket: str, key: str, path: Path, content_type: str) -> None:
    subprocess.run(
        [*wrangler, "r2", "object", "put", f"{bucket}/{key}", "--remote", "--force",
         "--file", str(path), "--content-type", content_type],
        check=True,
    )


def publish(args: argparse.Namespace) -> dict[str, Any]:
    database = args.database.resolve(strict=True)
    settings = args.settings.resolve(strict=True)
    try:
        settings_value = json.loads(settings.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("settings file must be valid UTF-8 JSON") from error
    if not isinstance(settings_value, dict):
        raise ValueError("settings JSON must contain an object")
    if not args.bucket or "/" in args.bucket:
        raise ValueError("provide a valid R2 bucket name")
    manifest_key = valid_object_key(args.manifest_key)

    with tempfile.TemporaryDirectory(prefix="gameradar-seed-") as temporary:
        work = Path(temporary)
        backup = work / "radar.sqlite"
        settings_snapshot = work / "settings.json"
        parts_dir = work / "parts"
        parts_dir.mkdir()
        sqlite_backup(database, backup)
        settings_snapshot.write_bytes(settings.read_bytes())
        settings_snapshot_value = json.loads(settings_snapshot.read_text(encoding="utf-8"))
        if not isinstance(settings_snapshot_value, dict):
            raise ValueError("settings snapshot must contain a JSON object")
        digest = hashlib.sha256()
        with backup.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        snapshot_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{digest.hexdigest()[:12]}"
        prefix = f"seed/snapshots/{snapshot_id}"
        database_meta = split_file(backup, parts_dir, prefix, args.part_size)
        settings_key = f"{prefix}/settings.json"
        manifest = build_manifest(database_meta, settings_snapshot, settings_key, snapshot_id)
        validate_manifest(manifest)
        manifest_path = work / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        for part in manifest["database"]["parts"]:
            upload(args.wrangler, args.bucket, part["key"], parts_dir / Path(part["key"]).name,
                   "application/octet-stream")
        upload(args.wrangler, args.bucket, settings_key, settings_snapshot, "application/json")
        upload(args.wrangler, args.bucket, manifest_key, manifest_path, "application/json")

        return {
            "snapshot_id": snapshot_id,
            "database_bytes": database_meta["size_bytes"],
            "parts": len(database_meta["parts"]),
            "manifest_key": manifest_key,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path, help="live or copied SQLite database")
    parser.add_argument("--settings", required=True, type=Path, help="matching settings.json")
    parser.add_argument("--bucket", required=True, help="private R2 bucket name")
    parser.add_argument("--manifest-key", default="seed/current.json")
    parser.add_argument("--part-size", type=int, default=250 * 1024 * 1024)
    parser.add_argument(
        "--wrangler", nargs="+", default=["npx", "wrangler"],
        help="Wrangler executable and arguments, e.g. node.exe path/to/wrangler.js",
    )
    args = parser.parse_args()
    if args.part_size < 1 or args.part_size > 250 * 1024 * 1024:
        parser.error("--part-size must be between 1 byte and 250 MiB")
    return args


if __name__ == "__main__":
    try:
        print(json.dumps(publish(parse_args()), ensure_ascii=False), flush=True)
    except Exception as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1)
