"""Behavioral tests for one-time legacy Telegram credential migration."""

from importlib import util
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/oci/telegram_credentials.py"


def _load_module():
    assert SCRIPT.is_file(), "Telegram credential migration tool is missing"
    spec = util.spec_from_file_location("autobit_telegram_credentials", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _trusted_uid(path: Path) -> int:
    return path.stat().st_uid


def _trusted_gid(path: Path) -> int:
    return path.stat().st_gid


def test_migration_copies_only_telegram_values_to_private_file(tmp_path, capsys):
    module = _load_module()
    source = tmp_path / "legacy.env"
    destination_dir = tmp_path / "etc-autobit"
    destination_dir.mkdir(mode=0o700)
    destination_dir.chmod(0o700)
    destination = destination_dir / "paper-notify.env"
    source.write_text(
        "UPBIT_ACCESS_KEY=do-not-copy\n"
        "UPBIT_SECRET_KEY=do-not-copy-either\n"
        "TELEGRAM_TOKEN='123456789:abcdefghijklmnopqrstuvwxyz_ABCD'\n"
        'TELEGRAM_CHAT_ID="-123456789"\n',
        encoding="utf-8",
    )

    module.migrate_legacy_telegram(
        source,
        destination,
        trusted_uid=_trusted_uid(destination_dir),
        trusted_gid=_trusted_gid(destination_dir),
    )

    assert destination.read_text(encoding="utf-8") == (
        "AUTOBIT_TELEGRAM_TOKEN=123456789:abcdefghijklmnopqrstuvwxyz_ABCD\n"
        "AUTOBIT_TELEGRAM_CHAT_ID=-123456789\n"
    )
    if os.name == "posix":
        assert destination.stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "payload",
    [
        "TELEGRAM_CHAT_ID=-123456789\n",
        "TELEGRAM_TOKEN=123456789:abcdefghijklmnopqrstuvwxyz_ABCD\n",
        "TELEGRAM_TOKEN=not-a-bot-token\nTELEGRAM_CHAT_ID=-123456789\n",
        "TELEGRAM_TOKEN=123456789:abcdefghijklmnopqrstuvwxyz_ABCD\nTELEGRAM_CHAT_ID=chat-name\n",
        (
            "TELEGRAM_TOKEN=123456789:abcdefghijklmnopqrstuvwxyz_ABCD\n"
            "TELEGRAM_TOKEN=987654321:abcdefghijklmnopqrstuvwxyz_WXYZ\n"
            "TELEGRAM_CHAT_ID=-123456789\n"
        ),
    ],
)
def test_migration_rejects_missing_duplicate_or_invalid_values(tmp_path, payload):
    module = _load_module()
    source = tmp_path / "legacy.env"
    destination_dir = tmp_path / "etc-autobit"
    destination_dir.mkdir(mode=0o700)
    destination_dir.chmod(0o700)
    destination = destination_dir / "paper-notify.env"
    source.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError):
        module.migrate_legacy_telegram(
            source,
            destination,
            trusted_uid=_trusted_uid(destination_dir),
            trusted_gid=_trusted_gid(destination_dir),
        )

    assert not destination.exists()


def test_migration_rejects_symlink_source_and_existing_destination(tmp_path):
    module = _load_module()
    real_source = tmp_path / "real.env"
    real_source.write_text(
        "TELEGRAM_TOKEN=123456789:abcdefghijklmnopqrstuvwxyz_ABCD\n"
        "TELEGRAM_CHAT_ID=-123456789\n",
        encoding="utf-8",
    )
    source = tmp_path / "legacy.env"
    try:
        source.symlink_to(real_source)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    destination_dir = tmp_path / "etc-autobit"
    destination_dir.mkdir(mode=0o700)
    destination_dir.chmod(0o700)
    destination = destination_dir / "paper-notify.env"

    with pytest.raises(ValueError):
        module.migrate_legacy_telegram(
            source,
            destination,
            trusted_uid=_trusted_uid(destination_dir),
            trusted_gid=_trusted_gid(destination_dir),
        )
    assert not destination.exists()

    source.unlink()
    source.write_bytes(real_source.read_bytes())
    destination.write_text("keep-existing\n", encoding="utf-8")
    destination.chmod(0o600)
    before = destination.read_bytes()
    with pytest.raises(FileExistsError):
        module.migrate_legacy_telegram(
            source,
            destination,
            trusted_uid=_trusted_uid(destination_dir),
            trusted_gid=_trusted_gid(destination_dir),
        )
    assert destination.read_bytes() == before
