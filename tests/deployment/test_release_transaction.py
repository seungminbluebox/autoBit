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
    (deploy / "telegram_credentials.py").write_text("# migration tool\n")
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
    seed = base / "seed/paper.sqlite3"
    seed.parent.mkdir(parents=True)
    connection = sqlite3.connect(seed)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.executescript("""
        CREATE TABLE schema_version(version INTEGER);
        INSERT INTO schema_version VALUES (1);
        CREATE TABLE events(sequence INTEGER);
        INSERT INTO events VALUES (1);
        CREATE TABLE orders(id INTEGER);
        CREATE TABLE snapshots(id INTEGER);
        """)
        connection.commit()
        assert Path(str(seed) + "-wal").stat().st_size > 32, "fixture must contain real WAL frames"
        state = base / "state"
        state.mkdir()
        shutil.copy2(seed, state / "paper.sqlite3")
        shutil.copy2(Path(str(seed) + "-wal"), state / "paper.sqlite3-wal")
    finally:
        connection.close()
    operation = run('backup_closed_ledger\n', base)
    assert operation.returncode == 0, operation.stderr
    backup = Path(operation.stdout.strip())
    bundle = backup / "bundle"
    checksums = (backup / "SHA256SUMS").read_text().splitlines()
    assert {line.split("  ", 1)[1] for line in checksums} == {
        "paper.sqlite3", "paper.sqlite3-wal"
    }
    assert {item.name for item in bundle.iterdir()} == {
        "paper.sqlite3", "paper.sqlite3-wal"
    }
    before = {item.name: item.read_bytes() for item in bundle.iterdir()}
    subprocess.run(
        ["sha256sum", "-c", str(backup / "SHA256SUMS")], cwd=bundle,
        check=True, capture_output=True,
    )
    assert {item.name: item.read_bytes() for item in bundle.iterdir()} == before
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


def test_dangling_ledger_symlink_is_not_classified_as_first_install():
    result = _run_linux_python(HARNESS + r'''
with tempfile.TemporaryDirectory(prefix="autobit-dangling-ledger-") as fixture:
    base = Path(fixture)
    candidate(base)
    (base / "state").mkdir()
    outside = base / "outside"
    outside.mkdir()
    (base / "state/paper.sqlite3").symlink_to(outside / "missing.sqlite3")
    operation = run('backup_closed_ledger\n', base)
    assert operation.returncode != 0
    assert "ledger" in operation.stderr.lower()
    assert not (base / "backups" / f"{TIMESTAMP}-{COMMIT}").exists()
    assert list(outside.iterdir()) == []
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
    (proc / "cmdline").write_bytes(
        b"python\0-m\0autobit.cli\0paper-run\0--db\0/var/lib/autobit/paper/paper.sqlite3.old\0"
    )
    operation = run('AUTOBIT_PROC_ROOT="$fixture/proc"\nassert_no_paper_writer\n', base)
    assert operation.returncode == 0, operation.stderr
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
wait_for_paper_service_stop() { :; }
assert_no_paper_writer() { :; }
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
wait_for_paper_service_stop() { printf 'inactive\n' >> "$fixture/actions"; }
assert_no_paper_writer() { printf 'no-writer\n' >> "$fixture/actions"; }
rollback_code_only
""", base)
    assert operation.returncode == 0, operation.stderr
    assert (base / "actions").read_text().splitlines() == [
        "stop", "disable", "inactive", "no-writer"
    ]
    assert state.read_bytes() == b"new-ledger"
    assert not (base / "current").exists() and not (base / "current").is_symlink()
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_post_backup_failure_matrix_restores_code_config_and_service_without_db_restore():
    result = _run_linux_python(HARNESS + r'''
stages = ("config", "journal-storage", "journald-reload", "journald-probe", "link",
          "daemon-reload", "enable", "start", "readiness")
cases = (
    (True, "disabled", "inactive"),
    (False, "enabled", "active"),
    (False, "enabled", "inactive"),
    (False, "disabled", "active"),
    (False, "disabled", "inactive"),
)
for first_install, expected_enabled, expected_active in cases:
    for stage in stages:
        with tempfile.TemporaryDirectory(prefix="autobit-activation-matrix-") as fixture:
            base = Path(fixture)
            wanted = candidate(base)
            previous = base / "releases" / ("0" * 40)
            previous.mkdir(parents=True)
            state = base / "state"
            state.mkdir()
            database = state / "paper.sqlite3"
            database.write_bytes(b"never-restore-this-ledger")
            evidence = base / "evidence"
            evidence.mkdir()
            (evidence / "retained.txt").write_text("retained\n")
            config = base / "config"
            config.mkdir()
            backup = base / "config-backup"
            backup.mkdir()
            if not first_install:
                (base / "current").symlink_to(previous)
                (config / "unit").write_text("previous unit\n")
                (config / "journal").write_text("previous journal\n")
                (backup / "autobit-paper.service").write_text("previous unit\n")
                (backup / "99-autobit-persistence.conf").write_text("previous journal\n")
            script = r"""
set -eu
source "$1"
fixture=$2
commit=$7
candidate_release="$fixture/releases/$commit"
previous_release=$3
current_link="$fixture/current"
ledger="$fixture/state/paper.sqlite3"
ledger_backup="$fixture/evidence"
unit_destination="$fixture/config/unit"
journal_destination="$fixture/config/journal"
config_backup="$fixture/config-backup"
previous_enabled=$4
previous_active=$5
unit_config_state=
journal_config_state=
activation_in_progress=1
candidate_enable_attempted=0
candidate_start_attempted=0
fail_stage=$6
active=inactive
enabled=disabled
[ "$previous_active" = active ] && active=inactive
printf '%s\n' "$active" > "$fixture/service-active"
printf '%s\n' "$enabled" > "$fixture/service-enabled"
maybe_fail() {
    [ "$1" != "$fail_stage" ] && return 0
    [ -e "$fixture/failed-once" ] && return 0
    : > "$fixture/failed-once"
    return 1
}
install_release_config() {
    maybe_fail config
    if [ -e "$unit_destination" ]; then
        unit_config_state=EXISTING
        journal_config_state=EXISTING
    else
        unit_config_state=ABSENT
        journal_config_state=ABSENT
    fi
    printf 'candidate unit\n' > "$unit_destination"
    printf 'candidate journal\n' > "$journal_destination"
    printf 'config\n' >> "$fixture/actions"
}
prepare_persistent_journal_storage() {
    printf 'journal-storage\n' >> "$fixture/actions"
    maybe_fail journal-storage
}
journald_reload() { printf 'journald-reload\n' >> "$fixture/actions"; maybe_fail journald-reload; }
journald_probe() { printf 'journald-probe\n' >> "$fixture/actions"; maybe_fail journald-probe; }
restart_journald() { printf 'rollback-journald\n' >> "$fixture/actions"; }
switch_current_atomically() {
    [ ! -e "$current_link" ] || unlink -- "$current_link"
    ln -s -- "$1" "$current_link"
    printf 'link:%s\n' "$1" >> "$fixture/actions"
    maybe_fail link
}
require_protected_directory() { :; }
validate_installed_config() { [ -f "$1" ] && [ ! -L "$1" ]; }
install() {
    while [ "$1" != -- ]; do shift; done
    shift
    command cp -- "$1" "$2"
    command chmod 0644 -- "$2"
}
service_stop() {
    if [ -z "$previous_release" ] && [ ! -e "$unit_destination" ]; then
        printf 'stop-missing-unit\n' >> "$fixture/actions"
        return 1
    fi
    printf 'inactive\n' > "$fixture/service-active"
    printf 'stop\n' >> "$fixture/actions"
}
service_disable() {
    if [ -z "$previous_release" ] && [ ! -e "$unit_destination" ]; then
        printf 'disable-missing-unit\n' >> "$fixture/actions"
        return 1
    fi
    printf 'disabled\n' > "$fixture/service-enabled"
    printf 'disable\n' >> "$fixture/actions"
}
service_enable() { printf 'enabled\n' > "$fixture/service-enabled"; printf 'enable\n' >> "$fixture/actions"; maybe_fail enable; }
service_start() { printf 'active\n' > "$fixture/service-active"; printf 'start\n' >> "$fixture/actions"; maybe_fail start; }
service_daemon_reload() { printf 'daemon-reload\n' >> "$fixture/actions"; maybe_fail daemon-reload; }
wait_for_started_service() { printf 'readiness\n' >> "$fixture/actions"; maybe_fail readiness; }
wait_for_paper_service_stop() { [ "$(<"$fixture/service-active")" = inactive ]; }
assert_no_paper_writer() { :; }
trap 'activation_exit_trap $?' EXIT
run_post_backup_activation
activation_in_progress=0
trap - EXIT
"""
            operation = subprocess.run(
                ["bash", "-s", "--", LIBRARY, str(base),
                 "" if first_install else str(previous),
                 expected_enabled, expected_active, stage, COMMIT],
                input=script, text=True, capture_output=True,
            )
            assert operation.returncode != 0 and operation.returncode != 70, (
                first_install, stage, operation.stdout, operation.stderr)
            assert database.read_bytes() == b"never-restore-this-ledger"
            assert (evidence / "retained.txt").read_text() == "retained\n"
            assert wanted.is_dir()
            if first_install:
                assert not (base / "current").exists() and not (base / "current").is_symlink()
                assert not (config / "unit").exists()
                assert not (config / "journal").exists()
                assert (base / "service-active").read_text().strip() == "inactive"
                assert (base / "service-enabled").read_text().strip() == "disabled"
            else:
                assert (base / "current").resolve() == previous
                assert (config / "unit").read_text() == "previous unit\n"
                assert (config / "journal").read_text() == "previous journal\n"
                assert (base / "service-active").read_text().strip() == expected_active
                assert (base / "service-enabled").read_text().strip() == expected_enabled
            assert str(wanted) in operation.stderr
            assert str(evidence) in operation.stderr
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_first_install_stop_failure_is_a_distinct_rollback_failure():
    result = _run_linux_python(HARNESS + r'''
with tempfile.TemporaryDirectory(prefix="autobit-stop-failure-") as fixture:
    base = Path(fixture)
    candidate(base)
    evidence = base / "backups/evidence"
    operation = run(r"""
previous_release=
ledger_backup="$fixture/backups/evidence"
activation_in_progress=1
service_stop() { return 1; }
service_disable() { printf 'disable\n' >> "$fixture/actions"; }
activation_exit_trap 9
""", base)
    assert operation.returncode == 70
    assert (base / "actions").read_text().splitlines() == ["disable"]
    assert "previous=NO_PREVIOUS_RELEASE" in operation.stderr
    assert str(base / "releases" / COMMIT) in operation.stderr
    assert str(evidence) in operation.stderr
''')
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("failed_check", ("active", "writer"))
def test_first_install_rollback_requires_inactive_writer_free_service(failed_check):
    result = _run_linux_python(HARNESS + f'''
with tempfile.TemporaryDirectory(prefix="autobit-stop-verification-") as fixture:
    base = Path(fixture)
    candidate(base)
    operation = run(r"""
previous_release=
service_stop() {{ :; }}
service_disable() {{ :; }}
wait_for_paper_service_stop() {{ {'return 1' if failed_check == 'active' else ':'}; }}
assert_no_paper_writer() {{ {'return 1' if failed_check == 'writer' else ':'}; }}
rollback_code_only
""", base)
    assert operation.returncode != 0
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
wait_for_paper_service_stop() { printf 'inactive\n' >> "$fixture/actions"; }
assert_no_paper_writer() { printf 'no-writer\n' >> "$fixture/actions"; }
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
        "stop", "inactive", "no-writer", "journald", "daemon-reload", "disable", "start"
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
wait_for_paper_service_stop() { :; }
assert_no_paper_writer() { :; }
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


def test_started_service_command_requires_exact_argv_and_paths():
    result = _run_linux_python(HARNESS + r'''
with tempfile.TemporaryDirectory(prefix="autobit-service-command-") as fixture:
    base = Path(fixture)
    proc = base / "proc/321"
    proc.mkdir(parents=True)
    expected = [
        "/opt/autobit/current/.venv/bin/python", "-m", "autobit.cli", "paper-run",
        "--db", "/var/lib/autobit/paper/paper.sqlite3",
        "--data-dir", "/var/lib/autobit/raw/paper",
        "--telegram-token-env", "AUTOBIT_TELEGRAM_TOKEN",
        "--telegram-chat-env", "AUTOBIT_TELEGRAM_CHAT_ID",
    ]
    def check(arguments):
        (proc / "cmdline").write_bytes(b"\0".join(value.encode() for value in arguments) + b"\0")
        return run(
            'AUTOBIT_PROC_ROOT="$fixture/proc"\n'
            'production_ledger=/var/lib/autobit/paper/paper.sqlite3\n'
            'production_data=/var/lib/autobit/raw/paper\n'
            'service_command_is_expected 321\n',
            base,
        )
    assert check(expected).returncode == 0
    near_matches = (
        [*expected[:5], expected[5] + ".old", *expected[6:]],
        [*expected[:7], expected[7] + "-alt"],
        [expected[0] + ".old", *expected[1:]],
        [*expected, "--db", expected[5]],
    )
    for arguments in near_matches:
        assert check(arguments).returncode != 0, arguments
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_candidate_missing_telegram_migration_tool_is_refused():
    result = _run_linux_python(HARNESS + r'''
with tempfile.TemporaryDirectory(prefix="autobit-readiness-telegram-") as fixture:
    base = Path(fixture)
    release = candidate(base)
    (release / "deploy/oci/telegram_credentials.py").unlink()
    operation = run('validate_release_candidate\n', base)
    assert operation.returncode != 0
    assert "telegram_credentials.py" in operation.stderr
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_notification_config_requires_exact_private_two_key_file():
    result = _run_linux_python(HARNESS + r'''
with tempfile.TemporaryDirectory(prefix="autobit-notification-config-") as fixture:
    base = Path(fixture)
    config = base / "paper-notify.env"
    uid = os.getuid()

    def check(payload, mode=0o600):
        config.write_text(payload)
        config.chmod(mode)
        operation = run(
            f'validate_notification_config "$fixture/paper-notify.env" {uid} {os.getgid()}\n',
            base,
        )
        config.unlink()
        return operation

    valid = check(
        "AUTOBIT_TELEGRAM_TOKEN=123456789:abcdefghijklmnopqrstuvwxyz_ABCD\n"
        "AUTOBIT_TELEGRAM_CHAT_ID=-123456789\n"
    )
    assert valid.returncode == 0, valid.stderr

    invalid_payloads = (
        "AUTOBIT_TELEGRAM_TOKEN=123456789:abcdefghijklmnopqrstuvwxyz_ABCD\n",
        "AUTOBIT_TELEGRAM_CHAT_ID=-123456789\n",
        "AUTOBIT_TELEGRAM_TOKEN=token\nAUTOBIT_TELEGRAM_CHAT_ID=-123\n",
        "AUTOBIT_TELEGRAM_TOKEN=123456789:abcdefghijklmnopqrstuvwxyz_ABCD\n"
        "AUTOBIT_TELEGRAM_CHAT_ID=-123456789\nUPBIT_ACCESS_KEY=forbidden\n",
        "AUTOBIT_TELEGRAM_TOKEN=123456789:abcdefghijklmnopqrstuvwxyz_ABCD\n"
        "AUTOBIT_TELEGRAM_TOKEN=123456789:abcdefghijklmnopqrstuvwxyz_ABCD\n"
        "AUTOBIT_TELEGRAM_CHAT_ID=-123456789\n",
    )
    for payload in invalid_payloads:
        rejected = check(payload)
        assert rejected.returncode != 0, payload

    public = check(
        "AUTOBIT_TELEGRAM_TOKEN=123456789:abcdefghijklmnopqrstuvwxyz_ABCD\n"
        "AUTOBIT_TELEGRAM_CHAT_ID=-123456789\n",
        mode=0o644,
    )
    assert public.returncode != 0

    config.write_text(
        "AUTOBIT_TELEGRAM_TOKEN=123456789:abcdefghijklmnopqrstuvwxyz_ABCD\n"
        "AUTOBIT_TELEGRAM_CHAT_ID=-123456789\n"
    )
    config.chmod(0o600)
    wrong_group = run(
        f'validate_notification_config "$fixture/paper-notify.env" {uid} {os.getgid() + 1}\n',
        base,
    )
    assert wrong_group.returncode != 0
''')
    assert result.returncode == 0, result.stdout + result.stderr
