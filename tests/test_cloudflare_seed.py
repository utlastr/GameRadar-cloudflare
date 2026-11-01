import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from cloudflare.container.seed_format import (
    assemble_parts,
    build_manifest,
    split_file,
    valid_object_key,
)
from cloudflare.container.restore_seed import restore


def test_chunked_seed_round_trip_and_settings_restore(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO sample(value) VALUES (?)", [("first",), ("second",)])
    conn.commit()
    conn.close()

    settings = tmp_path / "settings-source.json"
    settings.write_text(json.dumps({"version": 101}), encoding="utf-8")
    parts_dir = tmp_path / "parts"
    parts_dir.mkdir()
    database_meta = split_file(source, parts_dir, "seed/snapshots/test", 17)
    manifest = build_manifest(database_meta, settings, "seed/snapshots/test/settings.json", "test")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    settings_download = tmp_path / "settings-download.json"
    settings_download.write_bytes(settings.read_bytes())

    restored_db = tmp_path / "restored.db"
    restored_settings = tmp_path / "data" / "settings.json"
    restore(
        tmp_path / "manifest.json",
        parts_dir,
        restored_db,
        settings_download,
        restored_settings,
    )

    result = sqlite3.connect(restored_db)
    assert result.execute("PRAGMA quick_check").fetchone() == ("ok",)
    assert result.execute("SELECT value FROM sample ORDER BY id").fetchall() == [
        ("first",),
        ("second",),
    ]
    result.close()
    assert restored_settings.read_bytes() == settings.read_bytes()


def test_corrupt_part_does_not_replace_existing_database(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    source.write_bytes(b"correct database bytes")
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    parts_dir = tmp_path / "parts"
    parts_dir.mkdir()
    database_meta = split_file(source, parts_dir, "seed/snapshots/test", 7)
    manifest = build_manifest(database_meta, settings, "seed/snapshots/test/settings.json", "test")
    part_path = parts_dir / Path(manifest["database"]["parts"][0]["key"]).name
    part_path.write_bytes(b"corrupt")
    destination = tmp_path / "existing.db"
    destination.write_bytes(b"keep existing")

    with pytest.raises(ValueError, match="integrity verification"):
        assemble_parts(manifest, parts_dir, destination)
    assert destination.read_bytes() == b"keep existing"


def test_corrupt_settings_does_not_replace_existing_database(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    source.write_bytes(b"correct database bytes")
    settings = tmp_path / "settings-source.json"
    settings.write_text("{}", encoding="utf-8")
    parts_dir = tmp_path / "parts"
    parts_dir.mkdir()
    database_meta = split_file(source, parts_dir, "seed/snapshots/test", 10)
    manifest = build_manifest(database_meta, settings, "seed/snapshots/test/settings.json", "test")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    settings_download = tmp_path / "settings-download.json"
    settings_download.write_text("tampered", encoding="utf-8")
    destination = tmp_path / "existing.db"
    destination.write_bytes(b"keep existing")

    with pytest.raises(ValueError, match="unexpected size"):
        restore(
            manifest_path,
            parts_dir,
            destination,
            settings_download,
            tmp_path / "settings-live.json",
        )
    assert destination.read_bytes() == b"keep existing"


def test_seed_manifest_pointer_is_allowed_but_traversal_is_rejected() -> None:
    assert valid_object_key("seed/current.json") == "seed/current.json"
    with pytest.raises(ValueError):
        valid_object_key("seed/../private.json")
    with pytest.raises(ValueError):
        valid_object_key("/seed/current.json")


def test_manifest_checks_sizes_and_checksums(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "database": {
            "size_bytes": 1,
            "sha256": hashlib.sha256(b"x").hexdigest(),
            "parts": [{
                "key": "seed/snapshots/a/database-0000.bin",
                "size_bytes": 2,
                "sha256": hashlib.sha256(b"xx").hexdigest(),
            }],
        },
        "settings": {
            "key": "seed/snapshots/a/settings.json",
            "size_bytes": 2,
            "sha256": hashlib.sha256(b"{}").hexdigest(),
        },
    }
    from cloudflare.container.seed_format import validate_manifest

    with pytest.raises(ValueError, match="sizes"):
        validate_manifest(manifest)
