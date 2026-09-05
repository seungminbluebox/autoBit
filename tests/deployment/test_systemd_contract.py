import configparser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read_unit(relative_path: str) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(strict=True)
    parser.optionxform = str
    with (ROOT / relative_path).open(encoding="utf-8") as stream:
        parser.read_file(stream)
    return parser


def test_service_is_paper_only_and_home_is_inaccessible():
    unit = _read_unit("deploy/oci/systemd/autobit-paper.service")
    service = unit["Service"]
    assert service["ExecStart"] == (
        "/opt/autobit/current/.venv/bin/python -m autobit.cli paper-run "
        "--db /var/lib/autobit/paper/paper.sqlite3 "
        "--data-dir /var/lib/autobit/raw/paper"
    )
    assert service["User"] == service["Group"] == "autobit"
    assert service["Restart"] == "always"
    assert service["KillSignal"] == "SIGINT"
    assert service["ProtectHome"] == "true"
    assert service["ProtectSystem"] == "strict"
    assert service["ReadWritePaths"] == "/var/lib/autobit"
    text = (ROOT / "deploy/oci/systemd/autobit-paper.service").read_text(encoding="utf-8")
    assert "EnvironmentFile" not in text
    assert " live" not in text.lower()
    assert "telegram" not in text.lower()


def test_service_has_exact_restart_shutdown_and_resource_controls():
    service = _read_unit("deploy/oci/systemd/autobit-paper.service")["Service"]
    unit = _read_unit("deploy/oci/systemd/autobit-paper.service")["Unit"]
    assert unit["StartLimitIntervalSec"] == "0"
    assert service["RestartSec"] == "60s"
    assert service["TimeoutStopSec"] == "120s"
    assert service["UMask"] == "0077"
    assert service["Environment"] == (
        '"HOME=/var/lib/autobit" "XDG_CACHE_HOME=/var/lib/autobit/.cache" '
        '"PYTHONDONTWRITEBYTECODE=1" "OMP_NUM_THREADS=1" '
        '"OPENBLAS_NUM_THREADS=1" "MKL_NUM_THREADS=1" "NUMEXPR_NUM_THREADS=1"'
    )


def test_journal_is_persistent_and_exactly_bounded():
    journal = _read_unit("deploy/oci/journald/99-autobit-persistence.conf")["Journal"]
    assert dict(journal) == {
        "Storage": "persistent",
        "SystemMaxUse": "512M",
        "SystemKeepFree": "5G",
        "MaxRetentionSec": "90day",
    }
