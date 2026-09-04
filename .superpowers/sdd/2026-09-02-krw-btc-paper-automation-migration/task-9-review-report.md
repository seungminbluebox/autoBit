# Task 9 independent scoped review

## Spec compliance

- **N1: ADDRESSED.** The original-signal request now occurs at `src/autobit/paper/service.py:422`, before the trading transaction at `:448`. The called legacy loader at `:644` consequently allows `_ObservedCandleSource.prepare` to persist and notify API/schema outcomes (`src/autobit/cli.py:429-449`) outside the fill rollback. No exception suppression, fabricated context, nested commit, or service-to-generic fallback was introduced.
- **Production behavior: compliant with the bounded repair. Acceptance coverage: one Minor gap (M1 below).** The required repeated-failure scenario is not exercised by the added tests; this is not evidence that the implementation mishandles it.
- **Cannot verify globally from this scoped diff:** completion/order of the preceding plans, all legacy deletion gates, and all original strategy/data/safety requirements across the repository. The repair itself changes no strategy constants, market endpoint, credentials, initial equity, schema/event formats, retry architecture, or live/private surface. The complete repository coverage run and post-full gates remain separately pending; this report is not their substitute.

## Strengths

- The boundary correction is small and leaves context binding, actual-open execution, and immediate protective-stop handling inside their existing atomic block (`src/autobit/paper/service.py:448-504`). `SQLiteStore` still uses `BEGIN IMMEDIATE` with real outer commit/rollback and nested savepoints (`src/autobit/persistence/sqlite_store.py:900-934`). Operational evidence commits independently before this boundary, while a later trading failure still rolls back the trading mutation.
- Slow external work is followed by a fresh lease acquisition/renewal and completion check (`src/autobit/paper/service.py:424-444`). Within the transaction, health/risk evidence, operational alert ancestry, completion, and active orders are read again (`:449-471`). Prepared contexts are keyed by immutable order identity; an unprepared eligible unbound BUY fails closed. Canceled/filled orders are not selected from the new active-order reconciliation.
- Existing broker validation remains authoritative: binding requires an accepted unfilled BUY and subsequently reconciles the context (`src/autobit/execution/paper_broker.py:231-250`); context validation checks same-signal causal risk, the risk/exposure values, timing, and original quantity (`:1704-1749`). Explicit `None` for genuinely generic orders is preserved (`src/autobit/paper/service.py:635-641`, `:658-676`), not substituted after a service history failure.
- New tests retain real application/store/broker/health behavior and fake the public source, clock, sleeper, and delivery only. API and malformed-history tests assert unchanged pending orders/no fills and durable health after reopening (`tests/integration/test_paper_followup.py:221-331`). Distinct observation times isolate the original rollback defect from timestamp deduplication (`:244-249`, `:293-298`).
- Recovery checks the original accepted order identity, independently expected `70 / 110` quantity and at-most-70 notional, one fill, and replay equality after reopening (`tests/integration/test_paper_followup.py:334-394`). Alert coverage checks persisted source/attempt membership, same-event deduplication, and reopen equality, rather than only fake delivery counts (`:397-475`). The lease test uses a real second SQLiteStore and a reachable six-minute expiry/takeover (`:478-522`).

## Issues

### Critical

None.

### Important

None. No remaining N1 production defect or new blocking defect was found in the scoped code and named adjacent boundaries.

### Minor

**M1 — Required repeated-history-failure regression is missing.** `tests/integration/test_paper_followup.py:251-275` performs only one failing `run_once`; `:362-381` performs one failed cycle followed by successful recovery. Neither covers Task 9 acceptance case 1's repeated historical failures, each interleaved with a successful current-window request. Add a small bounded regression or extend the API test to attempt another failed cycle at a distinct observation time, assert durable failure evidence/last-failure advancement and unchanged pending order/context/fills after each attempt and reopen. Keep the expected consecutive-failure count consistent with intervening current-window successes; do not assert a three-failure latch. This is a narrow coverage gap, not a demonstrated runtime fault or a reason to change established health policy.

## Named adjacent checks and review boundaries

1. **Durable operational lineage across a thrown cycle:** followed `service._legacy_entry_context` into `_ObservedCandleSource.prepare/_persist/_notify` (`src/autobit/cli.py:421-517`), `HealthMonitor.persist` (`src/autobit/paper/health.py:548-570`), SQLite transaction/append boundaries (`src/autobit/persistence/sqlite_store.py:386-392`, `:494`, `:900-934`), and `deliver_alert_once/_claim_attempt` (`src/autobit/alerts/notifier.py:92-198`). Source and attempt are independently persisted before delivery; after moving the request there is no enclosing fill transaction to erase either when the request rethrows. Existing successful-delivery representation is the durable attempt, not a newly introduced success event.
2. **State ownership after slow preflight:** completed the cut-off service function context (`src/autobit/paper/service.py:312-407`, `:480-599`, `:631-656`) and checked lease acquisition/release (`src/autobit/persistence/sqlite_store.py:394-465`), completed-cycle validation (`src/autobit/paper/service.py:861-880`), and broker binding/causal context validation cited above. Renewal is after external history work and before the transaction; completion and active-order checks are repeated inside the write transaction. Exact owner/token release cannot delete the competing worker's lease.
3. **One capped recovery, rollback, generic compatibility:** inspected the preserved atomic fill/protection boundary and broker context validator, with the recovery test and retained broker/service/golden selection. Preflight does not write a context or execution event; failures return before the atomic fill block. The generic `None` path remains tied to absence of same-signal service-risk evidence, and no catches convert an invalid service context into generic execution.

Read the supplied 28,747-byte contextual diff once, for base `0f5025a85f7b866e67ef70bb8096a527ba92b5d7` through head `211a04698538dfcba2057aef9c371bebbc13680c`. Changed service context was read separately only because the supplied hunks cut off the affected functions and the named call-boundary checks required their remaining context. Also read the complete briefs, implementation report, original N1 report, relevant N1 rulings, installed task-reviewer template, and subsequent evidence-provenance amendment. No re-derived git diff, broad repository review, subagent, source/index/HEAD mutation, actual network/notification/private/live operation, or repeated suite was performed. No diagnostic test was needed: the code and existing evidence answered the concrete behavioral doubts.

## Verification evidence and limitations

- Independently read `command.txt`, `head.txt`, `run.ps1`, complete `stdout.txt`, and `exit-code.txt` under `C:/Users/boxma/AppData/Local/Temp/autobit-task9-broad-a0a86fcafde84c3ab26ef0ec7877e715`; checked stderr size is zero. The retained result is **525 passed in 294.69s (0:04:54), exit 0**, without warnings or other stdout noise. The exact selector includes paper broker/health/store/notifier, service/CLI/recovery/acceptance/followup, paper/core goldens and safety.
- That run's retained HEAD is **38143660b5188003d5327c69dd06a5439fd4bce2**, before the source pin, not `211a046...`. The wrapper captures HEAD but no working-diff artifact, so it is amended-working-tree covering evidence, not proof of a clean exact-head full run. The separately frozen full run is the controller's source-identity/final gate.
- The original RED and restart paths contain pytest fixtures, not durable command/stdout/exit logs. After requesting clarification, read the amended `task-9-report.md:46-54`, `:73-77`, `:107-110`: short-run evidence is explicitly transcribed from original tool output. RED is reported as `assert 0 == 1`, **1 failed, 21 deselected in 6.25s**; focused GREEN as **6 passed, 20 deselected in 11.39s**, `PYTEST_EXIT=0`; restart as **4 passed in 0.25s**, `PYTEST_EXIT=0`. These are transparently labeled transcriptions, not independently inspected original retained logs. No suite was rerun to manufacture missing evidence. The independently retained 525-pass run includes the new regression file.
- The full amended-source repository coverage run, post-full compile/exact-seven-command help/safety/diff checks, and two-week/30-trade operational observation are not claimed completed. No real trading readiness, integration, merge, push, or operational launch is authorized by this review.

## Assessment

**Task quality: Approved with Minor coverage follow-up.** N1's load-bearing transaction defect is repaired without weakening replay, context validation, or generic compatibility. The scoped code has no Critical/Important finding; required repeated-failure acceptance coverage should still be completed or explicitly dispositioned by the controller.

**Counts: C0 / I0 / M1.** Code approval is distinct from pending repository-wide verification and unperformed operational observation.
