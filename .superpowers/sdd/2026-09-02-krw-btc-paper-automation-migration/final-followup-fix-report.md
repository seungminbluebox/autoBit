# Final follow-up fix report

Status: `DONE_WITH_CONCERNS` -- original findings addressed with 482 focused tests
passing; independent review found Important N1. The frozen full run was explicitly
aborted on controller/user authorization so a new owner can repair N1 and verify
the amended tree. This is **not** a full-suite pass or overall readiness claim.

Base: `e8aca3b15d16616c87e50d7888fb8ce29960969d`

Frozen implementation: `7f4afb8283b8504310e856b00783c5a2e30b6d65`.

## Scope and diagnosis

This wave fixes the four findings confirmed after Task 8, without adding live
trading, changing strategy parameters, or widening the normal paper ingress:

1. A rolling 601-bar frame eventually omitted the entry signal, entry ATR, and
   early held highs. Consequently an otherwise valid long position could no
   longer reconstruct its initial R/high-water before the approved 1,095-bar
   maximum.
2. A paper BUY was sized at the signal close and its next-open fill was guarded
   only by cash. A gap-up could therefore exceed the 70% exposure cap.
3. Paper deliberately suppressed the close update after an entry survived its
   entry-candle hard stop. Core attempted the update, but Backtrader could not
   cancel a still-submitted initial stop, so the replacement was rejected by the
   duplicate-exit guard.
4. the no-live-surface test fixture deleted a pre-existing `.test-tmp` parent,
   rather than only paths the fixture owned.

## Approved design and compatibility

No database table/schema migration was introduced. New state is represented by
strictly versioned immutable events, so the generic SQLite store, its snapshot
format, and all valid existing event shapes remain compatible.

- `PAPER_ENTRY_CONTEXT` is a one-to-one deterministic binding between a service
  BUY and its same-signal public price/ATR, sizing parameters, and pre-order
  `BREAKER_STATE` risk event. It occurs after order metadata and before any
  execution/fill. Generic direct broker BUYs retain their legacy shape. A legacy
  pending service BUY is recognized only from causal same-signal risk ancestry;
  its original 601-bar public signal window is reloaded and validated before
  binding, so it cannot silently become an uncapped service order.
- `PAPER_EXECUTION` precedes every terminal outcome for a context-bound BUY and
  records the canonical eligible-open time, actual public reference open, and
  attempted quantity. Replay recomputes cash, fill cost, risk sizing, and
  exposure at that actual open. The result is bounded by both attempted and
  accepted quantities. A zero cap produces an exact no-fill `EXECUTION_CAP`
  rejection; a cap reduction produces an exact partial fill and terminal reason.
- `PAPER_POSITION` forms a deterministic per-entry chain over exact contiguous
  held-candle highs, the original BUY fill, the prior observation, immutable
  initial stop/R, and computed high-water. New positions build this evidence
  incrementally. Valid legacy open positions automatically request only their
  missing held interval, bounded to 1,096 completed candles and their actual BUY
  ancestry. The normal rolling request remains exactly 601 bars. Historical
  cache identity includes both start and end, avoiding collision with the old
  end-only 601-bar cache. Normal cache integrity and chronology validation are
  reused. An unavailable/unprovable public interval leaves the original hard
  stop and position intact, records health failure, and retries automatically;
  no ATR/high-water is fabricated and no manual approval is added.
- Every new event is validated during ordinary broker reconstruction before a
  subsequent mutation. Cardinality, deterministic identity, exact version,
  causal sequence, risk/fill/order ancestry, prices/quantities, observation
  continuity, and terminal outcomes are checked. Append/fill paths use the
  existing outer SQLite transactions and replay idempotently after restart.
- Paper now checks the entry-candle initial stop first, then observes that
  surviving candle and may publish the close-derived trailing stop for the next
  candle. Core's broker override retires a stop from Backtrader's submitted queue
  before replacement, preserving the shared duplicate-exit invariant.
- The safety fixture records which directories it created and removes only
  empty, fixture-owned parents. Pre-existing empty and non-empty parents survive.

The age-1,095 integration fixture bulk-seeds test-owned, immutable `PAPER_CYCLE`
cursor history and regenerates generic prefix snapshots with the real store
rebuild. The reopened store/application then runs every normal snapshot, replay,
chronology, and paper-domain validator. This establishes a reachable old state;
it is explicitly not evidence that the fixture executed 1,095 application
strategy cycles.

## Strict TDD evidence

Initial defect REDs were observed before their production fixes:

- Entry-close/replacement: selector `entry_close_trailing or
  generated_probe_cleanup` produced `6 failed, 124 deselected` in 11.24s. The
  failures were the missing paper next-bar trailing stop, no core trade after
  attempted replacement, and deletion of pre-existing fixture parents.
- Actual open sizing: `application_actual_open` produced `2 failed, 4 passed,
  58 deselected` in 11.85s; both gap cases filled the signal-close amount rather
  than the actual-open 70% cap.
- Long hold: ages 586, 588, 600, 1094, and 1095 produced `5 failed, 49
  deselected` in 6.13s. (The age-600 expectation was corrected: its inclusive
  fill-to-current interval is still exactly 601 bars; an explicit age-601 case
  covers the first wider backfill.)
- Range cache: the public backfill selector produced `1 failed, 64 deselected`
  in 3.27s because no bounded historical loader existed.
- Execution evidence hardening produced `7 failed` in 6.63s before implementation:
  missing execution evidence/orphan acceptance and legacy context chronology were
  the reproduced causes.
- The initial adversarial fixture wrote non-canonical tampered payloads, then an
  attempt to append 1,095 cycles through a full-snapshot-per-append loop was
  explicitly aborted as test setup inefficiency. Its durable directory is
  `autobit-final-followup-adversarial-b-a7c7c4ada7e1459bb63de4eb25d65ccd`,
  exit `-1`; exact owned worker PID 7644 was checked before it was stopped. The
  fixture was replaced by the approved bulk-seeded reachable-state setup above,
  without mocking production validation.

Focused GREEN progression:

- trailing/fixture: `6 passed, 124 deselected` (12.36s)
- actual-open plus entry trailing: `10 passed, 54 deselected` (25.35s)
- public range, long-hold boundary, actual-open, trailing: `17 passed, 103
  deselected` (41.79s)
- execution evidence: `7 passed` (5.59s), durable directory
  `autobit-final-followup-green-execution-6cfc85d6c1c6478ca0469df630826285`
- paper follow-up/acceptance/service and native execution/cost/partial/risk-time:
  `106 passed` (77.78s), durable directory
  `autobit-final-followup-focused-b-173179dee1ec4f3e9a0a3f73142d04ea`
- adversarial/restart suite: `21 passed` (71.23s), durable directory
  `autobit-final-followup-adversarial-d-75a6e80187c04a1e85e80562bd395f56`.
  It covers real application age-1,095 outage/retry/next-open exit, actual-open
  normal/reduced/partial/zero-cap outcomes, transaction rollback, old high-water
  restart, and eleven context/execution/position corruptions blocking mutation.
- A final real-application legacy pending-entry health hypothesis was checked and
  already passed (`1 passed, 20 deselected`, 3.92s), so no speculative production
  change was made. Directory:
  `autobit-final-followup-red-legacy-health-3b01be083a604564b2f45fb24add6456`.

The earlier broad focused run before execution-event hardening reported `417
passed, 3 failed` (143.75s); the three failures were old expectations encoding
the confirmed bugs. Their replacement expectations are independently derived:

- entry open 90, entry ATR 2 -> initial stop 85 at the fill boundary;
  entry high 101 is above +2R (100), so close trailing becomes
  `101 - 3 * (38 / 14) = 92.85714285714286` for the next candle (close ATR38/14);
- acceptance actual slipped open100.05, entry ATR5, risk budget0.5, and
  baseline ATR%=.02 yield loss/BTC `12.5 + .0005*(100.05+87.55)=12.5938`
  and volatility multiplier `.02/(5/100.05)=.4002`. Therefore filled quantity is
  `0.5 * 0.4002 / 12.5938 = 0.015888770664930364`, below the old signal-close
  quantity `0.01947312152224063`;
- the constant-quantity mutation is now rejected at causal signal sizing, before
  the later acceptance total, which proves the cap/reduction path participates
  in the result rather than merely changing an expected literal.

Durable broad-final focused run: `482 passed in 9763.07s (2:42:43)`, exit0.
Directory: `autobit-final-followup-focused-final-b-7fcacb38b133477badce871da7158c44`.
Started `2026-09-04T07:15:30.0457541Z`; finished
`2026-09-04T09:58:14.3528177Z`. The complete selection was:

```text
tests/unit/test_paper_broker.py tests/unit/test_sqlite_store.py
tests/integration/test_paper_service.py tests/integration/test_paper_cli.py
tests/integration/test_paper_restart.py tests/integration/test_paper_auto_recovery.py
tests/integration/test_paper_acceptance.py tests/integration/test_paper_followup.py
tests/safety/test_no_live_surface.py tests/regression/test_paper_golden.py
tests/regression/test_core_golden.py tests/integration/test_backtest_execution.py
tests/integration/test_backtest_costs.py tests/integration/test_backtest_partial_fill.py
tests/integration/test_backtest_risk_time.py
```

All durable directories named above are under
`C:/Users/boxma/AppData/Local/Temp/`; their command, stdout/stderr, start/finish,
HEAD and exit records are retained. A preceding final-focus invocation had two
incorrect test path names and collected no tests (exit4); its evidence remains
at `autobit-final-followup-focused-final-4e527e403d054d9cbdee884cddc9ab14`.

The 2h42 focused wall time was investigated before launching full coverage.
Fixture creation timestamps located a 9,615.288s gap between
`test_application_legacy_max_ho0` and the next test. Read-only Windows System
`Microsoft-Windows-Kernel-Power` records then established modern standby entry
(ID506, reason Lid) at `2026-09-04T07:17:56.6963657Z`, and exit (ID507, reason
Lid) at `2026-09-04T09:57:18.5719328Z`, with an intermediate austerity transition
at09:35:07. This host standby interval accounts for approximately the entire long
gap; it is not evidence of 2h40 of CPU processing. No blanket rerun was made.
The sole full command adds `--durations=15` for retained timing diagnostics.

## Required final verification

Repository-wide command (exact frozen implementation HEAD):

```text
.venv/Scripts/python.exe -B -m pytest -p no:cacheprovider --basetemp <unique> -q --cov=autobit --cov-report=term-missing --durations=15
```

Full result: **ABORTED, NOT PASSING**. Started `2026-09-04T10:01:13.8746225Z` on exact HEAD
`7f4afb8283b8504310e856b00783c5a2e30b6d65`. Evidence directory:
`C:/Users/boxma/AppData/Local/Temp/autobit-final-followup-full-85887b2b806f4647a8ca23b1dd828f1f`.
Exact command-line identity was checked: venv launcher PID30496, runtime worker
PID25128 (parent30496), shell parent29052. There was one full run, tool session77767.
The directory retains `head.txt`, empty `working-diff-stat.txt`,
`started.txt`, `finished.txt`, `command.txt`, complete `stdout.log`, complete
`stderr.log`, isolated `.coverage`, and `exit-code.txt`.

At `2026-09-04T10:15:40.7624094Z`, after the user approved N1 follow-up, the
controller explicitly instructed this worker to abort the superseded frozen run.
Immediately before termination, Win32 process identity was rechecked: PID25128,
parent30496, python.exe, exact task-specific full basetemp, and `--cov=autobit`.
Only PID25128 was stopped. Launcher30496 and shell29052 then completed, and a
read-only process query confirmed all three gone. The wrapper saved exit **-1**
and finish `2026-09-04T10:15:41.0035772Z`; tool session77767 closed with exit1.
Retained stdout is492 bytes (dots through38%, plus6), stderr0 bytes. There is no
pytest completion footer, coverage summary, or pass claim. No duplicate full run
was launched and no unrelated process was touched. All partial artifacts remain.

Post-full compile, exact-seven-help/public-only safety: **NOT RUN**. The controller
explicitly waived redundant post-full static checks on the superseded frozen
source when authorizing this abort. `verify-final-followup-post.ps1` remains as an
unexecuted evidence helper; a new owner must not treat it as passing verification.
Read-only `git diff --exit-code 7f4afb8... -- src tests docs` after termination exited0,
confirming no frozen production/test/runbook change. Earlier broad focused safety,
core/paper goldens and native execution selections are the completed evidence.

## Intended changed files

- `src/autobit/execution/paper_broker.py`
- `src/autobit/execution/backtest_broker.py`
- `src/autobit/paper/service.py`
- `src/autobit/cli.py`
- `tests/integration/test_paper_followup.py`
- `tests/integration/test_paper_service.py`
- `tests/integration/test_paper_cli.py`
- `tests/integration/test_paper_acceptance.py`
- `tests/safety/test_no_live_surface.py`
- `docs/paper-trading-runbook.md`
- this report and verification helpers (the post-full helper was not executed)

## Independent scoped review and residual concern

The controller's independent `final-followup-rereview-report.md` confirms original
I1, I2, both I3 repair sites, and M1 addressed. It identifies a new Important N1:
the legacy pending-entry signal-window lookup occurs inside the fill transaction,
so an observed API/schema failure is persisted only transiently and then erased
when the exception rolls that transaction back. Execution remains fail-closed,
but durable health can incorrectly show the earlier normal-frame success; an
operational notification could outlive its rolled-back evidence.

The reviewer retained distinct-time API/schema traces under
`C:/Users/boxma/AppData/Local/Temp/autobit-rereview-legacy-rollback-5s36foxf`.
The transaction boundary is confirmed in frozen service lines419/430/605 and the
observed source's failure path. The successful legacy recovery regression does
not cover this failure case. The controller instructed this worker not to edit
frozen source/tests. After the user approved N1 follow-up, the controller explicitly
superseded the frozen full run, authorized its exact-worker abort, and assigned a
new owner to the bounded residual repair and one final full verification on amended
code. Source freeze can now be released; no process from this run remains.

Therefore this report is **not** a merge, observation, or overall-completion
approval even if the full suite passes. Remaining known concern: **N1 Important**.
No live launch or two-week/30-trade paper-observation claim is made.
