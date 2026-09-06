"""Windows wrapper contracts; package tests never contact a server."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "deploy" / "oci" / "Deploy-OciPaper.ps1"


def _git(*args: object, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *map(str, args)], cwd=cwd, check=True, capture_output=True,
        text=True, encoding="utf-8",
    )


def _git_fixture_with_bare_origin(tmp_path: Path) -> tuple[Path, str]:
    remote = tmp_path / "origin.git"
    repository = tmp_path / "repository"
    _git("init", "--bare", remote)
    _git("init", "--initial-branch=main", repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Deployment Test", cwd=repository)
    (repository / "tracked.txt").write_text("tracked release content\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=repository)
    _git("commit", "-m", "initial release", cwd=repository)
    _git("remote", "add", "origin", remote, cwd=repository)
    _git("push", "-u", "origin", "main", cwd=repository)
    commit = _git("rev-parse", "HEAD", cwd=repository).stdout.strip()
    (repository / "untracked.txt").write_text("must not be bundled\n", encoding="utf-8")
    return repository, commit


def _pwsh(repository: Path, *args: object) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell 7 (pwsh) is unavailable")
    return subprocess.run(
        [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(WRAPPER),
         *map(str, args)],
        cwd=repository, capture_output=True, text=True, encoding="utf-8",
    )


def _pwsh_with_fake_remote(
    repository: Path, tmp_path: Path, *args: object, fail_prepare: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the real wrapper while replacing only its external SSH boundary."""
    executable = shutil.which("pwsh")
    if executable is None:
        pytest.skip("PowerShell 7 (pwsh) is unavailable")
    crlf_wrapper = tmp_path / "Deploy-OciPaper-CRLF.ps1"
    crlf_wrapper.write_text(
        WRAPPER.read_text(encoding="utf-8"), encoding="utf-8", newline="\r\n",
    )
    escaped_wrapper = str(crlf_wrapper).replace("'", "''")
    fail_prepare_literal = "$true" if fail_prepare else "$false"
    harness = tmp_path / "fake-remote-harness.ps1"
    harness.write_text(
        "$global:PREPARE_SSH_SEEN = $false\n"
        f"$global:FAIL_PREPARE = {fail_prepare_literal}\n"
        "function global:ssh {\n"
        "    $command = [string]$args[-1]\n"
        "    if ($command -eq 'mktemp -d /tmp/autobit-upload.XXXXXXXXXX') {\n"
        "        $global:LASTEXITCODE = 0\n"
        "        return '/tmp/autobit-upload.ABCDEFGHIJ'\n"
        "    }\n"
        "    if ($command.Contains('set -eu')) {\n"
        "        $global:PREPARE_SSH_SEEN = $true\n"
        "        if ($global:FAIL_PREPARE) {\n"
        "            $global:LASTEXITCODE = 91\n"
        "            return\n"
        "        }\n"
        "        if ($command.Contains(\"`r\")) {\n"
        "            $global:LASTEXITCODE = 97\n"
        "            return\n"
        "        }\n"
        "    }\n"
        "    $global:LASTEXITCODE = 0\n"
        "}\n"
        "function global:scp { $global:LASTEXITCODE = 0 }\n"
        "try {\n"
        f"    . '{escaped_wrapper}' @args\n"
        "}\n"
        "finally {\n"
        "    if (-not $global:PREPARE_SSH_SEEN) {\n"
        "        throw 'Prepare SSH call was not observed.'\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
        newline="\n",
    )
    return subprocess.run(
        [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(harness),
         *map(str, args)],
        cwd=repository, capture_output=True, text=True, encoding="utf-8",
    )


def _parse_manifest(path: Path) -> dict[str, str]:
    payload = path.read_bytes()
    assert not payload.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in payload
    lines = payload.decode("utf-8").splitlines()
    assert len(lines) == 3
    return dict(line.split("=", 1) for line in lines)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tar_names(path: Path) -> set[str]:
    with tarfile.open(path, "r:gz") as archive:
        return set(archive.getnames())


def test_package_only_archives_exact_remote_main_without_untracked_files(tmp_path: Path):
    repository, commit = _git_fixture_with_bare_origin(tmp_path)
    output = tmp_path / "package"

    result = _pwsh(repository, "-Mode", "Prepare", "-Commit", commit,
                   "-PackageOnly", "-PackageDirectory", output)

    assert result.returncode == 0, result.stderr
    manifest = _parse_manifest(output / "bundle.env")
    assert manifest == {
        "BUNDLE_VERSION": "1", "COMMIT": commit,
        "SOURCE_SHA256": _sha256(output / "source.tar.gz"),
    }
    names = _tar_names(output / "source.tar.gz")
    assert "source/tracked.txt" in names
    assert "source/untracked.txt" not in names


def test_package_only_rejects_a_tracked_dirty_worktree(tmp_path: Path):
    repository, commit = _git_fixture_with_bare_origin(tmp_path)
    (repository / "tracked.txt").write_text("local mutation\n", encoding="utf-8")

    result = _pwsh(repository, "-Mode", "Prepare", "-Commit", commit,
                   "-PackageOnly", "-PackageDirectory", tmp_path / "package")

    assert result.returncode != 0
    assert not (tmp_path / "package").exists()


def test_package_only_rejects_an_unpushed_or_mismatched_commit(tmp_path: Path):
    repository, commit = _git_fixture_with_bare_origin(tmp_path)
    (repository / "tracked.txt").write_text("new local commit\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=repository)
    _git("commit", "-m", "unpushed", cwd=repository)
    unpushed = _git("rev-parse", "HEAD", cwd=repository).stdout.strip()

    result = _pwsh(repository, "-Mode", "Prepare", "-Commit", unpushed,
                   "-PackageOnly", "-PackageDirectory", tmp_path / "package")

    assert result.returncode != 0
    assert unpushed != commit
    assert not (tmp_path / "package").exists()


def test_prepare_sends_lf_only_command_to_posix_remote(tmp_path: Path):
    repository, commit = _git_fixture_with_bare_origin(tmp_path)
    identity = tmp_path / "operator-key"
    identity.write_text("test-only identity placeholder\n", encoding="utf-8")

    result = _pwsh_with_fake_remote(
        repository, tmp_path,
        "-Mode", "Prepare", "-Commit", commit,
        "-HostName", "paper-host", "-User", "ubuntu",
        "-IdentityFile", identity,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_prepare_preserves_remote_failure_after_cleanup(tmp_path: Path):
    repository, commit = _git_fixture_with_bare_origin(tmp_path)
    identity = tmp_path / "operator-key"
    identity.write_text("test-only identity placeholder\n", encoding="utf-8")

    result = _pwsh_with_fake_remote(
        repository, tmp_path,
        "-Mode", "Prepare", "-Commit", commit,
        "-HostName", "paper-host", "-User", "ubuntu",
        "-IdentityFile", identity,
        fail_prepare=True,
    )

    assert result.returncode != 0
    assert "Remote prepare failed." in result.stdout + result.stderr


def test_git_attributes_force_lf_for_posix_deployment_inputs():
    paths = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "deploy" / "oci").rglob("*.sh"))
    ]
    paths.append("deploy/oci/runtime.env")

    result = _git("check-attr", "text", "eol", "--", *paths, cwd=ROOT)

    expected = []
    for path in paths:
        expected.extend((f"{path}: text: set", f"{path}: eol: lf"))
    assert result.stdout.splitlines() == expected


def test_remote_identity_lookup_does_not_disclose_a_missing_key_path(tmp_path: Path):
    repository, commit = _git_fixture_with_bare_origin(tmp_path)
    secret_key = tmp_path / "OCI_PRIVATE_KEY_do_not_leak_9d4f3a.pem"

    result = _pwsh(repository, "-Mode", "Activate", "-Commit", commit,
                   "-HostName", "paper-host", "-User", "ubuntu",
                   "-IdentityFile", secret_key)

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "IdentityFile must be an existing regular file." in combined
    assert str(secret_key) not in combined
    assert "OCI_PRIVATE_KEY_do_not_leak_9d4f3a" not in combined


def test_wrapper_declares_strict_operator_and_ssh_contracts():
    text = WRAPPER.read_text(encoding="utf-8")
    for parameter in ("Mode", "Commit", "HostName", "User", "IdentityFile", "PackageOnly",
                      "PackageDirectory"):
        assert f"${parameter}" in text
    for option in ("BatchMode=yes", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes"):
        assert option in text
    assert "prepare --archive" in text
    assert "activate --commit" in text
    assert "--archive" in text and "--manifest" in text and "--commit" in text
    assert "Get-Content" not in text
    assert "Get-FileHash -LiteralPath $IdentityFile" not in text
