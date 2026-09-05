#!/usr/bin/env bash
# Definitions only when sourced. Check-only commands never prepare a host.

die() { printf '%s\n' "autobit-deploy: $*" >&2; exit 1; }

require_commit() {
    case "$1" in
        *[!0-9a-f]*|'') die "commit must be lowercase hexadecimal" ;;
    esac
    [ "${#1}" -eq 40 ] || die "commit must contain 40 characters"
}

require_managed_path() {
    local candidate
    case "$1" in /*) ;; *) die "path must be absolute" ;; esac
    candidate=$(readlink -m -- "$1")
    case "$candidate" in
        /opt/autobit|/opt/autobit/*|/var/lib/autobit|/var/lib/autobit/*|/var/backups/autobit|/var/backups/autobit/*) ;;
        *) die "path is outside managed roots" ;;
    esac
    case "$candidate" in
        /|/home|/home/*) die "home and broad paths are forbidden" ;;
    esac
}

# This is a policy/preflight check, not protection against concurrent renames.
# Mutations beneath service-owned directories must also use directory descriptors.
require_literal_managed_path() {
    require_managed_path "$1"
    [ "$(readlink -m -- "$1")" = "$1" ] || die "managed path is not canonical"
    local part="$1"
    while [ "$part" != / ]; do
        [ ! -L "$part" ] || die "managed path contains a symbolic link"
        part=$(dirname -- "$part")
    done
}

make_directory_nofollow() {
    /usr/bin/python3 - "$@" <<'PY'
import grp
import os
import pwd
import sys

path, owner, group, mode = sys.argv[1:]
parts = path.split("/")
if not path.startswith("/") or any(part in ("", ".", "..") for part in parts[1:]):
    raise SystemExit("directory path must be canonical and absolute")
uid = pwd.getpwnam(owner).pw_uid
gid = grp.getgrnam(group).gr_gid
permissions = int(mode, 8)
if permissions not in (0o700, 0o755):
    raise SystemExit("unsupported managed directory mode")
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
parent = os.open("/", flags)
try:
    # Open each existing ancestor separately: O_NOFOLLOW on the leaf alone does
    # not prevent a symlink in an ancestor. Keep the current ancestor pinned.
    for component in parts[1:-1]:
        child = os.open(component, flags, dir_fd=parent)
        os.close(parent)
        parent = child
    try:
        os.mkdir(parts[-1], mode=0o700, dir_fd=parent)
    except FileExistsError:
        pass
    directory = os.open(parts[-1], flags, dir_fd=parent)
    try:
        # Do not resolve the pathname again, even after an attacker renames it.
        os.fchown(directory, uid, gid)
        os.fchmod(directory, permissions)
    finally:
        os.close(directory)
finally:
    os.close(parent)
PY
}

require_input_file() {
    case "$1" in /*) ;; *) die "input file must be absolute" ;; esac
    case "$1" in /home|/home/*) die "home inputs are forbidden" ;; esac
    case "$(readlink -m -- "$1")" in /home|/home/*) die "home inputs are forbidden" ;; esac
    [ -f "$1" ] && [ ! -L "$1" ] || die "input must be a regular file"
}

manifest_value() {
    local key="$1" file="$2" count
    count=$(awk -F= -v wanted="$key" '$1 == wanted { count += 1 } END { print count + 0 }' "$file")
    [ "$count" -eq 1 ] || die "manifest key count is invalid"
    awk -F= -v wanted="$key" '$1 == wanted { sub(/^[^=]*=/, ""); print }' "$file"
}

validate_manifest() {
    /usr/bin/python3 - "$1" "$2" <<'PY'
from pathlib import Path
import re
import sys
data = Path(sys.argv[1]).read_bytes()
pattern = rb"BUNDLE_VERSION=1\nCOMMIT=([0-9a-f]{40})\nSOURCE_SHA256=([0-9a-f]{64})\n"
match = re.fullmatch(pattern, data)
if match is None or match[1].decode() != sys.argv[2]:
    raise SystemExit("invalid bundle manifest or commit mismatch")
PY
}

validate_runtime() {
    /usr/bin/python3 - "$1" <<'PY'
from pathlib import Path
import sys
expected = {
    "RUNTIME_SCHEMA": "1",
    "UV_VERSION": "0.12.10",
    "UV_ARCHIVE_NAME": "uv-aarch64-unknown-linux-gnu.tar.gz",
    "UV_ARCHIVE_URL": "https://releases.astral.sh/github/uv/releases/download/0.12.10/uv-aarch64-unknown-linux-gnu.tar.gz",
    "UV_ARCHIVE_SHA256": "9ff6b9d4665edcdd3a88dcc73cd1eb641754deb927f14e8c62ebfde6bf4f5f5e",
    "PYTHON_VERSION": "3.12.14",
    "PYTHON_BUILD_TAG": "20260901",
    "PYTHON_ARCHIVE_NAME": "cpython-3.12.14+20260901-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz",
    "PYTHON_ARCHIVE_URL": "https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.12.14%2B20260901-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz",
    "PYTHON_ARCHIVE_SHA256": "577b4bec0793ad1ff0cbff9adbd0df078eddde38a4c41bf5d83ad381a85ee39d",
}
data = Path(sys.argv[1]).read_bytes()
if not data.endswith(b"\n") or b"\r" in data or b"\0" in data:
    raise SystemExit("runtime must use LF-terminated text")
try:
    entries = [line.split("=", 1) for line in data.decode("ascii").splitlines()]
    actual = dict(entries)
except (ValueError, UnicodeError):
    raise SystemExit("invalid runtime entries")
if len(entries) != len(expected) or actual != expected:
    raise SystemExit("runtime keys or pinned values differ")
PY
}

verify_sha256() {
    local actual
    actual=$(sha256sum -- "$1")
    [ "${actual%% *}" = "$2" ] || die "archive SHA-256 mismatch"
}

# This archive contains repository source only: reject all links and special
# files before extraction. Python 3.10 is the bootstrap interpreter on Ubuntu.
source_archive() {
    /usr/bin/python3 - "$@" <<'PY'
from pathlib import Path
import shutil
import sys
import tarfile
required = {
    "deploy/oci/runtime.env", "deploy/oci/systemd/autobit-paper.service",
    "deploy/oci/journald/99-autobit-persistence.conf", "deploy/oci/sqlite_tools.py",
    "deploy/oci/libdeploy.sh", "deploy/oci/install-release.sh", "uv.lock", "pyproject.toml",
}
with tarfile.open(sys.argv[1], "r:gz") as archive:
    seen, regular = set(), set()
    members = archive.getmembers()
    for member in members:
        name = member.name
        if member.isdir() and name.endswith("/"):
            name = name[:-1]
        parts = name.split("/")
        if (parts[0] != "source" or any(p in ("", ".", "..") for p in parts)
                or "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or not (member.isfile() or member.isdir()) or name in seen
                or (len(parts) == 1 and not member.isdir())):
            raise SystemExit("unsafe or duplicate source archive member")
        seen.add(name)
        if member.isfile():
            regular.add("/".join(parts[1:]))
    if not required <= regular:
        raise SystemExit("source archive is missing tracked deployment inputs")
    for name in seen:
        parts = name.split("/")
        if any("/".join(parts[1:i]) in regular for i in range(2, len(parts))):
            raise SystemExit("source archive file/directory collision")
    if len(sys.argv) == 3:
        root = Path(sys.argv[2])
        if not root.is_dir() or root.is_symlink() or any(root.iterdir()):
            raise SystemExit("extraction destination must be a new empty directory")
        for member in members:
            relative = member.name.rstrip("/").split("/")[1:]
            destination = root.joinpath(*relative)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                destination.chmod(0o755)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)
                destination.chmod(0o755 if member.mode & 0o111 else 0o644)
PY
}

validate_bundle() {
    require_commit "$3"
    require_input_file "$1"
    require_input_file "$2"
    validate_manifest "$2" "$3"
    verify_sha256 "$1" "$(manifest_value SOURCE_SHA256 "$2")"
    source_archive "$1"
}

acquire_deploy_lock() {
    /usr/bin/python3 - <<'PY'
import os
import stat
directory = os.stat("/run/lock")
if directory.st_uid != 0 or (directory.st_mode & 0o022 and not directory.st_mode & stat.S_ISVTX):
    raise SystemExit("lock directory must protect root-owned entries")
path = "/run/lock/autobit-deploy.lock"
try:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
except FileExistsError:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
try:
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_nlink != 1 or info.st_mode & 0o022):
        raise SystemExit("lock must be a protected root-owned regular file")
finally:
    os.close(descriptor)
PY
    # A root-owned entry in the checked directory cannot be swapped by other users.
    exec 9>>/run/lock/autobit-deploy.lock
    flock -n 9 || die "another deployment operation holds the lock"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    set -eu
    set -o pipefail
    case "${1-}" in
        check-path) [ "$#" -eq 2 ] || die "check-path requires one path"; require_managed_path "$2" ;;
        check-runtime) [ "$#" -eq 2 ] || die "check-runtime requires one file"; validate_runtime "$2" ;;
        check-bundle) [ "$#" -eq 4 ] || die "check-bundle requires archive, manifest, commit"; validate_bundle "$2" "$3" "$4" ;;
        *) die "expected check-path, check-runtime, or check-bundle" ;;
    esac
fi
