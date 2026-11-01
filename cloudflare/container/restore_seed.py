#!/usr/bin/env python3
"""Verify and atomically install a downloaded R2 seed manifest and its files."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    from .seed_format import (
        assemble_parts,
        restore_settings,
        valid_object_key,
        validate_manifest,
        verify_settings,
    )
except ImportError:
    from seed_format import (
        assemble_parts,
        restore_settings,
        valid_object_key,
        validate_manifest,
        verify_settings,
    )


def restore(manifest_path: Path, parts_dir: Path, database_path: Path,
            settings_download: Path, settings_path: Path) -> None:
    manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_manifest(manifest)
    verify_settings(manifest, settings_download)
    assemble_parts(manifest, parts_dir, database_path)
    restore_settings(manifest, settings_path, settings_download)


def fetch_and_restore(bucket: str, manifest_key: str, endpoint: str,
                      database_path: Path, settings_path: Path,
                      settings_only: bool = False) -> None:
    manifest_key = valid_object_key(manifest_key)
    with tempfile.TemporaryDirectory(prefix="gameradar-restore-") as temporary:
        work = Path(temporary)
        manifest_path = work / "manifest.json"
        subprocess.run(
            ["aws", "s3", "cp", f"s3://{bucket}/{manifest_key}", str(manifest_path),
             "--endpoint-url", endpoint, "--region", "auto"],
            check=True,
        )
        manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
        validate_manifest(manifest)
        settings_download = work / "settings.json"
        settings_key = valid_object_key(manifest["settings"]["key"])
        subprocess.run(
            ["aws", "s3", "cp", f"s3://{bucket}/{settings_key}", str(settings_download),
             "--endpoint-url", endpoint, "--region", "auto"],
            check=True,
        )
        verify_settings(manifest, settings_download)
        if settings_only:
            restore_settings(manifest, settings_path, settings_download)
            return

        parts_dir = work / "parts"
        parts_dir.mkdir()
        for part in manifest["database"]["parts"]:
            key = valid_object_key(part["key"])
            subprocess.run(
                ["aws", "s3", "cp", f"s3://{bucket}/{key}",
                 str(parts_dir / Path(key).name), "--endpoint-url", endpoint, "--region", "auto"],
                check=True,
            )
        restore(manifest_path, parts_dir, database_path, settings_download, settings_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--parts-dir", type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--settings-download", type=Path)
    parser.add_argument("--settings", required=True, type=Path)
    parser.add_argument("--bucket")
    parser.add_argument("--manifest-key")
    parser.add_argument("--endpoint")
    parser.add_argument("--settings-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.bucket and args.manifest_key and args.endpoint:
            fetch_and_restore(
                args.bucket, args.manifest_key, args.endpoint, args.database, args.settings,
                args.settings_only,
            )
        else:
            if not all((args.manifest, args.parts_dir, args.settings_download)):
                raise ValueError("manifest, parts-dir and settings-download are required without R2 fetch options")
            restore(
                args.manifest,
                args.parts_dir,
                args.database,
                args.settings_download,
                args.settings,
            )
    except Exception as error:
        print(f"Seed restore failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
