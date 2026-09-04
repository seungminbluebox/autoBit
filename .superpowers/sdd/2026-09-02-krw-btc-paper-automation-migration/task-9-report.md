# Task 9: durable legacy public-history failure recovery

Date: 2026-09-04

Source review base: `0f5025a85f7b866e67ef70bb8096a527ba92b5d7`
(report-only; source/test contents match `7f4afb8283b8504310e856b00783c5a2e30b6d65`).

## Root cause and minimum design

`PaperService.process_completed_candle` entered its outer trading transaction
before `_legacy_entry_context` asked `_ObservedCandleSource` for the original
signal window. The observed source correctly persisted `HEALTH_STATE` and,
when configured, an `ALERT_ATTEMPT` before re-raising. The enclosing fill
rollback then erased both events.

The repair resolves/validates each eligible unbound legacy BUY context before
the outer fill mutation. It preserves the prior post-current-frame lease
renewal, and adds a second renewal plus completed-cycle recheck after the
potentially slow historical lookup. Immediately inside the existing atomic
boundary it revalidates health/risk and alert chains, current cycle completion,
and the prepared active BUY identity before binding/executing. The broker's
existing immutable context validation rechecks same-signal risk ancestry.
Generic direct-broker BUYs are explicitly prepared as `None` and retain their
accepted generic execution contract. No schema/event format, retry policy,
or live/private surface changed.

## TDD evidence

Before the production edit, the real `_PaperApplication`, `SQLiteStore`,
`PaperBroker`, `HealthMonitor`, clock/sleeper and a valid legacy pending
service BUY reproduced the rollback with distinct observations. The initial
API RED command was:

```powershell
.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider --basetemp
C:\Users\boxma\AppData\Local\Temp\autobit-task9-red-94e3f67ce39d44a68cf9c92aaeeee2ce
tests/integration/test_paper_followup.py -k
legacy_pending_history_api_failure_survives_return_and_reopen -q
```

It failed as expected: durable `health.api_failures` was `0`, not `1`; the
retained latest state was the normal current-window success. The legacy
lookup clock was deliberately later than the successful normal-frame
observation, excluding timestamp deduplication as the cause.

This RED is transcribed from the original tool-captured stdout (there is no
separate retained stdout/exit directory):

```text
FAILED ... test_legacy_pending_history_api_failure_survives_return_and_reopen
AssertionError: assert 0 == 1
... HealthSnapshot(... api_failures=0, api_successes=1, ...)
1 failed, 21 deselected in 6.25s
```

After the minimum production edit, the same test passed. Additional real-stack
coverage verifies: malformed historical data remains `HALTED/schema_valid=False`
through close/reopen; failed lookup leaves order/context/execution/fill
unchanged; alert sources and attempts survive delivery/reopen and a repeated
same-event notification does not redeliver; valid history recovery binds the
accepted order and makes exactly one 70% actual-open-capped fill with replay
equality; and a source-driven six-minute delay lets a concrete competing lease
take over, after which post-lookup renewal returns `LEASE_HELD` with no mutation.

Focused GREEN:

```powershell
.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider --basetemp <unique-system-temp>
tests/integration/test_paper_followup.py -k
'legacy_pending_history_api_failure_survives_return_and_reopen or legacy_pending_history_schema_failure_halts_durably or legacy_pending_history_retry_binds_one_capped_fill_after_reopen or legacy_history_failure_alert_has_durable_exactly_once_lineage or slow_legacy_context_lookup_revalidates_lost_cycle_lease or legacy_pending_service_entry_is_bound_before_actual_open' -q
```

Result: `6 passed, 20 deselected in 11.39s`, exit `0`.
This is transcribed from the original tool-captured stdout, whose final lines
were `...... [100%]`, `6 passed, 20 deselected in 11.39s`, and
`PYTEST_EXIT=0`; no separate durable output directory was created for that
short focused run.

## Broader verification

The frozen amendment selection covered paper service/CLI/followup/recovery/
acceptance, broker/store/health/notifier, paper/core goldens and safety:

```powershell
.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider --basetemp
C:\Users\boxma\AppData\Local\Temp\autobit-task9-broad-a0a86fcafde84c3ab26ef0ec7877e715\pytest-tmp
tests/unit/test_paper_broker.py tests/unit/test_paper_health.py
tests/unit/test_sqlite_store.py tests/unit/test_notifier.py
tests/integration/test_paper_service.py tests/integration/test_paper_cli.py
tests/integration/test_paper_auto_recovery.py tests/integration/test_paper_acceptance.py
tests/integration/test_paper_followup.py tests/regression/test_paper_golden.py
tests/regression/test_core_golden.py tests/safety/test_no_live_surface.py -q
```

Result: `525 passed in 294.69s (0:04:54)`, exit `0`, empty stderr. Durable
command/head/start/stdout/stderr/exit/finish artifacts are in
`C:\Users\boxma\AppData\Local\Temp\autobit-task9-broad-a0a86fcafde84c3ab26ef0ec7877e715`.

Required separately after controller identified the omission from the broad
selector:

```powershell
.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider --basetemp <unique-system-temp>
tests/integration/test_paper_restart.py -q
```

Result: `4 passed in 0.25s`, exit `0`.
This is likewise transcribed from original tool-captured stdout:
`.... [100%]`, `4 passed in 0.25s`, `PYTEST_EXIT=0`; no separate durable
output directory was created for this short restart-only run.

## Complete frozen-source verification and M1 test-only follow-up

The sole complete repository coverage run was subsequently completed on the
frozen production commit `211a04698538dfcba2057aef9c371bebbc13680c`:
`1129 passed in 3887.08s (1:04:47)`, exit `0`, coverage `88%`, and empty
stderr. Its original command, head, stdout, stderr, exit and related
artifacts are retained in
`C:\Users\boxma\AppData\Local\Temp\autobit-task9-full-cc4dd7d520c14190af5e36ba0c1e4357`.

After that full result, the reviewer-recorded M1 acceptance gap was closed
with a test-only extension of
`test_legacy_pending_history_api_failure_survives_return_and_reopen`. The
same real application/store test now creates two distinct historical public
API failures with a successful current-window observation before each failed
history lookup, reopening SQLite between attempts. It asserts both retained
failure events, increasing durable `last_failure_at_utc` evidence and event
sequence, `api_failures == 1` after the second failure (success resets the
consecutive counter; no forced latch), and unchanged pending order, context,
execution and fills after each failure. The historical event's
`occurred_at_utc` deliberately remains its canonical requested signal-window
time; distinct observation time is instead proved by its durable payload and
unique event sequence/identity.

Fresh post-full explicit-node focused check (not a global `-k` selection):

```powershell
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider --basetemp "C:\Users\boxma\AppData\Local\Temp\autobit-task9-m1-focused-final-66da2033c71644bab93b61179ae8bce2\tmp" -q tests/integration/test_paper_followup.py::test_legacy_pending_history_api_failure_survives_return_and_reopen tests/integration/test_paper_followup.py::test_legacy_pending_history_schema_failure_halts_durably tests/integration/test_paper_followup.py::test_legacy_pending_history_retry_binds_one_capped_fill_after_reopen tests/integration/test_paper_followup.py::test_legacy_history_failure_alert_has_durable_exactly_once_lineage tests/integration/test_paper_followup.py::test_slow_legacy_context_lookup_revalidates_lost_cycle_lease tests/integration/test_paper_followup.py::test_legacy_pending_service_entry_is_bound_before_actual_open
```

Result: `6 passed in 14.25s`, exit `0`, empty stderr; original command,
stdout, stderr, exit, head and production-diff artifacts are retained in
`C:\Users\boxma\AppData\Local\Temp\autobit-task9-m1-focused-final-66da2033c71644bab93b61179ae8bce2`.
The separately selected whole restart file passed `4 passed in 0.37s`, exit
`0`, empty stderr, with the same artifact kinds in
`C:\Users\boxma\AppData\Local\Temp\autobit-task9-m1-restart-final-4562330d6f594262bfee126b88c3d963`.

Post-full static/interface/safety artifacts are in
`C:\Users\boxma\AppData\Local\Temp\autobit-task9-m1-static-final-a7453658f2a1495a9f279d0d8b6a299b`:
`compileall -q src tests`, root help and all seven documented CLI help
surfaces passed (each exit `0`); `tests/safety/test_no_live_surface.py` passed
`72` tests in `28.48s`; the exact private/live-token scan had no matches
(expected `rg` exit `1`); `git diff --check` and
`git diff 211a04698538dfcba2057aef9c371bebbc13680c -- src pyproject.toml`
were empty (both exit `0`). The focused/restart/static artifacts record the
production source hash and an empty production diff, proving this follow-up
does not alter production source or configuration.

Changed files: `src/autobit/paper/service.py`,
`tests/integration/test_paper_followup.py`, and this report. `git diff --check`
was clean before pinning and after the test-only extension. The original
production repair was independently reviewed as C0/I0/M1; the M1 test-only
delta now awaits its narrow independent re-review. The user-facing two-week/
30-trade paper observation remains pending and is not claimed here.
