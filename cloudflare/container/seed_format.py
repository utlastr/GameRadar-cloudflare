"""Shared validation and assembly for chunked R2 seed snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
MAX_WRANGLER_PART_BYTES = 250 * 1024 * 1024
_PART_NAME = re.compile(r"database-\d{4,}\.bin\Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def valid_object_key(value: Any) -> str:
    if (
        not isinstance(value, str)
        or "\\" in value
        or not re.fullmatch(r"seed/[A-Za-z0-9._/-]+", value)
    ):
        raise ValueError("manifest contains an invalid R2 object key")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not value.startswith("seed/"):
        raise ValueError("R2 object key is outside the seed namespace")
    return value


def validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported seed manifest")

    database = manifest.get("database")
    settings = manifest.get("settings")
    if not isinstance(database, dict) or not isinstance(settings, dict):
        raise ValueError("seed manifest is missing database or settings metadata")

    size = database.get("size_bytes")
    digest = database.get("sha256")
    parts = database.get("parts")
    if not isinstance(size, int) or size <= 0:
        raise ValueError("seed manifest has an invalid database size")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("seed manifest has an invalid database checksum")
    if not isinstance(parts, list) or not parts:
        raise ValueError("seed manifest has no database parts")

    part_size = 0
    seen = set()
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("seed manifest has an invalid part entry")
        key = valid_object_key(part.get("key"))
        name = PurePosixPath(key).name
        if not _PART_NAME.fullmatch(name) or name in seen:
            raise ValueError("seed manifest has a duplicate or invalid part name")
        seen.add(name)
        length = part.get("size_bytes")
        checksum = part.get("sha256")
        if not isinstance(length, int) or length <= 0 or length > MAX_WRANGLER_PART_BYTES:
            raise ValueError("seed manifest part size is invalid")
        if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("seed manifest part checksum is invalid")
        part_size += length
    if part_size != size:
        raise ValueError("seed manifest part sizes do not match the database size")

    settings_key = valid_object_key(settings.get("key"))
    settings_size = settings.get("size_bytes")
    settings_digest = settings.get("sha256")
    if not isinstance(settings_size, int) or settings_size <= 0:
        raise ValueError("seed manifest has an invalid settings size")
    if not isinstance(settings_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", settings_digest):
        raise ValueError("seed manifest has an invalid settings checksum")
    return manifest


def split_file(source: Path, output_dir: Path, object_prefix: str, part_size: int) -> dict[str, Any]:
    if part_size < 1 or part_size > MAX_WRANGLER_PART_BYTES:
        raise ValueError("part size must be between 1 byte and 250 MiB")
    prefix = valid_object_key(object_prefix.rstrip("/") + "/placeholder").rsplit("/", 1)[0]
    database_digest = hashlib.sha256()
    parts = []
    total = 0

    with source.open("rb") as stream:
        index = 0
        while True:
            block_digest = hashlib.sha256()
            part_name = f"database-{index:04d}.bin"
            part_path = output_dir / part_name
            part_size_written = 0
            with part_path.open("xb") as part_stream:
                while part_size_written < part_size:
                    block = stream.read(min(1024 * 1024, part_size - part_size_written))
                    if not block:
                        break
                    part_stream.write(block)
                    block_digest.update(block)
                    database_digest.update(block)
                    part_size_written += len(block)
            if part_size_written == 0:
                part_path.unlink()
                break

            key = f"{prefix}/{part_name}"
            parts.append({
                "key": key,
                "size_bytes": part_size_written,
                "sha256": block_digest.hexdigest(),
            })
            total += part_size_written
            index += 1

    if total != source.stat().st_size:
        raise ValueError("source file changed while its seed parts were being created")
    return {
        "size_bytes": total,
        "sha256": database_digest.hexdigest(),
        "parts": parts,
    }


def build_manifest(
    database: dict[str, Any],
    settings_path: Path,
    settings_key: str,
    snapshot_id: str,
) -> dict[str, Any]:
    settings_bytes = settings_path.read_bytes()
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "database": database,
        "settings": {
            "key": valid_object_key(settings_key),
            "size_bytes": len(settings_bytes),
            "sha256": hashlib.sha256(settings_bytes).hexdigest(),
        },
    }


def restore_settings(manifest_value: Any, settings_path: Path, download_path: Path) -> None:
    manifest = validate_manifest(manifest_value)
    metadata = manifest["settings"]
    verify_settings(manifest, download_path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(download_path, settings_path)


def verify_settings(manifest_value: Any, download_path: Path) -> None:
    manifest = validate_manifest(manifest_value)
    metadata = manifest["settings"]
    verify_file(download_path, metadata["size_bytes"], metadata["sha256"])


def assemble_parts(manifest_value: Any, parts_dir: Path, destination: Path) -> None:
    manifest = validate_manifest(manifest_value)
    database = manifest["database"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".radar-seed-", dir=destination.parent)
    temp_path = Path(temp_name)
    whole_digest = hashlib.sha256()
    total = 0

    try:
        with os.fdopen(fd, "wb") as output:
            for part in database["parts"]:
                key = valid_object_key(part["key"])
                name = PurePosixPath(key).name
                part_path = parts_dir / name
                if not part_path.is_file() or part_path.is_symlink():
                    raise ValueError(f"seed part is missing or unsafe: {name}")

                part_digest = hashlib.sha256()
                part_total = 0
                with part_path.open("rb") as source:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        output.write(block)
                        part_digest.update(block)
                        whole_digest.update(block)
                        part_total += len(block)
                if part_total != part["size_bytes"] or part_digest.hexdigest() != part["sha256"]:
                    raise ValueError(f"seed part failed integrity verification: {name}")
                total += part_total
            output.flush()
            os.fsync(output.fileno())

        if total != database["size_bytes"] or whole_digest.hexdigest() != database["sha256"]:
            raise ValueError("assembled seed database failed integrity verification")
        os.replace(temp_path, destination)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def verify_file(path: Path, size_bytes: int, expected_sha256: str) -> None:
    if not path.is_file() or path.stat().st_size != size_bytes:
        raise ValueError(f"seed object has an unexpected size: {path.name}")
    if sha256_file(path) != expected_sha256:
        raise ValueError(f"seed object failed integrity verification: {path.name}")
