"""Behavioral contracts for stopped-ledger release activation."""

from functools import lru_cache
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
COMMIT = "1" * 40


@lru_cache
def _shell_prefix():
    if os.name != "nt":
        return []
    if not shutil.which("wsl.exe"):
        pytest.skip("No WSL distribution is available")
    result = subprocess.run(["wsl.exe", "bash", "-c", "true"], capture_output=True)
    if result.returncode:
        pytest.skip("No runnable WSL distribution is available")
    return ["wsl.exe", "--exec"]


def _linux_path(path):
    if os.name != "nt":
        return str(path)
    _shell_prefix()
    return subprocess.run(
        ["wsl.exe", "wslpath", "-a", str(path)], check=True,
        capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()


def _run_linux_python(code):
    return subprocess.run(
        [*_shell_prefix(), "/usr/bin/python3", "-", _linux_path(ROOT / "deploy/oci/libdeploy.sh"),
         _linux_path(ROOT / "deploy/oci/sqlite_tools.py")],
        input=code, capture_output=True, text=True, encoding="utf-8",
    )


HARNESS = r'''
from pathlib import Path
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

LIBRARY, SQLITE_TOOLS = sys.argv[1:]
COMMIT = "1" * 40
TIMESTAMP = "20260905T010203Z"

def run(script, base):
    prefix = f"""set -eu\nsource "$1"\ncommit={COMMIT}\nactivation_timestamp={TIMESTAMP}\nfixture="$2"\ncandidate_release="$fixture/releases/{COMMIT}"\nledger="$fixture/state/paper.sqlite3"\nbackup_root="$fixture/backups"\ncurrent_link="$fixture/current"\n"""
    return subprocess.run(["bash", "-s", "--", LIBRARY, str(base)], input=prefix + script,
                          text=True, capture_output=True)

def candidate(base, *, metadata=True):
    release = base / "releases" / COMMIT
    (release / ".venv/bin").mkdir(parents=True)
    python = release / ".venv/bin/python"
    python.write_text('#!/bin/sh\necho \'{"ready":true}\'\n')
    python.chmod(0o755)
    deploy = release / "deploy/oci"
    (deploy / "systemd").mkdir(parents=True)
    (deploy / "journald").mkdir()
    shutil.copyfile(SQLITE_TOOLS, deploy / "sqlite_tools.py")
    (deploy / "systemd/autobit-paper.service").write_text("[Service]\n")
    (deploy / "journald/99-autobit-persistence.conf").write_text("[Journal]\n")
    for path in deploy.rglob("*"):
        if path.is_file():
            path.chmod(0o644)
    if metadata:
        (release / ".autobit-release").write_text(json.dumps({
            "commit": COMMIT,
            "python_version": "3.12.14",
            "source_archive_sha256": "2" * 64,
            "uv_lock_sha256": "3" * 64,
            "uv_version": "0.12.10",
        }, sort_keys=True, separators=(",", ":")) + "\n")
        (release / ".autobit-release").chmod(0o644)
    return release

def ledger(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript("""
        CREATE TABLE schema_version(version INTEGER);
        INSERT INTO schema_version VALUES (1);
        CREATE TABLE events(sequence INTEGER);
        CREATE TABLE orders(id INTEGER);
        CREATE TABLE snapshots(id INTEGER);
        """)
'''


def test_backup_copies_db_and_wal_but_never_shm():
    result = _run_linux_python(HARNESS + r'''
with tempfile.TemporaryDirectory(prefix="autobit-transaction-") as fixture:
    base = Path(fixture)
    candidate(base)
    ledger(base / "state/paper.sqlite3")
    (base / "state/paper.sqlite3-wal").write_bytes(b"")
    (base / "state/paper.sqlite3-shm").write_bytes(b"transient")
    operation = run('backup_closed_ledger\n', base)
    assert operation.returncode == 0, operation.stderr
    backup = Path(operation.stdout.strip())
    assert (backup / "bundle/paper.sqlite3").is_file()
    assert (backup / "bundle/paper.sqlite3-wal").is_file()
    assert not (backup / "bundle/paper.sqlite3-shm").exists()
    assert (backup / "SHA256SUMS").is_file()
    assert (backup / "verification/paper-status.json").is_file()
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_first_install_records_no_existing_ledger():
    result = _run_linux_python(HARNESS + r'''
with tempfile.TemporaryDirectory(prefix="autobit-first-install-") as fixture:
    base = Path(fixture)
    candidate(base)
    (base / "state").mkdir()
    operation = run('backup_closed_ledger\n', base)
    assert operation.returncode == 0, operation.stderr
    backup = Path(operation.stdout.strip())
    assert (backup / "ledger-state.txt").read_bytes() == b"NO_EXISTING_LEDGER\n"
    assert not (backup / "bundle").exists()
''')
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("companion", ("-wal", "-shm"))
def test_orphan_ledger_companion_is_refused(companion):
    result = _run_linux_python(HARNESS + f'''
with tempfile.TemporaryDirectory(prefix="autobit-orphan-") as fixture:
    base = Path(fixture)
    candidate(base)
    (base / "state").mkdir()
    (base / "state/paper.sqlite3{companion}").write_bytes(b"orphan")
    operation = run('backup_closed_ledger\\n', base)
    assert operation.returncode != 0
    assert "orphan" in operation.stderr.lower()
    assert not (base / "backups" / f"{{TIMESTAMP}}-{{COMMIT}}").exists()
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_existing_backup_directory_is_refused():
    result = _run_linux_python(HARNESS + r'''
with tempfile.TemporaryDirectory(prefix="autobit-existing-backup-") as fixture:
    base = Path(fixture)
    candidate(base)
    (base / "state").mkdir()
    existing = base / "backups" / f"{TIMESTAMP}-{COMMIT}"
    existing.mkdir(parents=True)
    marker = existing / "keep"
    marker.write_text("untouched")
    operation = run('backup_closed_ledger\n', base)
    assert operation.returncode != 0
    assert "already exists" in operation.stderr.lower()
    assert marker.read_text() == "untouched"
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_backup_root_symlink_is_refused_without_writing_through_it():
    result = _run_linux_python(HARNESS + r'''
with tempfile.TemporaryDirectory(prefix="autobit-backup-link-") as fixture:
    base = Path(fixture)
    candidate(base)
    (base / "state").mkdir()
    outside = base / "outside"
    outside.mkdir()
    (base / "backups").symlink_to(outside, target_is_directory=True)
    operation = run('backup_closed_ledger\n', base)
    assert operation.returncode != 0
    assert list(outside.iterdir()) == []
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_writable_backup_root_is_refused_before_evidence_creation():
    result = _run_linux_python(HARNESS + r'''
with tempfile.TemporaryDirectory(prefix="autobit-backup-mode-") as fixture:
    base = Path(fixture)
    candidate(base)
    (base / "state").mkdir()
    backup_root = base / "backups"
    backup_root.mkdir(mode=0o777)
    backup_root.chmod(0o777)
    operation = run('backup_closed_ledger\n', base)
    assert operation.returncode != 0
    assert "protected directory" in operation.stderr.lower()
    assert list(backup_root.iterdir()) == []
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_inactive_writer_check_rejects_exact_production_writer():
    result = _run_linux_python(HARNESS + r'''
with tempfile.TemporaryDirectory(prefix="autobit-writer-check-") as fixture:
    base = Path(fixture)
    proc = base / "proc/123"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(
        b"python\0-m\0autobit.cli\0paper-run\0--db\0/var/lib/autobit/paper/paper.sqlite3\0"
    )
    operation = run('AUTOBIT_PROC_ROOT="$fixture/proc"\nassert_no_paper_writer\n', base)
    assert operation.returncode != 0
    (proc / "cmdline").write_bytes(b"python\0-m\0autobit.cli\0paper-status\0")
    operation = run('AUTOBIT_PROC_ROOT="$fixture/proc"\nassert_no_paper_writer\n', base)
    assert operation.returncode == 0, operation.stderr
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_candidate_missing_readiness_metadata_is_refused():
    result = _run_linux_python(HARNESS + r'''
with tempfile.TemporaryDirectory(prefix="autobit-readiness-") as fixture:
    base = Path(fixture)
    candidate(base, metadata=False)
    operation = run('validate_release_candidate\n', base)
    assert operation.returncode != 0
    assert "readiness" in operation.stderr.lower()
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_atomic_switch_restores_previous_link_without_restoring_db():
    result = _run_linux_python(HARNESS + r'''
with tempfile.TemporaryDirectory(prefix="autobit-code-rollback-") as fixture:
    base = Path(fixture)
    previous = base / "releases" / ("0" * 40)
    previous.mkdir(parents=True)
    wanted = candidate(base)
    (base / "state").mkdir()
    state = base / "state/paper.sqlite3"
    state.write_bytes(b"production-ledger")
    before = state.read_bytes()
    (base / "current").symlink_to(previous)
    operation = run(r"""
previous_release="$fixture/releases/0000000000000000000000000000000000000000"
previous_enabled=enabled
switch_current_atomically "$candidate_release"
service_stop() { :; }
service_daemon_reload() { :; }
service_enable() { :; }
service_start() { :; }
restore_config_backups() { :; }
rollback_code_only
""", base)
    assert operation.returncode == 0, operation.stderr
    assert (base / "current").resolve() == previous
    assert state.read_bytes() == before
    assert wanted.is_dir()
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_first_install_rollback_stops_and_disables_candidate():
    result = _run_linux_python(HARNESS + r'''
with tempfile.TemporaryDirectory(prefix="autobit-first-rollback-") as fixture:
    base = Path(fixture)
    candidate(base)
    (base / "state").mkdir()
    state = base / "state/paper.sqlite3"
    state.write_bytes(b"new-ledger")
    operation = run(r"""
previous_release=
switch_current_atomically "$candidate_release"
service_stop() { printf 'stop\n' >> "$fixture/actions"; }
service_disable() { printf 'disable\n' >> "$fixture/actions"; }
rollback_code_only
""", base)
    assert operation.returncode == 0, operation.stderr
    assert (base / "actions").read_text().splitlines() == ["stop", "disable"]
    assert state.read_bytes() == b"new-ledger"
    assert (base / "current").resolve() == base / "releases" / COMMIT
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_existing_release_rollback_restores_config_and_effective_service_state():
    result = _run_linux_python(HARNESS + r'''
with tempfile.TemporaryDirectory(prefix="autobit-config-rollback-") as fixture:
    base = Path(fixture)
    previous = base / "releases" / ("0" * 40)
    previous.mkdir(parents=True)
    candidate(base)
    (base / "current").symlink_to(base / "releases" / COMMIT)
    config = base / "config"
    config.mkdir()
    backup = base / "config-backup"
    backup.mkdir()
    (config / "unit").write_text("candidate unit\n")
    (config / "journal").write_text("candidate journal\n")
    (backup / "autobit-paper.service").write_text("previous unit\n")
    (backup / "99-autobit-persistence.conf").write_text("previous journal\n")
    operation = run(r"""
previous_release="$fixture/releases/0000000000000000000000000000000000000000"
previous_enabled=disabled
unit_destination="$fixture/config/unit"
journal_destination="$fixture/config/journal"
config_backup="$fixture/config-backup"
unit_config_state=EXISTING
journal_config_state=EXISTING
install() {
    while [ "$1" != -- ]; do shift; done
    shift
    command cp -- "$1" "$2"
    command chmod 0644 -- "$2"
}
service_stop() { printf 'stop\n' >> "$fixture/actions"; }
restart_journald() { printf 'journald\n' >> "$fixture/actions"; }
service_daemon_reload() { printf 'daemon-reload\n' >> "$fixture/actions"; }
service_disable() { printf 'disable\n' >> "$fixture/actions"; }
service_start() { printf 'start\n' >> "$fixture/actions"; }
rollback_code_only
""", base)
    assert operation.returncode == 0, operation.stderr
    assert (base / "current").resolve() == previous
    assert (config / "unit").read_text() == "previous unit\n"
    assert (config / "journal").read_text() == "previous journal\n"
    assert (base / "actions").read_text().splitlines() == [
        "stop", "journald", "daemon-reload", "disable", "start"
    ]
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_failed_rollback_returns_distinct_code_and_both_release_targets():
    result = _run_linux_python(HARNESS + r'''
with tempfile.TemporaryDirectory(prefix="autobit-rollback-failure-") as fixture:
    base = Path(fixture)
    candidate(base)
    operation = run(r"""
previous_release="$fixture/releases/0000000000000000000000000000000000000000"
previous_enabled=enabled
activation_in_progress=1
service_stop() { :; }
restore_config_backups() { :; }
restart_journald() { :; }
service_daemon_reload() { :; }
service_enable() { :; }
service_start() { :; }
activation_exit_trap 9
""", base)
    assert operation.returncode == 70
    assert str(base / "releases" / ("0" * 40)) in operation.stderr
    assert str(base / "releases" / COMMIT) in operation.stderr
''')
    assert result.returncode == 0, result.stdout + result.stderr
