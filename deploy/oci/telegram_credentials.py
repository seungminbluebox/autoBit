#!/usr/bin/env python3
"""Migrate only Telegram values from the legacy dotenv into a private file."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import sys


LEGACY_ENV = Path("/home/ubuntu/autoBit/.env")
NOTIFICATION_ENV = Path("/etc/autobit/paper-notify.env")
_SOURCE_NAMES = frozenset({"TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"})
_TOKEN_PATTERN = re.compile(r"[1-9][0-9]{5,19}:[A-Za-z0-9_-]{20,128}")
_CHAT_PATTERN = re.compile(r"-?[1-9][0-9]{0,19}")
_MAX_SOURCE_BYTES = 64 * 1024


def _read_regular_file(path: Path) -> str:
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise ValueError("legacy environment file is missing") from error
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("legacy environment file must be one regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValueError("legacy environment file changed while opening")
        data = os.read(descriptor, _MAX_SOURCE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(data) > _MAX_SOURCE_BYTES or b"\0" in data:
        raise ValueError("legacy environment file is invalid")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("legacy environment file is not UTF-8") from error


def _unquote(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] in {"'", '"'}:
        if value[-1] != value[0]:
            raise ValueError("Telegram value has mismatched quotes")
        value = value[1:-1]
    if not value or value != value.strip() or any(character in value for character in "\r\n\0"):
        raise ValueError("Telegram value is invalid")
    return value


def _parse_legacy_telegram(text: str) -> tuple[str, str]:
    found: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or name not in _SOURCE_NAMES:
            continue
        if name in found:
            raise ValueError(f"duplicate {name}")
        found[name] = _unquote(raw_value)
    if set(found) != _SOURCE_NAMES:
        raise ValueError("both legacy Telegram values are required")
    token = found["TELEGRAM_TOKEN"]
    chat_id = found["TELEGRAM_CHAT_ID"]
    if _TOKEN_PATTERN.fullmatch(token) is None:
        raise ValueError("legacy Telegram token has an invalid shape")
    if _CHAT_PATTERN.fullmatch(chat_id) is None:
        raise ValueError("legacy Telegram chat ID has an invalid shape")
    return token, chat_id


def _validate_destination_parent(path: Path, trusted_uid: int, trusted_gid: int) -> None:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != trusted_uid
        or info.st_gid != trusted_gid
    ):
        raise ValueError("notification directory is not trusted")
    if os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("notification directory must have mode 0700")


def _fsync_directory(path: Path) -> None:
    """Persist a completed directory-entry update on the production POSIX host."""
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def migrate_legacy_telegram(
    source: Path,
    destination: Path,
    *,
    trusted_uid: int,
    trusted_gid: int,
) -> None:
    """Copy only validated Telegram values without sourcing the legacy file."""
    parent = destination.parent
    _validate_destination_parent(parent, trusted_uid, trusted_gid)
    if os.path.lexists(destination):
        raise FileExistsError("notification environment already exists")
    token, chat_id = _parse_legacy_telegram(_read_regular_file(source))
    payload = (
        f"AUTOBIT_TELEGRAM_TOKEN={token}\n"
        f"AUTOBIT_TELEGRAM_CHAT_ID={chat_id}\n"
    ).encode("ascii")
    temporary = parent / f".{destination.name}.new-{os.getpid()}"
    if os.path.lexists(temporary):
        raise FileExistsError("notification environment staging file already exists")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:
            temporary.chmod(0o600)
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        # A hard-link publish is atomic and fails if another invocation created
        # destination after the earlier check. Unlike os.replace(), it can never
        # overwrite credentials that are already installed.
        os.link(temporary, destination)
        temporary.unlink()
        _fsync_directory(parent)
        installed = destination.lstat()
        if (
            not stat.S_ISREG(installed.st_mode)
            or installed.st_uid != trusted_uid
            or installed.st_gid != trusted_gid
            or installed.st_nlink != 1
            or (os.name == "posix" and stat.S_IMODE(installed.st_mode) != 0o600)
        ):
            raise ValueError("installed notification environment is not private")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if os.path.lexists(temporary):
            temporary.unlink()


def main() -> int:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        print("Telegram credential migration requires root", file=sys.stderr)
        return 1
    try:
        if not NOTIFICATION_ENV.parent.exists():
            NOTIFICATION_ENV.parent.mkdir(mode=0o700)
        migrate_legacy_telegram(
            LEGACY_ENV,
            NOTIFICATION_ENV,
            trusted_uid=0,
            trusted_gid=0,
        )
    except Exception as error:
        print(f"Telegram credential migration failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(f"Telegram credentials installed: {NOTIFICATION_ENV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
