"""Root-only Linux probe for validation-before-repair and mount confinement."""

from __future__ import annotations

import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


UNAVAILABLE = 77


def _function(source: str, name: str, *, required: bool = True) -> str:
    match = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}", source, re.M | re.S)
    if match is None:
        if required:
            raise AssertionError(f"{name} function is missing")
        return ""
    return match.group(0)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _assert_modes_unchanged(before: dict[Path, int]) -> None:
    for path, wanted in before.items():
        actual = _mode(path)
        if actual != wanted:
            raise AssertionError(f"failed validation changed {path}: {wanted:o} -> {actual:o}")


def _assert_normalized(tool: Path, venv_python: Path) -> None:
    paths = [tool, *(path for path in tool.rglob("*") if not path.is_symlink())]
    for path in paths:
        mode = _mode(path)
        if mode & 0o022:
            raise AssertionError(f"runtime path is group/other-writable: {path} mode={mode:o}")
        if path.is_dir() and mode & 0o555 != 0o555:
            raise AssertionError(f"runtime directory is not readable/traversable: {path} mode={mode:o}")
        if path.is_file() and mode & 0o444 != 0o444:
            raise AssertionError(f"runtime file is not readable: {path} mode={mode:o}")
    executable = tool / "python/bin/python3.12"
    if _mode(executable) & 0o111 != 0o111:
        raise AssertionError("runtime executable did not preserve and extend execute permission")
    library = tool / "python/lib/stdlib.py"
    if _mode(library) & 0o111:
        raise AssertionError("non-executable runtime library became executable")
    result = subprocess.run(
        ["runuser", "-u", "nobody", "--", "env", "-i", "PATH=/usr/bin:/bin", str(venv_python), "--version"],
        capture_output=True,
        text=True,
    )
    if result.returncode or result.stdout.strip() != "Python 3.12.14":
        raise AssertionError("unprivileged user could not execute the absolute venv runtime link")


def _operation(installer: Path, tool: Path, digest: str) -> subprocess.CompletedProcess[str]:
    source = installer.read_text(encoding="utf-8")
    verify = _function(source, "verify_tool")
    normalize = _function(source, "normalize_tool_permissions", required=False)
    install = _function(source, "install_tool")
    script = f'''set -eu
{verify}
{normalize}
{install}
die() {{ printf '%s\n' "$*" >&2; exit 1; }}
require_literal_managed_path() {{ :; }}
download_dir=$1
python_version=3.12.14
uv_version=0.12.10
install_tool "$2" unused.tar.gz https://example.invalid/unused "$3" python
'''
    return subprocess.run(
        ["bash", "-s", "--", str(tool.parent), str(tool), digest],
        input=script,
        text=True,
        capture_output=True,
    )


def _fixture(root: Path, scenario: str) -> tuple[Path, Path, dict[Path, int]]:
    root.chmod(0o755)
    tool = root / "python-tool"
    binary = tool / "python/bin/python3.12"
    library = tool / "python/lib/stdlib.py"
    binary.parent.mkdir(parents=True)
    library.parent.mkdir()
    binary.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'Python 3.12.13'\n"
        if scenario == "bad-version"
        else "#!/bin/sh\nprintf '%s\\n' 'Python 3.12.14'\n",
        encoding="utf-8",
    )
    library.write_text("VALUE = 1\n", encoding="utf-8")
    digest_record = tool / ".archive-sha256"
    digest_record.write_text("wrong\n" if scenario == "bad-digest" else "expected\n", encoding="ascii")

    tool.chmod(0o755)
    (tool / "python").chmod(0o700)
    binary.parent.chmod(0o700)
    library.parent.chmod(0o700)
    binary.chmod(0o700)
    library.chmod(0o600)
    digest_record.chmod(0o600)

    if scenario == "bad-owner":
        try:
            account = pwd.getpwnam("nobody")
        except KeyError as error:
            raise RuntimeError("nobody account is unavailable") from error
        os.chown(library, account.pw_uid, account.pw_gid)

    venv_bin = root / "venv/bin"
    venv_bin.mkdir(parents=True, mode=0o755)
    venv_bin.parent.chmod(0o755)
    venv_python = venv_bin / "python"
    venv_python.symlink_to(binary)
    before = {path: _mode(path) for path in (tool / "python", binary.parent, library.parent, binary, library, digest_record)}
    return tool, venv_python, before


def _validation_scenario(installer: Path, scenario: str) -> None:
    with tempfile.TemporaryDirectory(prefix="autobit-tool-validation-") as directory:
        root = Path(directory)
        tool, venv_python, before = _fixture(root, scenario)
        result = _operation(installer, tool, "expected")
        if scenario == "valid":
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            _assert_normalized(tool, venv_python)
        else:
            if result.returncode == 0:
                raise AssertionError(f"{scenario} existing tool passed verification")
            _assert_modes_unchanged(before)


def _nested_mount_scenario(installer: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="autobit-tool-mount-") as directory:
        root = Path(directory)
        tool, _, _ = _fixture(root, "valid")
        outside = root / "outside"
        outside.mkdir(mode=0o700)
        marker = outside / "marker"
        marker.write_text("outside\n", encoding="utf-8")
        marker.chmod(0o600)
        mounted = tool / "mounted"
        mounted.mkdir(mode=0o700)
        before = {_path: _mode(_path) for _path in (outside, marker)}
        mounted_ok = subprocess.run(
            ["mount", "--bind", str(outside), str(mounted)],
            capture_output=True,
            text=True,
        )
        if mounted_ok.returncode:
            raise RuntimeError("bind mount is unavailable")
        try:
            result = _operation(installer, tool, "expected")
        finally:
            unmounted = subprocess.run(["umount", str(mounted)], capture_output=True, text=True)
            if unmounted.returncode:
                raise AssertionError(unmounted.stderr)
        if result.returncode == 0:
            raise AssertionError("runtime permission repair accepted a nested mount")
        _assert_modes_unchanged(before)


def main() -> int:
    if os.geteuid() != 0:
        print("root privileges are required", file=sys.stderr)
        return UNAVAILABLE
    installer = Path(sys.argv[1])
    scenario = sys.argv[2]
    try:
        if scenario == "nested-mount":
            _nested_mount_scenario(installer)
        elif scenario in {"valid", "bad-digest", "bad-version", "bad-owner"}:
            _validation_scenario(installer, scenario)
        else:
            raise AssertionError(f"unsupported scenario: {scenario}")
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return UNAVAILABLE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
