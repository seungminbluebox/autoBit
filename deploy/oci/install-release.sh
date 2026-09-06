#!/usr/bin/env bash
set -eu
set -o pipefail
umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source "$script_dir/libdeploy.sh"

check_host() {
    local operation="${1:-prepare}"
    [ "$(id -u)" -eq 0 ] || die "$operation requires root"
    [ "$(uname -m)" = aarch64 ] || die "$operation requires aarch64"
    [ -x /usr/bin/python3 ] || die "bootstrap python3 is unavailable"
    /usr/bin/python3 - "$operation" <<'PY'
from pathlib import Path
import shutil
import sys
entries = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines()
               if "=" in line and not line.startswith("#"))
if entries.get("ID", "").strip('"') != "ubuntu" or entries.get("VERSION_ID", "").strip('"') != "22.04":
    raise SystemExit("{} requires Ubuntu 22.04".format(sys.argv[1]))
if sys.argv[1] == "prepare" and shutil.disk_usage("/").free < 5 * 1024 ** 3:
    raise SystemExit("prepare requires at least 5 GiB free on root filesystem")
PY
    local command
    for command in flock readlink awk sha256sum runuser getent; do
        command -v "$command" >/dev/null || die "required command unavailable: $command"
    done
    if [ "$operation" = prepare ]; then
        for command in curl rmdir stat tar unlink useradd systemd-analyze; do
            command -v "$command" >/dev/null || die "required command unavailable: $command"
        done
    else
        for command in cmp cp date install journalctl ln mv stat systemctl systemd-tmpfiles; do
            command -v "$command" >/dev/null || die "required command unavailable: $command"
        done
    fi
}

managed_directory() {
    require_literal_managed_path "$1"
    make_directory_nofollow "$1" "$2" "$3" "$4"
}

prepare_directories() {
    local path
    for path in /opt/autobit /opt/autobit/releases /opt/autobit/tools \
        /var/lib/autobit /var/lib/autobit/paper /var/lib/autobit/raw/paper \
        /var/lib/autobit/.cache /var/backups/autobit /var/backups/autobit/paper /var/backups/autobit/config; do
        require_literal_managed_path "$path"
    done
    if ! getent passwd autobit >/dev/null; then
        useradd --system --user-group --home-dir /var/lib/autobit --no-create-home --shell /usr/sbin/nologin autobit
    fi
    [ "$(getent passwd autobit | cut -d: -f6)" = /var/lib/autobit ] || die "autobit home differs"
    [ "$(getent passwd autobit | cut -d: -f7)" = /usr/sbin/nologin ] || die "autobit must have no login shell"
    [ "$(id -gn autobit)" = autobit ] || die "autobit primary group differs"
    [ "$(id -u autobit)" -ne 0 ] || die "autobit must be unprivileged"
    for path in /opt/autobit /opt/autobit/releases /opt/autobit/tools; do
        managed_directory "$path" root root 0755
    done
    for path in /var/lib/autobit /var/lib/autobit/paper /var/lib/autobit/raw \
        /var/lib/autobit/raw/paper /var/lib/autobit/.cache; do
        managed_directory "$path" autobit autobit 0700
    done
    for path in /var/backups/autobit /var/backups/autobit/paper /var/backups/autobit/config; do
        managed_directory "$path" root root 0700
    done
}

load_runtime() {
    validate_runtime "$1"
    # Every value is read as data only, after exact-key and pinned-value checks.
    runtime_schema=$(manifest_value RUNTIME_SCHEMA "$1")
    uv_version=$(manifest_value UV_VERSION "$1")
    uv_archive_name=$(manifest_value UV_ARCHIVE_NAME "$1")
    uv_archive_url=$(manifest_value UV_ARCHIVE_URL "$1")
    uv_archive_sha256=$(manifest_value UV_ARCHIVE_SHA256 "$1")
    python_version=$(manifest_value PYTHON_VERSION "$1")
    python_build_tag=$(manifest_value PYTHON_BUILD_TAG "$1")
    python_archive_name=$(manifest_value PYTHON_ARCHIVE_NAME "$1")
    python_archive_url=$(manifest_value PYTHON_ARCHIVE_URL "$1")
    python_archive_sha256=$(manifest_value PYTHON_ARCHIVE_SHA256 "$1")
    [ "$runtime_schema" = 1 ] || die "unsupported runtime schema"
}

normalize_tool_permissions() {
    local directory="$1" operation="${2:-normalize}"
    require_literal_managed_path "$directory"
    [ "$operation" = check ] || [ "$operation" = normalize ] \
        || die "unsupported tool permission operation"
    /usr/bin/python3 - "$directory" "$operation" <<'PY'
import os
import re
import stat
import sys
from collections import Counter

root_path, operation = sys.argv[1:]
open_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
directory_flags = open_flags | os.O_DIRECTORY


def mount_path(value):
    return re.sub(r"\\([0-7]{3})", lambda item: chr(int(item.group(1), 8)), value)


def reject_nested_mounts(root):
    prefix = root.rstrip(os.sep) + os.sep
    with open("/proc/self/mountinfo", encoding="utf-8") as stream:
        for line in stream:
            fields = line.split()
            if len(fields) < 6:
                raise SystemExit("invalid mountinfo entry")
            mounted_at = mount_path(fields[4])
            if mounted_at.startswith(prefix):
                raise SystemExit("tool directory contains a nested mount: " + mounted_at)


def scan(directory_fd, relative=()):
    entries = []
    with os.scandir(directory_fd) as children:
        ordered = sorted(children, key=lambda item: item.name)
        for child in ordered:
            name = child.name
            if name in ("", ".", "..") or "/" in name or "\x00" in name:
                raise SystemExit("invalid tool tree entry")
            info = child.stat(follow_symlinks=False)
            child_relative = relative + (name,)
            if stat.S_ISLNK(info.st_mode):
                continue
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise SystemExit("tool directory changed during validation")
                    entries.append((child_relative, opened))
                    entries.extend(scan(child_fd, child_relative))
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(info.st_mode):
                entries.append((child_relative, info))
            else:
                raise SystemExit("tool tree contains an unsupported file type")
    return entries


def open_relative(root_fd, relative, is_directory):
    descriptor = os.dup(root_fd)
    try:
        for index, name in enumerate(relative):
            flags = open_flags
            if index < len(relative) - 1 or is_directory:
                flags |= os.O_DIRECTORY
            next_descriptor = os.open(name, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


root_fd = os.open(root_path, directory_flags)
try:
    resolved_root = os.readlink("/proc/self/fd/{}".format(root_fd))
    if not resolved_root.startswith("/") or resolved_root.endswith(" (deleted)"):
        raise SystemExit("tool directory path is unstable")
    reject_nested_mounts(resolved_root)
    root_info = os.fstat(root_fd)
    plan = [((), root_info)] + scan(root_fd)
    regular_links = Counter(
        (info.st_dev, info.st_ino)
        for _, info in plan
        if stat.S_ISREG(info.st_mode)
    )
    for _, info in plan:
        if stat.S_ISREG(info.st_mode):
            if regular_links[(info.st_dev, info.st_ino)] != info.st_nlink:
                raise SystemExit("tool file has a hard link outside the tool directory")
        elif not stat.S_ISDIR(info.st_mode):
            raise SystemExit("invalid tool permission target")
    if operation == "check":
        raise SystemExit(0)

    # Recheck the namespace before the first mutation. A service process cannot
    # create host mounts, and descriptor/inode checks below pin every target.
    reject_nested_mounts(resolved_root)
    for relative, expected in plan:
        is_directory = stat.S_ISDIR(expected.st_mode)
        descriptor = open_relative(root_fd, relative, is_directory)
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
                raise SystemExit("tool tree changed before permission repair")
            mode = stat.S_IMODE(current.st_mode)
            if is_directory:
                wanted = (mode | 0o555) & ~0o022
            else:
                wanted = (mode | 0o444) & ~0o022
                if mode & 0o111:
                    wanted |= 0o111
            os.fchmod(descriptor, wanted)
        finally:
            os.close(descriptor)
finally:
    os.close(root_fd)
PY
}

verify_tool() {
    local directory="$1" digest="$2" kind="$3" actual
    require_literal_managed_path "$directory"
    [ -d "$directory" ] && [ ! -L "$directory" ] || die "tool directory is missing or linked"
    # Reject mount and hard-link escapes before reading or executing tool data.
    normalize_tool_permissions "$directory" check
    [ -f "$directory/.archive-sha256" ] && [ ! -L "$directory/.archive-sha256" ] || die "tool hash record missing"
    [ "$(cat -- "$directory/.archive-sha256")" = "$digest" ] || die "installed tool archive hash differs"
    # Existing root-owned tools must not be writable by the service account.
    [ -z "$(find "$directory" -xdev \( ! -user root -o ! -group root -o -perm /022 \) ! -type l -print -quit)" ] || die "tool ownership or permissions differ"
    case "$kind" in
        uv)
            [ -f "$directory/uv" ] && [ ! -L "$directory/uv" ] || die "uv binary is invalid"
            actual=$(env -i PATH=/usr/bin:/bin "$directory/uv" --version)
            case "$actual" in "uv $uv_version"|"uv $uv_version ("*")") ;; *) die "uv version differs" ;; esac
            ;;
        python)
            [ -f "$directory/python/bin/python3.12" ] && [ ! -L "$directory/python/bin/python3.12" ] || die "python binary is invalid"
            [ "$(env -i PATH=/usr/bin:/bin "$directory/python/bin/python3.12" --version)" = "Python $python_version" ] || die "python version differs"
            ;;
    esac
}

install_tool() {
    local directory="$1" name="$2" url="$3" digest="$4" kind="$5" tool_stage
    require_literal_managed_path "$directory"
    if [ -e "$directory" ]; then
        verify_tool "$directory" "$digest" "$kind"
        normalize_tool_permissions "$directory"
        verify_tool "$directory" "$digest" "$kind"
        return
    fi
    curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 --retry 3 --output "$download_dir/$name" "$url"
    verify_sha256 "$download_dir/$name" "$digest"
    managed_directory "$(dirname -- "$directory")" root root 0755
    tool_stage="${directory}.staging-$$"
    require_literal_managed_path "$tool_stage"
    [ ! -e "$tool_stage" ] || die "tool staging directory already exists: $tool_stage"
    mkdir -m 0755 -- "$tool_stage"
    printf 'Tool staging retained on failure: %s\n' "$tool_stage"
    case "$kind" in
        uv) tar --extract --gzip --file "$download_dir/$name" --directory "$tool_stage" --strip-components=1 --no-same-owner ;;
        python) tar --extract --gzip --file "$download_dir/$name" --directory "$tool_stage" --no-same-owner ;;
    esac
    printf '%s\n' "$digest" > "$tool_stage/.archive-sha256"
    verify_tool "$tool_stage" "$digest" "$kind"
    normalize_tool_permissions "$tool_stage"
    verify_tool "$tool_stage" "$digest" "$kind"
    mv -T --no-clobber -- "$tool_stage" "$directory"
    [ ! -e "$tool_stage" ] || die "tool publication was refused"
}

candidate_python() {
    runuser -u autobit -- env -i PATH=/usr/bin:/bin \
        XDG_CACHE_HOME=/var/lib/autobit/.cache PYTHONDONTWRITEBYTECODE=1 \
        OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 \
        "$staging/.venv/bin/python" "$@"
}

cleanup_prepare_scratch() {
    local name owner mode scratch_file
    case "$download_dir" in
        /tmp/autobit-prepare.????????) ;;
        *) die "prepare scratch path is invalid: $download_dir" ;;
    esac
    [ -d "$download_dir" ] && [ ! -L "$download_dir" ] \
        || die "prepare scratch directory is invalid: $download_dir"
    owner=$(stat -c %u -- "$download_dir")
    mode=$(stat -c %a -- "$download_dir")
    [ "$owner" = "$(id -u)" ] && [ "$mode" = 700 ] \
        || die "prepare scratch directory is not private to its owner"

    # Validate the complete allowlist before removing any evidence. Archive
    # names have already passed the pinned runtime manifest validator, but are
    # checked again here because they form cleanup paths.
    for name in source.tar.gz bundle.manifest autobit-paper.service \
        "$uv_archive_name" "$python_archive_name"; do
        case "$name" in ''|.|..|*/*) die "prepare scratch file name is invalid" ;; esac
        scratch_file="$download_dir/$name"
        case "$scratch_file" in "$download_dir"/*) ;; *) die "prepare scratch file path escaped" ;; esac
        if [ -e "$scratch_file" ] || [ -L "$scratch_file" ]; then
            [ -f "$scratch_file" ] && [ ! -L "$scratch_file" ] \
                || die "prepare scratch artifact is not a regular file: $scratch_file"
        fi
    done
    for scratch_file in "$download_dir"/* "$download_dir"/.[!.]* "$download_dir"/..?*; do
        if [ ! -e "$scratch_file" ] && [ ! -L "$scratch_file" ]; then
            continue
        fi
        case "$scratch_file" in
            "$download_dir/source.tar.gz"|"$download_dir/bundle.manifest"|\
            "$download_dir/autobit-paper.service"|\
            "$download_dir/$uv_archive_name"|"$download_dir/$python_archive_name") ;;
            *) die "prepare scratch contains an unexpected artifact: $scratch_file" ;;
        esac
    done
    for name in source.tar.gz bundle.manifest autobit-paper.service \
        "$uv_archive_name" "$python_archive_name"; do
        scratch_file="$download_dir/$name"
        if [ -e "$scratch_file" ]; then
            unlink -- "$scratch_file"
        fi
    done
    # The directory is known-empty here; no recursive deletion is used.
    rmdir -- "$download_dir"
}

prepare_release() {
    check_host prepare
    acquire_deploy_lock
    validate_bundle "$archive" "$manifest" "$commit"
    release="/opt/autobit/releases/$commit"
    staging="/opt/autobit/releases/.staging-${commit}-$$"
    require_literal_managed_path "$release"
    require_literal_managed_path "$staging"
    [ ! -e "$release" ] || die "immutable release already exists: $release"
    [ ! -e "$staging" ] || die "staging directory already exists: $staging"
    download_dir=$(mktemp -d /tmp/autobit-prepare.XXXXXXXX)
    trap 'result=$?; if [ "$result" -ne 0 ]; then printf "Prepare failed; staging retained for review: %s\n" "$staging" >&2; printf "Prepare scratch retained for review: %s\n" "$download_dir" >&2; fi' EXIT
    # Freeze uploads in a root-private directory, then recheck the copied bytes.
    cp -- "$archive" "$download_dir/source.tar.gz"
    cp -- "$manifest" "$download_dir/bundle.manifest"
    archive="$download_dir/source.tar.gz"
    manifest="$download_dir/bundle.manifest"
    validate_bundle "$archive" "$manifest" "$commit"
    source_archive_sha256=$(manifest_value SOURCE_SHA256 "$manifest")
    prepare_directories
    mkdir -m 0755 -- "$staging"
    source_archive "$archive" "$staging"
    load_runtime "$staging/deploy/oci/runtime.env"
    uv_dir="/opt/autobit/tools/uv/$uv_version"
    python_dir="/opt/autobit/tools/python/${python_version}+${python_build_tag}"
    install_tool "$uv_dir" "$uv_archive_name" "$uv_archive_url" "$uv_archive_sha256" uv
    install_tool "$python_dir" "$python_archive_name" "$python_archive_url" "$python_archive_sha256" python
    uv_lock_sha256=$(sha256sum -- "$staging/uv.lock")
    uv_lock_sha256=${uv_lock_sha256%% *}
    env -i PATH=/usr/bin:/bin "$uv_dir/uv" venv --python "$python_dir/python/bin/python3.12" "$staging/.venv"
    # Non-editable installation preserves imports when staging is renamed.
    env -i PATH=/usr/bin:/bin UV_PROJECT_ENVIRONMENT="$staging/.venv" \
        /opt/autobit/tools/uv/0.12.10/uv sync \
        --frozen --no-dev --no-editable \
        --python /opt/autobit/tools/python/3.12.14+20260901/python/bin/python3.12 \
        --project "$staging"
    verify_sha256 "$staging/uv.lock" "$uv_lock_sha256"
    # Builds run with restrictive umask; runtime files must be readable by autobit.
    chmod -R a+rX,go-w -- "$staging"
    candidate_python -c 'import platform, sys; assert platform.machine() == "aarch64"; assert sys.version_info[:3] == (3, 12, 14); import autobit, numpy, pandas, scipy, backtrader, pandas_ta, httpx'
    candidate_python -m autobit.cli --help
    smoke="/var/lib/autobit/.cache/smoke/$commit"
    require_literal_managed_path "$smoke"
    [ ! -e "$smoke" ] || die "smoke directory already exists: $smoke"
    managed_directory /var/lib/autobit/.cache/smoke autobit autobit 0700
    managed_directory "$smoke" autobit autobit 0700
    ledger=/var/lib/autobit/paper/paper.sqlite3
    require_literal_managed_path "$ledger"
    if [ -e "$ledger" ]; then
        [ -f "$ledger" ] || die "paper ledger must be a regular file"
        runuser -u autobit -- env -i PATH=/usr/bin:/bin /usr/bin/python3 \
            "$staging/deploy/oci/sqlite_tools.py" snapshot --source "$ledger" --destination "$smoke/paper.sqlite3"
    else
        candidate_python -m autobit.cli paper-once --db "$smoke/paper.sqlite3" --data-dir "$smoke/data"
    fi
    candidate_python -m autobit.cli paper-status --db "$smoke/paper.sqlite3"
    # The candidate executable exists at staging until the atomic publication.
    # Verify that exact path; the tracked unit itself retains its current path.
    # Root-written artifacts stay in root-private scratch, never in the smoke
    # directory whose ancestors a running autobit process is allowed to rename.
    /usr/bin/python3 - "$staging/deploy/oci/systemd/autobit-paper.service" "$download_dir/autobit-paper.service" "$staging" <<'PY'
from pathlib import Path
import sys
text = Path(sys.argv[1]).read_text()
if text.count("/opt/autobit/current") != 2:
    raise SystemExit("candidate unit must contain exactly two current paths")
with Path(sys.argv[2]).open("x") as output:
    output.write(text.replace("/opt/autobit/current", sys.argv[3]))
PY
    systemd-analyze verify "$download_dir/autobit-paper.service"
    /usr/bin/python3 - "$staging/.autobit-release" "$commit" "$uv_version" "$python_version" "$uv_lock_sha256" "$source_archive_sha256" <<'PY'
import json
from pathlib import Path
import sys
keys = ("commit", "uv_version", "python_version", "uv_lock_sha256", "source_archive_sha256")
with Path(sys.argv[1]).open("x") as output:
    json.dump(dict(zip(keys, sys.argv[2:])), output, separators=(",", ":"), sort_keys=True)
    output.write("\n")
PY
    chmod 0644 -- "$staging/.autobit-release"
    chown -R root:root -- "$staging"
    require_literal_managed_path "$release"
    [ ! -e "$release" ] || die "immutable release appeared during prepare"
    mv -T --no-clobber -- "$staging" "$release"
    [ ! -e "$staging" ] || die "release publication was refused"
    chmod -R go-w -- "$release"
    cleanup_prepare_scratch
    trap - EXIT
    printf 'Prepared immutable release: %s\n' "$release"
}

command_name=${1-}
[ -n "$command_name" ] || die "expected prepare or activate --commit"
shift
case "$command_name" in
    prepare)
        archive= manifest= commit=
        while [ "$#" -gt 0 ]; do
            [ "$#" -ge 2 ] || die "option requires a value"
            case "$1" in
                --archive) [ -z "$archive" ] || die "duplicate archive option"; archive=$2 ;;
                --manifest) [ -z "$manifest" ] || die "duplicate manifest option"; manifest=$2 ;;
                --commit) [ -z "$commit" ] || die "duplicate commit option"; commit=$2 ;;
                *) die "unknown prepare option" ;;
            esac
            shift 2
        done
        [ -n "$archive" ] && [ -n "$manifest" ] && [ -n "$commit" ] \
            || die "archive, manifest, and commit are required"
        require_commit "$commit"
        require_input_file "$archive"
        require_input_file "$manifest"
        prepare_release
        ;;
    activate)
        [ "$#" -eq 2 ] && [ "$1" = --commit ] || die "activate --commit requires one value"
        commit=$2
        require_commit "$commit"
        check_host activate
        acquire_deploy_lock
        activate_transaction
        ;;
    *) die "expected prepare or activate --commit" ;;
esac
