"""Linux behavior probe for runtime-tool permission normalization."""

from __future__ import annotations

import io
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def _add_directory(archive: tarfile.TarFile, name: str) -> None:
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    member.mode = 0o755
    archive.addfile(member)


def main() -> None:
    installer = Path(sys.argv[1]).read_text(encoding="utf-8")
    scenario = sys.argv[2]
    if scenario not in {"new", "existing"}:
        raise AssertionError(f"unsupported scenario: {scenario}")
    match = re.search(r"^install_tool\(\) \{\n.*?^\}", installer, re.M | re.S)
    if match is None:
        raise AssertionError("install_tool function is missing")

    with tempfile.TemporaryDirectory(prefix="autobit-tool-permissions-") as fixture:
        root = Path(fixture)
        download = root / "download"
        download.mkdir()
        archive_path = download / "python.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            _add_directory(archive, "python")
            _add_directory(archive, "python/bin")
            _add_directory(archive, "python/lib")
            executable = tarfile.TarInfo("python/bin/python3.12")
            payload = b"#!/bin/sh\nexit 0\n"
            executable.size = len(payload)
            executable.mode = 0o755
            archive.addfile(executable, io.BytesIO(payload))
            module = tarfile.TarInfo("python/lib/stdlib.py")
            module_payload = b"VALUE = 1\n"
            module.size = len(module_payload)
            module.mode = 0o644
            archive.addfile(module, io.BytesIO(module_payload))

        tool = root / "python-tool"
        if scenario == "existing":
            binary = tool / "python/bin/python3.12"
            binary.parent.mkdir(parents=True, mode=0o700)
            binary.write_bytes(payload)
            binary.chmod(0o700)
            library = tool / "python/lib/stdlib.py"
            library.parent.mkdir(mode=0o700)
            library.write_bytes(module_payload)
            library.chmod(0o600)
        script = f'''set -eu
umask 077
{match.group(0)}
download_dir=$1
tool=$2
require_literal_managed_path() {{ :; }}
verify_sha256() {{ :; }}
managed_directory() {{ mkdir -p -- "$1"; }}
curl() {{ :; }}
chown() {{ :; }}
verify_tool() {{ :; }}
install_tool "$tool" python.tar.gz https://example.invalid/python.tar.gz ignored python
'''
        result = subprocess.run(
            ["bash", "-s", "--", str(download), str(tool)],
            input=script,
            text=True,
            capture_output=True,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

        directories = (tool / "python", tool / "python/bin", tool / "python/lib")
        for directory in directories:
            mode = stat.S_IMODE(directory.stat().st_mode)
            if mode & 0o005 != 0o005:
                raise AssertionError(f"runtime directory is not service-traversable: {directory} mode={mode:o}")

        binary = tool / "python/bin/python3.12"
        binary_mode = stat.S_IMODE(binary.stat().st_mode)
        if binary_mode & 0o005 != 0o005:
            raise AssertionError(f"runtime executable is not service-readable/executable: mode={binary_mode:o}")
        library = tool / "python/lib/stdlib.py"
        library_mode = stat.S_IMODE(library.stat().st_mode)
        if library_mode & 0o004 != 0o004:
            raise AssertionError(f"runtime library is not service-readable: mode={library_mode:o}")
        for path in (*directories, binary, library):
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o022:
                raise AssertionError(f"runtime path is group/other-writable: {path} mode={mode:o}")


if __name__ == "__main__":
    main()
