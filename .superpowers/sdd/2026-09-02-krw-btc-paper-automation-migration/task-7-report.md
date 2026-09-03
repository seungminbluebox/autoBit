# Task 7 Report: Full Paper Acceptance and Fault-Injection Runbook

## Scope and files

- Added `tests/integration/test_paper_acceptance.py` and `tests/fixtures/paper_acceptance.json`.
- Added `docs/paper-trading-runbook.md`.
- Updated `README.md` with a runbook link and the paper-only observation boundary.
- No production source changed. The existing `_PaperApplication`, `PaperService`, `PaperBroker`, `HealthMonitor`, `SQLiteStore`, and replay behavior already supplied the real integration seam; the deterministic public-candle source and harness remain test-only.

## Deterministic acceptance evidence

The fixture keeps normalized initial equity at exactly `100.0`. The test drives the real operational application with a deterministic synthetic public-candle source and asserts:

- one BUY entry; a close/reopen of the same SQLite ledger before the entry fill;
- one three-failure public API outage, with only `API_FAILURES` as the halted reason;
- automatic `HALTED -> REDUCED -> NORMAL` recovery without a resume command;
- reduced health risk and a >2x ATR ratio which makes `calculate_size` bind on the real volatility constraint and produce a smaller quantity;
- exactly one `HARD_STOP`, final `FLAT`, no duplicate order IDs, no negative-cash fill state, and no oversell;
- exact equality between the live SQLite replay and a separately reopened replay.

## RED/GREEN record

RED command:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/integration/test_paper_acceptance.py -v
```

Result: expected RED — `fixture 'app_harness' not found`, confirming the requested acceptance harness/fixture was absent before implementation.

GREEN command (an ignored local basetemp was needed because the host denies access to pytest's default Temp directory):

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/integration/test_paper_acceptance.py -v --basetemp .pytest-acceptance.tmp
```

Result: `1 passed` (the only warning was the host's pre-existing `.pytest_cache` permission warning).

## Regression and safety evidence

Focused paper/restart/health/SQLite/safety coverage was run with an isolated ignored basetemp. It collected 443 tests and showed only passes throughout the captured output. The full coverage command below then completed successfully: **1056 passed in 47m11s**, with 88% total coverage. Its sole warning was the host's pre-existing permission denial when pytest attempted to write its default `.pytest_cache` path.

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/integration/test_paper_service.py tests/integration/test_paper_restart.py tests/integration/test_paper_auto_recovery.py tests/integration/test_paper_cli.py tests/unit/test_paper_broker.py tests/unit/test_paper_health.py tests/unit/test_sqlite_store.py tests/safety/test_no_live_surface.py -v --basetemp .pytest-paper.tmp

& '.\.venv\Scripts\python.exe' -m pytest --cov=autobit --cov-report=term-missing -q --basetemp .pytest-verify.tmp
```

The test body imports only existing paper-only components; it adds neither an authenticated endpoint nor an exchange adapter. The accepted seven CLI commands and their parser options are unchanged.

CLI smoke and static safety evidence:

```powershell
& '.\.venv\Scripts\python.exe' -m autobit.cli --help
& '.\.venv\Scripts\python.exe' -m autobit.cli paper-once --db reports/paper-smoke.sqlite3 --data-dir data/processed
& '.\.venv\Scripts\python.exe' -m autobit.cli paper-status --db reports/paper-smoke.sqlite3
rg -n "buy_market_order|sell_market_order|UPBIT_ACCESS_KEY|UPBIT_SECRET_KEY|create_upbit" -g "*.py" .
& '.\.venv\Scripts\python.exe' -m compileall -q src tests
git diff --check
```

Results: CLI help exited 0 and listed exactly the seven approved commands. The deliberately incomplete local public-data path made `paper-once` exit safely with `SAFE_OPERATION_ERROR` (exit 2); `paper-status` then exited 0 with readable normalized `FLAT` state, `100.0` cash/equity, and a safely halted schema reason. The forbidden-token scan returned no matches (rg exit 1), compilation exited 0, and `git diff --check` exited 0.

## Runbook review

The runbook contains parser-matching PowerShell commands for start, status, Ctrl+C stop, stopped-only DB backup, WAL/SHM companion copying and hash/status verification, isolated restore verification, corruption detection, API outage and stale candle behavior, automatic recovery conditions, evidence locations, and the two-week plus 30-trade paper observation gate. It states repeatedly that PASS allows continued paper evaluation only and can neither enable live trading nor recommend buying.

## Self-review and concerns

- The fixture/harness stays in the test because its source sequencing is acceptance-only; there was no missing production seam to justify an application change.
- The host denies access to the default pytest Temp and `.pytest_cache` locations. All meaningful test reruns use explicitly named ignored basetemp directories; this is an environment warning, not a test failure.
- Full coverage, focused paper tests, acceptance, CLI help/smoke/status, no-private-token scan, compilation, and diff check are complete. Git status still contains pre-existing/generated `__pycache__` directories and a local `.test-tmp` directory created before ignored basetemp names were used; none are staged or part of this task commit.

## Commit

Implementation commit: `7ab96cddfc060119727ada183375181a6b97f7fb` (`test: verify automated paper recovery end to end`).
