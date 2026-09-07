"""Installer guard contracts; behavioral checks never invoke prepare."""

from functools import lru_cache
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMMIT = "1" * 40
REQUIRED = (
    "deploy/oci/runtime.env", "deploy/oci/systemd/autobit-paper.service",
    "deploy/oci/journald/99-autobit-persistence.conf", "deploy/oci/sqlite_tools.py",
    "deploy/oci/libdeploy.sh", "deploy/oci/install-release.sh",
    "deploy/oci/telegram_credentials.py", "uv.lock", "pyproject.toml",
)


@lru_cache
def _shell_prefix():
    if os.name != "nt":
        return ["bash"]
    if not shutil.which("wsl.exe"):
        pytest.skip("No WSL distribution is available")
    result = subprocess.run(["wsl.exe", "bash", "-c", "true"], capture_output=True)
    if result.returncode:
        pytest.skip("No runnable WSL distribution is available")
    return ["wsl.exe", "--exec", "bash"]


def _linux_path(path):
    if os.name != "nt":
        return str(path)
    _shell_prefix()
    return subprocess.run(
        ["wsl.exe", "wslpath", "-a", str(path)], check=True,
        capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()


def _run_shell(script, *args):
    prefix = _shell_prefix()
    return subprocess.run(
        [*prefix, _linux_path(ROOT / script), *map(str, args)],
        capture_output=True, text=True, encoding="utf-8",
    )


@pytest.mark.parametrize("name", ["libdeploy.sh", "install-release.sh"])
def test_installer_scripts_have_strict_non_destructive_contract(name):
    path = ROOT / "deploy/oci" / name
    assert path.is_file(), f"missing installer script: {name}"
    text = path.read_text(encoding="utf-8")
    assert "set -eu" in text
    for forbidden in (
        "rm -rf", "curl |", "wget |", "/home/ubuntu/autoBit/.env",
        "EnvironmentFile", "/v1/accounts", "/v1/orders", "cli live",
        "source runtime.env", ". runtime.env", "eval ",
    ):
        assert forbidden not in text
    combined = text + (ROOT / "deploy/oci/libdeploy.sh").read_text(encoding="utf-8")
    assert "flock" in combined


def test_prepare_does_not_change_services_or_current():
    installer = (ROOT / "deploy/oci/install-release.sh").read_text(encoding="utf-8")
    match = re.search(r"^prepare_release\(\) \{\n.*?^\}", installer, re.M | re.S)
    assert match is not None
    text = match.group(0)
    assert "systemctl" not in text
    assert "daemon-reload" not in text
    assert "ln -s" not in text
    assert "--frozen --no-dev" in text
    assert "systemd-analyze verify" in text


def test_activate_dispatch_and_transaction_contract():
    installer = (ROOT / "deploy/oci/install-release.sh").read_text(encoding="utf-8")
    library = (ROOT / "deploy/oci/libdeploy.sh").read_text(encoding="utf-8")
    combined = installer + library
    assert "activate --commit" in installer
    assert "activate_transaction" in installer
    assert "NO_EXISTING_LEDGER" in combined
    assert '"${ledger}-shm"' in combined
    assert "systemctl stop autobit-paper.service" in combined
    assert "systemctl enable autobit-paper.service" in combined
    assert "systemctl start autobit-paper.service" in combined
    assert "ln -s --" in combined
    assert "mv -Tf --" in combined
    assert "InvocationID" in combined
    assert "paper-status" in combined
    assert "180" in combined
    assert "vacuum" not in combined.lower()


def test_shared_lock_rejects_symlink_and_nonroot_precreation():
    text = (ROOT / "deploy/oci/libdeploy.sh").read_text(encoding="utf-8")
    assert "os.O_NOFOLLOW" in text
    assert "st_uid" in text
    assert "st_nlink" in text
    assert "exec 9>>/run/lock/autobit-deploy.lock" in text


def test_installer_rejects_protected_and_broad_paths():
    for candidate in ("/", "/home", "/home/ubuntu/autoBit", "/home/ubuntu/autoBit/.env",
                      "/opt/autobit/../../home", "/opt/autobit-other", "opt/autobit"):
        assert _run_shell("deploy/oci/libdeploy.sh", "check-path", candidate).returncode != 0


def test_installer_accepts_only_managed_roots():
    for candidate in ("/opt/autobit/releases", "/var/lib/autobit/paper", "/var/backups/autobit/paper"):
        assert _run_shell("deploy/oci/libdeploy.sh", "check-path", candidate).returncode == 0


def test_runtime_exact_keys_values_and_bytes(tmp_path):
    original = (ROOT / "deploy/oci/runtime.env").read_bytes()
    path = tmp_path / "runtime.env"
    path.write_bytes(original)
    assert _run_shell("deploy/oci/libdeploy.sh", "check-runtime", _linux_path(path)).returncode == 0
    for invalid in (
        original + b"EXTRA=value\n", original + b"UV_VERSION=0.12.10\n",
        original.replace(b"0.12.10", b"0.12.11"), original.replace(b"\n", b"\r\n"),
        original + b"\x00", original.rstrip(b"\n"),
        original.replace(b"https://releases.astral.sh", b"https://untrusted.invalid"),
    ):
        path.write_bytes(invalid)
        assert _run_shell("deploy/oci/libdeploy.sh", "check-runtime", _linux_path(path)).returncode != 0


def _bundle(tmp_path, *, name=None, kind=None, omit=None, archive_commit=COMMIT):
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": archive_commit}) as stream:
        for required in REQUIRED:
            if required != omit:
                item = tarfile.TarInfo("source/" + required)
                payload = b"fixture\n"
                item.size = len(payload)
                stream.addfile(item, io.BytesIO(payload))
        if name:
            item = tarfile.TarInfo(name)
            if kind:
                item.type = kind
                item.linkname = "/tmp/outside"
            stream.addfile(item, io.BytesIO(b""))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = tmp_path / "bundle.manifest"
    manifest.write_bytes(f"BUNDLE_VERSION=1\nCOMMIT={COMMIT}\nSOURCE_SHA256={digest}\n".encode())
    return archive, manifest


def _check_bundle(archive, manifest, commit=COMMIT):
    return _run_shell("deploy/oci/libdeploy.sh", "check-bundle",
                      _linux_path(archive), _linux_path(manifest), commit)


def test_bundle_manifest_hash_and_commit_are_strict(tmp_path):
    archive, manifest = _bundle(tmp_path)
    original = manifest.read_bytes()
    assert _check_bundle(archive, manifest).returncode == 0
    for invalid in (
        original + b"EXTRA=1\n", original + b"BUNDLE_VERSION=1\n",
        original.replace(b"\n", b"\r\n"), original + b"\0",
        original.rstrip(b"\n"), original.replace(b"BUNDLE_VERSION=1", b"BUNDLE_VERSION=2"),
        original.replace(COMMIT.encode(), b"2" * 40),
        original.replace(b"SOURCE_SHA256=", b"SOURCE_SHA256=A"),
        original.replace(b"SOURCE_SHA256=", b"SOURCE_SHA256=" + b"0" * 64 + b"\nUNUSED="),
    ):
        manifest.write_bytes(invalid)
        assert _check_bundle(archive, manifest).returncode != 0
    manifest.write_bytes(original)
    for invalid_commit in ("A" * 40, "1" * 39, "", "../release"):
        assert _check_bundle(archive, manifest, invalid_commit).returncode != 0
    with archive.open("ab") as stream:
        stream.write(b"corruption")
    assert _check_bundle(archive, manifest).returncode != 0


@pytest.mark.parametrize("name,kind", [
    ("../outside", None), ("/source/outside", None), ("source/../outside", None),
    ("source//outside", None), ("source/./outside", None), ("elsewhere/file", None),
    ("source/link", tarfile.SYMTYPE), ("source/link", tarfile.LNKTYPE),
    ("source/pipe", tarfile.FIFOTYPE), ("source/uv.lock", None),
])
def test_archive_rejects_unsafe_members(tmp_path, name, kind):
    archive, manifest = _bundle(tmp_path, name=name, kind=kind)
    assert _check_bundle(archive, manifest).returncode != 0


def test_archive_requires_tracked_inputs(tmp_path):
    archive, manifest = _bundle(tmp_path, omit="uv.lock")
    assert _check_bundle(archive, manifest).returncode != 0


def test_library_sourcing_has_no_dispatch_or_shell_option_side_effects():
    result = subprocess.run(
        [*_shell_prefix(), "-c", 'before=$-; source "$1"; [ "$before" = "$-" ]',
         "bash", _linux_path(ROOT / "deploy/oci/libdeploy.sh")], capture_output=True,
    )
    assert result.returncode == 0
    assert result.stdout == b""


def _run_linux_python(code, *args):
    """Use a native Linux temporary filesystem for real symlink/permission tests."""
    prefix = _shell_prefix()[:-1]
    return subprocess.run(
        [*prefix, "/usr/bin/python3", "-", *args], input=code,
        capture_output=True, text=True, encoding="utf-8",
    )


def test_new_runtime_tool_tree_is_service_traversable():
    probe = (ROOT / "tests/deployment/tool_permissions_probe.py").read_text(encoding="utf-8")
    result = _run_linux_python(
        probe,
        _linux_path(ROOT / "deploy/oci/install-release.sh"),
        "new",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_existing_runtime_tool_tree_is_made_service_traversable():
    probe = (ROOT / "tests/deployment/tool_permissions_probe.py").read_text(encoding="utf-8")
    result = _run_linux_python(
        probe,
        _linux_path(ROOT / "deploy/oci/install-release.sh"),
        "existing",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_tool_repair_is_bounded_and_reverified():
    installer = (ROOT / "deploy/oci/install-release.sh").read_text(encoding="utf-8")
    install = re.search(r"^install_tool\(\) \{\n.*?^\}", installer, re.M | re.S)
    assert install is not None
    text = install.group(0)
    assert "chmod -R" not in text
    existing = text[text.index('if [ -e "$directory" ]'):text.index("    curl --fail")]
    assert existing.count('verify_tool "$directory" "$digest" "$kind"') == 2
    assert existing.index('verify_tool "$directory"') < existing.index(
        'normalize_tool_permissions "$directory"'
    ) < existing.rindex('verify_tool "$directory"')
    new_tree = text[text.index('printf \'%s\\n\' "$digest"'):]
    assert new_tree.count('verify_tool "$tool_stage" "$digest" "$kind"') == 2
    assert new_tree.index('verify_tool "$tool_stage"') < new_tree.index(
        'normalize_tool_permissions "$tool_stage"'
    ) < new_tree.rindex('verify_tool "$tool_stage"')


@pytest.mark.parametrize("scenario", ["valid", "bad-digest", "bad-version", "bad-owner"])
def test_existing_runtime_tool_is_verified_before_permission_repair(scenario):
    probe = (ROOT / "tests/deployment/tool_validation_probe.py").read_text(encoding="utf-8")
    result = _run_linux_python(
        probe,
        _linux_path(ROOT / "deploy/oci/install-release.sh"),
        scenario,
    )
    if result.returncode == 77:
        pytest.skip(result.stderr.strip())
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_tool_permission_repair_rejects_nested_mount():
    probe = (ROOT / "tests/deployment/tool_validation_probe.py").read_text(encoding="utf-8")
    result = _run_linux_python(
        probe,
        _linux_path(ROOT / "deploy/oci/install-release.sh"),
        "nested-mount",
    )
    if result.returncode == 77:
        pytest.skip(result.stderr.strip())
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("existing_leaf", [False, True])
def test_directory_creation_cannot_follow_ancestor_swap_after_path_check(existing_leaf):
    installer = (ROOT / "deploy/oci/install-release.sh").read_text(encoding="utf-8")
    function = re.search(r"^managed_directory\(\) \{\n.*?^\}", installer, re.M | re.S)
    assert function is not None
    result = _run_linux_python(r'''
from pathlib import Path
import os
import stat
import subprocess
import sys
import tempfile
with tempfile.TemporaryDirectory(prefix="autobit-directory-race-") as fixture:
    base = Path(fixture)
    (base / "cache").mkdir()
    outside = base / "outside"
    outside.mkdir(mode=0o755)
    if sys.argv[3] == "True":
        (outside / "smoke").mkdir(mode=0o755)
    before = (outside / "smoke").stat() if (outside / "smoke").exists() else None
    # Substitute only the lexical allowlist with a temporary fixture mapping.
    # The attacker swaps the accepted ancestor immediately after that check.
    script = 'source "$1"\n' + sys.argv[2] + r"""
fixture=$2
require_literal_managed_path() {
    [ "$(readlink -m -- "$1")" = "$1" ] || return 1
    mv -- "$fixture/cache" "$fixture/cache-original"
    ln -s -- "$fixture/outside" "$fixture/cache"
}
managed_directory "$fixture/cache/smoke" "$(id -un)" "$(id -gn)" 0700
"""
    operation = subprocess.run(["bash", "-s", "--", sys.argv[1], fixture],
                               input=script, text=True, capture_output=True)
    if before is None:
        assert not (outside / "smoke").exists(), "root creation escaped into the outside directory"
    else:
        after = (outside / "smoke").stat()
        assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
            before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)
        ), "root chown/chmod followed the swapped ancestor"
    assert operation.returncode != 0, "a symlinked ancestor must fail closed"
''', _linux_path(ROOT / "deploy/oci/libdeploy.sh"), function.group(0), str(existing_leaf))
    assert result.returncode == 0, result.stdout + result.stderr


def test_candidate_unit_write_is_outside_service_owned_smoke_after_swap():
    installer = (ROOT / "deploy/oci/install-release.sh").read_text(encoding="utf-8")
    start = installer.index('    /usr/bin/python3 - "$staging/deploy/oci/systemd/autobit-paper.service"')
    end = installer.index('\n    /usr/bin/python3 - "$staging/.autobit-release"', start)
    writer = installer[start:end]
    result = _run_linux_python(r'''
from pathlib import Path
import subprocess
import sys
import tempfile
with tempfile.TemporaryDirectory(prefix="autobit-unit-race-") as fixture:
    base = Path(fixture)
    staging = base / "staging"
    unit = staging / "deploy/oci/systemd/autobit-paper.service"
    unit.parent.mkdir(parents=True)
    original = "WorkingDirectory=/opt/autobit/current\nExecStart=/opt/autobit/current/.venv/bin/python -m autobit.cli paper-run\n"
    unit.write_text(original)
    scratch = base / "private-scratch"
    scratch.mkdir(mode=0o700)
    smoke = base / "smoke"
    smoke.mkdir()
    outside = base / "outside-system-unit-directory"
    outside.mkdir()
    # A compromised service replaces its smoke directory after paper-status.
    smoke.rename(base / "old-smoke")
    smoke.symlink_to(outside, target_is_directory=True)
    script = r"""
set -eu
staging=$1
smoke=$2
download_dir=$3
systemd-analyze() { [ "$1" = verify ] && [ -f "$2" ]; }
""" + sys.argv[1]
    operation = subprocess.run(["bash", "-s", "--", str(staging), str(smoke), str(scratch)],
                               input=script, text=True, capture_output=True)
    assert not (outside / "autobit-paper.service").exists(), "root wrote a unit through service-owned ancestors"
    assert operation.returncode == 0, operation.stderr
    assert (scratch / "autobit-paper.service").read_text() == original.replace("/opt/autobit/current", str(staging))
    assert unit.read_text() == original
''', writer)
    assert result.returncode == 0, result.stdout + result.stderr


def test_prepare_success_cleans_only_validated_private_scratch_files():
    installer = (ROOT / "deploy/oci/install-release.sh").read_text(encoding="utf-8")
    match = re.search(r"^cleanup_prepare_scratch\(\) \{\n.*?^\}", installer, re.M | re.S)
    assert match is not None, "missing bounded prepare scratch cleanup"
    prepare = re.search(r"^prepare_release\(\) \{\n.*?^\}", installer, re.M | re.S)
    assert prepare is not None
    assert prepare.group(0).index("cleanup_prepare_scratch") < prepare.group(0).index(
        "Prepared immutable release"
    )
    assert "trap - EXIT" in prepare.group(0)
    assert "rm -rf" not in installer

    result = _run_linux_python(r'''
from pathlib import Path
import subprocess
import sys

helper = sys.argv[1]
script = r"""
set -eu
die() { printf '%s\n' "$*" >&2; return 1; }
""" + helper + r"""
download_dir=$(mktemp -d /tmp/autobit-prepare.XXXXXXXX)
uv_archive_name=uv.tar.gz
python_archive_name=python.tar.gz
printf '%s\n' "$download_dir"
for name in source.tar.gz bundle.manifest autobit-paper.service "$uv_archive_name" "$python_archive_name"; do
    : > "$download_dir/$name"
done
cleanup_prepare_scratch
"""
operation = subprocess.run(["bash", "-s"], input=script, text=True, capture_output=True)
assert operation.returncode == 0, operation.stderr
scratch = Path(operation.stdout.strip())
assert not scratch.exists(), "successful prepare retained private scratch"
''', match.group(0))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("case", ("unexpected", "known-symlink", "unsafe-name"))
def test_prepare_scratch_cleanup_fails_closed_and_retains_evidence(case):
    installer = (ROOT / "deploy/oci/install-release.sh").read_text(encoding="utf-8")
    match = re.search(r"^cleanup_prepare_scratch\(\) \{\n.*?^\}", installer, re.M | re.S)
    assert match is not None, "missing bounded prepare scratch cleanup"
    result = _run_linux_python(r'''
from pathlib import Path
import subprocess
import sys

helper, case = sys.argv[1:]
script = r"""
set -eu
die() { printf '%s\n' "$*" >&2; return 1; }
""" + helper + r"""
download_dir=$(mktemp -d /tmp/autobit-prepare.XXXXXXXX)
uv_archive_name=uv.tar.gz
python_archive_name=python.tar.gz
printf '%s\n' "$download_dir"
for name in source.tar.gz bundle.manifest autobit-paper.service "$uv_archive_name" "$python_archive_name"; do
    : > "$download_dir/$name"
done
case "$1" in
    unexpected) : > "$download_dir/operator-note" ;;
    known-symlink)
        : > "$download_dir/outside-marker"
        unlink -- "$download_dir/uv.tar.gz"
        ln -s -- "$download_dir/outside-marker" "$download_dir/uv.tar.gz"
        ;;
    unsafe-name) uv_archive_name=../outside-marker ;;
esac
cleanup_prepare_scratch
"""
operation = subprocess.run(["bash", "-s", "--", case], input=script, text=True, capture_output=True)
scratch = Path(operation.stdout.splitlines()[0])
try:
    assert operation.returncode != 0, "unsafe scratch shape was accepted"
    assert scratch.is_dir(), "failure did not retain scratch evidence"
    if case == "known-symlink":
        assert (scratch / "uv.tar.gz").is_symlink()
        assert (scratch / "outside-marker").read_bytes() == b""
    if case == "unexpected":
        assert (scratch / "operator-note").is_file()
        assert (scratch / "source.tar.gz").is_file(), "known evidence was deleted before the unknown entry was rejected"
finally:
    for child in scratch.iterdir():
        child.unlink()
    scratch.rmdir()
''', match.group(0), case)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("swap_at", ["none", "cache", "smoke"])
@pytest.mark.parametrize("mode", ["0700", "0755"])
def test_directory_descriptors_pin_ancestors_and_leaf_during_mutation(swap_at, mode):
    library = (ROOT / "deploy/oci/libdeploy.sh").read_text(encoding="utf-8")
    helper = library.split("make_directory_nofollow() {", 1)[1].split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    result = _run_linux_python(r'''
from pathlib import Path
import grp
import os
import pwd
import stat
import sys
import tempfile
code, swap_at, wanted_mode = sys.argv[1:]
with tempfile.TemporaryDirectory(prefix="autobit-descriptor-race-") as fixture:
    base = Path(fixture)
    cache = base / "cache"
    cache.mkdir()
    (cache / "smoke").mkdir(mode=0o755)
    outside = base / "outside"
    outside.mkdir(mode=0o755)
    original_open = os.open
    swapped = False
    def racing_open(path, flags, *args, **kwargs):
        global swapped
        descriptor = original_open(path, flags, *args, **kwargs)
        if not swapped and path == swap_at:
            # Swap after the real open returns, before the following root
            # mkdir/chown/chmod: only a pinned descriptor stays on this inode.
            swapped = True
            target = cache if swap_at == "cache" else cache / "smoke"
            target.rename(base / "pinned-original")
            target.symlink_to(outside, target_is_directory=True)
        return descriptor
    sys.argv = ["make_directory_nofollow", str(cache / "smoke"),
                pwd.getpwuid(os.getuid()).pw_name, grp.getgrgid(os.getgid()).gr_name, wanted_mode]
    os.open = racing_open
    try:
        exec(compile(code, "libdeploy.sh:make_directory_nofollow", "exec"), {})
    finally:
        os.open = original_open
    assert swapped == (swap_at != "none")
    assert list(outside.iterdir()) == [], "directory creation followed the replaced name"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755, "metadata mutation followed the replaced name"
    if swap_at == "cache":
        target = base / "pinned-original/smoke"
    elif swap_at == "smoke":
        target = base / "pinned-original"
    else:
        target = cache / "smoke"
    info = target.stat()
    assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (
        os.getuid(), os.getgid(), int(wanted_mode, 8)
    )
''', helper, swap_at, mode)
    assert result.returncode == 0, result.stdout + result.stderr


def _fresh_host_hierarchy_result(prepare_function):
    return _run_linux_python(r'''
from pathlib import Path
import os
import stat
import subprocess
import sys
import tempfile
expected = {
    "/opt/autobit": ("root", "root", "0755"),
    "/opt/autobit/releases": ("root", "root", "0755"),
    "/opt/autobit/tools": ("root", "root", "0755"),
    "/var/lib/autobit": ("autobit", "autobit", "0700"),
    "/var/lib/autobit/paper": ("autobit", "autobit", "0700"),
    "/var/lib/autobit/raw": ("autobit", "autobit", "0700"),
    "/var/lib/autobit/raw/paper": ("autobit", "autobit", "0700"),
    "/var/lib/autobit/.cache": ("autobit", "autobit", "0700"),
    "/var/backups/autobit": ("root", "root", "0700"),
    "/var/backups/autobit/paper": ("root", "root", "0700"),
    "/var/backups/autobit/config": ("root", "root", "0700"),
}
with tempfile.TemporaryDirectory(prefix="autobit-fresh-hierarchy-") as fixture:
    base = Path(fixture)
    # Only the normal OS ancestors exist; no autobit directory is pre-created.
    for ancestor in ("opt", "var/lib", "var/backups"):
        (base / ancestor).mkdir(parents=True, exist_ok=True)
    script = 'set -eu\nsource "$1"\n' + sys.argv[2] + r"""
fixture=$2
fixture_owner=$(command id -un)
fixture_group=$(command id -gn)
# Account lookup and absolute managed paths are mapped to the disposable tree.
# All directory creation/metadata work still runs the production descriptor helper.
getent() { printf '%s\n' 'autobit:x:123:123::/var/lib/autobit:/usr/sbin/nologin'; }
id() {
    case "$1" in -gn) printf 'autobit\n' ;; -u) printf '123\n' ;; *) return 1 ;; esac
}
useradd() { return 99; }
require_literal_managed_path() { :; }
managed_directory() {
    printf '%s %s %s %s\n' "$1" "$2" "$3" "$4" >> "$fixture/requests"
    make_directory_nofollow "$fixture$1" "$fixture_owner" "$fixture_group" "$4"
}
prepare_directories
"""
    operation = subprocess.run(["bash", "-s", "--", sys.argv[1], fixture],
                               input=script, text=True, capture_output=True)
    assert operation.returncode == 0, operation.stderr
    requests = [line.split() for line in (base / "requests").read_text().splitlines()]
    assert {path: tuple(values) for path, *values in requests} == expected
    paths = [request[0] for request in requests]
    assert paths.index("/var/lib/autobit/raw") < paths.index("/var/lib/autobit/raw/paper")
    for path, (_, _, mode) in expected.items():
        target = base / path.lstrip("/")
        assert target.is_dir() and not target.is_symlink(), path
        info = target.stat()
        assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (
            os.getuid(), os.getgid(), int(mode, 8)
        ), path
''', _linux_path(ROOT / "deploy/oci/libdeploy.sh"), prepare_function)


def test_fresh_host_prepares_entire_hierarchy_with_raw_parent_before_paper():
    installer = (ROOT / "deploy/oci/install-release.sh").read_text(encoding="utf-8")
    function = re.search(r"^prepare_directories\(\) \{\n.*?^\}", installer, re.M | re.S)
    assert function is not None
    result = _fresh_host_hierarchy_result(function.group(0))
    assert result.returncode == 0, result.stdout + result.stderr
