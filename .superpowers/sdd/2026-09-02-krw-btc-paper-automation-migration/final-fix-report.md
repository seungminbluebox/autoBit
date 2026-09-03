# KRW-BTC Paper Automation Final-Fix Report

Date: 2026-09-04

Branch: `codex/krw-btc-rebuild`

Review base: `ae8d269c1cec68ef209e7983f0f2a9310900df57`

Implementation commit: `bdc964b` (`fix: close final paper parity and recovery gaps`)

Result: **DONE** — all three binding Important findings I1-I3 are fixed and the complete repository suite is green.

## Scope and boundaries

This was the single final-fix wave requested by the final reviewer. It preserves:

- public-candle and optional Telegram network access only; no private Upbit, account, balance, exchange-order, websocket, or live-trading surface;
- exactly seven CLI commands, in this order: `data-download`, `data-quality`, `backtest`, `walk-forward`, `paper-once`, `paper-run`, `paper-status`;
- the normalized paper account starting at exactly `100`;
- atomic SQLite mutations, immutable event evidence, full replay validation, deterministic identities, leases, and retry idempotency;
- one completed four-hour cycle per `paper-once` invocation, chronological backlog processing, and automatic health recovery;
- next-bar execution for ordinary market signals and next-bar effectiveness for completed-candle trailing-stop updates.

No private or live exchange API was accessed during development or verification.

## I1 — initial-stop parity

### Root cause

`PaperBroker.set_stop` previously forced every stop to become active at the next four-hour boundary. `PaperService` did calculate the BUY-fill-derived `entry fill - 2.5 ATR` initial stop and call the intrabar stop path on the entry candle, but the broker rejected that stop as ineligible until the following candle. This contradicted the proven backtest lifecycle. Fill, first-stop persistence, and same-candle stop evaluation were also separate transactions, leaving a crash window in which a durable entry could be temporarily unprotected.

### Fix

- A strictly identified first `HARD_STOP` for the currently open BUY entry may become effective at that BUY fill's exact four-hour boundary.
- Replay requires the source BUY fill to occur first in immutable event sequence, at the same timestamp, and rejects same-boundary activation for trailing/replacement stops or non-current sources.
- Trailing/default stop updates still become effective only at the next four-hour boundary.
- Pending-open fill, automatic partial-BUY remainder cancellation, initial-stop persistence, and same-candle open/low stop evaluation now share one outer store transaction. A failure between fill and stop rolls the entire group back.
- A partial BUY's remainder is terminally canceled before stop evaluation; the stop exit is sized from reconciled owned BTC only. This prevents oversell, duplicate remainder work, or later remainder reopening.
- Same-timestamp entry and stop fills are ordered by immutable event sequence rather than hash-derived fill ID, so replay and completed-trade reconstruction preserve causal BUY-then-SELL order.
- Gap behavior remains conservative: when the entry candle opens through the fill-derived stop, the stop reference uses the worse open; otherwise a low breach uses the stop price. Bound fee and slippage rates are applied to both fills.

### Permanent regressions

- exact entry-candle fill/stop order, fill, fee, slippage, equity, and completed-trade ledgers;
- half-size partial BUY, exact remainder cancellation, exact owned-quantity stop exit, replay, and retry idempotency;
- crash after pending BUY fill but before first-stop persistence, including close/reopen retry;
- fill-price-derived gap-down stop behavior;
- direct broker rejection of forged same-boundary stop activation;
- real SQLite paper/core parity on an entry-candle hard-stop breach.

### RED / GREEN evidence

Initial RED node set:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/integration/test_paper_service.py::test_fill_derived_initial_stop_protects_the_entry_bar_with_complete_cost_ledgers `
  tests/integration/test_paper_service.py::test_partial_entry_remainder_is_canceled_before_same_bar_initial_stop_exit `
  tests/integration/test_paper_service.py::test_crash_between_entry_fill_and_initial_stop_rolls_back_the_fill `
  -p no:cacheprovider --basetemp .final-fix-red.tmp/i1-red
```

Result: `3 failed in 2.97s`. Both stop cases remained long; the crash boundary did not raise because fill and stop were not yet one protected transaction.

Focused GREEN after production changes: `6 passed in 3.27s`. The strengthened core/paper parity fixture initially exposed hash ordering of same-time fills; after switching to event-sequence ordering, the parity pair passed: `2 passed in 5.27s`.

## I2 — truthful `paper-status` MTM

### Root cause

`paper-status` reported `PaperReconciliation.equity`. Broker reconciliation values an open position at its last fill, so an otherwise valid completed cycle with a changed close but no new fill still reported the stale fill-marked value. The output had no explicit equity time or machine-readable provenance, making that stale value look current.

### Fix

`_status_equity` now selects the latest validated completed-close projection only when all binding evidence agrees:

- the latest completed cycle is uniquely validated and has `PROCESSED` status;
- the canonical base risk cursor equals `latest cycle end - 4h`;
- its immutable risk event precedes the terminal cycle event;
- the validated risk projection and reconciled broker state are arithmetically possible (flat equity must exactly match cash within the established absolute tolerance; long inventory must imply a positive finite mark).

When bound, `normalized_equity` is the completed-close projection, `equity_as_of_utc` is the canonical exclusive completed-cycle end, `equity_status` is `CURRENT`, and `equity_provenance` is `COMPLETED_CLOSE_MTM`.

When current mark evidence is absent, the command does not invent one:

- a last-fill broker fallback is `STALE` / `LAST_FILL_BROKER_EQUITY`, with the latest fill time;
- a non-empty ledger with no fill/mark is `STALE` / `INITIAL_EQUITY`, with null `equity_as_of_utc`;
- a genuinely fresh ledger is `UNAVAILABLE` / `INITIAL_EQUITY`, with null `equity_as_of_utc`.

The command remains read-only and source-preserving, including when reading a committed active WAL snapshot. Risk/broker contradictions exit safely without modifying main DB, WAL, or sidecars.

### Permanent regressions

- fresh-ledger unavailable initial equity;
- flat completed-close current MTM;
- open-position, no-fill close-price change read from active WAL (`100` last-fill broker value versus `94` current projection);
- latest processed cycle without valid current mark evidence falls back as stale;
- forged flat risk equity contradicting broker cash fails safely;
- existing active-WAL pending/long/stop/recovery status retains stale last-fill provenance and time.

### RED / GREEN evidence

Initial RED node set:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/integration/test_paper_cli.py::test_paper_status_is_byte_and_metadata_read_only_with_stable_json `
  tests/integration/test_paper_cli.py::test_paper_status_reports_flat_completed_close_mtm_provenance `
  tests/integration/test_paper_cli.py::test_paper_status_uses_active_wal_completed_close_mtm_after_no_fill_price_change `
  tests/integration/test_paper_cli.py::test_paper_status_labels_latest_cycle_without_mark_as_stale_fallback `
  tests/integration/test_paper_cli.py::test_paper_status_rejects_completed_close_risk_that_contradicts_flat_broker `
  -p no:cacheprovider --basetemp .final-fix-red.tmp/i2-red
```

Result: `5 failed in 5.50s`: provenance/time fields were missing, the open no-fill case reported `100` instead of `94`, and contradictory risk evidence did not fail.

Focused GREEN: `5 passed in 4.16s`.

## I3 — durable first-cycle retry cursor

### Root cause

Before the first terminal cycle existed, the service derived backlog only from completed cycles and trading/risk obligations. The application prepared/fetched public data before `PaperService.process_completed_candle` could durably bind the requested end. If that first request failed and the process restarted after a four-hour rollover, health evidence proved a failure but did not contain a validated target; the resolver therefore selected the newer matured boundary and abandoned the earlier candle.

### Fix and schema choice

A dedicated immutable evidence event was added rather than deriving authority from free-form health/event IDs:

```text
event_type = PAPER_CYCLE_ATTEMPT
event_id   = paper-cycle-attempt:<canonical target Z>
payload    = {"end_utc": <canonical target Z>, "version": 1}
envelope   = target - 4h
```

The event is written after lease acquisition and before public candle preparation/acquisition. Validation requires an aware UTC canonical `Z` target on an exact four-hour boundary, a matching deterministic ID and envelope, known exact payload keys/version/types, target maturity relative to the current validated clock boundary, uniqueness, and strict event-sequence precedence before any matching terminal `PAPER_CYCLE`. At most one attempt may be unfinished, and after an existing completed chain it must be the immediate chronological successor.

`oldest_required_end` includes attempted-but-uncompleted targets and therefore retries the earliest durable obligation after restart/rollover. A valid terminal cycle logically clears that attempt and advances the normal chain. The requested end cannot skip an unfinished durable attempt. Duplicate retry writes are idempotent, lease behavior is unchanged, each application call still processes exactly one target, and exceptions return to the scheduler's existing bounded retry/health path without a busy loop.

The eager application wrapper was removed: public source preparation now occurs inside the source load invoked only after the attempt event is durable. Preflight uses the same strict service resolver, so malformed attempt, health, risk, cycle, order, fill, or stop evidence fails before network access.

### Compatibility

- SQLite schema/version is unchanged. `PAPER_CYCLE_ATTEMPT` is immutable event evidence and is intentionally ignored by the generic state projection reducer while remaining covered by snapshot/event digest validation.
- Existing valid ledgers without attempt events remain accepted and use their established completed/risk/order/fill/stop chronology.
- Once an attempt event exists, its stricter versioned contract is mandatory; forged IDs, extra/missing fields, noncanonical offsets, malformed/future targets, duplicates, wrong event types using the reserved prefix, and impossible chronology fail closed.
- Legacy health event IDs are never interpreted as target authority.

### Permanent regressions

- application-level first API failure at `08:00`, store close/process restart after `12:00`, then exact `08:00` completion before `12:00`, with one order/fill and no duplicate attempts or cycles;
- forged ID, noncanonical timestamp, future target, and reserved-prefix wrong-type rejection before source access or mutation;
- crash immediately after durable attempt and before source access, then later restart retry of the original target exactly once;
- existing source-failure behavior updated to require one durable attempt, no terminal cycle, and released lease.

### RED / GREEN evidence

Initial RED node set:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/integration/test_paper_cli.py::test_first_api_failure_survives_rollover_and_restart_before_newer_cycle `
  tests/integration/test_paper_cli.py::test_application_rejects_forged_malformed_or_future_attempt_before_source `
  tests/integration/test_paper_service.py::test_crash_after_cycle_attempt_retries_original_target_without_source_access `
  -p no:cacheprovider --basetemp .final-fix-red.tmp/i3-red
```

Result: `6 failed in 4.88s` (the parameterized malformed-evidence node contributes four cases): no durable attempt existed, malformed attempts only fell through to a generic missing-cursor failure, and the new crash boundary was never reached.

Focused GREEN: `6 passed in 4.13s`.

## Focused and broad regression evidence

All commands used the worktree-local Python environment, a worktree-local `--basetemp`, and `-p no:cacheprovider` to avoid host Temp/cache ACL behavior.

- Paper service plus CLI/status: `92 passed in 15.90s`.
- Broker/store/health unit group: `255 passed in 16.29s`; the subsequently added direct fill-boundary broker node also passed, and is included in the final full run.
- Restart plus automatic recovery: `40 passed in 8.68s`.
- Acceptance, backtest execution, paper parity, and no-live safety group: `90 passed in 43.82s`.
- Strengthened paper/core parity pair: `2 passed in 5.27s`.
- Additional strict hardening nodes: `2 passed`.

Representative broad commands:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_paper_service.py tests/integration/test_paper_cli.py -p no:cacheprovider --basetemp .final-fix-red.tmp/paper-service-cli
.\.venv\Scripts\python.exe -m pytest tests/unit/test_paper_broker.py tests/unit/test_sqlite_store.py tests/unit/test_paper_health.py -p no:cacheprovider --basetemp .final-fix-red.tmp/paper-ledger-unit
.\.venv\Scripts\python.exe -m pytest tests/integration/test_paper_restart.py tests/integration/test_paper_auto_recovery.py -p no:cacheprovider --basetemp .final-fix-red.tmp/paper-restart-recovery
```

## Full repository coverage suite

The final reviewer required one complete coverage run. It was started exactly once and allowed to finish through the long real walk-forward golden phase:

```powershell
$env:COVERAGE_FILE='.final-fix-red.tmp/full-coverage-data'
.\.venv\Scripts\python.exe -m pytest --cov=autobit --cov-report=term-missing -v -p no:cacheprovider --basetemp .final-fix-red.tmp/full-coverage
```

Exact result:

```text
exit code: 0
TOTAL: 8405 statements, 1014 missed, 88% coverage
1071 passed in 2289.48s (0:38:09)
```

The long `test_real_walk_forward_golden_is_exact_and_byte_stable` passed before the remaining safety and unit tests completed.

## Static, CLI, and boundary verification

- `python -m compileall -q src tests` — exit `0`.
- Root help and all seven `<command> --help` invocations — exit `0` for every invocation.
- Parser command tuple — exactly `('data-download', 'data-quality', 'backtest', 'walk-forward', 'paper-once', 'paper-run', 'paper-status')`.
- Private/live scan:

  ```powershell
  rg -n 'buy_market_order|sell_market_order|create_order|place_order|get_accounts|get_balance|UPBIT_ACCESS_KEY|UPBIT_SECRET_KEY|create_upbit|pyupbit|ccxt|wss://|/v1/orders|/v1/accounts' src pyproject.toml
  ```

  Exit `1` with no matches, as expected.
- `git diff --check` before staging and `git diff --cached --check` after explicit staging — exit `0`.
- Branch and base checks — branch `codex/krw-btc-rebuild`; pre-commit `HEAD` and merge-base both exactly `ae8d269c1cec68ef209e7983f0f2a9310900df57`.

## Transaction/crash self-review

- The attempt is committed before source access, but deliberately outside the later trading transaction so source/API failure cannot erase the retry cursor.
- Lease acquisition precedes attempt creation; `finally` releases the exact lease holder on source failure, fault injection, unsafe data, or success. A crash leaves only the normal expiring durable lease and idempotent attempt.
- The entry fill, partial remainder terminal cancellation, initial stop, same-bar stop order/fill, and stop deactivation are nested inside one SQLite transaction. Every nested broker mutation uses the store's supported nested transaction model; an exception rolls back to the pre-fill state.
- After commit/restart, replay validates every order/fill/stop event, causal sequence, costs, exact inventory, and completed trade. Same-time fills are reconstructed by event sequence.
- Terminal `PAPER_CYCLE` remains separate and last. A crash before it retains a retryable attempt; deterministic order/risk/cycle identities and reconciliation prevent duplicate fills or cycles.
- Status opens the ledger read-only, validates the same cycle/attempt/risk/health/broker chains, and never checkpoints or rewrites an active WAL.

## Files changed

- `src/autobit/paper/service.py`
- `src/autobit/execution/paper_broker.py`
- `src/autobit/cli.py`
- `tests/integration/test_paper_service.py`
- `tests/integration/test_paper_cli.py`
- `tests/unit/test_paper_broker.py`
- `tests/regression/test_paper_golden.py`
- `README.md`
- `docs/paper-trading-runbook.md`
- this evidence report

## Commits and concerns

- Implementation/tests/operator docs: `bdc964b` — `fix: close final paper parity and recovery gaps`.
- This report is intentionally committed as the immediately following evidence-only commit; its hash is recorded in the final handoff because a commit cannot self-identify its own hash.
- No unresolved implementation or verification concern remains.
- Pre-existing/generated untracked `.coverage`, `.test-tmp/`, `__pycache__/`, and inaccessible `.pytest_cache/` artifacts were preserved and excluded from both commits.
