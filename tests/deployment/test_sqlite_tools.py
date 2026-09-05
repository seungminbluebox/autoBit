from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from deploy.oci import sqlite_tools as module


def _ledger_fixture(path: Path, *, journal_mode: str = "DELETE") -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode={}".format(journal_mode))
    connection.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    connection.execute(
        """
        CREATE TABLE events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            occurred_at_utc TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """,
    )
    connection.execute(
        """
        CREATE TABLE orders (
            order_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            side TEXT NOT NULL,
            requested_quantity REAL NOT NULL,
            filled_quantity REAL NOT NULL,
            status TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        )
        """,
    )
    connection.execute(
        """
        CREATE TABLE snapshots (
            sequence INTEGER PRIMARY KEY,
            state_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        )
        """,
    )
    connection.execute("INSERT INTO schema_version (version) VALUES (1)")
    connection.execute(
        "INSERT INTO snapshots (sequence, state_json, created_at_utc) VALUES (?, ?, ?)",
        (0, "{}", "2026-09-05T00:00:00Z"),
    )
    connection.commit()
    return connection


def test_online_snapshot_is_consistent_and_does_not_copy_companions(tmp_path: Path) -> None:
    source = tmp_path / "paper.sqlite3"
    destination = tmp_path / "snapshot.sqlite3"
    connection = _ledger_fixture(source, journal_mode="WAL")
    connection.execute(
        "INSERT INTO events(event_id, event_type, occurred_at_utc, payload_json) VALUES(?, ?, ?, ?)",
        ("cycle:1", "PAPER_CYCLE", "2026-09-05T00:00:00Z", "{}"),
    )
    connection.commit()

    module.snapshot(source, destination)

    assert module.probe(destination)["event_count"] == 1
    assert not Path("{}-wal".format(destination)).exists()
    assert not Path("{}-shm".format(destination)).exists()
    connection.close()


def test_snapshot_reads_source_paths_with_uri_reserved_characters(tmp_path: Path) -> None:
    source = tmp_path / "paper #%.sqlite3"
    destination = tmp_path / "snapshot.sqlite3"
    connection = _ledger_fixture(source)
    connection.close()

    module.snapshot(source, destination)

    assert module.probe(destination)["event_count"] == 0


def test_snapshot_rejects_missing_source(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        module.snapshot(tmp_path / "missing.sqlite3", tmp_path / "snapshot.sqlite3")


@pytest.mark.parametrize("destination_name", ("paper.sqlite3", "existing.sqlite3"))
def test_snapshot_rejects_existing_or_identical_destination(
    tmp_path: Path, destination_name: str
) -> None:
    source = tmp_path / "paper.sqlite3"
    connection = _ledger_fixture(source)
    connection.close()
    destination = tmp_path / destination_name
    if destination != source:
        destination.touch()

    with pytest.raises(ValueError, match="snapshot destination must be new and distinct"):
        module.snapshot(source, destination)


def test_snapshot_rejects_dangling_destination_symlink_without_creating_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.sqlite3"
    destination = tmp_path / "snapshot.sqlite3"
    external_target = tmp_path.parent / "outside.sqlite3"
    connection = _ledger_fixture(source)
    connection.close()
    destination.symlink_to(external_target)

    with pytest.raises(ValueError, match="snapshot destination must be new and distinct"):
        module.snapshot(source, destination)

    assert destination.is_symlink()
    assert not external_target.exists()


def test_snapshot_removes_its_new_destination_when_quick_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "paper.sqlite3"
    destination = tmp_path / "nested" / "snapshot.sqlite3"
    connection = _ledger_fixture(source)
    connection.close()
    monkeypatch.setattr(module, "_quick_check", lambda _: False)

    with pytest.raises(RuntimeError, match="snapshot quick_check failed"):
        module.snapshot(source, destination)

    assert not destination.exists()
    assert source.exists()


def test_probe_reports_persistent_paper_state(tmp_path: Path) -> None:
    database = tmp_path / "paper.sqlite3"
    connection = _ledger_fixture(database)
    connection.execute(
        "INSERT INTO events(event_id, event_type, occurred_at_utc, payload_json) VALUES (?, ?, ?, ?)",
        ("cycle:2", "PAPER_CYCLE", "2026-09-05T00:00:01Z", "{}"),
    )
    connection.execute(
        """
        INSERT INTO orders(
            order_id, idempotency_key, side, requested_quantity, filled_quantity,
            status, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("order:1", "cycle:2", "BUY", 1.0, 1.0, "FILLED", "2026-09-05T00:00:01Z"),
    )
    connection.execute(
        "INSERT INTO snapshots (sequence, state_json, created_at_utc) VALUES (?, ?, ?)",
        (2, "{}", "2026-09-05T00:00:01Z"),
    )
    connection.commit()
    connection.close()

    assert module.probe(database) == {
        "schema_version": 1,
        "event_count": 1,
        "max_event_sequence": 1,
        "order_count": 1,
        "snapshot_count": 2,
        "quick_check": "ok",
    }


def test_probe_rejects_unknown_schema_version(tmp_path: Path) -> None:
    database = tmp_path / "paper.sqlite3"
    connection = _ledger_fixture(database)
    connection.execute("UPDATE schema_version SET version = 999")
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="unsupported schema version"):
        module.probe(database)


def test_probe_rejects_a_database_missing_a_required_table(tmp_path: Path) -> None:
    database = tmp_path / "paper.sqlite3"
    connection = _ledger_fixture(database)
    connection.execute("DROP TABLE orders")
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="missing required tables"):
        module.probe(database)


def test_probe_cli_emits_sorted_compact_json(tmp_path: Path) -> None:
    database = tmp_path / "paper.sqlite3"
    connection = _ledger_fixture(database)
    connection.close()

    result = subprocess.run(
        [sys.executable, "deploy/oci/sqlite_tools.py", "probe", "--db", str(database)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == json.dumps(module.probe(database), sort_keys=True, separators=(",", ":")) + "\n"
