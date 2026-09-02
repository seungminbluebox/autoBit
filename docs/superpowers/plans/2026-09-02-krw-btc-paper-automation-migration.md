# KRW-BTC Paper Automation and Full Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add restart-safe normalized paper trading with automatic health checks, safety cooldowns, automatic reduced-size recovery, alerts, and remove every legacy live-order path.

**Architecture:** The paper service reuses the exact strategy, sizing, risk, and state-machine code proven by the offline plans. A transactional SQLite event store is the source of truth for simulated cash, BTC, orders, positions, breakers, and idempotency keys. A public-candle scheduler and paper broker are adapters; there is no authenticated exchange adapter.

**Tech Stack:** Python 3.12.13, SQLite 3 via the standard library, httpx 0.28+, pandas 2.2+, pytest 9+, core and validation packages from Plans 1 and 2

**Spec:** `docs/superpowers/specs/2026-09-02-krw-btc-trend-system-design.md`

## Global Constraints

- Complete the core/backtest and Walk-forward/reporting plans first.
- Paper equity starts at normalized 100 and never represents real KRW.
- Only public `KRW-BTC` market data may be requested.
- No access key, secret key, account balance, private WebSocket, or order endpoint may exist.
- The service may pause entries when data or state is unsafe, but performs health checks and resumes automatically when the approved conditions recover.
- A restart may not duplicate a signal, order, fill, or alert.
- Every stored timestamp is UTC; KST is presentation only.
- Telegram notification is optional and never controls trading state.
- All legacy root-level trading modules are deleted only after the replacement safety and regression suites pass.
- Every task follows red-green-refactor TDD and ends with a focused commit.

---

## File Map Locked by This Plan

```text
src/autobit/persistence/sqlite_store.py   Schema, migrations, transactions, replay
src/autobit/execution/paper_broker.py     Simulated market fills and ledger updates
src/autobit/paper/service.py              One completed-candle processing cycle
src/autobit/paper/scheduler.py            UTC 4-hour boundary scheduling
src/autobit/paper/health.py               API/data/order/ledger health and recovery
src/autobit/alerts/notifier.py             Optional notification protocol and Telegram adapter
src/autobit/cli.py                         Adds `paper-run`, `paper-once`, `paper-status`
tests/unit/test_sqlite_store.py
tests/unit/test_paper_broker.py
tests/unit/test_paper_health.py
tests/unit/test_scheduler.py
tests/integration/test_paper_restart.py
tests/integration/test_paper_auto_recovery.py
tests/integration/test_paper_cli.py
tests/safety/test_no_live_surface.py
tests/regression/test_paper_golden.py
README.md
```

### Task 1: Transactional SQLite Event Store

**Files:**
- Create: `src/autobit/persistence/sqlite_store.py`
- Create: `tests/unit/test_sqlite_store.py`
- Create: `tests/integration/test_paper_restart.py`

**Interfaces:**
- Consumes: SQLite path and immutable domain events
- Produces: `SQLiteStore.initialize`, `append_event`, `record_order_once`, `load_snapshot`, `replay_state`, `transaction`

- [ ] **Step 1: Write failing schema, idempotency, and replay tests**

```python
from pathlib import Path

from autobit.domain.models import PositionState
from autobit.persistence.sqlite_store import SQLiteStore


def test_order_idempotency_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    first = SQLiteStore(path)
    first.initialize(initial_equity=100.0)
    assert first.record_order_once("KRW-BTC:2026-01-01T00:00:00Z:ENTRY", "BUY", 0.1)
    first.close()
    second = SQLiteStore(path)
    second.initialize(initial_equity=100.0)
    assert not second.record_order_once("KRW-BTC:2026-01-01T00:00:00Z:ENTRY", "BUY", 0.1)


def test_replay_restores_cash_position_and_breaker_state(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "paper.sqlite3")
    store.initialize(initial_equity=100.0)
    store.append_fill(order_id="entry-1", side="BUY", quantity=0.2, price=100.0, fee=0.01,
                      occurred_at="2026-01-01T04:00:00Z")
    snapshot = store.replay_state()
    assert snapshot.position_state is PositionState.LONG
    assert snapshot.btc_quantity == 0.2
    assert snapshot.cash == 79.99
```

- [ ] **Step 2: Run tests and verify missing module failure**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_sqlite_store.py tests/integration/test_paper_restart.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement versioned schema and atomic event append**

Create tables with these keys:

```sql
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  occurred_at_utc TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE orders (
  order_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  side TEXT NOT NULL,
  requested_quantity REAL NOT NULL,
  filled_quantity REAL NOT NULL,
  status TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL
);
CREATE TABLE snapshots (
  sequence INTEGER PRIMARY KEY,
  state_json TEXT NOT NULL,
  created_at_utc TEXT NOT NULL
);
```

Enable foreign keys and WAL, use `BEGIN IMMEDIATE` for order/fill/ledger updates, and store JSON with sorted keys. On initialization, reject an existing database whose configured market is not `KRW-BTC` or whose normalized initial equity is not 100.

- [ ] **Step 4: Run storage and restart tests**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_sqlite_store.py tests/integration/test_paper_restart.py -v`

Expected: all tests PASS; reopening and replaying produces the identical snapshot.

- [ ] **Step 5: Commit persistence**

```powershell
git add src/autobit/persistence/sqlite_store.py tests/unit/test_sqlite_store.py tests/integration/test_paper_restart.py
git commit -m "feat: persist paper state transactionally"
```

### Task 2: Paper Broker with the Backtest Execution Contract

**Files:**
- Create: `src/autobit/execution/paper_broker.py`
- Create: `tests/unit/test_paper_broker.py`
- Create: `tests/regression/test_paper_golden.py`

**Interfaces:**
- Consumes: completed candle, pending order, cost config, SQLiteStore
- Produces: `PaperBroker.submit`, `PaperBroker.process_open`, `PaperBroker.process_intrabar_stop`, `PaperBroker.reconcile`

- [ ] **Step 1: Write failing fill, no-duplicate, and balance tests**

```python
from pathlib import Path

import pytest

from autobit.config import CostConfig
from autobit.execution.paper_broker import PaperBroker
from autobit.persistence.sqlite_store import SQLiteStore


def test_market_buy_fills_next_open_once(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "paper.sqlite3")
    store.initialize(initial_equity=100.0)
    broker = PaperBroker(store, CostConfig(fee_rate=0.0005, slippage_rate=0.0005))
    order = broker.submit_entry("2026-01-01T00:00:00Z", quantity=0.2)
    first = broker.process_open(order.order_id, "2026-01-01T04:00:00Z", open_price=100.0)
    second = broker.process_open(order.order_id, "2026-01-01T04:00:00Z", open_price=100.0)
    assert first.fill_price == pytest.approx(100.05)
    assert second is None
    assert store.replay_state().btc_quantity == pytest.approx(0.2)


def test_sell_cannot_exceed_owned_btc(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "paper.sqlite3")
    store.initialize(initial_equity=100.0)
    broker = PaperBroker(store, CostConfig())
    with pytest.raises(ValueError, match="sell quantity exceeds position"):
        broker.submit_exit("2026-01-01T00:00:00Z", quantity=0.1, owned_quantity=0.0, reason="EXIT_CHANNEL")
```

- [ ] **Step 2: Run tests and verify failure**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_paper_broker.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement deterministic simulated execution**

Entry market fill is `open * (1 + slippage)`, exit market fill is `open * (1 - slippage)`, and fee is `notional * fee_rate`. Validate cash including fee before accepting entry. Persist Created, Submitted, Accepted, and Completed events in one order lifecycle while keeping unique event IDs. A hard stop uses the prior active stop; if open is below stop, use open before sell slippage, otherwise use stop before sell slippage.

The paper broker supports injected partial fills for tests. Entry partial fill accepts actual quantity and cancels remainder at candle end. Exit partial fill leaves `EXIT_PENDING` and submits the exact remainder once at the following candle open.

- [ ] **Step 4: Run paper golden parity**

Feed the same fixture used by the core golden backtest through the paper broker one candle at a time. Assert identical zero-cost trade timestamps, prices, reasons, and final equity.

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_paper_broker.py tests/regression/test_paper_golden.py -v`

Expected: all tests PASS and paper/backtest parity holds.

- [ ] **Step 5: Commit paper execution**

```powershell
git add src/autobit/execution/paper_broker.py tests/unit/test_paper_broker.py tests/regression/test_paper_golden.py
git commit -m "feat: simulate restart-safe paper fills"
```

### Task 3: One-Candle Service and UTC Scheduler

**Files:**
- Create: `src/autobit/paper/service.py`
- Create: `src/autobit/paper/scheduler.py`
- Create: `tests/unit/test_scheduler.py`
- Create: `tests/integration/test_paper_service.py`

**Interfaces:**
- Consumes: public client, store, strategy/risk/sizing services, clock
- Produces: `PaperService.process_completed_candle(end_utc) -> CycleResult`, `next_cycle_at(now_utc)`

- [ ] **Step 1: Write failing boundary and once-only tests**

```python
from datetime import datetime, timezone

from autobit.paper.scheduler import next_cycle_at


def test_next_cycle_is_ten_minutes_after_next_four_hour_close() -> None:
    now = datetime(2026, 1, 1, 5, 23, tzinfo=timezone.utc)
    assert next_cycle_at(now) == datetime(2026, 1, 1, 8, 10, tzinfo=timezone.utc)


def test_exact_boundary_does_not_process_unclosed_bar() -> None:
    now = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    assert next_cycle_at(now) == datetime(2026, 1, 1, 8, 10, tzinfo=timezone.utc)
```

The integration test invokes `process_completed_candle` twice with the same end timestamp and asserts the second result is `ALREADY_PROCESSED`, with no new order or ledger event.

- [ ] **Step 2: Run scheduler/service tests and verify failure**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_scheduler.py tests/integration/test_paper_service.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement a deterministic cycle**

At each cycle: acquire a single-process SQLite lease, reconcile existing pending paper orders, fetch enough completed public candles, canonicalize, compute indicators, mark health, update an existing stop/exit first, calculate risk and sizing, and only then consider one entry. Store `cycle:{candle_end_utc}` as an idempotency key. Release the lease in `finally`.

Use a clock protocol so tests never sleep. The production scheduler waits until UTC hours 00, 04, 08, 12, 16, or 20 plus ten minutes. It performs one cycle and computes the next boundary rather than adding a fixed four-hour sleep, preventing clock drift.

- [ ] **Step 4: Run service tests including restart during a cycle**

Inject an exception after order acceptance but before response return, reopen the store, rerun the same candle, and assert reconciliation completes the existing order without creating another.

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_scheduler.py tests/integration/test_paper_service.py tests/integration/test_paper_restart.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit scheduling and service orchestration**

```powershell
git add src/autobit/paper tests/unit/test_scheduler.py tests/integration/test_paper_service.py tests/integration/test_paper_restart.py
git commit -m "feat: process completed paper candles automatically"
```

### Task 4: Health Breakers and Fully Automatic Recovery

**Files:**
- Create: `src/autobit/paper/health.py`
- Create: `tests/unit/test_paper_health.py`
- Create: `tests/integration/test_paper_auto_recovery.py`

**Interfaces:**
- Consumes: API attempts, candle freshness, timestamp order, order reconciliation, ledger reconciliation, fill deviation
- Produces: `HealthSnapshot`, `HealthAction`, `RecoveryProgress`

- [ ] **Step 1: Write failing halt and auto-resume tests**

```python
from datetime import datetime, timedelta, timezone

from autobit.paper.health import HealthMonitor


def test_three_api_failures_halt_and_three_successes_auto_resume() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monitor = HealthMonitor()
    monitor.record_api_failure(now)
    monitor.record_api_failure(now + timedelta(seconds=2))
    action = monitor.record_api_failure(now + timedelta(seconds=6))
    assert action.halt_entries
    assert action.reason == "API_FAILURES"
    monitor.record_api_success(now + timedelta(minutes=1))
    monitor.record_api_success(now + timedelta(minutes=2))
    action = monitor.record_api_success(now + timedelta(minutes=3))
    assert action.resume_reduced


def test_unresolved_order_blocks_resume_even_when_api_is_healthy() -> None:
    monitor = HealthMonitor()
    monitor.set_unresolved_orders(1)
    for minute in range(3):
        monitor.record_api_success(datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc))
    assert not monitor.current_action().resume_reduced
```

- [ ] **Step 2: Run health tests and verify failure**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_paper_health.py tests/integration/test_paper_auto_recovery.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement exact health predicates and backoff**

Health reasons are `API_FAILURES`, `STALE_CANDLE`, `TIMESTAMP_REVERSAL`, `SCHEMA_ERROR`, `UNRESOLVED_ORDER`, `LEDGER_MISMATCH`, and `FILL_DEVIATION`. Three consecutive API failures halt entries. A completed candle missing ten minutes after close is stale. Any unresolved order or ledger mismatch blocks recovery. Fill deviation greater than 5% halts entries.

Recovery requires three consecutive successful API health calls, zero unresolved orders, ledger equality within `1e-10`, monotonic timestamps, and a valid latest completed candle. Health retries use 1, 2, 4, 8, 16 seconds and then a five-minute ceiling. On recovery, set the risk layer's reduced-size mode; do not bypass drawdown, streak, weekly, or volatility breakers.

- [ ] **Step 4: Run fault-injection tests**

Inject each reason independently and in combinations. Assert the most restrictive state persists, recovery never needs human input, and no entry occurs before all required predicates recover.

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_paper_health.py tests/integration/test_paper_auto_recovery.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit health automation**

```powershell
git add src/autobit/paper/health.py tests/unit/test_paper_health.py tests/integration/test_paper_auto_recovery.py
git commit -m "feat: halt and auto-recover paper trading safely"
```

### Task 5: Optional Alerts and Safe Paper CLI

**Files:**
- Create: `src/autobit/alerts/notifier.py`
- Modify: `src/autobit/cli.py`
- Create: `tests/unit/test_notifier.py`
- Create: `tests/integration/test_paper_cli.py`

**Interfaces:**
- Consumes: domain events and optional Telegram bot/chat values
- Produces: `Notifier.send(event)`, `paper-once`, `paper-run`, `paper-status`

- [ ] **Step 1: Write failing non-fatal alert and CLI tests**

```python
from autobit.alerts.notifier import NullNotifier, SafeNotifier


def test_alert_failure_never_changes_cycle_result() -> None:
    class BrokenNotifier:
        def send(self, event: dict[str, object]) -> None:
            raise RuntimeError("network down")

    safe = SafeNotifier(BrokenNotifier())
    assert not safe.send({"type": "HALTED", "reason": "API_FAILURES"})
    assert NullNotifier().send({"type": "HEALTHY"})
```

CLI integration tests assert `paper-status --db PATH` is read-only, `paper-once` processes one completed candle, and `paper-run` accepts `--db`, `--data-dir`, and optional `--telegram-token-env`/`--telegram-chat-env` names without accepting raw secrets on the command line.

- [ ] **Step 2: Run tests and verify failure**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_notifier.py tests/integration/test_paper_cli.py -v`

Expected: collection FAIL or unrecognized CLI commands.

- [ ] **Step 3: Implement alert isolation and commands**

The Telegram adapter uses `POST https://api.telegram.org/bot{token}/sendMessage` with a five-second timeout. Read token/chat values only from explicitly named environment variables at runtime; do not use Upbit credentials. Log alert failure as `ALERT_FAILURE` and continue the completed trading cycle.

`paper-status` prints normalized cash, BTC, equity, state, active stop, breaker reasons, last completed candle, pending orders, and next scheduled run. `paper-once` exits nonzero on unsafe data but leaves the store recoverable. `paper-run` owns the scheduler loop and handles Ctrl+C after the active transaction finishes.

- [ ] **Step 4: Run CLI and notifier tests**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_notifier.py tests/integration/test_paper_cli.py -v`

Expected: all tests PASS; notifier exceptions are contained.

- [ ] **Step 5: Commit operator surfaces**

```powershell
git add src/autobit/alerts src/autobit/cli.py tests/unit/test_notifier.py tests/integration/test_paper_cli.py
git commit -m "feat: expose safe automated paper commands"
```

### Task 6: Remove Legacy Live-Order Code and Lock the Safety Boundary

**Files:**
- Delete: `main.py`
- Delete: `market_mode.py`
- Delete: `strategy_loader.py`
- Delete: `trade.py`
- Delete: `upbit_api.py`
- Delete: `config.py`
- Delete: `logutils.py`
- Delete: `analyze_log.py`
- Delete: `telegram_alert.py`
- Delete: `strategies/bull.py`
- Delete: `strategies/defensive.py`
- Delete: `strategies/sideways.py`
- Modify: `tests/safety/test_no_live_surface.py`
- Create: `README.md`

**Interfaces:**
- Consumes: complete replacement package
- Produces: a repository whose only executable trading modes are backtest, Walk-forward, and normalized paper trading

- [ ] **Step 1: Strengthen the safety test before deletion**

```python
from pathlib import Path


def test_repository_has_no_live_order_or_upbit_credential_surface() -> None:
    forbidden = (
        "buy_market_order", "sell_market_order", "create_upbit",
        "UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY", "api.upbit.com/v1/orders",
    )
    candidates = [path for path in Path(".").rglob("*.py") if ".venv" not in path.parts]
    source = "\n".join(path.read_text(encoding="utf-8") for path in candidates)
    for token in forbidden:
        assert token not in source


def test_legacy_entrypoints_are_gone() -> None:
    for name in ("main.py", "trade.py", "upbit_api.py", "market_mode.py", "strategy_loader.py"):
        assert not Path(name).exists()
```

- [ ] **Step 2: Run the safety test and verify it fails against legacy files**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/safety/test_no_live_surface.py -v`

Expected: FAIL and identify the existing legacy order and credential strings.

- [ ] **Step 3: Delete the listed legacy files and document safe commands**

Use `apply_patch` deletions so the exact removed content remains reviewable in Git. Remove now-empty `strategies/` source files; ignored `__pycache__` directories are not part of the patch. README must document environment creation, public data collection, quality check, backtest, Walk-forward, `paper-once`, `paper-run`, `paper-status`, generated files, normalized equity, and the explicit absence of live trading.

- [ ] **Step 4: Run full safety and replacement regression suites**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/safety tests/regression tests/integration -v
& '.\.venv\Scripts\python.exe' -m autobit.cli --help
rg -n "buy_market_order|sell_market_order|UPBIT_ACCESS_KEY|UPBIT_SECRET_KEY|create_upbit" -g "*.py" .
```

Expected: tests PASS, CLI lists no live mode, and `rg` returns no matches.

- [ ] **Step 5: Commit the full migration**

```powershell
git add -A
git commit -m "refactor: replace legacy live bot with safe research system"
```

### Task 7: Full Paper Acceptance and Fault-Injection Runbook

**Files:**
- Create: `tests/integration/test_paper_acceptance.py`
- Create: `docs/paper-trading-runbook.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: complete application and synthetic public-candle stream
- Produces: one-command acceptance evidence and an operator runbook

- [ ] **Step 1: Write the failing acceptance scenario**

Create a deterministic stream that causes one entry, a restart, one API outage, automatic recovery, a volatility reduction, one hard stop, and a final flat state. Assert:

```python
def test_paper_acceptance_scenario(app_harness) -> None:
    result = app_harness.run("tests/fixtures/paper_acceptance.json")
    assert result.duplicate_orders == 0
    assert result.negative_cash_events == 0
    assert result.oversell_events == 0
    assert result.halt_reasons == ["API_FAILURES"]
    assert result.automatic_recoveries == 1
    assert result.final_state == "FLAT"
    assert result.replay_matches_live_state
```

- [ ] **Step 2: Run the acceptance test and verify the fixture/harness is missing**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/integration/test_paper_acceptance.py -v`

Expected: FAIL because the acceptance harness or fixture is absent.

- [ ] **Step 3: Add the deterministic harness, fixture, and runbook**

The runbook must contain exact commands for start, status, graceful stop, database backup copy while stopped, restore verification, corrupted-state detection, API outage behavior, stale-candle behavior, automatic recovery criteria, report locations, and the two-week-plus-30-trade graduation rule. It must state that PASS permits only continued paper evaluation and never enables live trading.

- [ ] **Step 4: Run every test and a one-cycle CLI smoke test**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest --cov=autobit --cov-report=term-missing -v
& '.\.venv\Scripts\python.exe' -m autobit.cli paper-once --db reports/paper-smoke.sqlite3 --data-dir data/processed
& '.\.venv\Scripts\python.exe' -m autobit.cli paper-status --db reports/paper-smoke.sqlite3
git status --short
```

Expected: all tests PASS; the paper command either processes a valid completed candle or exits safely with a documented public-data error; status remains readable and no untracked source file appears.

- [ ] **Step 5: Commit acceptance evidence**

```powershell
git add tests/integration/test_paper_acceptance.py tests/fixtures/paper_acceptance.json docs/paper-trading-runbook.md README.md
git commit -m "test: verify automated paper recovery end to end"
```

## Plan 3 Completion Gate

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest --cov=autobit --cov-report=term-missing -v
rg -n "buy_market_order|sell_market_order|UPBIT_ACCESS_KEY|UPBIT_SECRET_KEY|create_upbit" -g "*.py" .
git status --short
```

Required result: all tests pass, forbidden live tokens have no matches, restart replay is exact, fault injection proves automatic recovery without duplicate orders, and the only remaining worktree changes are intentional report/data artifacts ignored by Git. The system then enters the minimum two-week and 30-trade paper observation period; it does not progress to live trading.
