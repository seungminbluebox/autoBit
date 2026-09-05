"""Installer guard contracts; behavioral checks never invoke prepare."""

from functools import lru_cache
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMMIT = "1" * 40
REQUIRED = (
    "deploy/oci/runtime.env", "deploy/oci/systemd/autobit-paper.service",
    "deploy/oci/journald/99-autobit-persistence.conf", "deploy/oci/sqlite_tools.py",
    "deploy/oci/libdeploy.sh", "deploy/oci/install-release.sh", "uv.lock", "pyproject.toml",
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
    text = (ROOT / "deploy/oci/install-release.sh").read_text(encoding="utf-8")
    assert "systemctl" not in text
    assert "daemon-reload" not in text
    assert "ln -s" not in text
    assert "--frozen --no-dev" in text
    assert "systemd-analyze verify" in text


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
