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

## Fix round 1: reviewer findings I1–I4

### RED/GREEN sizing evidence

RED command:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/integration/test_paper_acceptance.py -v -p no:cacheprovider --basetemp .task7-fix-red.tmp
```

Result: expected causal failure — the captured real `PaperService.calculate_size` call supplied `risk_rate=0.02`, not the required reduced value. This proved that the old entry occurred before recovery.

The scenario now has no pre-outage entry. It closes and reopens the same ledger, records exactly three failed public requests, obtains three valid public responses, processes its single high-volatility breakout entry while health is `REDUCED`, fills it on the next completed candle, and triggers one later hard stop. The real service sizing capture asserts `risk_rate=0.005`, `exposure_cap=0.175`, and an ATR ratio greater than 2.0. The submitted and filled BUY quantities must equal the direct production sizer result built from those captured arguments; they must also be smaller than normal-health risk at the same high ATR and reduced-health risk at baseline ATR.

The permanent adversarial test replaces `autobit.paper.service.calculate_size` with a risk/ATR-agnostic constant `SizeDecision(0.5, ...)` and requires the causal sizing assertions to fail. It would fail closed if the scenario stopped routing the health-adjusted/volatility inputs to the actual service sizer. The harness also now compares the first closed ledger snapshot before restart and closes the final live store before independently reopening it for replay equality.

GREEN command:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/integration/test_paper_acceptance.py -v -p no:cacheprovider --basetemp .task7-fix-green.tmp
```

Result: `2 passed in 12.46s`.

### Runbook corrections

- API-outage text now says stop evidence remains durable but candle-dependent stop evaluation/filling waits for a valid completed public candle; `HALTED` then blocks entries without blocking protective processing.
- Recovery now requires every health reason to clear, expressly including a valid fill reconciliation within the configured 5% deviation, and directs operators to `breaker_health_reasons` and `health_recovery_progress`.
- Backup/restore treats only the main SQLite DB plus optional WAL as durable. Restore verification creates a new GUID-named directory, fails if it exists or is nonempty, restores only the selected DB/WAL pair, and runs `paper-status` on that candidate.

### Regression evidence

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/integration/test_paper_acceptance.py tests/integration/test_paper_auto_recovery.py tests/integration/test_paper_cli.py tests/integration/test_paper_service.py tests/safety/test_no_live_surface.py -v -p no:cacheprovider --basetemp .task7-fix-focused.tmp
```

Result: `186 passed in 39.65s`.

Final compile/CLI/safety/diff commands and the separate fix-commit hash are recorded after the final verification.

Final verification commands:

```powershell
& '.\.venv\Scripts\python.exe' -m compileall -q src tests
& '.\.venv\Scripts\python.exe' -m autobit.cli --help
& '.\.venv\Scripts\python.exe' -m autobit.cli paper-once --db reports/paper-smoke.sqlite3 --data-dir data/processed
& '.\.venv\Scripts\python.exe' -m autobit.cli paper-status --db reports/paper-smoke.sqlite3
rg -n "buy_market_order|sell_market_order|UPBIT_ACCESS_KEY|UPBIT_SECRET_KEY|create_upbit" -g "*.py" .
rg -n "FILL_DEVIATION|5%|breaker_health_reasons|health_recovery_progress|attempt-|shm" docs/paper-trading-runbook.md
git diff --check
```

Exact outcomes: compilation 0; CLI help 0 with the same seven commands; safe smoke `paper-once` 2 (`SAFE_OPERATION_ERROR`) and readable `paper-status` 0; private-surface scan 1 with no matches; required runbook content scan 0; and `git diff --check` 0.

Fix implementation commit: `27d031e33326710eadb9e2bcc80ac59bdd18eea9` (`test: prove reduced paper sizing in acceptance`).
