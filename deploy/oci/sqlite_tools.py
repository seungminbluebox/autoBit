"""Safe SQLite snapshot and persistent-state probe helpers for deployments."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from typing import Sequence
from urllib.parse import quote


_REQUIRED_TABLES = frozenset(("schema_version", "events", "orders", "snapshots"))
_SUPPORTED_SCHEMA_VERSION = 1


def _read_only_uri(path: Path) -> str:
    return "file:{}?mode=ro".format(quote(path.as_posix(), safe="/"))


def _quick_check(connection: sqlite3.Connection) -> str | None:
    result = connection.execute("PRAGMA quick_check").fetchone()
    if result == ("ok",):
        return "ok"
    return None


def _unlink_created_destination(destination: Path, expected_parent: Path) -> None:
    if not destination.exists():
        return
    resolved_destination = destination.resolve(strict=False)
    if resolved_destination.parent == expected_parent:
        destination.unlink()


def snapshot(source: Path, destination: Path) -> None:
    """Create a transactionally consistent SQLite backup at a new path."""
    source_path = Path(source).resolve(strict=True)
    destination_path = Path(destination)
    resolved_destination = destination_path.resolve(strict=False)
    if destination_path.exists() or source_path == resolved_destination:
        raise ValueError("snapshot destination must be new and distinct")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    expected_parent = destination_path.parent.resolve(strict=True)
    destination_created = False
    try:
        with closing(sqlite3.connect(_read_only_uri(source_path), uri=True)) as source_db:
            source_db.execute("PRAGMA query_only=ON")
            with closing(sqlite3.connect(destination_path)) as destination_db:
                destination_created = True
                source_db.backup(destination_db)
                if _quick_check(destination_db) != "ok":
                    raise RuntimeError("snapshot quick_check failed")
                destination_db.execute("PRAGMA journal_mode=DELETE")
    except BaseException:
        if destination_created:
            _unlink_created_destination(destination_path, expected_parent)
        raise


def probe(database: Path) -> dict[str, object]:
    """Return the persistent paper-state facts needed after a restart."""
    database_path = Path(database).resolve(strict=True)
    with sqlite3.connect(_read_only_uri(database_path), uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        tables = {str(row[0]) for row in rows}
        missing_tables = sorted(_REQUIRED_TABLES - tables)
        if missing_tables:
            raise ValueError(
                "missing required tables: {}".format(", ".join(missing_tables))
            )

        versions = [
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_version ORDER BY version"
            ).fetchall()
        ]
        if versions != [_SUPPORTED_SCHEMA_VERSION]:
            raise ValueError("unsupported schema version: {}".format(versions))
        if _quick_check(connection) != "ok":
            raise RuntimeError("probe quick_check failed")

        event_count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        max_event_sequence = connection.execute(
            "SELECT MAX(sequence) FROM events"
        ).fetchone()[0]
        order_count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM snapshots"
        ).fetchone()[0]

    return {
        "schema_version": _SUPPORTED_SCHEMA_VERSION,
        "event_count": int(event_count),
        "max_event_sequence": max_event_sequence,
        "order_count": int(order_count),
        "snapshot_count": int(snapshot_count),
        "quick_check": "ok",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--source", type=Path, required=True)
    snapshot_parser.add_argument("--destination", type=Path, required=True)
    probe_parser = subparsers.add_parser("probe")
    probe_parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "snapshot":
            snapshot(args.source, args.destination)
        else:
            print(json.dumps(probe(args.db), sort_keys=True, separators=(",", ":")))
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
