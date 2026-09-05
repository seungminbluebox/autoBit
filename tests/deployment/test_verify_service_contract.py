import json
from functools import lru_cache
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/oci/verify-service.sh"
OCI_RUNBOOK = ROOT / "docs/runbooks/oci-paper.md"


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


def _linux_path(path: Path) -> str:
    if os.name != "nt":
        return str(path)
    _shell_prefix()
    windows_path = str(path).replace("\\", "/")
    return subprocess.run(
        ["wsl.exe", "wslpath", "-a", windows_path],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _run_dispatch_harness(mode: str, failure: str = "") -> subprocess.CompletedProcess[str]:
    harness = r'''
set -eu
source "$1"
events="$2"
failure=$3
initialize_evidence() { evidence=/tmp/mock-verification-evidence; printf 'initialize\n' >> "$events"; }
collect_evidence() {
    printf 'collect:%s\n' "$1" >> "$events"
    [ "$failure" != "collect-$1" ]
}
systemctl() {
    [ "$1" = restart ] && [ "$2" = autobit-paper.service ]
    printf 'restart\n' >> "$events"
    [ "$failure" != restart ]
}
wait_for_started_service() { printf 'readiness\n' >> "$events"; [ "$failure" != readiness ]; }
compare_restart_evidence() { printf 'compare\n' >> "$events"; [ "$failure" != compare ]; }
verify_journal_preservation() { printf 'journal\n' >> "$events"; [ "$failure" != journal ]; }
dispatch_verification "$4"
'''
    import tempfile

    with tempfile.TemporaryDirectory(prefix="autobit-verify-dispatch-") as directory:
        events = Path(directory) / "events.txt"
        events.touch()
        result = subprocess.run(
            [
                *_shell_prefix(),
                "bash",
                "-s",
                "--",
                _linux_path(SCRIPT),
                _linux_path(events),
                failure,
                mode,
            ],
            input=harness.encode("utf-8"),
            capture_output=True,
        )
        result.stdout = result.stdout.decode("utf-8")
        result.stderr = result.stderr.decode("utf-8")
        result.events = events.read_text(encoding="utf-8").splitlines() if events.exists() else []  # type: ignore[attr-defined]
        return result


def _script_text() -> str:
    assert SCRIPT.is_file(), "missing service verification script"
    return SCRIPT.read_text(encoding="utf-8")


def _function(text: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}", text, re.M | re.S)
    assert match is not None, f"missing shell function: {name}"
    return match.group(0)


def _python_heredoc(function_name: str) -> str:
    function = _function(_script_text(), function_name)
    match = re.search(r"<<'PY'\n(.*?)\nPY", function, re.S)
    assert match is not None, f"missing Python validator in {function_name}"
    return match.group(1)


def _valid_restart_evidence(root: Path) -> None:
    status = {
        "market": "KRW-BTC",
        "mode": "normalized-paper",
        "last_completed_candle_utc": None,
        "normalized_cash": 100.0,
        "btc_quantity": 0.0,
        "position_state": "FLAT",
        "active_stop": None,
        "pending_orders": [],
        "health_stage": "NORMAL",
    }
    probe = {"event_count": 0, "max_event_sequence": None}
    for phase in ("before", "after"):
        (root / f"paper-status-{phase}.json").write_text(
            json.dumps(status), encoding="utf-8"
        )
        (root / f"ledger-probe-{phase}.json").write_text(
            json.dumps(probe), encoding="utf-8"
        )


def _run_restart_comparison(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _python_heredoc("compare_restart_evidence"), str(root)],
        check=False,
        capture_output=True,
        text=True,
    )


def _write_service_evidence(root: Path, before: str, after: str) -> None:
    for phase, invocation_id in (("before", before), ("after", after)):
        (root / f"service-{phase}.txt").write_text(
            "ActiveState=active\n"
            "SubState=running\n"
            "MainPID=123\n"
            f"InvocationID={invocation_id}\n"
            "ExecMainStartTimestampMonotonic=123456\n",
            encoding="utf-8",
        )


def _run_journal_validation(root: Path) -> subprocess.CompletedProcess[str]:
    cursor = "s=before;i=1"
    before = root / "journal-before-readable-after-restart.export"
    after = root / "journal-after-saved-cursor.export"
    before.write_text(f"__CURSOR={cursor}\nMESSAGE=before\n", encoding="utf-8")
    if not after.exists():
        after.write_text("__CURSOR=s=after;i=2\nMESSAGE=after\n", encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _python_heredoc("verify_journal_preservation"),
            cursor,
            str(before),
            str(after),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_inspect_is_read_only_and_restart_is_one_explicit_mutation():
    text = _script_text()
    inspect = _function(text, "run_inspect")
    restart = _function(text, "run_restart")

    assert "collect_evidence before" in inspect
    for mutation in (
        "systemctl restart",
        "systemctl stop",
        "systemctl start",
        "systemctl enable",
        "systemctl disable",
        "systemctl daemon-reload",
    ):
        assert mutation not in inspect

    restart_call = "systemctl restart autobit-paper.service"
    assert text.count(restart_call) == 1
    assert restart.index("collect_evidence before") < restart.index(restart_call)
    assert restart.count(restart_call) == 1
    assert "wait_for_started_service" in restart
    assert "180" in text


def test_executable_dispatcher_orders_inspect_and_exactly_one_restart():
    inspect = _run_dispatch_harness("inspect")
    restart = _run_dispatch_harness("restart")

    assert inspect.returncode == 0, inspect.stderr
    assert inspect.events == ["initialize", "collect:before"]  # type: ignore[attr-defined]
    assert restart.returncode == 0, restart.stderr
    assert restart.events == [  # type: ignore[attr-defined]
        "initialize",
        "collect:before",
        "restart",
        "readiness",
        "collect:after",
        "compare",
        "journal",
    ]


@pytest.mark.parametrize(
    "failure",
    ("collect-before", "restart", "readiness", "collect-after", "compare", "journal"),
)
def test_executable_dispatcher_propagates_command_and_validator_failures(failure: str):
    result = _run_dispatch_harness("restart", failure)

    assert result.returncode != 0, failure
    assert result.events.count("restart") == (0 if failure == "collect-before" else 1)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("is_active", "active_state", "sub_state", "accepted"),
    (
        (True, "active", "running", True),
        (False, "active", "running", False),
        (True, "activating", "start", False),
        (True, "deactivating", "stop-sigterm", False),
        (True, "active", "exited", False),
    ),
)
def test_runtime_state_validator_rejects_transitional_or_nonrunning_service(
    is_active: bool,
    active_state: str,
    sub_state: str,
    accepted: bool,
):
    harness = r'''
set -eu
source "$1"
service=autobit-paper.service
is_active=$2
configured_active_state=$3
configured_sub_state=$4
systemctl() {
    if [ "$1" = is-active ]; then [ "$is_active" = true ]; return; fi
    [ "$1" = show ] || return 2
    case "$3" in
        --property=ActiveState) printf '%s\n' "$configured_active_state" ;;
        --property=SubState) printf '%s\n' "$configured_sub_state" ;;
        *) return 2 ;;
    esac
}
validate_service_runtime_state
'''
    result = subprocess.run(
        [
            *_shell_prefix(),
            "bash",
            "-s",
            "--",
            _linux_path(SCRIPT),
            str(is_active).lower(),
            active_state,
            sub_state,
        ],
        input=harness.encode("utf-8"),
        capture_output=True,
    )

    assert (result.returncode == 0) is accepted


def test_evidence_uses_probes_without_copying_or_hashing_the_active_ledger():
    text = _script_text()
    assert "set -eu" in text
    assert "set -o pipefail" in text
    assert "umask 077" in text
    assert 'source "$script_dir/libdeploy.sh"' in text
    assert "acquire_deploy_lock" in text
    assert "/var/backups/autobit/verification/${utc_timestamp}-${commit}" in text

    assert 'sqlite_tools.py" probe --db "$ledger"' in text
    assert "-m autobit.cli paper-status" in text
    assert "systemctl show" in text
    assert "journalctl" in text and "--show-cursor" in text

    for forbidden in (
        "sha256sum",
        "sqlite_tools.py\" snapshot",
        "paper.sqlite3-wal",
        "paper.sqlite3-shm",
        "jq ",
        "runtime.env",
        ".env",
        "autobit.cli live",
        "/v1/accounts",
        "/v1/orders",
        "UPBIT_ACCESS_KEY",
        "UPBIT_SECRET_KEY",
    ):
        assert forbidden not in text
    assert not re.search(r"\bcp\b.*paper\.sqlite3", text)


def test_before_and_after_evidence_and_preservation_checks_are_explicit():
    text = _script_text()
    required_evidence = (
        "service-${phase}.txt",
        "process-command-${phase}.json",
        "paper-status-${phase}.json",
        "ledger-probe-${phase}.json",
        "journal-${phase}.export",
        "journal-cursor-${phase}.txt",
        "boot-id-${phase}.txt",
        "release-${phase}.txt",
        "time-${phase}-utc.txt",
    )
    for name in required_evidence:
        assert name in text

    compare = _function(text, "compare_restart_evidence")
    for field in (
        '"market"',
        '"mode"',
        '"event_count"',
        '"max_event_sequence"',
        '"last_completed_candle_utc"',
        '"normalized_cash"',
        '"btc_quantity"',
        '"position_state"',
        '"active_stop"',
        '"pending_orders"',
        '"health_stage"',
    ):
        assert field in compare
    assert '"KRW-BTC"' in compare
    assert '"normalized-paper"' in compare
    assert "must not decrease" in compare
    assert "--after-cursor" in text
    assert "pre-restart journal entry is no longer readable" in text
    assert "no journal entry exists after the saved cursor" in text


@pytest.mark.parametrize(
    ("document", "field"),
    (
        ("paper-status", "market"),
        ("paper-status", "mode"),
        ("paper-status", "last_completed_candle_utc"),
        ("paper-status", "normalized_cash"),
        ("paper-status", "btc_quantity"),
        ("paper-status", "position_state"),
        ("paper-status", "active_stop"),
        ("paper-status", "pending_orders"),
        ("paper-status", "health_stage"),
        ("ledger-probe", "event_count"),
        ("ledger-probe", "max_event_sequence"),
    ),
)
@pytest.mark.parametrize("phase", ("before", "after"))
def test_restart_comparison_rejects_each_missing_required_field(
    tmp_path: Path, document: str, field: str, phase: str
) -> None:
    _valid_restart_evidence(tmp_path)
    path = tmp_path / f"{document}-{phase}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload[field]
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = _run_restart_comparison(tmp_path)

    assert result.returncode != 0, f"accepted missing {document}.{field}"


@pytest.mark.parametrize(
    ("document", "field", "invalid"),
    (
        ("paper-status", "market", ["KRW-BTC"]),
        ("paper-status", "mode", {"value": "normalized-paper"}),
        ("paper-status", "last_completed_candle_utc", 123),
        ("paper-status", "normalized_cash", True),
        ("paper-status", "btc_quantity", False),
        ("paper-status", "position_state", ["FLAT"]),
        ("paper-status", "active_stop", []),
        ("paper-status", "pending_orders", {}),
        ("paper-status", "health_stage", 0),
        ("ledger-probe", "event_count", True),
        ("ledger-probe", "max_event_sequence", False),
    ),
)
@pytest.mark.parametrize("phase", ("before", "after"))
def test_restart_comparison_rejects_each_wrong_typed_required_field(
    tmp_path: Path, document: str, field: str, invalid: object, phase: str
) -> None:
    _valid_restart_evidence(tmp_path)
    path = tmp_path / f"{document}-{phase}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[field] = invalid
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = _run_restart_comparison(tmp_path)

    assert result.returncode != 0, f"accepted wrong type for {document}.{field}"


def test_restart_comparison_rejects_malformed_json(tmp_path: Path) -> None:
    _valid_restart_evidence(tmp_path)
    (tmp_path / "paper-status-after.json").write_text("{", encoding="utf-8")

    result = _run_restart_comparison(tmp_path)

    assert result.returncode != 0


@pytest.mark.parametrize(
    ("before_invocation", "after_invocation"),
    (
        ("1" * 32, "1" * 32),
        ("", "2" * 32),
        ("not-an-invocation-id", "2" * 32),
        ("1" * 32, ""),
    ),
)
def test_restart_requires_a_new_valid_systemd_invocation(
    tmp_path: Path, before_invocation: str, after_invocation: str
) -> None:
    _write_service_evidence(tmp_path, before_invocation, after_invocation)
    (tmp_path / "journal-after-saved-cursor.export").write_text(
        "__CURSOR=s=after;i=2\n"
        f"_SYSTEMD_INVOCATION_ID={after_invocation}\n"
        "MESSAGE=after\n",
        encoding="utf-8",
    )

    result = _run_journal_validation(tmp_path)

    assert result.returncode != 0, "accepted an unchanged InvocationID"


def test_post_cursor_journal_must_belong_to_after_invocation(tmp_path: Path) -> None:
    before_invocation = "1" * 32
    after_invocation = "2" * 32
    _write_service_evidence(tmp_path, before_invocation, after_invocation)
    (tmp_path / "journal-after-saved-cursor.export").write_text(
        "__CURSOR=s=after;i=2\n"
        f"_SYSTEMD_INVOCATION_ID={before_invocation}\n"
        "MESSAGE=unrelated-old-invocation-entry\n",
        encoding="utf-8",
    )

    result = _run_journal_validation(tmp_path)

    assert result.returncode != 0, "accepted journal evidence from the old invocation"


def test_new_invocation_with_matching_post_cursor_journal_is_accepted(
    tmp_path: Path,
) -> None:
    before_invocation = "1" * 32
    after_invocation = "2" * 32
    _write_service_evidence(tmp_path, before_invocation, after_invocation)
    (tmp_path / "journal-after-saved-cursor.export").write_text(
        "__CURSOR=s=after;i=2\n"
        f"_SYSTEMD_INVOCATION_ID={after_invocation}\n"
        "MESSAGE=started\n",
        encoding="utf-8",
    )

    result = _run_journal_validation(tmp_path)

    assert result.returncode == 0, result.stderr


def test_runbook_covers_manual_and_oci_workflows_and_approval_boundaries():
    assert OCI_RUNBOOK.is_file(), "missing OCI paper runbook"
    text = OCI_RUNBOOK.read_text(encoding="utf-8")
    for required in (
        "Read-Host",
        "-Mode Prepare",
        "-Mode Activate",
        ".autobit-release",
        "systemctl status autobit-paper.service",
        "paper-status",
        "journalctl -u autobit-paper.service",
        "verify-service.sh inspect",
        "verify-service.sh restart",
        "/var/backups/autobit/verification/",
        "paper.sqlite3-wal",
        "code-only",
        "legacy `.env`",
        "live",
        "journald",
        "same-volume",
        "VM reboot",
        "OCI external backup",
        "Always Free",
        "artificial load",
    ):
        assert required in text
    assert text.index("server-change approval") < text.index("-Mode Activate")
    assert "unattended reboot" not in text.lower()
    for forbidden in (
        "ocid1.",
        "SHA256:",
        "BEGIN OPENSSH PRIVATE KEY",
    ):
        assert forbidden not in text
    assert not re.search(r"[A-Za-z]:\\[^\n`]*\.key\b", text)


def test_operator_docs_link_to_the_oci_runbook_without_replacing_local_steps():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    paper = (ROOT / "docs/paper-trading-runbook.md").read_text(encoding="utf-8")
    link = "docs/runbooks/oci-paper.md"
    assert link in readme
    assert "runbooks/oci-paper.md" in paper
    assert 'paper-run --db ".\\data\\paper\\paper.sqlite3"' in readme
    assert 'paper-run --db ".\\data\\paper\\paper.sqlite3"' in paper
