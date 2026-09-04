# Shared Strategy Engine and Locked Live Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use one authoritative trading policy in backtest, paper, and locked live code, with automatic drawdown recovery and strict historical-ledger compatibility.

**Architecture:** Pure core decisions and a shared risk reducer consume normalized execution facts; mode-specific adapters execute intents and retain their own truthful fill/persistence semantics. An isolated Upbit connector and durable live service are implemented behind a fixed denial gate, tested entirely offline.

**Tech Stack:** Python 3.12, existing pandas/pandas-ta/Backtrader, SQLite, httpx, pytest. Use the existing worktree virtualenv and standard-library HMAC/JWT construction with explicit contract tests; no dependency installation needed.

**Spec:** `docs/superpowers/specs/2026-09-04-shared-engine-locked-live-design.md` (authoritative addition to the 2026-09-02 strategy specification).

## Global Constraints

- Work only in `.worktrees/krw-btc-rebuild`, branch `codex/krw-btc-rebuild`; initial HEAD `d95880008adc331a5e232fc89cfad1e04e4c0c32`.
- No private API calls, credential discovery, actual orders/cancellations, live activation, deployment, merge, push, destructive cleanup, or unrelated edits.
- Preserve existing untracked coverage/cache files. Use explicit `git add` paths; no broad staging.
- KRW-BTC spot only; completed UTC 4-hour bars; existing strategy/cost/sizing numbers are unchanged except the specified drawdown recovery repair.
- Initial stop is actual entry fill minus 2.5 entry ATR. Trailing activates at +2R and takes effect next bar. Stagnant 60 bars without +1R; maximum 1095 bars.
- Risk policy v2 resumes after 72 hours at drawdown >=15% with risk 0.0025 and exposure 0.15, subject to every other independent gate. Preserve the original equity peak and episode start.
- Old v1 risk events retain existing strict schema/envelope/chronology/health-projection validation; missing raw inputs prevent full historical canonical recomputation. New events use v2; reject detectable corruption and version downgrade without rewriting history or inventing inputs.
- Shared decisions do not imply identical fills, returns, hot reload, or completed seven-year performance validation.
- The production live gate always denies before credentials/signing/private I/O. No CLI/env/test-unlock switch ships; tests may monkeypatch the gate and use MockTransport only.
- One implementation worker at a time. Each task uses RED→GREEN, commits, retained evidence, and a separate task review. Controller writes docs/coordination, not production/test fixes.
- Run targeted suites per task; the controller owns one final full-suite run after source freeze. Never duplicate a running full suite.

## File and responsibility map

- `risk/breakers.py`: authoritative current restrictions; paper replay retains version-aware historical validation without an unused duplicate evaluator.
- `core/models.py`: immutable decision inputs/outputs; `core/risk_state.py`: normalized risk state/observations and reducer; `core/engine.py`: common trading policy.
- `backtest/engine.py`, `paper/service.py`: adapters invoking core; existing broker/store/health mechanics stay mode-specific.
- `live/guard.py`: fixed release lock; `live/client.py`: isolated authenticated HTTP contract; `live/journal.py`: durable real-order state; `live/service.py`: real execution/reconciliation adapter invoking core.
- `tests/unit`, `tests/integration`, `tests/safety`: policy boundaries, real adapter use, ledger compatibility, offline venue protocol, locked execution and isolation.
- `README.md`, `docs/runbooks/locked-live.md`: operational limits and shared-engine maintenance instructions.

### Task 1: Repair drawdown recovery and version historical replay

**Files:** Modify `src/autobit/risk/breakers.py`, `src/autobit/paper/service.py`, affected strict payload validation in `src/autobit/persistence/`; tests in `tests/unit/test_drawdown_recovery_v2.py` and `tests/integration/test_paper_risk_policy_migration.py`; update directly affected existing risk expectations, not unrelated tests. Do not create an unused legacy evaluator.

**Interfaces:** Preserve the exact keyword-only `evaluate_risk(...) -> RiskDecision` public interface currently in `risk/breakers.py`. New evaluations use v2 unconditionally. Paper risk payload version becomes 2 for new writes and admits 1 for strict historical reads. Existing v1 lacks raw volatility/pre-transition inputs: preserve existing validation strength rather than claim full canonical decision recomputation. No legacy policy selector for new decisions.

- [ ] Read current evaluator, PaperService `_advance_risk_state`, risk payload parser, `_validate_health_and_risk_chain`, and Backtrader `_evaluate_current_risk`; trace how starts are preserved/cleared and what triggers force exit.
- [ ] Add a parameterized RED test with an otherwise healthy snapshot and fixed episode start. Use this concrete boundary assertion:

```python
from datetime import datetime, timedelta, timezone
from autobit.config import RiskConfig
from autobit.risk.breakers import evaluate_risk

def test_drawdown_can_resume_without_erasing_historical_loss():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = evaluate_risk(now=start + timedelta(hours=72), drawdown=0.16,
        daily_loss=0.0, weekly_loss=0.0, consecutive_losses=0,
        volatility_ratio=1.0, system_healthy=True, config=RiskConfig(),
        recovery_started_at=start)
    assert (result.risk_rate, result.exposure_cap) == (0.0025, 0.15)
    assert 'drawdown_halt' not in result.reasons
    assert 'recovery' in result.reasons
```

- [ ] Run `.venv\Scripts\python.exe -m pytest tests/unit/test_drawdown_recovery_v2.py -q -p no:cacheprovider --no-cov`; retain the expected assertion failure, not an import/fixture error.
- [ ] Implement the post-expiry branch as recovery ladder selection regardless of lingering historical drawdown. Keep pre-expiry halt and independent restrictions unchanged:

```python
if now_utc < recovery_expiry:
    reasons.append('drawdown_halt')
    halts.append(recovery_expiry)
else:
    risk_rate, exposure_cap = _recovery_ladder(drawdown_value)
    if drawdown_value > 0.0:
        reasons.append('recovery')
```

- [ ] Preserve strict v1 schema/envelope/cursor/projection/health validation and check contradictions derivable from stored facts, without inventing raw volatility inputs. Write a real old-valid SQLite history fixture, open it under new code, append v2 decisions, restart, and compare preserved peak/start/history. Reject detectable altered schema/health projections, unknown versions, and v2→v1 downgrade. Name exactly what each mutation test detects; do not claim all old canonical decisions can be reconstructed. No raw-input persistence redesign is required in this task.
- [ ] Add 71:59:59/72:00:00/240-hour, DD exactly .15, simultaneous daily/weekly/volatility/unhealthy gates, repeated bars/restarts, no recurring recovery-entry force exits, peak recovery followed by a new drawdown episode tests. Verify existing entry/stop/health replay contracts.
- [ ] Run new tests and affected existing risk/paper restart suites; record exact commands/counts/exits and full retained output. Self-review, explicitly stage changed source/tests, commit `fix: resume drawdown recovery with versioned ledger replay`, and report. Do not run the full suite here.

### Task 2: Introduce core and migrate both existing execution adapters

**Files:** Create `src/autobit/core/__init__.py`, `models.py`, `risk_state.py`, `engine.py`; modify `src/autobit/backtest/engine.py`, `src/autobit/paper/service.py`, `src/autobit/strategy/donchian_trend.py` only to centralize existing policy and keep compatibility; create `tests/unit/test_shared_decision_engine.py`, `tests/integration/test_shared_mode_policy.py`. Update risk replay imports from Task 1 as needed without changing version semantics.

**Interfaces:** The following public types/functions are the stable boundary for Task 3. Existing risk-state fields move into `core.risk_state.RiskState` with compatibility properties/defaults as needed, not a dependency on PaperService. Add `initial_equity: float = 100.0` to the normalized state so the first week's baseline is actual initial equity in live and configured initial equity in backtest; paper's historical payload stays compatible with its normalized100 contract. `advance_risk_state(state: RiskState, observation: RiskObservation, *, config: RiskConfig) -> RiskState` receives immutable normalized facts: `now: datetime`, `equity: float`, `closed_trades: tuple[ClosedTradeObservation, ...]` in full completed-trade order, `volatility_ratio: float`, `volatility_bar_valid: bool`, and `system_healthy: bool`. `ClosedTradeObservation` contains `net_pnl: float` and `exit_time: datetime`; timestamps are needed to count profitable trades after a streak halt. Trade index validation prevents skipped/double counts; the core owns date baselines, rolling history, cooldown starts and loss/volatility progression. Historical v1 replay remains isolated. Health-only same-bar followups must not count a second volatility recovery bar or append duplicate equity history; retain the existing projection/replay contract.

`core.models.PositionContext` holds `entry_price`, `initial_stop`, `current_stop`, `high_water`, `quantity` (floats), `held_bars` (int). `quantity` is the factual currently owned BTC and is needed to return a full-exit quantity. `DecisionInput` holds `row: Mapping[str, object]`, `cash: float`, `equity: float`, `position: PositionContext | None`, `has_pending_order: bool`, and `risk: RiskDecision`. `Decision` holds `action: Literal['hold','buy','sell']`, `reason: str | None`, `quantity: float`, `next_stop: float | None`, and `risk: RiskDecision`.

`StrategyEngine(strategy: StrategyConfig, costs: CostConfig)` exposes `decide(snapshot: DecisionInput) -> Decision`, `initial_stop(entry_price: float, entry_atr: float) -> float`, and `protect(position: PositionContext, observed_price: float) -> bool` (true iff the active stop is breached; invalid inputs fail closed). Verify actual config type names in `config.py` before creating the API; if current cost class has a different name, use that existing class and record the concrete type in the report and ledger rather than inventing a second configuration.

- [ ] Add RED tests for a hand-computed flat entry, close exit, forced exit, 59/60 stagnant boundary, 1094/1095 max-hold boundary, monotone next-stop/next-bar activation, pending-order suppression, invalid snapshot, and shared risk reducer chronology. Representative exit assertion:

```python
from dataclasses import replace

def assert_stagnant_boundary(engine, healthy_long_snapshot):
    before = replace(healthy_long_snapshot,
        position=replace(healthy_long_snapshot.position, held_bars=59))
    after = replace(before, position=replace(before.position, held_bars=60))
    assert engine.decide(before).action == 'hold'
    assert engine.decide(after).action == 'sell'
    assert engine.decide(after).reason == 'stagnant_exit'
```

The fixture uses high_water below entry+1R, no close exit, no risk halt, and a valid completed indicator row; assert actual current public reason spelling if compatibility requires it, documenting that mapping. Independently compute expected position sizing with a small hand-checkable input rather than comparing only production helpers against themselves.
- [ ] Run the new unit suite to RED. Then move policy to the core, calling existing authoritative entry/exit/trailing/sizing functions; the core must contain no I/O. Normalize invalid/NaN inputs to safe no-new-entry behavior consistent with existing contracts.
- [ ] Migrate Backtrader and PaperService to `StrategyEngine.decide` and `advance_risk_state`. Remove their duplicated exit/risk logic; retain thin compatibility wrappers only where tests/external interfaces require them. Initial-stop/holding thresholds must have one current implementation. Keep public facts vs policy distinct: adapters report completed fills, current active stops, and completed-bar holding age, not policy conclusions.
- [ ] For each real adapter, inject a recording/decorating engine and show its returned intent is what gets submitted. One test alters only the common engine result and observes both adapters change behavior. The engine must be called on actual processing paths, not a separate test-only facade.
- [ ] Verify long-hold rolling history, next-bar stop, partial fills/cancelled stops, actual-open caps, rollback-resistant health, stale status, old event ancestry and v1→v2 replay. Preserve fail-closed ledger parsing while relocating reducer state.
- [ ] Run unit/shared-mode/affected backtest and paper suites, including focused golden/causality coverage proportionate to changes. Record exact commands/counts/exits, self-review, explicitly stage/commit `refactor: share trading decisions and risk state across modes`. Report exported constructors/type fields and the adapter normalization contract for Task 3. No full-suite run here.

### Task 3: Implement isolated locked Upbit execution and durable reconciliation

**Files:** Create `src/autobit/live/__init__.py`, `guard.py`, `client.py`, `journal.py`, `service.py`; create `tests/unit/test_live_client.py`, `tests/integration/test_locked_live_service.py`, `tests/safety/test_live_boundary.py`; modify `tests/safety/test_no_live_surface.py` and its scanner implementation only to enforce the approved replacement boundary, plus user documentation.

**Interfaces:** Consume the exact `StrategyEngine`, `DecisionInput`, `Decision`, `RiskState`, `RiskObservation`, and `advance_risk_state` produced by Task 2; read its interface report. `guard.require_live_authorization() -> None` raises `LiveTradingLockedError` unconditionally in production. `LiveClient` provides `accounts()`, `order_chance(market: str)`, `place_market_buy(identifier: str, amount_krw: Decimal)`, `place_market_sell(identifier: str, volume_btc: Decimal)`, `get_order(identifier: str)`, `cancel_order(identifier: str)` with validated typed results. Private request creation and credentials supplier access occur only after the guard. Accept a supplied httpx client/transport for tests without exposing a production unlock flag or arbitrary network destination.

`LiveService.process_completed_candle(snapshot: DecisionInput, observation: RiskObservation) -> Decision` calls the same core, reconciles factual balances/order state and submits only permitted actual intents through the locked client. `on_price(observed_price: float) -> None` applies the common active-stop rule; `reconcile() -> None` reconciles outstanding durable identifiers. A live-only SQLite journal identifies environment/market and rejects paper/normalized books. Internal response/journal types may be focused immutable dataclasses; record their exact fields in the report for Task 4.

- [ ] Read official Upbit authentication, create-order, get-order, cancel-order, accounts, and order-chance docs. Record exact official URLs in the report. Do not call private endpoints, inspect secrets, or use a real API key. Verify paths and market-buy/market-sell semantics before writing request tests.
- [ ] Add RED fixed-lock tests proving no credential supplier/signing/HTTP/journal mutation is reached when locked, even if environment/CLI values attempt activation. Example:

```python
import pytest
from autobit.live.guard import LiveTradingLockedError, require_live_authorization

def test_release_has_no_live_unlock(monkeypatch):
    monkeypatch.setenv('AUTOBIT_LIVE_ENABLED', 'true')
    with pytest.raises(LiveTradingLockedError):
        require_live_authorization()
```

- [ ] Implement the fixed guard first. In test files only, monkeypatch the guard and inject `httpx.MockTransport` to exercise the real connector. Use HS512, unique nonces, ordered non-escaped query SHA512, strict Decimal serialization; deny nonofficial host/path, redirect, insecure TLS and environment proxy inheritance. Do not leak tokens/keys in repr/errors/logs. Check official error payloads without returning arbitrary sensitive response bodies.
- [ ] Add RED tests decoding fake JWTs and independently verifying signature/hash/nonce, exact buy/sell fields and omission rules, identifier query/cancel, malformed/negative/nonfinite/foreign-market/mismatched-order responses, HTTP failures and timeout. Implement only the allowed authenticated endpoints; all enter through the gate.
- [ ] Implement durable intent-before-send and transactional single-writer ownership. A timeout or restart with an unresolved identifier must query that identifier and never blindly create a new order. Handle accepted/partial/completed/cancelled/zero-fill states with cumulative fill/fee monotonicity, reconciliation against actual balances, and no selling more BTC than owned. Unknown or contradictory state prevents new exposure and retries reconciliation automatically.
- [ ] Add tests for crash before/after send, timeout followed by found order, not-found ambiguity, partial fill plus cancel, restart with pending order, repeated reconciliation, two simultaneous service instances, locked startup, actual KRW versus normalized100, current exchange minimum/available funds, and health failure/recovery. Real-price protection cannot use historical candle lows as proof of actual execution.
- [ ] Integrate `LiveService` with the actual shared engine/reducer. Offline end-to-end trace must include a common decision, persisted intent, mocked venue acknowledgement/fill and resumed service using that actual fill. Test `on_price` current-stop and next-bar activation timing without claiming venue-native stop support.
- [ ] Replace the blanket no-private-code safety guarantee atomically with a narrow live boundary plus fixed gate. Preserve existing adversarial scanner tests outside live and add checks that backtest/paper/core cannot import/reach authenticated live I/O. Do not globally allow private symbols, arbitrary URLs or exclude all new code from safety coverage.
- [ ] Run live/safety plus affected regression tests, retain exact evidence, self-review and commit `feat: add locked Upbit execution with durable reconciliation`. Report limitations truthfully; stubs or mocked fills in production do not count as completed live implementation. No full suite and no private network execution.

### Task 4: Cross-mode acceptance and operational documentation

**Files:** Create `tests/integration/test_three_mode_contract.py`, `docs/runbooks/locked-live.md`; modify `README.md`, existing CLI module only for locked status/clear safety messaging, shared-mode test fixtures and directly affected documentation. Do not add an activation switch.

**Interfaces:** All three adapters consume Task 2's core and Task 3's live interfaces unchanged. Existing seven CLI commands and help contracts remain; new status command, if added, is read-only and always says live locked. Existing backtest/paper commands remain usable without importing or loading credentials from live.

- [ ] Add RED acceptance using one hand-checked normalized snapshot and the three real adapter paths, collecting submitted intentions before mode-specific execution differences. Assert exact direction/reason/size/next-stop/risk and distinguish actual versus simulated fills. The fixture must not seed impossible production state merely to bypass checks.
- [ ] Add a shared-policy mutation test with `monkeypatch` or an injected common engine decorator: change one current decision rule and show all three paths follow it. Include normal entry, close exit, delayed trailing update, post-72-hour reduced recovery, and another active independent halt. Do not accept three facades calling the same unused helper as parity proof.
- [ ] Check documentation against code: explain one-place editing, restart/version behavior, immutable historical v1 replay, mode-specific executions, normalized100 vs realKRW, fixed live lock, unresolved-order reconciliation, and unperformed performance/observation work. Minimal lock behavior in operational examples:

```python
from autobit.live.guard import LiveTradingLockedError, require_live_authorization

try:
    require_live_authorization()
except LiveTradingLockedError:
    print('Live trading is locked; paper and backtest remain available.')
```

- [ ] Run the acceptance/safety/CLI tests and compile checks; record outputs. Self-review and commit `test: verify shared policy across all execution modes`. Return a requirement-to-test checklist and explicit unresolved limitations.
- [ ] Controller freezes source after task review, records HEAD/diff/command/start, then runs `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` once with retained stdout/stderr/exit/end. Do not infer success from progress dots or historical results. Investigate real failures with a scoped fix/review rather than launch duplicate full runs.
- [ ] Controller requests one final whole-change review using the new plan's base `d958800` and the adjacent existing contracts it affects. Record all findings/fixes/rulings. No merge/push/live activation; hand off only what the actual verification proves.

## Self-review and execution notes

The user explicitly requested documentation followed by implementation, so execute in this session without asking again which execution method to use. Use an implementation worker and an independent reviewer per task as required by subagent-driven-development. Helpers are Bash scripts; if Windows drive handling fails, create equivalent plan-owned artifacts with native PowerShell/apply_patch and record the fallback. Review interfaces on each producer report before dispatching its consumer; settle internal naming differences in the ledger without expanding approved behavior.
