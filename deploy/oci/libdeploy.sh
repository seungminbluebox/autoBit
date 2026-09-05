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

require_protected_directory() {
    /usr/bin/python3 - "$1" "${2:-$(id -u)}" <<'PY'
import os
import stat
import sys

path, wanted_uid = sys.argv[1:]
parts = path.split("/")
if not path.startswith("/") or any(part in ("", ".", "..") for part in parts[1:]):
    raise SystemExit("protected directory path must be canonical and absolute")
flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
descriptor = os.open("/", flags)
try:
    for component in parts[1:]:
        child = os.open(component, flags, dir_fd=descriptor)
        os.close(descriptor)
        descriptor = child
    info = os.fstat(descriptor)
    if info.st_uid != int(wanted_uid) or info.st_mode & 0o022:
        raise SystemExit("protected directory ownership or mode differs")
finally:
    os.close(descriptor)
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

validate_release_candidate() {
    require_commit "$commit"
    [ -d "$candidate_release" ] && [ ! -L "$candidate_release" ] || die "candidate release is missing or linked"
    case "$candidate_release" in
        /opt/autobit/releases/*) require_literal_managed_path "$candidate_release" ;;
    esac
    /usr/bin/python3 - "$candidate_release" "$commit" <<'PY'
import json
import os
from pathlib import Path
import re
import stat
import sys

release = Path(sys.argv[1])
commit = sys.argv[2]
required = (
    (".autobit-release", False),
    ("deploy/oci/sqlite_tools.py", False),
    ("deploy/oci/systemd/autobit-paper.service", False),
    ("deploy/oci/journald/99-autobit-persistence.conf", False),
)
for relative, executable in required:
    path = release / relative
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise SystemExit("candidate readiness file is missing: {}".format(relative))
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
            or info.st_mode & 0o022 or executable and not info.st_mode & 0o111):
        raise SystemExit("candidate readiness file is not protected: {}".format(relative))

python_path = release / ".venv/bin/python"
try:
    python_info = python_path.lstat()
except FileNotFoundError:
    raise SystemExit("candidate readiness file is missing: .venv/bin/python")
if stat.S_ISLNK(python_info.st_mode):
    expected_python = Path(
        "/opt/autobit/tools/python/3.12.14+20260901/python/bin/python3.12"
    )
    try:
        if python_path.resolve(strict=True) != expected_python:
            raise SystemExit("candidate Python link target differs")
        target_info = expected_python.lstat()
    except FileNotFoundError:
        raise SystemExit("candidate Python link target is missing")
    if (not stat.S_ISREG(target_info.st_mode) or target_info.st_uid != 0
            or target_info.st_nlink != 1 or target_info.st_mode & 0o022
            or not target_info.st_mode & 0o111):
        raise SystemExit("candidate Python target is not protected")
elif (not stat.S_ISREG(python_info.st_mode) or python_info.st_uid != 0
      or python_info.st_nlink != 1 or python_info.st_mode & 0o022
      or not python_info.st_mode & 0o111):
    raise SystemExit("candidate Python executable is not protected")

metadata_path = release / ".autobit-release"
data = metadata_path.read_bytes()
if not data.endswith(b"\n") or b"\r" in data or b"\0" in data:
    raise SystemExit("candidate readiness metadata has invalid bytes")
try:
    metadata = json.loads(data)
except (UnicodeDecodeError, json.JSONDecodeError):
    raise SystemExit("candidate readiness metadata is invalid")
expected_keys = {
    "commit", "python_version", "source_archive_sha256", "uv_lock_sha256", "uv_version"
}
if set(metadata) != expected_keys or metadata.get("commit") != commit:
    raise SystemExit("candidate readiness metadata differs")
if metadata.get("python_version") != "3.12.14" or metadata.get("uv_version") != "0.12.10":
    raise SystemExit("candidate readiness runtime differs")
for key in ("source_archive_sha256", "uv_lock_sha256"):
    if not isinstance(metadata.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", metadata[key]):
        raise SystemExit("candidate readiness digest is invalid")
PY
}

assert_no_paper_writer() {
    /usr/bin/python3 - "${AUTOBIT_PROC_ROOT:-/proc}" "${production_ledger:-/var/lib/autobit/paper/paper.sqlite3}" <<'PY'
from pathlib import Path
import sys

proc_root = Path(sys.argv[1])
ledger = sys.argv[2].encode()
for cmdline_path in proc_root.glob("[0-9]*/cmdline"):
    try:
        command = cmdline_path.read_bytes()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        continue
    if not command.endswith(b"\0"):
        continue
    arguments = command[:-1].split(b"\0")
    is_paper_run = any(
        arguments[index:index + 3] == [b"-m", b"autobit.cli", b"paper-run"]
        for index in range(len(arguments) - 2)
    )
    uses_ledger = any(
        arguments[index:index + 2] == [b"--db", ledger]
        for index in range(len(arguments) - 1)
    )
    if is_paper_run and uses_ledger:
        raise SystemExit("paper ledger writer remains active")
PY
}

backup_closed_ledger() {
    require_commit "$commit"
    local backup bundle verification verification_source owner group
    backup="${backup_root}/${activation_timestamp}-${commit}"
    [ ! -e "$backup" ] && [ ! -L "$backup" ] || die "backup directory already exists: $backup"
    [ ! -L "$ledger" ] || die "paper ledger must not be a symbolic link"
    if [ ! -e "$ledger" ]; then
        [ ! -e "${ledger}-wal" ] && [ ! -L "${ledger}-wal" ] \
            && [ ! -e "${ledger}-shm" ] && [ ! -L "${ledger}-shm" ] \
            || die "orphan paper ledger WAL or SHM companion"
    else
        [ -f "$ledger" ] && [ ! -L "$ledger" ] || die "paper ledger must be a regular file"
        for companion in "${ledger}-wal" "${ledger}-shm"; do
            if [ -e "$companion" ] || [ -L "$companion" ]; then
                [ -f "$companion" ] && [ ! -L "$companion" ] || die "paper ledger companion is invalid"
            fi
        done
    fi

    owner=$(id -un)
    group=$(id -gn)
    if [ ! -d "$backup_root" ]; then
        [ ! -e "$backup_root" ] && [ ! -L "$backup_root" ] || die "backup root is invalid"
        make_directory_nofollow "$backup_root" "$owner" "$group" 0700
    fi
    require_protected_directory "$backup_root"
    make_directory_nofollow "$backup" "$owner" "$group" 0700
    printf '%s\n' "${previous_release:-NO_PREVIOUS_RELEASE}" > "$backup/release-before.txt"
    printf '%s\n' "$activation_timestamp" > "$backup/created-at-utc.txt"

    if [ ! -e "$ledger" ]; then
        printf 'NO_EXISTING_LEDGER\n' > "$backup/ledger-state.txt"
        ledger_backup=$backup
        printf '%s\n' "$backup"
        return
    fi

    bundle="$backup/bundle"
    verification="$backup/verification"
    verification_source="$verification/source"
    make_directory_nofollow "$bundle" "$owner" "$group" 0700
    make_directory_nofollow "$verification" "$owner" "$group" 0700
    make_directory_nofollow "$verification_source" "$owner" "$group" 0700
    cp --preserve=mode,timestamps -- "$ledger" "$bundle/paper.sqlite3"
    if [ -e "${ledger}-wal" ]; then
        cp --preserve=mode,timestamps -- "${ledger}-wal" "$bundle/paper.sqlite3-wal"
    fi
    (
        cd -- "$bundle"
        if [ -e paper.sqlite3-wal ]; then
            sha256sum -- paper.sqlite3 paper.sqlite3-wal
        else
            sha256sum -- paper.sqlite3
        fi
    ) > "$backup/SHA256SUMS"
    (cd -- "$bundle" && sha256sum -c -- "$backup/SHA256SUMS" >/dev/null)
    cp --preserve=mode,timestamps -- "$bundle/paper.sqlite3" "$verification_source/paper.sqlite3"
    if [ -e "$bundle/paper.sqlite3-wal" ]; then
        cp --preserve=mode,timestamps -- "$bundle/paper.sqlite3-wal" "$verification_source/paper.sqlite3-wal"
    fi
    /usr/bin/python3 "$candidate_release/deploy/oci/sqlite_tools.py" snapshot \
        --source "$verification_source/paper.sqlite3" --destination "$verification/paper.sqlite3"
    "$candidate_release/.venv/bin/python" -m autobit.cli paper-status \
        --db "$verification/paper.sqlite3" > "$verification/paper-status.json"
    /usr/bin/python3 - "$verification/paper-status.json" <<'PY'
import json
from pathlib import Path
import sys
value = json.loads(Path(sys.argv[1]).read_text())
if not isinstance(value, dict):
    raise SystemExit("candidate paper-status did not return a JSON object")
PY
    ledger_backup=$backup
    printf '%s\n' "$backup"
}

switch_current_atomically() {
    local target="$1" next_link parent
    if [ ! -d "$target" ] || [ -L "$target" ]; then
        printf 'release target is missing or linked: %s\n' "$target" >&2
        return 1
    fi
    parent=$(dirname -- "$current_link")
    require_protected_directory "$parent" || return 1
    next_link="$parent/.current-${commit}-$$"
    if [ -e "$next_link" ] || [ -L "$next_link" ]; then
        printf 'atomic current link already exists: %s\n' "$next_link" >&2
        return 1
    fi
    ln -s -- "$target" "$next_link" || return 1
    mv -Tf -- "$next_link" "$current_link" || return 1
}

service_unit_exists() { systemctl cat autobit-paper.service >/dev/null 2>&1; }
service_stop() { systemctl stop autobit-paper.service; }
service_disable() { systemctl disable autobit-paper.service; }
service_enable() { systemctl enable autobit-paper.service; }
service_start() { systemctl start autobit-paper.service; }
service_daemon_reload() { systemctl daemon-reload; }
service_is_enabled() { systemctl is-enabled --quiet autobit-paper.service; }
service_is_active() { systemctl is-active --quiet autobit-paper.service; }

wait_for_paper_service_stop() {
    local state attempts=0
    while [ "$attempts" -lt 120 ]; do
        state=$(systemctl is-active autobit-paper.service 2>/dev/null || :)
        case "$state" in active|activating) ;; *) return 0 ;; esac
        attempts=$((attempts + 1))
        sleep 1
    done
    printf 'paper service did not stop within 120 seconds\n' >&2
    return 1
}

ensure_config_backup() {
    [ -n "${config_backup:-}" ] && return
    require_protected_directory "$config_backup_root" 0
    config_backup="${config_backup_root}/${activation_timestamp}-${commit}"
    [ ! -e "$config_backup" ] && [ ! -L "$config_backup" ] \
        || die "config backup directory already exists: $config_backup"
    make_directory_nofollow "$config_backup" root root 0700
}

validate_installed_config() {
    /usr/bin/python3 - "$1" <<'PY'
import os
import stat
import sys
path = sys.argv[1]
info = os.lstat(path)
if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022:
    raise SystemExit("installed config must be a protected root-owned regular file")
PY
}

install_config_atomically() {
    local source="$1" destination="$2" backup_name="$3" state_name="$4" temporary state
    require_protected_directory "$(dirname -- "$destination")" 0
    state=ABSENT
    if [ -e "$destination" ] || [ -L "$destination" ]; then
        validate_installed_config "$destination"
        if cmp -s -- "$source" "$destination"; then
            printf -v "$state_name" '%s' UNCHANGED
            return
        fi
        ensure_config_backup
        cp --preserve=mode,timestamps -- "$destination" "$config_backup/$backup_name"
        state=EXISTING
    fi
    temporary="${destination}.new-${commit}-$$"
    [ ! -e "$temporary" ] && [ ! -L "$temporary" ] || die "config staging path already exists: $temporary"
    install --owner=root --group=root --mode=0644 -- "$source" "$temporary"
    mv -Tf -- "$temporary" "$destination"
    printf -v "$state_name" '%s' "$state"
}

install_release_config() {
    require_protected_directory /etc/systemd 0
    if [ ! -d /etc/systemd/journald.conf.d ]; then
        [ ! -e /etc/systemd/journald.conf.d ] && [ ! -L /etc/systemd/journald.conf.d ] \
            || die "journald config directory is invalid"
        make_directory_nofollow /etc/systemd/journald.conf.d root root 0755
    fi
    unit_config_state=
    journal_config_state=
    install_config_atomically \
        "$candidate_release/deploy/oci/systemd/autobit-paper.service" \
        "$unit_destination" autobit-paper.service unit_config_state
    install_config_atomically \
        "$candidate_release/deploy/oci/journald/99-autobit-persistence.conf" \
        "$journal_destination" 99-autobit-persistence.conf journal_config_state
}

restore_one_config() {
    local state="$1" destination="$2" backup_name="$3" temporary
    case "$state" in
        EXISTING)
            temporary="${destination}.rollback-${commit}-$$"
            [ ! -e "$temporary" ] && [ ! -L "$temporary" ] || return 1
            install --owner=root --group=root --mode=0644 -- "$config_backup/$backup_name" "$temporary" || return 1
            mv -Tf -- "$temporary" "$destination" || return 1
            ;;
        ABSENT)
            if [ -e "$destination" ] || [ -L "$destination" ]; then
                validate_installed_config "$destination" || return 1
                unlink -- "$destination" || return 1
            fi
            ;;
        UNCHANGED|'') ;;
        *) return 1 ;;
    esac
}

restore_config_backups() {
    restore_one_config "${unit_config_state:-}" "$unit_destination" autobit-paper.service || return 1
    restore_one_config "${journal_config_state:-}" "$journal_destination" 99-autobit-persistence.conf || return 1
}

prepare_persistent_journal_storage() {
    local journal_group
    if [ -d /run/log/journal ] && [ ! -L /run/log/journal ]; then
        journal_group=$(stat -c %G -- /run/log/journal)
    else
        getent group systemd-journal >/dev/null || die "systemd journal group is unavailable"
        journal_group=systemd-journal
    fi
    if [ ! -d /var/log/journal ]; then
        [ ! -e /var/log/journal ] && [ ! -L /var/log/journal ] || die "journal directory is invalid"
        install -d --owner=root --group="$journal_group" --mode=2755 -- /var/log/journal
    fi
    systemd-tmpfiles --create --prefix /var/log/journal
}

journald_reload() { systemctl restart systemd-journald; }
journald_probe() { journalctl --disk-usage >/dev/null; }

restart_journald() { journald_reload && journald_probe; }

prepare_persistent_journal() {
    prepare_persistent_journal_storage
    restart_journald
}

candidate_paper_status() {
    runuser -u autobit -- env -i PATH=/usr/bin:/bin \
        HOME=/var/lib/autobit XDG_CACHE_HOME=/var/lib/autobit/.cache PYTHONDONTWRITEBYTECODE=1 \
        OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 \
        "$candidate_release/.venv/bin/python" -m autobit.cli paper-status --db "$1"
}

service_command_is_expected() {
    /usr/bin/python3 - "$1" "${AUTOBIT_PROC_ROOT:-/proc}" \
        "${production_ledger:-/var/lib/autobit/paper/paper.sqlite3}" \
        "${production_data:-/var/lib/autobit/raw/paper}" \
        "${paper_python:-/opt/autobit/current/.venv/bin/python}" <<'PY'
from pathlib import Path
import sys
pid, proc_root, ledger, data, python = sys.argv[1:]
try:
    command = (Path(proc_root) / pid / "cmdline").read_bytes()
except (FileNotFoundError, PermissionError, ProcessLookupError):
    raise SystemExit(1)
if not command.endswith(b"\0"):
    raise SystemExit(1)
arguments = command[:-1].split(b"\0")
expected = [
    python.encode(), b"-m", b"autobit.cli", b"paper-run",
    b"--db", ledger.encode(), b"--data-dir", data.encode(),
]
if arguments != expected:
    raise SystemExit(1)
PY
}

validate_started_service_once() {
    local pid invocation record
    systemctl is-active --quiet autobit-paper.service || return 1
    pid=$(systemctl show --property=MainPID --value autobit-paper.service) || return 1
    case "$pid" in ''|*[!0-9]*|0) return 1 ;; esac
    service_command_is_expected "$pid" || return 1
    candidate_paper_status "$production_ledger" >/dev/null || return 1
    invocation=$(systemctl show --property=InvocationID --value autobit-paper.service) || return 1
    case "$invocation" in ''|00000000000000000000000000000000) return 1 ;; esac
    record=$(journalctl _SYSTEMD_INVOCATION_ID="$invocation" --lines=1 --no-pager --output=cat 2>/dev/null) || return 1
    [ -n "$record" ] || return 1
}

wait_for_started_service() {
    local attempts=0
    while [ "$attempts" -lt 180 ]; do
        if validate_started_service_once; then
            return 0
        fi
        attempts=$((attempts + 1))
        [ "$attempts" -lt 180 ] && sleep 1
    done
    return 1
}

remove_candidate_current() {
    local target
    if [ ! -e "$current_link" ] && [ ! -L "$current_link" ]; then
        return 0
    fi
    [ -L "$current_link" ] || return 1
    target=$(readlink -- "$current_link") || return 1
    [ "$target" = "$candidate_release" ] || return 1
    unlink -- "$current_link"
}

rollback_code_only() {
    local stop_failed=0 disable_failed=0
    if [ -n "${previous_release:-}" ] \
        || [ "${candidate_start_attempted:-1}" = 1 ]; then
        service_stop || stop_failed=1
    fi
    if [ -z "${previous_release:-}" ] \
        && [ "${candidate_enable_attempted:-1}" = 1 ]; then
        service_disable || disable_failed=1
    fi
    [ "$stop_failed" -eq 0 ] && [ "$disable_failed" -eq 0 ] || return 1
    wait_for_paper_service_stop || return 1
    assert_no_paper_writer || return 1
    if [ -n "${unit_config_state:-}" ] || [ -n "${journal_config_state:-}" ]; then
        restore_config_backups || return 1
    fi
    case "${journal_config_state:-}" in
        EXISTING|ABSENT) restart_journald || return 1 ;;
    esac
    if [ -n "${previous_release:-}" ]; then
        switch_current_atomically "$previous_release" || return 1
    else
        remove_candidate_current || return 1
    fi
    if [ -n "${unit_config_state:-}" ] || [ -n "${journal_config_state:-}" ]; then
        service_daemon_reload || return 1
    fi
    if [ -n "${previous_release:-}" ]; then
        if [ "${previous_enabled:-enabled}" = enabled ]; then
            service_enable || return 1
        else
            service_disable || return 1
        fi
        if [ "${previous_active:-active}" = active ]; then
            service_start || return 1
        fi
    fi
}

run_post_backup_activation() {
    install_release_config
    prepare_persistent_journal_storage
    journald_reload
    journald_probe
    switch_current_atomically "$candidate_release"
    service_daemon_reload
    candidate_enable_attempted=1
    service_enable
    candidate_start_attempted=1
    service_start
    wait_for_started_service \
        || die "candidate service did not become ready within 180 seconds"
}

activation_exit_trap() {
    local result="$1" rollback_result
    trap - EXIT
    [ "${activation_in_progress:-0}" = 1 ] || exit "$result"
    set +e
    printf 'Activation failed; candidate retained: %s\n' "$candidate_release" >&2
    printf 'Ledger backup evidence: %s\n' "${ledger_backup:-not-created}" >&2
    rollback_code_only
    rollback_result=$?
    if [ "$rollback_result" -ne 0 ]; then
        printf 'Code rollback failed (previous=%s candidate=%s)\n' \
            "${previous_release:-NO_PREVIOUS_RELEASE}" "$candidate_release" >&2
        exit 70
    fi
    [ "$result" -ne 0 ] || result=1
    exit "$result"
}

activate_transaction() {
    production_ledger=/var/lib/autobit/paper/paper.sqlite3
    production_data=/var/lib/autobit/raw/paper
    candidate_release="/opt/autobit/releases/$commit"
    backup_root=/var/backups/autobit/paper
    config_backup_root=/var/backups/autobit/config
    current_link=/opt/autobit/current
    unit_destination=/etc/systemd/system/autobit-paper.service
    journal_destination=/etc/systemd/journald.conf.d/99-autobit-persistence.conf
    activation_timestamp=$(date -u +%Y%m%dT%H%M%SZ)
    ledger=$production_ledger
    ledger_backup=
    config_backup=
    previous_release=
    previous_enabled=disabled
    previous_active=inactive
    candidate_enable_attempted=0
    candidate_start_attempted=0

    validate_release_candidate
    if [ -L "$current_link" ]; then
        previous_release=$(readlink -- "$current_link")
        previous_commit=${previous_release#/opt/autobit/releases/}
        [ "$previous_release" = "/opt/autobit/releases/$previous_commit" ] \
            || die "current release target is invalid"
        require_commit "$previous_commit"
        [ -d "$previous_release" ] && [ ! -L "$previous_release" ] || die "current release target is missing or linked"
    elif [ -e "$current_link" ]; then
        die "current must be absent or a symbolic link"
    fi
    if service_is_enabled; then
        previous_enabled=enabled
    fi
    if service_is_active; then
        previous_active=active
    fi

    activation_in_progress=1
    trap 'activation_exit_trap $?' EXIT
    if service_unit_exists; then
        service_stop
    fi
    wait_for_paper_service_stop
    assert_no_paper_writer
    backup_closed_ledger >/dev/null
    run_post_backup_activation
    activation_in_progress=0
    trap - EXIT
    printf 'Activated release: %s\n' "$candidate_release"
    printf 'Ledger backup evidence: %s\n' "$ledger_backup"
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
