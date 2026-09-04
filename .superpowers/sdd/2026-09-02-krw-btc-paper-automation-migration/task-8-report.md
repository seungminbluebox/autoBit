# Task 8: sequence-bound `paper-status` equity

Date: 2026-09-04

Base: `fc8b6fe05cab3cf4054034548a54556635abe50b`

## Root cause and fix

`_status_equity` proved its selected risk event preceded the latest completed
`PAPER_CYCLE`, but did not prove that current broker balances still belonged to
that cycle.  A durable raw `FILL` after the terminal cycle could therefore
combine the old completed-close equity with post-fill cash/inventory; a BUY
looked falsely `CURRENT`, while a flat SELL could fail the old cash
contradiction check.

The minimal fix binds the selected cycle to the validated current broker
reconciliation.  It returns the existing `STALE / LAST_FILL_BROKER_EQUITY`
fallback when any reconciled raw `FILL` has an immutable sequence later than
the selected terminal cycle.  It neither uses timestamps nor IDs as ordering
proxies, and it leaves existing strict chain/contradiction validation intact.

## TDD evidence

Before production code, real SQLiteStore/PaperBroker/CLI tests created
fee-bearing durable post-terminal fills in both active-WAL and reopened
snapshots.  Expected RED:

```
.venv\\Scripts\\python.exe -m pytest tests/integration/test_paper_cli.py -k
"later_fee_bearing or fill_precedes_terminal_cycle" --basetemp
.test-tmp/task8-red -p no:cacheprovider

1 passed, 4 failed in 8.92s
BUY: false CURRENT, normalized_equity 100.0 instead of 99.97996.
SELL: paper-status exited 2 / INVALID_OR_MISSING_LEDGER instead of stale.
```

The BUY case is LONG, the SELL case is FLAT, both use exact replayed last-fill
equity and newest fill time, and each asserts its raw `FILL` sequence is later
than the terminal cycle.  Their names/documentation explicitly identify the
pre-risk/cycle crash window; removing the sequence guard recreates the RED.
The existing active-WAL positive control now explicitly proves a fill before
its terminal cycle remains `CURRENT / COMPLETED_CLOSE_MTM`.

GREEN command:

```
.venv\\Scripts\\python.exe -m pytest tests/integration/test_paper_cli.py -k
"later_fee_bearing or fill_precedes_terminal_cycle" --basetemp
.test-tmp/task8-green -p no:cacheprovider
```

Result: `5 passed in 5.75s`.

## Generated-basetemp boundary

The initial broad safety run exposed URL-shaped cache data created under an
unignored worktree `.test-tmp/` basetemp.  `.gitignore` now narrowly ignores
only `.test-tmp/`; this is generated pytest state, never runtime/package
source.  Safety regressions prove that no `.test-tmp/` worktree surface is
scanned while a newly-created unignored `src/autobit/*.py` module is still
enumerated and its forbidden `pyupbit` import is detected.  Subsequent pytest
basetemp and coverage files used system Temp directories.

## Verification

Focused paper CLI/service/restart/recovery/acceptance/broker/store/safety:

```
.venv\\Scripts\\python.exe -m pytest tests/integration/test_paper_cli.py
tests/integration/test_paper_service.py tests/integration/test_paper_restart.py
tests/integration/test_paper_auto_recovery.py
tests/integration/test_paper_acceptance.py tests/unit/test_paper_broker.py
tests/unit/test_sqlite_store.py tests/safety/test_no_live_surface.py
--basetemp C:\\Users\\boxma\\AppData\\Local\\Temp\\autobit-task8-focused-final-1b92ef16b8d648869ec5eb306957c4e9\\basetemp
-p no:cacheprovider
```

Result: `391 passed in 103.55s`; durable output:
`C:\\Users\\boxma\\AppData\\Local\\Temp\\autobit-task8-focused-final-1b92ef16b8d648869ec5eb306957c4e9\\focused.log`
(`focused.err` is empty).

Fresh final paper-status/safety check:

```
.venv\\Scripts\\python.exe -m pytest tests/integration/test_paper_cli.py
tests/safety/test_no_live_surface.py --basetemp
C:\\Users\\boxma\\AppData\\Local\\Temp\\autobit-task8-focused-final-e19265778b674433961171d00aa4e85c\\basetemp
-p no:cacheprovider
```

Result: `117 passed in 31.88s`; durable output:
`C:\\Users\\boxma\\AppData\\Local\\Temp\\autobit-task8-focused-final-e19265778b674433961171d00aa4e85c\\focused.log`
(`focused.err` is empty).

Full repository coverage used:

```
$env:COVERAGE_FILE = 'C:\\Users\\boxma\\AppData\\Local\\Temp\\autobit-task8-full-final-cf2e90dbdfc3438a8f0483330cc36540\\coverage'
.venv\\Scripts\\python.exe -m pytest --cov=autobit --cov-report=term-missing -q
-p no:cacheprovider --basetemp
C:\\Users\\boxma\\AppData\\Local\\Temp\\autobit-task8-full-final-cf2e90dbdfc3438a8f0483330cc36540\\basetemp
```

Recovered durable output at
`C:\\Users\\boxma\\AppData\\Local\\Temp\\autobit-task8-full-final-cf2e90dbdfc3438a8f0483330cc36540\\full.log`
has the complete pytest footer: `1077 passed in 4353.28s (1:12:33)`, `8408`
statements, `1014` missed, `88%` coverage. Its sibling `full.err` is zero
bytes. The interruption lost the historical shell exit-code file, so this
report does not claim a directly recovered process exit code; the completed
pytest footer and empty stderr are the retained evidence. The four modified
Task 8 source/test/ignore files were last written at 00:33--00:42 UTC, before
the full log was last written at 01:59:28 UTC, and no production/test edit was
made during recovery; the fresh 117-test check above verifies the recovered
working tree.
- `.venv\\Scripts\\python.exe -m compileall -q src tests`: exit 0.
- `.venv\\Scripts\\autobit.exe --help`, plus `--help` for exactly
  `data-download`, `data-quality`, `backtest`, `walk-forward`, `paper-once`,
  `paper-run`, and `paper-status`: all exit 0. Durable help output is under
  `C:\\Users\\boxma\\AppData\\Local\\Temp\\autobit-task8-help-final`.
- `rg -n "buy_market_order|sell_market_order|create_order|place_order|get_accounts|get_balance|UPBIT_ACCESS_KEY|UPBIT_SECRET_KEY|create_upbit|pyupbit|ccxt|wss://|/v1/orders|/v1/accounts" src pyproject.toml`:
  no matches.
- `git diff --check`: clean before staging.

## Scope and concerns

Files changed are `.gitignore`, `src/autobit/cli.py`, the CLI integration
tests, the safety tests, and this evidence report.  No schema, command,
private/live-network, or trading behavior changed.  Generated `.coverage` and
`__pycache__` files remain untracked and are excluded from the commit.

No unresolved implementation concern remains.  This remains paper-only and
does not enable live trading or recommend a buy.
