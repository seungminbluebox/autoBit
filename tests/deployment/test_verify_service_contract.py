from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/oci/verify-service.sh"
OCI_RUNBOOK = ROOT / "docs/runbooks/oci-paper.md"


def _script_text() -> str:
    assert SCRIPT.is_file(), "missing service verification script"
    return SCRIPT.read_text(encoding="utf-8")


def _function(text: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}", text, re.M | re.S)
    assert match is not None, f"missing shell function: {name}"
    return match.group(0)


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
