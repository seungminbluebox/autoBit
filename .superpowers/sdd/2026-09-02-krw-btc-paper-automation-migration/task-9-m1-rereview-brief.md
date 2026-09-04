# Task 9 M1 — test-only acceptance completion re-review

This is the narrowly scoped completion of your prior Task 9 M1. Read `task-9-brief.md`, the appended completion section of `task-9-report.md`, and the controller-supplied contextual diff. Previous reviewed head/fix base is `211a04698538dfcba2057aef9c371bebbc13680c`; final head and diff path arrive in the dispatch.

## Finding under verification (verbatim)

**M1 — Required repeated-history-failure regression is missing.** `tests/integration/test_paper_followup.py:251-275` performs only one failing `run_once`; `:362-381` performs one failed cycle followed by successful recovery. Neither covers Task 9 acceptance case 1's repeated historical failures, each interleaved with a successful current-window request. Add a small bounded regression or extend the API test to attempt another failed cycle at a distinct observation time, assert durable failure evidence/last-failure advancement and unchanged pending order/context/fills after each attempt and reopen. Keep the expected consecutive-failure count consistent with intervening current-window successes; do not assert a three-failure latch. This is a narrow coverage gap, not a demonstrated runtime fault or a reason to change established health policy.

## Scope and evidence

- Only the amended test and its completion/evidence claims are under review; inspect new breakage introduced by that delta. Do not repeat the prior production or whole-branch review.
- No production changes are authorized for this completion. Public KRW-BTC data only; normalized initial equity100, risk2%, normal exposure70%; retain existing costs, exact strategy parameters,601-bar normal ingress,1095-bar maximum hold, health/reduced-recovery predicates, immutable replay and next-bar protection. No private API, real funds, live mode, merge, push or operational launch.
- Full suite already ran on frozen211a046:1129 passed,88% coverage,exit0. Controller independently inspected its retained stdout/footer, source pin and empty stderr. The controller ruling in `progress.md` accepts this complete full run on unchanged production source plus fresh focused/restart coverage of the sole isolated test-only delta. Do not claim a second full run occurred on the amended test tree.
- Inspect retained focused/restart command/output/exit evidence and source-identity proof from the appended implementation report. Do not rerun a suite merely to repeat existing evidence; only a specific unanswered code doubt warrants a narrowly targeted probe.
- Review is read-only except your designated report `task-9-m1-rereview-report.md`. Do not edit source/tests/index/HEAD/branch or dispatch subagents.

## Report

Write M1 ADDRESSED or NOT ADDRESSED with exact file:line evidence; new breakage severity/counts or None; out-of-scope observations or None; spec compliance and task quality verdict; inspected evidence and limitations. Return only a short verdict and report path. Do not pre-judge the outcome; an implementation report is an unverified claim until checked against the delta and retained outputs.
