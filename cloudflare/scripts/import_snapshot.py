#!/usr/bin/env python3
"""Resumably import a read-only GameRadar SQLite snapshot into DO shards."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SHARD_COUNT = 10
DEFAULT_BATCH_ROWS = 1000
MAX_REQUEST_BODY_BYTES = 1_300_000
COMPRESS_TEXT_BYTES = 128_000
GZIP_PREFIX = "__RADAR_GZIP_BASE64__:"
BLOB_PREFIX = "__RADAR_BLOB_BASE64__:"


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def fnv1a(text: str) -> int:
    value = 2166136261
    encoded = text.encode("utf-16-le", errors="surrogatepass")
    for offset in range(0, len(encoded), 2):
        unit = encoded[offset] | (encoded[offset + 1] << 8)
        value ^= unit
        value = (value * 16777619) & 0xFFFFFFFF
    return value


def entity_shard(source: str, entity: str) -> int:
    return fnv1a(f"{source}:{entity}") % SHARD_COUNT


def stable_shard(table: str, key: list[Any]) -> int:
    encoded = json.dumps(key, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return fnv1a(f"{table}:{encoded}") % SHARD_COUNT


def encode_cell(value: Any) -> Any:
    if isinstance(value, bytes):
        return BLOB_PREFIX + base64.b64encode(value).decode("ascii")
    if isinstance(value, str) and len(value.encode("utf-8")) > COMPRESS_TEXT_BYTES:
        packed = gzip.compress(value.encode("utf-8"), compresslevel=6, mtime=0)
        encoded = GZIP_PREFIX + base64.b64encode(packed).decode("ascii")
        if len(encoded.encode("utf-8")) < len(value.encode("utf-8")):
            return encoded
    if isinstance(value, float) and not (float("-inf") < value < float("inf")):
        raise ValueError("snapshot contains a non-finite REAL value")
    return value


def decode_cell(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(GZIP_PREFIX):
        packed = base64.b64decode(value[len(GZIP_PREFIX) :])
        return gzip.decompress(packed).decode("utf-8")
    if isinstance(value, str) and value.startswith(BLOB_PREFIX):
        return base64.b64decode(value[len(BLOB_PREFIX) :])
    return value


def row_to_json(values: tuple[Any, ...]) -> list[Any]:
    return [encode_cell(value) for value in values]


def table_manifest(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[str]]:
    definitions: list[dict[str, Any]] = []
    tables = conn.execute(
        """
        SELECT name, sql FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()

    for name, ddl in tables:
        if not ddl:
            raise ValueError(f"table {name!r} has no importable CREATE TABLE statement")
        info = conn.execute(f"PRAGMA table_info({quote_identifier(name)})").fetchall()
        columns = [row[1] for row in info]
        primary_key = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
        integer_pk = (
            len(primary_key) == 1
            and next(row[2] for row in info if row[1] == primary_key[0]).strip().upper()
            == "INTEGER"
        )

        unique_key: list[str] = []
        if not primary_key:
            for index in conn.execute(f"PRAGMA index_list({quote_identifier(name)})"):
                if not index[2] or (len(index) > 4 and index[4]):
                    continue
                index_columns = conn.execute(
                    f"PRAGMA index_info({quote_identifier(index[1])})"
                ).fetchall()
                candidate = [row[2] for row in sorted(index_columns, key=lambda row: row[0])]
                if candidate and all(column is not None for column in candidate):
                    unique_key = candidate
                    break

        rowid_column = None
        if not integer_pk:
            existing = {column.casefold() for column in columns}
            rowid_column = next(
                (candidate for candidate in ("rowid", "_rowid_", "oid")
                 if candidate.casefold() not in existing),
                None,
            )
            if rowid_column is None:
                raise ValueError(f"cannot preserve hidden rowid for table {name!r}")

        insert_columns = ([rowid_column] if rowid_column else []) + columns
        definitions.append(
            {
                "name": name,
                "sql": ddl,
                "sourceColumns": columns,
                "insertColumns": insert_columns,
                "cursorColumn": primary_key[0] if integer_pk else rowid_column,
                "integerPrimaryKey": integer_pk,
                "primaryKey": primary_key,
                "uniqueKey": unique_key,
            }
        )

    indexes = [
        row[0]
        for row in conn.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'index' AND sql IS NOT NULL
            ORDER BY name
            """
        )
    ]
    objects = conn.execute(
        """
        SELECT type, count(*) FROM sqlite_master
        WHERE type IN ('view', 'trigger')
        GROUP BY type
        """
    ).fetchall()
    if objects:
        raise ValueError(f"snapshot has unsupported schema objects: {objects!r}")
    return definitions, indexes


def table_row_count(conn: sqlite3.Connection, name: str) -> int:
    return int(
        conn.execute(f"SELECT count(*) FROM {quote_identifier(name)}").fetchone()[0]
    )


def row_shard(definition: dict[str, Any], rowid: int, values: tuple[Any, ...]) -> int:
    name = definition["name"]
    columns = definition["sourceColumns"]
    record = dict(zip(columns, values))

    if name in ("entities", "metrics"):
        return entity_shard(str(record["source"]), str(record["entity"]))

    key_columns = definition["primaryKey"] or definition["uniqueKey"]
    if key_columns:
        return stable_shard(name, [record[column] for column in key_columns])
    return stable_shard(name, [rowid])


def request_json(
    base_url: str,
    token: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method="GET" if body is None else "POST",
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        payload = error.read()
        try:
            detail = json.loads(payload)
        except Exception:
            detail = payload[:1000].decode("utf-8", errors="replace")
        raise RuntimeError(f"{path} returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"{path} request failed: {error.reason}") from error
    return json.loads(payload or b"{}")


def json_request_size(value: Any) -> int:
    return len(
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    )


def load_checkpoint(path: Path, identity: dict[str, int]) -> dict[str, Any]:
    if not path.exists():
        return {"identity": identity, "lastRowid": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("identity") != identity:
        raise ValueError("checkpoint belongs to a different snapshot file")
    state.setdefault("lastRowid", {})
    return state


def save_checkpoint(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def read_chunk(
    conn: sqlite3.Connection,
    definition: dict[str, Any],
    after_cursor: int | None,
    limit: int,
) -> list[tuple[Any, ...]]:
    cursor = quote_identifier(definition["cursorColumn"])
    table = quote_identifier(definition["name"])
    if after_cursor is None:
        return conn.execute(
            f"SELECT {cursor}, * FROM {table} ORDER BY {cursor} LIMIT ?",
            (limit,),
        ).fetchall()
    return conn.execute(
        f"SELECT {cursor}, * FROM {table} WHERE {cursor} > ? ORDER BY {cursor} LIMIT ?",
        (after_cursor, limit),
    ).fetchall()


def remote_status(base_url: str, token: str) -> dict[str, Any]:
    return request_json(base_url, token, "/api/cloudflare/import/status")


def run_import(args: argparse.Namespace) -> None:
    source = Path(args.database).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    stat = source.stat()
    identity = {"size": stat.st_size, "mtimeNs": stat.st_mtime_ns}
    checkpoint = Path(args.checkpoint).resolve()
    state = load_checkpoint(checkpoint, identity)
    conn = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise ValueError(f"source snapshot failed quick_check: {check}")
        definitions, indexes = table_manifest(conn)
        row_counts = {
            definition["name"]: table_row_count(conn, definition["name"])
            for definition in definitions
        }
        print(
            f"snapshot={source.name} bytes={stat.st_size} "
            f"tables={len(definitions)} indexes={len(indexes)} "
            f"rows={sum(row_counts.values()):,} quick_check={check}",
            flush=True,
        )
        if args.dry_run:
            for definition in definitions:
                print(
                    f"{definition['name']}\t{row_counts[definition['name']]:,}",
                    flush=True,
                )
            return

        token = os.environ.get("IMPORT_TOKEN")
        if not token:
            raise ValueError("set IMPORT_TOKEN in the environment; do not pass it on the command line")
        if not args.base_url:
            raise ValueError("--base-url is required unless --dry-run is set")

        schema = [
            {
                "name": definition["name"],
                "sql": definition["sql"],
                "columns": definition["insertColumns"],
            }
            for definition in definitions
        ]
        request_json(
            args.base_url,
            token,
            "/api/cloudflare/import/schema",
            {"tables": schema},
        )

        total_rows = sum(row_counts.values())
        completed_rows = 0
        for definition in definitions:
            table = definition["name"]
            last_cursor = state.get("lastCursor", {}).get(
                table, state.get("lastRowid", {}).get(table)
            )
            if last_cursor is not None:
                last_cursor = int(last_cursor)
            chunk_limit = args.batch_rows
            while True:
                chunk = read_chunk(conn, definition, last_cursor, chunk_limit)
                if not chunk:
                    break

                end_rowid = int(chunk[-1][0])
                groups: dict[int, list[list[Any]]] = {}
                for raw in chunk:
                    rowid = int(raw[0])
                    values = tuple(raw[1:]) if definition["integerPrimaryKey"] else tuple(raw)
                    shard = row_shard(definition, rowid, tuple(raw[1:]))
                    groups.setdefault(shard, []).append(row_to_json(values))

                batch_id = f"{table}:{last_cursor}:{end_rowid}"
                request_body = {
                    "table": table,
                    "columns": definition["insertColumns"],
                    "batches": [
                        {
                            "shard": shard,
                            "batchId": batch_id,
                            "rows": rows,
                        }
                        for shard, rows in sorted(groups.items())
                    ],
                }
                if json_request_size(request_body) > MAX_REQUEST_BODY_BYTES:
                    if chunk_limit <= 1:
                        raise ValueError(
                            f"single-row import batch for {table!r} exceeds "
                            f"{MAX_REQUEST_BODY_BYTES} UTF-8 bytes after compression"
                        )
                    chunk_limit = max(1, chunk_limit // 2)
                    continue

                request_json(
                    args.base_url,
                    token,
                    "/api/cloudflare/import/batch",
                    request_body,
                )
                last_cursor = end_rowid
                state.setdefault("lastCursor", {})[table] = last_cursor
                save_checkpoint(checkpoint, state)
                completed_rows += len(chunk)
                print(
                    f"imported={completed_rows:,}/{total_rows:,} "
                    f"table={table} cursor={last_cursor}",
                    flush=True,
                )
                if chunk_limit < args.batch_rows:
                    chunk_limit = min(args.batch_rows, chunk_limit * 2)

        for index_sql in indexes:
            request_json(
                args.base_url,
                token,
                "/api/cloudflare/import/schema",
                {"indexes": [index_sql]},
            )

        remote = remote_status(args.base_url, token)
        imported = remote.get("tables", {})
        mismatches = {
            table: {"local": expected, "remote": int(imported.get(table, 0))}
            for table, expected in row_counts.items()
            if int(imported.get(table, 0)) != expected
        }
        if mismatches:
            raise RuntimeError(f"row-count verification failed: {mismatches}")
        state["verified"] = {
            "tables": len(definitions),
            "rows": total_rows,
            "storageBytes": remote.get("storageBytes"),
            "verifiedAt": int(time.time()),
        }
        save_checkpoint(checkpoint, state)
        print(
            f"verified tables={len(definitions)} rows={total_rows:,} "
            f"cloudflare_storage_bytes={remote.get('storageBytes')}",
            flush=True,
        )
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="immutable SQLite snapshot")
    parser.add_argument("--base-url", help="deployed Worker base URL")
    parser.add_argument(
        "--checkpoint",
        default="tmp/cloudflare-import/import-checkpoint.json",
        help="local resumable progress file",
    )
    parser.add_argument("--batch-rows", type=int, default=DEFAULT_BATCH_ROWS)
    parser.add_argument("--dry-run", action="store_true", help="validate and report source only")
    args = parser.parse_args()
    if args.batch_rows < 1 or args.batch_rows > 10_000:
        parser.error("--batch-rows must be in the range 1..10000")
    return args


if __name__ == "__main__":
    try:
        run_import(parse_args())
    except Exception as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        sys.exit(1)
