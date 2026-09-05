# OCI Paper Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible, paper-only OCI ARM64 deployment that automatically runs `paper-run`, preserves the SQLite ledger and journal across restarts and releases, and cannot touch the legacy home tree or unlock live trading.

**Architecture:** A pinned Python 3.12/uv toolchain builds immutable commit releases under `/opt/autobit`; a hardened systemd unit runs one writer against persistent state under `/var/lib/autobit`. A strict server-side shell installer performs preparation, stopped-ledger backup, atomic symlink activation, and code rollback, while a Windows PowerShell wrapper packages the exact remote `main` commit and transfers it without storing infrastructure identifiers.

**Tech Stack:** Python 3.12, uv 0.12.10, `uv.lock`, POSIX shell, PowerShell 7, systemd 249, journald, SQLite, pytest, Git, SSH/SCP.

**Spec:** `docs/superpowers/specs/2026-09-05-oci-paper-deployment-design.md`

## Global Constraints

- Work only in the existing linked worktree `.worktrees/krw-btc-rebuild` on branch `codex/krw-btc-rebuild`; planning starts from commit `6fadfdc`.
- Target Ubuntu 22.04 on `aarch64`, 1 OCPU, 6 GB RAM. Do not resize the instance, add a block volume, install Docker/Podman, change the OS timezone, or open an inbound port.
- The operating service command is only `python -m autobit.cli paper-run --db /var/lib/autobit/paper/paper.sqlite3 --data-dir /var/lib/autobit/raw/paper`.
- Keep the production live guard fixed. Do not add a live CLI/service, API key discovery, `EnvironmentFile`, private Upbit requests, actual orders, or a lock bypass.
- Never open, source, print, hash, copy, move, chmod, chown, overwrite, or delete `/home/ubuntu/autoBit/.env`. Do not enumerate or mutate the legacy tree. Metadata-only `stat` before and after deployment is allowed.
- Do not place the OCI IP, OCID, SSH private-key path, host-key fingerprints, tokens, or secrets in any tracked file, test fixture, commit message, or journal output.
- Use exact release artifact URLs and SHA-256 values. Reject mutable `latest` downloads and all pipe-to-shell installers.
- Preserve existing untracked coverage/cache files and the inaccessible `.pytest_cache`; use explicit `git add` paths and `-p no:cacheprovider` in new test commands.
- No server mutation, `main` merge/push, service restart, VM reboot, OCI backup creation, or paid-resource change occurs during Tasks 1-8. Tasks 9-10 are controller-only gates and require the stated user approval.
- A code rollback never restores the live SQLite bundle automatically. Future schema changes must remain backward-compatible or receive a separate migration design.

## File and Responsibility Map

- `pyproject.toml`: requires the exact uv version used to generate and consume the lock.
- `uv.lock`: exact universal Python dependency resolution used by Windows verification and ARM64 production.
- `deploy/oci/runtime.env`: non-secret, literal ARM64 runtime artifact names, URLs, versions, and SHA-256 values.
- `deploy/oci/systemd/autobit-paper.service`: the single paper service, restart behavior, resource limits, and filesystem isolation.
- `deploy/oci/journald/99-autobit-persistence.conf`: explicit system-wide persistent journal retention bounds.
- `deploy/oci/sqlite_tools.py`: Python 3.10-compatible online SQLite snapshot and read-only ledger probe for staging/restart verification.
- `deploy/oci/libdeploy.sh`: path guards, manifest parsing, archive/runtime verification, release preparation, stopped backup, atomic link operations, and service checks.
- `deploy/oci/install-release.sh`: small root-only `prepare`/`activate` orchestration entrypoint protected by `flock`.
- `deploy/oci/Deploy-OciPaper.ps1`: exact-remote-main packaging, local bundle manifest, strict SSH/SCP transfer, and explicit prepare/activate modes.
- `deploy/oci/verify-service.sh`: non-disruptive service/ledger inspection with root-only evidence output, plus an explicitly selected controlled service-restart verification.
- `tests/deployment/`: runtime, unit, SQLite, shell-contract, wrapper, and paper-only deployment tests.
- `tests/safety/test_no_live_surface.py`: expands the existing safety boundary to tracked deployment assets.
- `docs/runbooks/oci-paper.md`: first install, status, logs, deployment, rollback evidence, and restart verification commands without infrastructure identifiers.
- `README.md`, `docs/paper-trading-runbook.md`: link to the OCI runbook and distinguish local/manual operation from the cloud service.

---

### Task 1: Pin the Python dependency graph and ARM64 runtime artifacts

**Files:**
- Modify: `pyproject.toml`
- Create: `uv.lock`
- Create: `deploy/oci/runtime.env`
- Create: `tests/deployment/test_runtime_contract.py`

**Interfaces:**
- Consumes: existing PEP 621 project and `dev` optional dependency group.
- Produces: `runtime.env` keys `RUNTIME_SCHEMA`, `UV_VERSION`, `UV_ARCHIVE_NAME`, `UV_ARCHIVE_URL`, `UV_ARCHIVE_SHA256`, `PYTHON_VERSION`, `PYTHON_BUILD_TAG`, `PYTHON_ARCHIVE_NAME`, `PYTHON_ARCHIVE_URL`, `PYTHON_ARCHIVE_SHA256`; exact `uv.lock` consumed by Task 4.

- [ ] **Step 1: Write the failing runtime contract tests**

Create a parser that rejects duplicate/non-literal lines and assert the exact approved artifacts:

```python
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "deploy" / "oci" / "runtime.env"


def _runtime_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in RUNTIME.read_text(encoding="utf-8").splitlines():
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*=[A-Za-z0-9_./:+%=-]+", raw)
        key, value = raw.split("=", 1)
        assert key not in values
        values[key] = value
    return values


def test_arm64_runtime_is_exactly_pinned():
    values = _runtime_values()
    assert values["RUNTIME_SCHEMA"] == "1"
    assert values["UV_VERSION"] == "0.12.10"
    assert values["UV_ARCHIVE_SHA256"] == "9ff6b9d4665edcdd3a88dcc73cd1eb641754deb927f14e8c62ebfde6bf4f5f5e"
    assert values["PYTHON_VERSION"] == "3.12.14"
    assert values["PYTHON_BUILD_TAG"] == "20260901"
    assert values["PYTHON_ARCHIVE_SHA256"] == "577b4bec0793ad1ff0cbff9adbd0df078eddde38a4c41bf5d83ad381a85ee39d"
    assert all("latest" not in value.lower() for value in values.values())


def test_uv_lock_and_exact_required_version_exist():
    assert (ROOT / "uv.lock").is_file()
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'required-version = "==0.12.10"' in project
```

- [ ] **Step 2: Run the tests and preserve the expected RED result**

Run:

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests\deployment\test_runtime_contract.py -v -p no:cacheprovider --basetemp .test-tmp\oci-runtime-red
```

Expected: FAIL because `runtime.env` and `uv.lock` do not exist.

- [ ] **Step 3: Add the exact uv requirement and runtime manifest**

Append this table to `pyproject.toml` using `apply_patch`:

```toml
[tool.uv]
required-version = "==0.12.10"
```

Create `deploy/oci/runtime.env` with exactly these LF-terminated lines:

```dotenv
RUNTIME_SCHEMA=1
UV_VERSION=0.12.10
UV_ARCHIVE_NAME=uv-aarch64-unknown-linux-gnu.tar.gz
UV_ARCHIVE_URL=https://releases.astral.sh/github/uv/releases/download/0.12.10/uv-aarch64-unknown-linux-gnu.tar.gz
UV_ARCHIVE_SHA256=9ff6b9d4665edcdd3a88dcc73cd1eb641754deb927f14e8c62ebfde6bf4f5f5e
PYTHON_VERSION=3.12.14
PYTHON_BUILD_TAG=20260901
PYTHON_ARCHIVE_NAME=cpython-3.12.14+20260901-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz
PYTHON_ARCHIVE_URL=https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.12.14%2B20260901-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz
PYTHON_ARCHIVE_SHA256=577b4bec0793ad1ff0cbff9adbd0df078eddde38a4c41bf5d83ad381a85ee39d
```

- [ ] **Step 4: Download the official Windows uv binary without executing an installer script**

Use a fresh temporary directory, verify the official checksum, and extract only after it matches:

```powershell
$uvTemp = Join-Path ([System.IO.Path]::GetTempPath()) ("autobit-uv-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -LiteralPath $uvTemp -ErrorAction Stop | Out-Null
$uvZip = Join-Path $uvTemp "uv-x86_64-pc-windows-msvc.zip"
Invoke-WebRequest -UseBasicParsing -Uri "https://releases.astral.sh/github/uv/releases/download/0.12.10/uv-x86_64-pc-windows-msvc.zip" -OutFile $uvZip
if ((Get-FileHash -LiteralPath $uvZip -Algorithm SHA256).Hash.ToLowerInvariant() -ne "f65744f94072152b1f86ba2aace4d01f1124d9a8ecb235805039e3718c36cac2") { throw "uv checksum mismatch" }
Expand-Archive -LiteralPath $uvZip -DestinationPath $uvTemp -Force
$uvExe = (Get-ChildItem -LiteralPath $uvTemp -Filter uv.exe -Recurse | Select-Object -First 1).FullName
& $uvExe --version
```

Expected: `uv 0.12.10`.

- [ ] **Step 5: Generate and validate the universal lock**

Run from the worktree root:

```powershell
& $uvExe lock --python ".\.venv\Scripts\python.exe"
& $uvExe lock --check
$env:UV_PROJECT_ENVIRONMENT = ".test-tmp\oci-lock-venv"
& $uvExe sync --frozen --extra dev --python ".\.venv\Scripts\python.exe"
Remove-Item Env:UV_PROJECT_ENVIRONMENT
```

Expected: all commands exit 0 and `uv.lock` includes hashes for every resolved distribution. Before removing `$uvTemp`, resolve both `$uvTemp` and `[System.IO.Path]::GetTempPath()`, assert the former starts with the latter plus the directory separator, then use `Remove-Item -LiteralPath $uvTemp -Recurse -Force`.

- [ ] **Step 6: Run runtime tests and dependency smoke tests**

Run:

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests\deployment\test_runtime_contract.py tests\integration\test_dependency_compat.py -v -p no:cacheprovider --basetemp .test-tmp\oci-runtime-green
```

Expected: PASS.

- [ ] **Step 7: Commit only the runtime contract**

```powershell
git add -- pyproject.toml uv.lock deploy/oci/runtime.env tests/deployment/test_runtime_contract.py
git diff --cached --check
git commit -m "build: pin OCI ARM64 runtime"
```

### Task 2: Add the hardened paper-only systemd and journal assets

**Files:**
- Create: `deploy/oci/systemd/autobit-paper.service`
- Create: `deploy/oci/journald/99-autobit-persistence.conf`
- Create: `tests/deployment/test_systemd_contract.py`

**Interfaces:**
- Consumes: persistent paths and service name from the approved spec.
- Produces: unit file installed by Task 5 and journal drop-in installed by Task 5.

- [ ] **Step 1: Write failing static contract tests**

Parse the files with `configparser.RawConfigParser(strict=True)` and assert the exact values. Include these paper-only exclusions:

```python
def test_service_is_paper_only_and_home_is_inaccessible():
    unit = _read_unit("deploy/oci/systemd/autobit-paper.service")
    service = unit["Service"]
    assert service["ExecStart"] == (
        "/opt/autobit/current/.venv/bin/python -m autobit.cli paper-run "
        "--db /var/lib/autobit/paper/paper.sqlite3 "
        "--data-dir /var/lib/autobit/raw/paper"
    )
    assert service["User"] == service["Group"] == "autobit"
    assert service["Restart"] == "always"
    assert service["KillSignal"] == "SIGINT"
    assert service["ProtectHome"] == "true"
    assert service["ProtectSystem"] == "strict"
    assert service["ReadWritePaths"] == "/var/lib/autobit"
    text = (ROOT / "deploy/oci/systemd/autobit-paper.service").read_text()
    assert "EnvironmentFile" not in text
    assert " live" not in text.lower()
    assert "telegram" not in text.lower()
```

Also assert `StartLimitIntervalSec=0`, `RestartSec=60s`, `TimeoutStopSec=120s`, `UMask=0077`, the four numeric-library thread limits, `PYTHONDONTWRITEBYTECODE=1`, and the exact four journald bounds.

- [ ] **Step 2: Run the tests and verify RED**

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests\deployment\test_systemd_contract.py -v -p no:cacheprovider --basetemp .test-tmp\oci-systemd-red
```

Expected: FAIL because both configuration assets are absent.

- [ ] **Step 3: Create the exact service unit**

Use `apply_patch` to create:

```ini
[Unit]
Description=autoBit KRW-BTC normalized paper trading
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=autobit
Group=autobit
WorkingDirectory=/opt/autobit/current
ExecStart=/opt/autobit/current/.venv/bin/python -m autobit.cli paper-run --db /var/lib/autobit/paper/paper.sqlite3 --data-dir /var/lib/autobit/raw/paper
Restart=always
RestartSec=60s
KillSignal=SIGINT
TimeoutStopSec=120s
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/autobit
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
Environment="HOME=/var/lib/autobit" "XDG_CACHE_HOME=/var/lib/autobit/.cache" "PYTHONDONTWRITEBYTECODE=1" "OMP_NUM_THREADS=1" "OPENBLAS_NUM_THREADS=1" "MKL_NUM_THREADS=1" "NUMEXPR_NUM_THREADS=1"

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Create the bounded persistent-journal drop-in**

```ini
[Journal]
Storage=persistent
SystemMaxUse=512M
SystemKeepFree=5G
MaxRetentionSec=90day
```

- [ ] **Step 5: Run tests and commit**

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests\deployment\test_systemd_contract.py -v -p no:cacheprovider --basetemp .test-tmp\oci-systemd-green
git add -- deploy/oci/systemd/autobit-paper.service deploy/oci/journald/99-autobit-persistence.conf tests/deployment/test_systemd_contract.py
git diff --cached --check
git commit -m "ops: define hardened paper service"
```

Expected: PASS, then one focused commit.

### Task 3: Add consistent SQLite snapshot and restart-probe tooling

**Files:**
- Create: `deploy/oci/sqlite_tools.py`
- Create: `tests/deployment/test_sqlite_tools.py`

**Interfaces:**
- Produces: `snapshot(source: Path, destination: Path) -> None`; `probe(database: Path) -> dict[str, object]`; CLI subcommands `snapshot --source PATH --destination PATH` and `probe --db PATH`.
- Probe JSON keys: `schema_version`, `event_count`, `max_event_sequence`, `order_count`, `snapshot_count`, `quick_check`. Output uses sorted keys and compact separators.

- [ ] **Step 1: Write RED tests against a WAL-mode fixture**

Use standard-library SQLite to create `schema_version`, `events`, `orders`, and `snapshots`. Keep one writer connection open in WAL mode, invoke `snapshot`, and assert the destination has all committed rows without copying `-wal` or `-shm`:

```python
def test_online_snapshot_is_consistent_and_does_not_copy_companions(tmp_path):
    source = tmp_path / "paper.sqlite3"
    destination = tmp_path / "snapshot.sqlite3"
    connection = _ledger_fixture(source, journal_mode="WAL")
    connection.execute(
        "INSERT INTO events(event_id, event_type, occurred_at_utc, payload_json) VALUES(?, ?, ?, ?)",
        ("cycle:1", "PAPER_CYCLE", "2026-09-05T00:00:00Z", "{}"),
    )
    connection.commit()
    module.snapshot(source, destination)
    assert module.probe(destination)["event_count"] == 1
    assert not Path(f"{destination}-wal").exists()
    assert not Path(f"{destination}-shm").exists()
    connection.close()
```

Add tests that reject a missing source, an existing destination, source==destination, failed `quick_check`, and unknown probe schema.

- [ ] **Step 2: Run and verify RED**

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests\deployment\test_sqlite_tools.py -v -p no:cacheprovider --basetemp .test-tmp\oci-sqlite-red
```

Expected: import/file-not-found failure for `deploy/oci/sqlite_tools.py`.

- [ ] **Step 3: Implement the online backup with read-only source access**

Use this core, then add exact path/error handling and CLI parsing:

```python
def snapshot(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    if destination.exists() or source == destination.resolve(strict=False):
        raise ValueError("snapshot destination must be new and distinct")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{quote(source.as_posix(), safe='/')}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_db:
        source_db.execute("PRAGMA query_only=ON")
        with sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)
            result = destination_db.execute("PRAGMA quick_check").fetchone()
            if result != ("ok",):
                raise RuntimeError("snapshot quick_check failed")
```

On any failure, unlink only a destination created by this call after verifying its resolved parent is the caller-supplied destination parent. Never remove the source or companion files. `probe` must open `mode=ro`, set `query_only=ON`, verify the exact required tables, run `quick_check`, and use aggregate `SELECT COUNT(*)`/`MAX(sequence)` queries only.

- [ ] **Step 4: Verify Python 3.10 syntax compatibility and GREEN tests**

Avoid 3.11/3.12-only standard-library APIs. Run:

```powershell
& ".\.venv\Scripts\python.exe" -m py_compile deploy\oci\sqlite_tools.py
& ".\.venv\Scripts\python.exe" -m pytest tests\deployment\test_sqlite_tools.py -v -p no:cacheprovider --basetemp .test-tmp\oci-sqlite-green
```

Expected: PASS. The same compile command will later run with `/usr/bin/python3` 3.10 on OCI.

- [ ] **Step 5: Commit**

```powershell
git add -- deploy/oci/sqlite_tools.py tests/deployment/test_sqlite_tools.py
git diff --cached --check
git commit -m "ops: add safe SQLite deployment probes"
```

### Task 4: Implement strict bundle validation and immutable release preparation

**Files:**
- Create: `deploy/oci/libdeploy.sh`
- Create: `deploy/oci/install-release.sh`
- Create: `tests/deployment/test_installer_contract.py`

**Interfaces:**
- Prepare interface: `sudo bash install-release.sh prepare --archive "$ArchivePath" --manifest "$ManifestPath" --commit "$Commit"`; all three variables must already contain validated absolute paths or a 40-character lowercase hex commit.
- Activate interface: `sudo bash "/opt/autobit/releases/${commit}/deploy/oci/install-release.sh" activate --commit "$commit"`; `commit` is the validated 40-character lowercase hex value.
- Bundle manifest has exactly three LF-delimited lines: `BUNDLE_VERSION=1`, `COMMIT=${commit}`, `SOURCE_SHA256=${source_sha256}`; `source_sha256` is exactly 64 lowercase hexadecimal characters.
- `prepare` may create the system user, managed directories, verified toolchain, and immutable release; it must not install/enable/start/stop/restart a service or change `current`.

- [ ] **Step 1: Write RED installer-contract tests**

Assert both scripts exist, use `set -eu`, use `flock`, never source `runtime.env` or the upload manifest, and contain no `rm -rf`, `curl |`, `wget |`, `/home/ubuntu/autoBit/.env`, private API path, live command, or environment-file reference. Test the exact runtime-key parser and path policy by invoking exported check-only modes:

```python
def test_installer_rejects_protected_and_broad_paths():
    for candidate in ("/", "/home", "/home/ubuntu/autoBit", "/home/ubuntu/autoBit/.env"):
        result = _run_shell("deploy/oci/libdeploy.sh", "check-path", candidate)
        assert result.returncode != 0


def test_installer_accepts_only_managed_roots():
    for candidate in ("/opt/autobit/releases", "/var/lib/autobit/paper", "/var/backups/autobit/paper"):
        result = _run_shell("deploy/oci/libdeploy.sh", "check-path", candidate)
        assert result.returncode == 0
```

On Windows, use `wsl.exe wslpath -a` to translate the worktree path before calling `bash`; skip only these behavioral tests when no WSL distribution is available. Static tests never skip.

- [ ] **Step 2: Run and verify RED**

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests\deployment\test_installer_contract.py -v -p no:cacheprovider --basetemp .test-tmp\oci-installer-red
```

Expected: FAIL because the shell scripts do not exist.

- [ ] **Step 3: Implement strict parsing and path guards in `libdeploy.sh`**

Expose a test-safe dispatcher only when the file is executed directly; sourcing it performs no action. Use exact regex checks and never `eval`/source manifests:

```sh
require_commit() {
    case "$1" in
        *[!0-9a-f]*|'') die "commit must be lowercase hexadecimal" ;;
    esac
    [ "${#1}" -eq 40 ] || die "commit must contain 40 characters"
}

require_managed_path() {
    candidate=$(readlink -m -- "$1")
    case "$candidate" in
        /opt/autobit|/opt/autobit/*|/var/lib/autobit|/var/lib/autobit/*|/var/backups/autobit|/var/backups/autobit/*) ;;
        *) die "path is outside managed roots" ;;
    esac
    case "$candidate" in
        /|/home|/home/*) die "home and broad paths are forbidden" ;;
    esac
}

manifest_value() {
    key=$1
    file=$2
    count=$(awk -F= -v wanted="$key" '$1 == wanted { count += 1 } END { print count + 0 }' "$file")
    [ "$count" -eq 1 ] || die "manifest key count is invalid"
    awk -F= -v wanted="$key" '$1 == wanted { sub(/^[^=]*=/, ""); print }' "$file"
}
```

Validate exactly three manifest lines/keys, no CR/NUL, commit equality, 64 lowercase hex SHA, archive hash, `uname -m=aarch64`, Ubuntu ID/version, at least 5 GiB free on `/`, `/usr/bin/python3` availability, and root UID. Archive entries must all begin `source/`, contain no empty/absolute/`..` segments, and contain the tracked runtime, unit, lock, project, and installer files.

- [ ] **Step 4: Implement verified runtime installation and frozen release build**

`prepare` must acquire `/run/lock/autobit-deploy.lock`, create `autobit` as a no-login system user with home `/var/lib/autobit`, and create only these modes:

```text
/opt/autobit                         root:root       0755
/opt/autobit/releases                root:root       0755
/opt/autobit/tools                   root:root       0755
/var/lib/autobit                     autobit:autobit 0700
/var/lib/autobit/paper               autobit:autobit 0700
/var/lib/autobit/raw/paper           autobit:autobit 0700
/var/lib/autobit/.cache              autobit:autobit 0700
/var/backups/autobit                 root:root       0700
/var/backups/autobit/paper           root:root       0700
/var/backups/autobit/config          root:root       0700
```

Read every `runtime.env` key with `manifest_value`. Download to a `mktemp -d` directory using `curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --output`, verify with `sha256sum`, then extract. Install uv under `/opt/autobit/tools/uv/0.12.10/` and Python under `/opt/autobit/tools/python/3.12.14+20260901/`; if either directory exists, verify its binary version and recorded archive hash rather than overwriting it.

Extract source to `/opt/autobit/releases/.staging-${commit}-$$`, create `.venv` there, and run:

```sh
UV_PROJECT_ENVIRONMENT="$staging/.venv" \
    /opt/autobit/tools/uv/0.12.10/uv sync \
    --frozen --no-dev \
    --python /opt/autobit/tools/python/3.12.14+20260901/python/bin/python3.12 \
    --project "$staging"
```

Verify `platform.machine() == "aarch64"`, `sys.version_info[:3] == (3, 12, 14)`, imports of `autobit`, `numpy`, `pandas`, `scipy`, `backtrader`, `pandas_ta`, and `httpx`, plus `python -m autobit.cli --help`. If a live ledger exists, create an online snapshot with Task 3 and run candidate `paper-status` against the snapshot. If no ledger exists, run `paper-once` against a commit-specific smoke DB/data directory under `/var/lib/autobit/.cache/smoke/`, then run `paper-status` there.

Create `.autobit-release` containing compact JSON with `commit`, `uv_version`, `python_version`, `uv_lock_sha256`, and `source_archive_sha256`. Rename staging to `/opt/autobit/releases/${commit}` only after every check passes, then remove group/other write permissions. Never delete a failed staging directory; print its exact path for review.

- [ ] **Step 5: Add candidate unit verification without changing the installed unit**

Copy the tracked unit to the release smoke directory, replace both `/opt/autobit/current` occurrences with the exact candidate release path in that copy, and run `systemd-analyze verify` on the copy. Do not call daemon-reload during `prepare`.

- [ ] **Step 6: Run syntax/contract tests and commit**

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests\deployment\test_installer_contract.py tests\deployment\test_runtime_contract.py tests\deployment\test_systemd_contract.py tests\deployment\test_sqlite_tools.py -v -p no:cacheprovider --basetemp .test-tmp\oci-prepare-green
```

When WSL is available, additionally run translated-path `bash -n` for both shell files. Expected: PASS.

```powershell
git add -- deploy/oci/libdeploy.sh deploy/oci/install-release.sh tests/deployment/test_installer_contract.py
git diff --cached --check
git commit -m "ops: prepare immutable OCI releases"
```

### Task 5: Add stopped-ledger backup, atomic activation, and code rollback

**Files:**
- Modify: `deploy/oci/libdeploy.sh`
- Modify: `deploy/oci/install-release.sh`
- Modify: `tests/deployment/test_installer_contract.py`
- Create: `tests/deployment/test_release_transaction.py`

**Interfaces:**
- Consumes: a prepared `/opt/autobit/releases/${commit}/.autobit-release` and Task 2 assets.
- Produces: `activate --commit "$commit"` where `commit` is exactly 40 lowercase hex characters; backup directories named with UTC basic timestamp plus commit; atomic `/opt/autobit/current`; enabled `autobit-paper.service`.

- [ ] **Step 1: Write transaction RED tests**

Build a Linux-only temporary-root harness around exported pure shell functions. Assert:

```python
def test_backup_copies_db_and_wal_but_never_shm(transaction_root):
    _make_ledger_bundle(transaction_root, companions=("-wal", "-shm"))
    backup = _run_function("backup_closed_ledger", transaction_root)
    assert (backup / "paper.sqlite3").is_file()
    assert (backup / "paper.sqlite3-wal").is_file()
    assert not (backup / "paper.sqlite3-shm").exists()
    assert (backup / "SHA256SUMS").is_file()
    assert (backup / "paper-status.json").is_file()


def test_atomic_switch_restores_previous_link_without_restoring_db(transaction_root):
    previous, candidate, ledger = _transaction_fixture(transaction_root)
    before = ledger.read_bytes()
    _simulate_failed_activation(previous, candidate, ledger)
    assert _current_target(transaction_root) == previous
    assert ledger.read_bytes() == before
```

Also cover first install (`NO_EXISTING_LEDGER`), orphan WAL/SHM refusal, existing backup-directory refusal, inactive writer check, and a candidate missing readiness metadata.

- [ ] **Step 2: Run and verify RED**

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests\deployment\test_release_transaction.py tests\deployment\test_installer_contract.py -v -p no:cacheprovider --basetemp .test-tmp\oci-transaction-red
```

Expected: FAIL because activation/backup functions are absent.

- [ ] **Step 3: Implement stopped backup and manifest evidence**

`activate` acquires the same `flock`, validates the commit/readiness JSON and candidate binaries, records the previous symlink target, then calls `systemctl stop autobit-paper.service` only if the unit exists. Wait until `systemctl is-active` is not `active`/`activating`, and reject any remaining process whose command line contains both `autobit.cli paper-run` and the production DB path.

For an existing ledger, copy the closed DB and existing WAL into a `bundle/` child with `cp --preserve=mode,timestamps`; never copy SHM. Write `SHA256SUMS` for only the durable bundle files and verify it with `sha256sum -c`. Use Task 3 to consolidate that bundle into `verification/paper.sqlite3`, run candidate `paper-status` against the verification copy, and save the JSON beside it so validation cannot add an SHM file to the restore bundle. Also write `release-before.txt` and `created-at-utc.txt`. For first install, require DB, WAL, and SHM all absent and write exactly `NO_EXISTING_LEDGER\n` to `ledger-state.txt`. Any orphan companion aborts before changing `current`.

- [ ] **Step 4: Install config with recoverable backups and switch atomically**

If installed unit or journald drop-in content differs, copy it to a fresh `/var/backups/autobit/config/${timestamp}-${commit}/` before replacement. Install the tracked files with root ownership and mode `0644`. Create `/var/log/journal` with the existing systemd journal group ownership, run `systemd-tmpfiles --create --prefix /var/log/journal`, `systemctl restart systemd-journald`, and confirm `journalctl --disk-usage` exits 0. Never run a vacuum command.

Create a new sibling link and rename it:

```sh
next_link="/opt/autobit/.current-${commit}-$$"
ln -s -- "/opt/autobit/releases/${commit}" "$next_link"
mv -Tf -- "$next_link" /opt/autobit/current
systemctl daemon-reload
systemctl enable autobit-paper.service
systemctl start autobit-paper.service
```

Poll for at most 180 seconds. Success requires `systemctl is-active --quiet`, one non-zero `MainPID`, a command line containing the exact paper DB/data paths, candidate `paper-status` exit 0, and at least one journal record for the current invocation ID.

- [ ] **Step 5: Implement code-only rollback**

If post-start validation fails, call `systemctl stop`, atomically repoint `current` to the recorded previous release when one exists, daemon-reload, and start the previous service. Do not copy any backup over `/var/lib/autobit`. On first-install failure with no previous release, leave the candidate and backup evidence intact, keep the service stopped/disabled, and return non-zero with the recovery paths. A failed rollback returns a distinct non-zero code and prints both release targets.

- [ ] **Step 6: Run transaction, paper restart, and live-lock tests**

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests\deployment tests\integration\test_paper_restart.py tests\integration\test_paper_acceptance.py tests\safety\test_live_boundary.py -v -p no:cacheprovider --basetemp .test-tmp\oci-activate-green
```

Expected: PASS; Linux shell behavioral tests may skip on Windows only when WSL is unavailable, with static contracts still passing.

- [ ] **Step 7: Commit**

```powershell
git add -- deploy/oci/libdeploy.sh deploy/oci/install-release.sh tests/deployment/test_installer_contract.py tests/deployment/test_release_transaction.py
git diff --cached --check
git commit -m "ops: activate OCI releases transactionally"
```

### Task 6: Build the Windows exact-main packaging and SSH wrapper

**Files:**
- Create: `deploy/oci/Deploy-OciPaper.ps1`
- Create: `tests/deployment/test_deploy_wrapper.py`

**Interfaces:**
- Prepare: `Deploy-OciPaper.ps1 -Mode Prepare -Commit $Commit -HostName $HostName -User ubuntu -IdentityFile $IdentityFile`.
- Activate: `Deploy-OciPaper.ps1 -Mode Activate -Commit $Commit -HostName $HostName -User ubuntu -IdentityFile $IdentityFile`.
- Local test/package: `Deploy-OciPaper.ps1 -Mode Prepare -Commit $Commit -PackageOnly -PackageDirectory $PackageDirectory`.
- `$Commit` is a 40-character lowercase hex commit, `$HostName` is an operator-supplied SSH host or alias, `$IdentityFile` is an existing operator-local private-key file, and `$PackageDirectory` is a new empty local directory.
- The wrapper accepts host/user/key values only as process parameters and never writes them to the bundle or repository.

- [ ] **Step 1: Write wrapper RED tests with a temporary Git remote**

Create a bare repository and working clone in `tmp_path`, commit known tracked/untracked files, push `main`, and call PowerShell 7 in `PackageOnly` mode. Assert the tar contains only the exact commit under `source/`, the manifest contains only three keys, and mismatched/non-remote commits fail:

```python
def test_package_only_archives_exact_remote_main_without_untracked_files(tmp_path):
    repository, commit = _git_fixture_with_bare_origin(tmp_path)
    output = tmp_path / "package"
    result = _pwsh(repository, "-Mode", "Prepare", "-Commit", commit,
                    "-PackageOnly", "-PackageDirectory", str(output))
    assert result.returncode == 0
    manifest = _parse_manifest(output / "bundle.env")
    assert manifest["COMMIT"] == commit
    assert _sha256(output / "source.tar.gz") == manifest["SOURCE_SHA256"]
    names = _tar_names(output / "source.tar.gz")
    assert "source/tracked.txt" in names
    assert "source/untracked.txt" not in names
```

Skip subprocess behavior only when `pwsh` is unavailable; retain static tests for parameter names, `BatchMode=yes`, `IdentitiesOnly=yes`, and `StrictHostKeyChecking=yes`.

- [ ] **Step 2: Run and verify RED**

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests\deployment\test_deploy_wrapper.py -v -p no:cacheprovider --basetemp .test-tmp\oci-wrapper-red
```

Expected: FAIL because the wrapper is absent.

- [ ] **Step 3: Implement exact remote-main packaging**

Validate commit with `^[0-9a-f]{40}$`, execute `git ls-remote --exit-code origin refs/heads/main`, require exactly one returned hash equal to `-Commit`, and run `git cat-file -e "${Commit}^{commit}"`. Produce `source.tar.gz` with `git archive --format=tar.gz --prefix=source/ --output=$archive $Commit`; hash with `Get-FileHash` and write `bundle.env` as BOM-free UTF-8 with LF lines:

```text
BUNDLE_VERSION=1
COMMIT=${Commit}
SOURCE_SHA256=${lowercaseSha256}
```

For `PackageOnly`, require a new empty output directory and stop before resolving SSH arguments.

- [ ] **Step 4: Implement strict SSH/SCP prepare and activate modes**

Require existing identity file metadata but never call `Get-Content`/`Get-FileHash` on it. Pass these common arguments as an array to both tools:

```powershell
$sshOptions = @(
    "-o", "BatchMode=yes",
    "-o", "IdentitiesOnly=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-i", $IdentityFile
)
```

Prepare mode creates a remote directory using `mktemp -d /tmp/autobit-upload.XXXXXXXXXX`, transfers only `source.tar.gz` and `bundle.env`, remotely verifies the archive SHA, extracts `source/deploy/oci/install-release.sh` from the verified tar, and calls its `prepare` interface with sudo. Activate mode uploads nothing and calls the prepared release installer `activate --commit` using the exact absolute release path. Validate host/user/remote-directory tokens before interpolation; do not print the identity path.

In `finally`, delete local temp files only after resolving the generated directory beneath `[System.IO.Path]::GetTempPath()`. On the server, remove the two uploaded regular files with `rm -f --` and remove the now-empty upload directory with `rmdir --`; never use recursive remote deletion.

- [ ] **Step 5: Run wrapper tests and commit**

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests\deployment\test_deploy_wrapper.py tests\deployment\test_installer_contract.py -v -p no:cacheprovider --basetemp .test-tmp\oci-wrapper-green
git add -- deploy/oci/Deploy-OciPaper.ps1 tests/deployment/test_deploy_wrapper.py
git diff --cached --check
git commit -m "ops: package and transfer exact main release"
```

Expected: PASS.

### Task 7: Add service-restart evidence collection and the OCI runbook

**Files:**
- Create: `deploy/oci/verify-service.sh`
- Create: `tests/deployment/test_verify_service_contract.py`
- Create: `docs/runbooks/oci-paper.md`
- Modify: `README.md`
- Modify: `docs/paper-trading-runbook.md`

**Interfaces:**
- Non-disruptive evidence collection: `sudo bash /opt/autobit/current/deploy/oci/verify-service.sh inspect`; it does not change the service or ledger but creates a new root-only evidence directory.
- Controlled mutation: `sudo bash /opt/autobit/current/deploy/oci/verify-service.sh restart`
- Evidence root: `/var/backups/autobit/verification/${utc_timestamp}-${commit}/`.

- [ ] **Step 1: Write RED verification-script tests**

Assert `inspect` never calls `systemctl restart/stop/start`; `restart` records before evidence first and performs exactly one `systemctl restart autobit-paper.service`. Assert it never hashes an active DB, copies the production ledger, reads `.env`, uses jq, or invokes a live/private command. Require use of Task 3 `probe`, `paper-status`, `systemctl show`, and `journalctl --show-cursor`.

- [ ] **Step 2: Run and verify RED**

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests\deployment\test_verify_service_contract.py -v -p no:cacheprovider --basetemp .test-tmp\oci-verify-red
```

Expected: FAIL because `verify-service.sh` is absent.

- [ ] **Step 3: Implement read-only inspect and one-restart verification**

Both modes acquire the deploy `flock` and create root-only evidence. Before evidence contains service properties, process command line, `paper-status-before.json`, `ledger-probe-before.json`, journal cursor, boot ID, release target, and UTC time. `restart` then executes one service restart, waits at most 180 seconds for the Task 5 health conditions, and records equivalent after evidence.

Use a Python snippet to compare JSON. Require unchanged `market=KRW-BTC` and `mode=normalized-paper`; `event_count`, `max_event_sequence`, and `last_completed_candle_utc` must not decrease. If the last candle did not advance, require cash, BTC quantity, position, active stop, pending orders, and health stage to match exactly. Verify that pre-restart journal entries remain readable and at least one entry exists after the saved cursor. Never require the active DB file hash to remain constant while the writer runs.

- [ ] **Step 4: Write the operator runbook**

Document these exact flows without real infrastructure values:

1. Read-only local and SSH prerequisites.
2. `Read-Host` prompts for host/identity path so no values are pasted into the document or shell history.
3. `Prepare` command and how to read the returned release metadata.
4. Separate `Activate` command only after the server-change approval.
5. `systemctl status`, `paper-status`, and `journalctl -u autobit-paper.service` commands.
6. `verify-service.sh inspect` and `restart` evidence paths.
7. Backup bundle contents and code-only rollback behavior.
8. Explicit statement that the legacy `.env` is unused, live remains locked, journald limits are global, and same-volume backups do not protect disk loss.
9. VM reboot and OCI external backup remain separate approval steps; do not provide an unattended reboot command.
10. Always Free idle-reclamation risk and the prohibition on artificial load.

Link the runbook from README and the existing paper runbook. Keep local Windows instructions intact.

- [ ] **Step 5: Run tests and commit**

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests\deployment\test_verify_service_contract.py tests\deployment\test_systemd_contract.py tests\integration\test_paper_cli.py -v -p no:cacheprovider --basetemp .test-tmp\oci-runbook-green
git add -- deploy/oci/verify-service.sh tests/deployment/test_verify_service_contract.py docs/runbooks/oci-paper.md README.md docs/paper-trading-runbook.md
git diff --cached --check
git commit -m "docs: add OCI paper operations runbook"
```

Expected: PASS.

### Task 8: Extend safety coverage and freeze the release candidate

**Files:**
- Modify: `tests/safety/test_no_live_surface.py`
- Create evidence only under ignored `.superpowers/sdd/2026-09-05-oci-paper-deployment/`; do not commit evidence.

**Interfaces:**
- Produces: a source-frozen branch commit reviewed against the spec, with targeted and full-suite evidence.

- [ ] **Step 1: Add deployment-surface safety tests**

Enumerate tracked files beneath `deploy/oci` and assert:

```python
def test_oci_deployment_surface_cannot_activate_live_or_load_secrets():
    files = _git_paths("deploy/oci")
    combined = "\n".join(path.read_text(encoding="utf-8", errors="strict") for path in files)
    forbidden = (
        "EnvironmentFile", "UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY",
        "--telegram-token-env", "--telegram-chat-env", "autobit.cli live",
        "/v1/orders", "/v1/accounts", "source /home/ubuntu/autoBit/.env",
    )
    assert all(token not in combined for token in forbidden)
    service = Path("deploy/oci/systemd/autobit-paper.service").read_text()
    assert "paper-run" in service
    assert "ProtectHome=true" in service
```

Also assert the tracked deployment text contains no IPv4 literal, OCI OCID prefix, Windows drive path ending `.key`, or SSH SHA256 fingerprint.

- [ ] **Step 2: Run the focused deployment and safety suite**

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests\deployment tests\safety\test_no_live_surface.py tests\safety\test_live_boundary.py tests\integration\test_paper_acceptance.py tests\integration\test_paper_restart.py tests\integration\test_three_mode_contract.py -v -p no:cacheprovider --basetemp .test-tmp\oci-focused-final
```

Expected: PASS. Save stdout, stderr, exit code, command, HEAD, and UTC timestamps beneath the ignored evidence directory.

- [ ] **Step 3: Run shell and PowerShell syntax checks**

Run PowerShell parser validation without executing the wrapper:

```powershell
$tokens = $null
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path "deploy\oci\Deploy-OciPaper.ps1"), [ref]$tokens, [ref]$errors) | Out-Null
if ($errors.Count -ne 0) { $errors | Format-List; throw "PowerShell parse failed" }
```

Run `bash -n` for all tracked `deploy/oci/*.sh` in WSL and later repeat it on OCI. Expected: zero parser errors.

- [ ] **Step 4: Review against every spec section and commit the safety delta**

Check off spec Sections 1-13 against concrete files/tests. Search for unfinished-work markers, mutable `latest` download URLs, pipe-to-shell, `rm -rf`, `EnvironmentFile`, infrastructure identifiers, and unreviewed service names. Fix any gap with a RED test first.

```powershell
git add -- tests/safety/test_no_live_surface.py
git diff --cached --check
git commit -m "test: lock OCI deployment to paper mode"
```

- [ ] **Step 5: Run the full repository suite once after source freeze**

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests -v -p no:cacheprovider --basetemp .test-tmp\oci-full-final
```

Expected: every test passes. Do not edit source after this run without rerunning the affected tests and the final full suite. Retain durable evidence with exact count, elapsed time, exit code, HEAD, and command.

- [ ] **Step 6: Perform final code review**

Use `superpowers:requesting-code-review` after all tests pass. Resolve all validated findings through RED→GREEN fixes and repeat source-freeze verification. Confirm `git status --short` contains only the pre-existing untracked coverage/cache artifacts and no unstaged deployment changes.

### Task 9: Integrate the reviewed branch into remote `main` (controller only)

**Files:** No new file changes expected.

**Interfaces:**
- Consumes: source-frozen reviewed branch and Task 8 evidence.
- Produces: remote `refs/heads/main` equal to the reviewed 40-character commit.

- [ ] **Step 1: Stop and obtain explicit integration approval**

Report the final commit, changed-file summary, test evidence, live-lock result, and that OCI remains unmodified. Ask the user to approve `main` merge and push. Do not combine this approval with OCI activation.

- [ ] **Step 2: Invoke the finishing workflow and recheck repository state**

Use `superpowers:finishing-a-development-branch`. In both the linked worktree and primary checkout, inspect branch, `git status --short`, worktree list, and `git fetch origin`. Preserve unrelated/untracked user files and stop if tracked local changes overlap.

- [ ] **Step 3: Merge without rewriting history and push normally**

Merge `codex/krw-btc-rebuild` into local `main` using a normal fast-forward when possible; otherwise use a non-destructive merge commit after confirming no conflict. Never force push. Run the focused deployment safety suite on the exact merge commit, then:

```powershell
git push origin main
git ls-remote --exit-code origin refs/heads/main
```

Expected: the remote hash exactly matches local `main`. Record that 40-character hash for Task 10.

### Task 10: Prepare, activate, and restart-test the exact OCI release (controller only)

**Files:** Server paths from the spec; no repository edits.

**Interfaces:**
- Consumes: the exact remote-main commit from Task 9, operator-local host/user/key values, and approved deployment tools.
- Produces: active `autobit-paper.service`, persistent state/logs, stopped-ledger backup, and restart evidence.

- [ ] **Step 1: Perform a new read-only OCI preflight**

Verify TCP 22, strict known-host matching, `uname -m=aarch64`, Ubuntu version, systemd/cron state, free disk, absence of Docker/Podman requirement, current listening ports, and service-name collision. Use `stat` metadata only for `/home/ubuntu/autoBit` and its `.env`; do not read or hash contents. Confirm no failed unit and no process already writing the planned production DB.

- [ ] **Step 2: Obtain explicit approval for initial server mutations**

Report the preflight, exact remote-main commit, directories/users/config to be created, global journald bounds, expected outbound downloads, and that no paid OCI resource or inbound rule changes. Wait for approval before running `Prepare`.

- [ ] **Step 3: Run remote Prepare and inspect without activation**

Collect operator-local values through `Read-Host`, then invoke `Deploy-OciPaper.ps1 -Mode Prepare`. Verify the returned release metadata, exact uv/Python versions, frozen install, ARM64 imports, candidate unit verification, smoke ledger/status, permissions, and unchanged legacy metadata. Run `verify-service.sh inspect` only if a prior service already exists. Report results before activation.

- [ ] **Step 4: Obtain separate activation approval**

State that activation will install the unit/journal drop-in, gracefully stop any previous paper service, create the closed-ledger backup, atomically change `current`, enable/start the service, and may restart journald. Wait for approval.

- [ ] **Step 5: Activate and validate the live paper service**

Invoke `Deploy-OciPaper.ps1 -Mode Activate` for the exact commit. Confirm `systemctl is-enabled`, `systemctl is-active`, a single MainPID, exact paper-only command line, successful `paper-status`, new journal entries, persistent state paths, backup manifest/hashes, no new listening port, unchanged legacy metadata, and no private Upbit request or credential access.

- [ ] **Step 6: Run the controlled service-restart test**

Execute `verify-service.sh restart` exactly once. Confirm non-decreasing event/sequence/candle state, exact same state when no new candle matured, no duplicate order evidence, pre/post journal availability, same release target, and service active. Save and report the root-only verification evidence path.

- [ ] **Step 7: Hand off operation and remaining user-only choices**

Give the user the runbook links and one-at-a-time commands for status and logs. State that VM reboot was not performed and OCI Boot Volume/external backup was not created. Explain that each requires a separate choice because reboot interrupts SSH and external backup can affect quota/cost. Do not recommend artificial workload to avoid Always Free idle reclamation.
