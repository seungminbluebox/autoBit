# Task 9 M1 test-only re-review

## Finding verdict and spec compliance

**M1: ADDRESSED. Spec compliance: compliant. Task quality: Approved.**

The isolated extension of `test_legacy_pending_history_api_failure_survives_return_and_reopen` satisfies the previous finding without changing production behavior:

- **Repeated real application attempt:** `tests/integration/test_paper_followup.py:277-285` reopens SQLite, advances the fake clock by four hours and calls the real `_PaperApplication.run_once` with a fresh lease token. The existing source returns a valid current-window frame before throwing on the original-signal window (`:242-249`); neither application nor persistence/health behavior is mocked.
- **Distinct, retained failures and correct consecutive counters:** `:286-298` asserts two durable failed health observations, later `last_failure_at_utc`, increasing event sequence, success counts `[1, 0, 1, 0]`, failure counts `[0, 1, 0, 1]`, and final `api_failures == 1`. Thus each failed history request is visibly preceded by current-window success. It neither mistakes the two cycle failures for consecutive API failures nor invents a three-failure latch. Canonical logical event time is not incorrectly required to advance; actual observation-time evidence advances instead.
- **No trading mutation:** the first failure already checks the exact accepted active order, no fills, and absent context (`:262-265`). The new second-failure checks repeat these properties (`:299-301`), and another close/reopen retains the counter, last failure, same pending order, no fills and no context (`:304-311`). Exact original active-order equality plus absent context/no fills is the existing broker's fail-closed legacy-order assertion; the delta does not weaken those validators.
- **Bounded and isolated:** one extra attempt in the same per-test temporary database; no loop, real network, delivered notification, global mutable test state, fixture relaxation, production source change or new architecture. Existing first-failure/reopen assertions remain intact.

## Strengths and new breakage

The assertions target persisted product state, including event sequence and alternating counter evidence, rather than only counting source calls. Reopening between attempts and again after the second failure specifically covers the durability gap identified in M1. Reusing the existing source and real stack keeps this 37-line addition proportionate to the required acceptance case.

**New breakage: None. Critical 0 / Important 0 / Minor 0.**

**Out-of-scope observations: None.** The prior production review and whole branch were not reopened.

## Inspected evidence

Read the M1 re-review brief, original Task 9 requirements retained from the prior review, appended implementation completion/provenance, applicable controller ruling at `progress.md:106-108`, and the complete supplied 11,451-byte contextual diff once: base `211a04698538dfcba2057aef9c371bebbc13680c`, head `4e5e985207750290177c37c3bd2bfd9acf9432d9`. The package changes only the test and report. Read the unchanged beginning of the affected test (`tests/integration/test_paper_followup.py:221-265`) because the supplied hunk begins mid-function and validating the repeated attempt requires its source/first-attempt setup. No other source was rereviewed and no git diff was regenerated.

Independently inspected retained command, complete stdout, exit, HEAD and zero-byte stderr/production-diff artifacts:

- `C:/Users/boxma/AppData/Local/Temp/autobit-task9-m1-focused-final-66da2033c71644bab93b61179ae8bce2`: six explicit test node IDs, including the amended regression; **6 passed in 14.25s, exit 0**, empty stderr.
- `C:/Users/boxma/AppData/Local/Temp/autobit-task9-m1-restart-final-4562330d6f594262bfee126b88c3d963`: whole `tests/integration/test_paper_restart.py`; **4 passed in 0.37s, exit 0**, empty stderr.
- Both runs record HEAD `211a04698538dfcba2057aef9c371bebbc13680c` and empty production diffs. These are fresh tests of the then-uncommitted test extension on unchanged production, subsequently pinned in the supplied `4e5e985...` test/report-only diff; they are not represented as runs initially launched at the later commit.
- `C:/Users/boxma/AppData/Local/Temp/autobit-task9-m1-static-final-a7453658f2a1495a9f279d0d8b6a299b`: read exact commands, summary, all thirteen retained exit files, HEAD, safety stdout, and artifact sizes. Compile, root plus seven CLI help commands, safety, and diff checks exit 0; the exact private/live-token search has no output and expected exit 1. Safety stdout is **72 passed in 28.48s**. Production diff stdout and stderr are empty; HEAD is the same production pin.
- Static evidence nuance: `git diff --check` has empty stdout and exit 0, but `12-stderr.log` contains two Git LF-to-CRLF normalization warnings for the report and amended test. These are not pytest warnings or behavioral failures, but that particular command's complete output was not literally empty. Focused/restart/safety test output is pristine.

## Limitations and final assessment

The full-suite result **1129 passed, 88% coverage, exit 0** on frozen `211a046...` is the controller-verified result recorded in the binding brief/ruling; this narrow re-review did not rerun or independently re-audit that full suite. Under the explicit ruling, it combines with fresh focused/restart verification of the isolated test-only extension. **No second full run on the amended test tree is claimed.**

No diagnostic probe was necessary: the test source and retained outputs answer the M1 questions. No suites were repeated, subagents dispatched, or source/tests/index/HEAD changed; only this report was written.

M1 is closed with no new finding. This completes the scoped test-only acceptance review, not two-week/30-trade operational observation, live-trading readiness, or authorization to merge/push/launch.
