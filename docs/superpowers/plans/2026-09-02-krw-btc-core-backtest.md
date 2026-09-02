# KRW-BTC Core and Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a safe, deterministic Python package that collects and validates Upbit KRW-BTC 4-hour candles, computes the approved trend strategy, applies sizing and risk rules, and runs a cost-aware Backtrader simulation.

**Architecture:** Pure domain, strategy, sizing, and risk functions live under `src/autobit` and do not know about Backtrader or HTTP. Public market data and Backtrader are adapters around those functions. The first deliverable contains no private Upbit client, API-key loading, or live-order method.

**Tech Stack:** Python 3.12.13, pandas 2.2+, NumPy 2+, pandas-ta 0.4.71b0, Backtrader 1.9.78.123, SciPy 1.15+, httpx 0.28+, pytest 9+, pytest-cov 6+

**Spec:** `docs/superpowers/specs/2026-09-02-krw-btc-trend-system-design.md`

## Global Constraints

- Only Upbit Korea `KRW-BTC` spot is permitted.
- The portfolio may be long one BTC position or hold cash; leverage, shorts, pyramiding, and averaging down are forbidden.
- Initial equity is normalized to `100`; no real KRW is used in this plan.
- Signals use completed 240-minute candles; normal signal execution occurs at the next candle open.
- Default parameters are EMA 200, entry Donchian 50, exit Donchian 20, ATR 14, initial stop 2.5 ATR, and trailing stop 3 ATR after +2R.
- Default fee is 0.05% per side; slippage scenarios are 0%, 0.05%, 0.10%, and 0.20% per side.
- No module may accept Upbit access keys or call private account/order endpoints.
- Every task follows red-green-refactor TDD and ends with a focused commit.

---

## File Map Locked by This Plan

```text
pyproject.toml                         Package metadata and dependency bounds
.gitignore                             Generated-data and virtualenv exclusions
src/autobit/__init__.py                Package version only
src/autobit/config.py                  Immutable strategy, risk, cost, and data configs
src/autobit/domain/models.py           Enums and immutable event/value objects
src/autobit/domain/state_machine.py    Allowed lifecycle transitions
src/autobit/data/quality.py            Canonical OHLCV and quality checks
src/autobit/data/upbit_public.py        Public candle HTTP adapter only
src/autobit/data/collector.py           Pagination and collection checkpoint logic
src/autobit/data/storage.py             Atomic raw/processed snapshot writes
src/autobit/indicators/trend.py         EMA, Donchian, and ATR columns
src/autobit/strategy/donchian_trend.py  Pure entry and exit decisions
src/autobit/risk/breakers.py            Drawdown, streak, volatility, and recovery state
src/autobit/risk/position_sizer.py      Fee/slippage-aware conservative quantity
src/autobit/execution/backtest_broker.py Backtrader strategy and execution adapter
src/autobit/backtest/engine.py          Scenario orchestration
src/autobit/backtest/analyzers.py       Metrics and trade ledger extraction
src/autobit/backtest/benchmark.py       Cost-aware buy-and-hold
src/autobit/reporting/reports.py        JSON/CSV result serialization
src/autobit/cli.py                      Safe offline command surface
tests/unit/                             Pure-function tests
tests/integration/                      Adapter and timing tests
tests/regression/                       Golden deterministic run
```

### Task 1: Package Bootstrap and Dependency Compatibility Fence

**Files:**
- Create: `pyproject.toml`
- Create: `src/autobit/__init__.py`
- Create: `tests/integration/test_dependency_compat.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: bundled CPython 3.12.13 or any CPython `>=3.12,<3.13`
- Produces: importable package `autobit` with `__version__ == "0.1.0"`

- [ ] **Step 1: Write the failing compatibility test**

```python
from __future__ import annotations

import sys


def test_supported_python_and_core_imports() -> None:
    assert sys.version_info[:2] == (3, 12)
    import backtrader
    import httpx
    import numpy
    import pandas
    import pandas_ta
    import scipy

    assert backtrader.__version__ == "1.9.78.123"
    assert httpx.__version__
    assert numpy.__version__
    assert pandas.__version__
    pandas_ta_version = getattr(pandas_ta, "__version__", getattr(pandas_ta, "version", ""))
    assert pandas_ta_version == "0.4.71b0"
    assert scipy.__version__


def test_package_version() -> None:
    import autobit

    assert autobit.__version__ == "0.1.0"
```

- [ ] **Step 2: Run the test and verify that the missing environment/package fails**

Run:

```powershell
& 'C:\Users\boxma\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest tests/integration/test_dependency_compat.py -v
```

Expected: FAIL because pytest or the project dependencies are not installed.

- [ ] **Step 3: Add the package metadata and create the project environment**

Use this `pyproject.toml` dependency contract:

```toml
[build-system]
requires = ["setuptools>=75", "wheel>=0.45"]
build-backend = "setuptools.build_meta"

[project]
name = "autobit"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "backtrader==1.9.78.123",
  "httpx>=0.28,<1",
  "matplotlib>=3.9,<4",
  "numpy>=2,<3",
  "pandas>=2.2,<3",
  "pandas-ta==0.4.71b0",
  "scipy>=1.15,<2",
]

[project.optional-dependencies]
dev = [
  "pytest>=9,<10",
  "pytest-cov>=6,<8",
]

[project.scripts]
autobit = "autobit.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
addopts = "-ra --strict-markers"
testpaths = ["tests"]
```

Use this package initializer:

```python
__version__ = "0.1.0"
```

Create and install into `.venv`:

```powershell
& 'C:\Users\boxma\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install --upgrade pip
& '.\.venv\Scripts\python.exe' -m pip install --pre -e '.[dev]'
```

Append `.venv/`, `data/raw/`, `data/processed/`, `reports/`, `*.sqlite3`, and `*.tmp` to `.gitignore` while preserving `.env`.

- [ ] **Step 4: Run the compatibility test**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/integration/test_dependency_compat.py -v`

Expected: 2 tests PASS.

- [ ] **Step 5: Commit the bootstrap**

```powershell
git add pyproject.toml .gitignore src/autobit/__init__.py tests/integration/test_dependency_compat.py
git commit -m "build: bootstrap autobit research package"
```

### Task 2: Configuration, Domain Models, and State Machine

**Files:**
- Create: `src/autobit/config.py`
- Create: `src/autobit/domain/models.py`
- Create: `src/autobit/domain/state_machine.py`
- Create: `tests/unit/test_state_machine.py`
- Create: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: no earlier runtime objects
- Produces: `StrategyConfig`, `CostConfig`, `RiskConfig`, `DataConfig`, `ExchangeRulesConfig`, `PositionState`, `OrderStatus`, `OrderEvent`, `transition_state`

- [ ] **Step 1: Write failing config and transition tests**

```python
from dataclasses import FrozenInstanceError

import pytest

from autobit.config import CostConfig, DataConfig, ExchangeRulesConfig, RiskConfig, StrategyConfig
from autobit.domain.models import OrderStatus, PositionState
from autobit.domain.state_machine import InvalidTransition, transition_state


def test_approved_defaults_are_immutable() -> None:
    strategy = StrategyConfig()
    assert (strategy.ema_period, strategy.entry_period, strategy.exit_period) == (200, 50, 20)
    assert (strategy.atr_period, strategy.initial_atr_mult, strategy.trailing_atr_mult) == (14, 2.5, 3.0)
    assert CostConfig().fee_rate == pytest.approx(0.0005)
    assert RiskConfig().base_risk_rate == pytest.approx(0.02)
    assert DataConfig().market == "KRW-BTC"
    rules = ExchangeRulesConfig()
    assert rules.min_order_krw == 5_000
    assert rules.krw_btc_tick_size == 1_000
    assert rules.checked_at_utc == "2026-09-02T00:00:00Z"
    with pytest.raises(FrozenInstanceError):
        strategy.ema_period = 20


def test_only_allowed_position_transitions_succeed() -> None:
    assert transition_state(PositionState.FLAT, OrderStatus.SUBMITTED) is PositionState.ENTRY_PENDING
    assert transition_state(PositionState.ENTRY_PENDING, OrderStatus.COMPLETED) is PositionState.LONG
    assert transition_state(PositionState.LONG, OrderStatus.SUBMITTED) is PositionState.EXIT_PENDING
    assert transition_state(PositionState.EXIT_PENDING, OrderStatus.COMPLETED) is PositionState.FLAT
    with pytest.raises(InvalidTransition):
        transition_state(PositionState.FLAT, OrderStatus.COMPLETED)
```

- [ ] **Step 2: Run the tests and verify missing imports fail**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_config.py tests/unit/test_state_machine.py -v`

Expected: collection FAIL because the modules do not exist.

- [ ] **Step 3: Implement immutable configs and explicit transitions**

Create frozen dataclasses with these exact defaults:

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    ema_period: int = 200
    entry_period: int = 50
    exit_period: int = 20
    atr_period: int = 14
    initial_atr_mult: float = 2.5
    profit_activation_r: float = 2.0
    trailing_atr_mult: float = 3.0
    stagnant_bars: int = 60
    stagnant_min_r: float = 1.0
    max_holding_bars: int = 1095
    warmup_bars: int = 600


@dataclass(frozen=True, slots=True)
class CostConfig:
    fee_rate: float = 0.0005
    slippage_rate: float = 0.0005


@dataclass(frozen=True, slots=True)
class RiskConfig:
    base_risk_rate: float = 0.02
    max_exposure: float = 0.70
    hard_drawdown: float = 0.15
    daily_loss_limit: float = 0.04
    weekly_reduce_limit: float = 0.05
    weekly_halt_limit: float = 0.07


@dataclass(frozen=True, slots=True)
class DataConfig:
    market: str = "KRW-BTC"
    candle_unit_minutes: int = 240
    page_size: int = 200
    years: int = 7


@dataclass(frozen=True, slots=True)
class ExchangeRulesConfig:
    fee_rate: float = 0.0005
    min_order_krw: int = 5_000
    krw_btc_tick_size: int = 1_000
    checked_at_utc: str = "2026-09-02T00:00:00Z"
    candle_source_url: str = "https://docs.upbit.com/kr/reference/list-candles-minutes"
    order_policy_source_url: str = "https://docs.upbit.com/kr/docs/krw-market-info"
    fee_source_url: str = "https://docs.upbit.com/kr/docs/upbit-strategy-toolkit-reference"
```

Define enum values exactly as the spec names them. Implement `transition_state` with an explicit mapping for entry and exit lifecycle events; cancellations and rejections from `ENTRY_PENDING` return `FLAT`, while partial entry becomes `LONG` with actual filled quantity managed by the broker.

- [ ] **Step 4: Run the focused tests**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_config.py tests/unit/test_state_machine.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit domain primitives**

```powershell
git add src/autobit/config.py src/autobit/domain tests/unit/test_config.py tests/unit/test_state_machine.py
git commit -m "feat: define immutable trading domain"
```

### Task 3: Canonical OHLCV and Data Quality Pipeline

**Files:**
- Create: `src/autobit/data/quality.py`
- Create: `tests/unit/test_data_quality.py`

**Interfaces:**
- Consumes: raw `pandas.DataFrame`, expected frequency `4h`
- Produces: `QualityResult(frame: DataFrame, report: QualityReport)` via `canonicalize_ohlcv(raw, now_utc)`

- [ ] **Step 1: Write failing data-quality tests**

```python
from datetime import datetime, timezone

import pandas as pd
import pytest

from autobit.data.quality import canonicalize_ohlcv


def make_frame(times: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {"open": [100.0] * len(times), "high": [102.0] * len(times), "low": [99.0] * len(times),
         "close": [101.0] * len(times), "volume": [2.0] * len(times)},
        index=pd.to_datetime(times, utc=True),
    )


def test_single_gap_is_flat_filled_and_flagged() -> None:
    raw = make_frame(["2026-01-01T00:00:00Z", "2026-01-01T08:00:00Z"])
    result = canonicalize_ohlcv(raw, datetime(2026, 1, 2, tzinfo=timezone.utc))
    filled = result.frame.loc[pd.Timestamp("2026-01-01T04:00:00Z")]
    assert filled[["open", "high", "low", "close"]].tolist() == [101.0] * 4
    assert filled["volume"] == 0.0
    assert bool(filled["is_filled"])
    assert result.report.short_gap_bars == 1


def test_long_gap_is_not_filled() -> None:
    raw = make_frame(["2026-01-01T00:00:00Z", "2026-01-01T12:00:00Z"])
    result = canonicalize_ohlcv(raw, datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert pd.isna(result.frame.loc[pd.Timestamp("2026-01-01T04:00:00Z"), "close"])
    assert result.report.long_gap_regions == 1


def test_impossible_candle_is_quarantined_and_reported() -> None:
    raw = make_frame(["2026-01-01T00:00:00Z"])
    raw.loc[:, "high"] = 98.0
    result = canonicalize_ohlcv(raw, datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert result.report.impossible_candles == 1
    assert pd.isna(result.frame.iloc[0]["close"])
    assert not bool(result.frame.iloc[0]["entry_data_valid"])
```

- [ ] **Step 2: Run the focused test and verify failure**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_data_quality.py -v`

Expected: collection FAIL because `autobit.data.quality` does not exist.

- [ ] **Step 3: Implement canonicalization and the report object**

Implement `QualityReport` as a frozen dataclass with integer fields for total bars, duplicates, conflicting duplicates, short-gap bars, long-gap regions, impossible candles, nonpositive prices, negative volume, zero volume, spike flags, and removed partial bars. Implement `canonicalize_ohlcv` in this exact order:

```python
def canonicalize_ohlcv(raw: pd.DataFrame, now_utc: datetime) -> QualityResult:
    frame = raw.copy()
    frame.columns = frame.columns.str.lower().str.strip()
    frame.index = pd.to_datetime(frame.index, utc=True)
    frame = frame.sort_index()
    frame = _deduplicate_or_raise(frame)
    _coerce_float64(frame)
    frame = _quarantine_invalid_values(frame)
    frame = _drop_unclosed_last_bar(frame, now_utc, pd.Timedelta(hours=4))
    frame = _reindex_and_fill_single_gaps(frame, pd.Timedelta(hours=4))
    frame = _flag_spikes_and_flat_bars(frame)
    report = _build_quality_report(frame)
    return QualityResult(frame=frame, report=report)
```

Use `is_filled`, `anomaly_spike`, `anomaly_flat`, `segment_id`, and `entry_data_valid`. Quarantined invalid rows keep their timestamp and flags but set OHLCV to NaN so they cannot create a signal. `entry_data_valid` is false when the preceding 200 rows within the same segment contain a filled, quarantined, or unverified anomaly row. A long gap increments `segment_id` and leaves missing OHLC values as NaN.

- [ ] **Step 4: Run quality tests and coverage**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_data_quality.py --cov=autobit.data.quality --cov-report=term-missing -v`

Expected: all tests PASS and the invalid-value, single-gap, and long-gap branches are covered.

- [ ] **Step 5: Commit the quality pipeline**

```powershell
git add src/autobit/data/quality.py tests/unit/test_data_quality.py
git commit -m "feat: add canonical OHLCV quality pipeline"
```

### Task 4: Public Upbit Collector and Atomic Storage

**Files:**
- Create: `src/autobit/data/upbit_public.py`
- Create: `src/autobit/data/collector.py`
- Create: `src/autobit/data/storage.py`
- Create: `tests/integration/test_upbit_collector.py`
- Create: `tests/unit/test_storage.py`

**Interfaces:**
- Consumes: `DataConfig`, `httpx.Client`, UTC start/end
- Produces: `UpbitPublicClient.fetch_page(to_utc: str) -> list[dict[str, object]]`, `collect_range(client: UpbitPublicClient, start_utc: str, end_utc: str) -> DataFrame`, `save_snapshot(root: Path, payload: list[dict[str, object]]) -> SnapshotManifest`

- [ ] **Step 1: Write failing pagination and atomic-write tests**

```python
import json
from pathlib import Path

import httpx

from autobit.config import DataConfig
from autobit.data.collector import collect_range
from autobit.data.storage import save_json_snapshot
from autobit.data.upbit_public import UpbitPublicClient


def test_client_uses_only_public_candle_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    client = UpbitPublicClient(httpx.Client(transport=httpx.MockTransport(handler)), DataConfig())
    assert client.fetch_page("2026-01-01T00:00:00Z") == []
    assert seen[0].url.path == "/v1/candles/minutes/240"
    assert seen[0].url.params["market"] == "KRW-BTC"
    assert "Authorization" not in seen[0].headers


def test_snapshot_write_is_content_addressed(tmp_path: Path) -> None:
    manifest = save_json_snapshot(tmp_path, [{"market": "KRW-BTC", "trade_price": 100.0}])
    assert manifest.path.exists()
    assert manifest.sha256 in manifest.path.name
    assert json.loads(manifest.path.read_text(encoding="utf-8"))[0]["market"] == "KRW-BTC"
    assert not list(tmp_path.glob("*.tmp"))
```

- [ ] **Step 2: Run tests and verify missing-module failures**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/integration/test_upbit_collector.py tests/unit/test_storage.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement the public adapter, retry contract, pagination, and storage**

The adapter must construct only `GET https://api.upbit.com/v1/candles/minutes/240`, send `market`, `to`, and `count`, parse `Remaining-Req`, and retry transport/429/5xx responses after 1, 2, and 4 seconds. After three failures it raises `PublicDataUnavailable`.

Pagination must move `to` to the oldest returned `candle_date_time_utc`, stop before `start_utc`, deduplicate overlaps, and write a checkpoint containing the oldest successfully stored timestamp and raw snapshot hash.

Atomic storage must use a sibling `.tmp` path, flush and close it, then replace the destination. The manifest fields are `path`, `sha256`, `row_count`, `created_at_utc`, `source_url`, and `config_hash`.

- [ ] **Step 4: Run adapter tests without network access**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/integration/test_upbit_collector.py tests/unit/test_storage.py -v`

Expected: all tests PASS and MockTransport records no Authorization header.

- [ ] **Step 5: Commit the public data adapter**

```powershell
git add src/autobit/data tests/integration/test_upbit_collector.py tests/unit/test_storage.py
git commit -m "feat: collect public Upbit candle snapshots"
```

### Task 5: Indicators and Pure Donchian Strategy

**Files:**
- Create: `src/autobit/indicators/trend.py`
- Create: `src/autobit/strategy/donchian_trend.py`
- Create: `tests/unit/test_indicators.py`
- Create: `tests/unit/test_strategy.py`
- Create: `tests/regression/test_indicator_prefix_stability.py`

**Interfaces:**
- Consumes: canonical OHLCV and `StrategyConfig`
- Produces: `compute_trend_indicators(frame, config) -> DataFrame`, `evaluate_entry(row, state) -> bool`, `evaluate_close_exit(row) -> bool`, `next_stop(position, row) -> float`

- [ ] **Step 1: Write failing lookahead and signal tests**

```python
import numpy as np
import pandas as pd

from autobit.config import StrategyConfig
from autobit.indicators.trend import compute_trend_indicators
from autobit.strategy.donchian_trend import evaluate_entry


def test_entry_channel_excludes_current_high() -> None:
    index = pd.date_range("2025-01-01", periods=650, freq="4h", tz="UTC")
    close = np.linspace(100.0, 200.0, 650)
    frame = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 10.0}, index=index)
    frame.loc[index[-1], ["high", "close"]] = [500.0, 250.0]
    enriched = compute_trend_indicators(frame, StrategyConfig())
    assert enriched.loc[index[-1], "entry_high"] < enriched.loc[index[-1], "high"]
    assert evaluate_entry(enriched.iloc[-1], is_flat=True)


def test_nan_or_invalid_data_never_enters() -> None:
    row = pd.Series({"close": 101.0, "ema_200": np.nan, "entry_high": 100.0,
                     "previous_close": 99.0, "previous_entry_high": 100.0,
                     "atr_14": 2.0, "entry_data_valid": True})
    assert not evaluate_entry(row, is_flat=True)
    row["ema_200"] = 90.0
    row["entry_data_valid"] = False
    assert not evaluate_entry(row, is_flat=True)
```

- [ ] **Step 2: Run the tests and verify failure**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_indicators.py tests/unit/test_strategy.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement stable indicators and Boolean strategy rules**

Use pandas-ta for EMA and ATR, and explicit shifted rolling windows for Donchian:

```python
def compute_trend_indicators(frame: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    result = frame.copy()
    result[f"ema_{config.ema_period}"] = result.ta.ema(length=config.ema_period)
    result[f"atr_{config.atr_period}"] = result.ta.atr(length=config.atr_period)
    result["entry_high"] = result["high"].shift(1).rolling(config.entry_period).max()
    result["exit_low"] = result["low"].shift(1).rolling(config.exit_period).min()
    result["previous_close"] = result["close"].shift(1)
    result["previous_entry_high"] = result["entry_high"].shift(1)
    result["warmup_complete"] = result.groupby("segment_id").cumcount() >= config.warmup_bars
    return result
```

`evaluate_entry` requires flat state, warmup complete, entry data valid, all finite required values, current close above entry high and EMA, and previous close at or below previous entry high. `evaluate_close_exit` is `close < exit_low`. `next_stop` activates only after high-water reaches +2R and returns the maximum of the prior stop, entry, and high-water minus 3 ATR.

- [ ] **Step 4: Run signal and prefix-stability tests**

The prefix regression test computes indicators over 650 rows and over the first 620 rows, then uses `pandas.testing.assert_series_equal` on the shared EMA, ATR, entry-high, and exit-low values after NaN normalization.

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_indicators.py tests/unit/test_strategy.py tests/regression/test_indicator_prefix_stability.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit the strategy core**

```powershell
git add src/autobit/indicators src/autobit/strategy tests/unit/test_indicators.py tests/unit/test_strategy.py tests/regression/test_indicator_prefix_stability.py
git commit -m "feat: implement approved Donchian trend signals"
```

### Task 6: Position Sizing and Automatic Risk State

**Files:**
- Create: `src/autobit/risk/position_sizer.py`
- Create: `src/autobit/risk/breakers.py`
- Create: `tests/unit/test_position_sizer.py`
- Create: `tests/unit/test_breakers.py`

**Interfaces:**
- Consumes: equity, cash, expected entry/stop, ATR baseline/current, cost config, risk snapshot
- Produces: `SizeDecision(quantity, binding_constraint, estimated_loss)`, `RiskDecision(risk_rate, exposure_cap, halted_until, reasons)`

- [ ] **Step 1: Write failing sizing and recovery tests**

```python
from datetime import datetime, timedelta, timezone

import pytest

from autobit.config import CostConfig, RiskConfig
from autobit.risk.breakers import evaluate_risk
from autobit.risk.position_sizer import calculate_size


def test_size_never_exceeds_two_units_of_loss_or_seventy_exposure() -> None:
    decision = calculate_size(equity=100.0, cash=100.0, entry=100.0, stop=90.0,
                              current_atr_pct=0.02, baseline_atr_pct=0.02,
                              risk_rate=0.02, exposure_cap=0.70, costs=CostConfig())
    assert decision.estimated_loss <= 2.01
    assert decision.quantity * 100.0 <= 70.0


@pytest.mark.parametrize("entry,stop,current_atr", [(100.0, 100.0, 0.02), (100.0, 90.0, 0.0)])
def test_invalid_risk_inputs_return_zero(entry: float, stop: float, current_atr: float) -> None:
    decision = calculate_size(equity=100.0, cash=100.0, entry=entry, stop=stop,
                              current_atr_pct=current_atr, baseline_atr_pct=0.02,
                              risk_rate=0.02, exposure_cap=0.70, costs=CostConfig())
    assert decision.quantity == 0.0


def test_fifteen_percent_drawdown_cools_then_auto_recovers_small() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    halted = evaluate_risk(now=now, drawdown=0.15, daily_loss=0.0, weekly_loss=0.0,
                           consecutive_losses=0, volatility_ratio=1.0, system_healthy=True,
                           config=RiskConfig())
    assert halted.risk_rate == 0.0
    recovered = evaluate_risk(now=now + timedelta(hours=73), drawdown=0.14, daily_loss=0.0,
                              weekly_loss=0.0, consecutive_losses=0, volatility_ratio=1.0,
                              system_healthy=True, config=RiskConfig(), recovery_started_at=now)
    assert recovered.risk_rate == pytest.approx(0.0025)
    assert recovered.exposure_cap == pytest.approx(0.15)
```

- [ ] **Step 2: Run tests and verify failure**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_position_sizer.py tests/unit/test_breakers.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement the conservative minimum ladder and breaker precedence**

`calculate_size` must compute fee/slippage-adjusted loss per BTC, fixed-risk quantity, the volatility-reduced quantity `fixed * min(1, baseline/current)`, exposure quantity, and cash quantity. Return the minimum with its binding name. Never round upward.

`evaluate_risk` must return the most restrictive active rule. Encode exact drawdown tiers, daily 4%, weekly 5% reduction and 7% cooldown, 3/5 loss streak rules, 2x/3x volatility rules, and 72-hour drawdown recovery. System unhealthy always returns zero risk regardless of other inputs.

- [ ] **Step 4: Run all risk tests**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_position_sizer.py tests/unit/test_breakers.py -v`

Expected: all tests PASS, including boundary values at 5%, 10%, and 15% drawdown.

- [ ] **Step 5: Commit risk controls**

```powershell
git add src/autobit/risk tests/unit/test_position_sizer.py tests/unit/test_breakers.py
git commit -m "feat: enforce sizing and automatic risk recovery"
```

### Task 7: Event-Driven Backtrader Adapter

**Files:**
- Create: `src/autobit/execution/backtest_broker.py`
- Create: `src/autobit/backtest/engine.py`
- Create: `tests/integration/test_backtest_execution.py`
- Create: `tests/integration/test_backtest_costs.py`
- Create: `tests/integration/test_backtest_partial_fill.py`

**Interfaces:**
- Consumes: enriched DataFrame, `BacktestConfig(strategy, risk, costs, initial_equity=100)`
- Produces: `BacktestResult(equity_curve, orders, trades, final_equity, total_fees, total_slippage)` via `run_backtest`

- [ ] **Step 1: Write failing next-open and stop-order tests**

```python
import pandas as pd
import pytest

from autobit.backtest.engine import BacktestConfig, run_backtest
from autobit.config import CostConfig


def test_close_signal_fills_at_next_open() -> None:
    frame = pd.read_csv("tests/fixtures/entry_next_open.csv", parse_dates=["timestamp"], index_col="timestamp")
    result = run_backtest(frame, BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)))
    entry = next(order for order in result.orders if order.side == "BUY" and order.status == "COMPLETED")
    assert entry.signal_time == pd.Timestamp("2025-01-05T00:00:00Z")
    assert entry.fill_time == pd.Timestamp("2025-01-05T04:00:00Z")
    assert entry.fill_price == pytest.approx(105.0)


def test_gap_below_stop_fills_at_worse_open() -> None:
    frame = pd.read_csv("tests/fixtures/gap_stop.csv", parse_dates=["timestamp"], index_col="timestamp")
    result = run_backtest(frame, BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0)))
    stop = next(order for order in result.orders if order.reason == "HARD_STOP")
    assert stop.stop_price == pytest.approx(95.0)
    assert stop.fill_price == pytest.approx(90.0)
```

- [ ] **Step 2: Create deterministic fixtures and verify the tests fail**

Create fixture CSV files with 610 warmup rows plus the exact final bars that generate the asserted signal, next open, and gap. Keep indicator columns precomputed in the fixture so execution timing is isolated from indicator tests.

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/integration/test_backtest_execution.py tests/integration/test_backtest_costs.py -v`

Expected: collection FAIL because `run_backtest` is missing.

- [ ] **Step 3: Implement Backtrader without cheat modes**

Construct `bt.Cerebro(cheat_on_open=False)`, call `broker.set_coc(False)`, set normalized cash, install percentage commission, and use an explicit slippage model. Strategy `next()` may create a market entry only when no order is pending and position state is `FLAT`; it stores the signal timestamp and lets Backtrader fill at the next open. Hard/trailing stops use the stop active before the current bar. Update a new trailing stop only after the bar finishes.

Record every Created, Submitted, Accepted, Partial, Completed, Canceled, Expired, Margin, and Rejected callback into immutable order events. Reject negative cash, a second position, and sell size above the current BTC position.

- [ ] **Step 4: Run timing, gap, same-bar, and cost tests**

Add assertions that a bar touching both the old stop and +2R exits at the old stop, that 0.05% fee is charged on both sides, and that 0.05% buy slippage increases fill price while sell slippage decreases fill price. In `test_backtest_partial_fill.py`, inject a 50% entry fill and assert the remainder is canceled without retry; inject a 50% exit fill and assert the exact remainder is sold once at the following open.

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/integration/test_backtest_execution.py tests/integration/test_backtest_costs.py tests/integration/test_backtest_partial_fill.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit the event-driven adapter**

```powershell
git add src/autobit/execution src/autobit/backtest/engine.py tests/fixtures tests/integration/test_backtest_execution.py tests/integration/test_backtest_costs.py tests/integration/test_backtest_partial_fill.py
git commit -m "feat: add event-driven next-open backtester"
```

### Task 8: Metrics, Benchmark, Reports, Safe CLI, and Core Golden Test

**Files:**
- Create: `src/autobit/backtest/analyzers.py`
- Create: `src/autobit/backtest/benchmark.py`
- Create: `src/autobit/reporting/reports.py`
- Create: `src/autobit/cli.py`
- Create: `tests/unit/test_metrics.py`
- Create: `tests/integration/test_cli_backtest.py`
- Create: `tests/regression/test_core_golden.py`
- Create: `tests/safety/test_no_live_surface.py`

**Interfaces:**
- Consumes: `BacktestResult`, processed snapshot path, config objects
- Produces: `PerformanceMetrics`, buy-and-hold result, versioned JSON/CSV report bundle, commands `data-quality` and `backtest`

- [ ] **Step 1: Write failing metric and safety-surface tests**

```python
from pathlib import Path

import pytest

from autobit.backtest.analyzers import calculate_metrics


def test_profit_factor_and_expectancy_use_closed_trades() -> None:
    metrics = calculate_metrics([10.0, -4.0, 6.0, -2.0], periods_per_year=2190)
    assert metrics.profit_factor == pytest.approx(16.0 / 6.0)
    assert metrics.expectancy == pytest.approx(2.5)
    assert metrics.win_rate == pytest.approx(0.5)


def test_source_contains_no_private_upbit_or_live_order_surface() -> None:
    forbidden = ("buy_market_order", "sell_market_order", "UPBIT_ACCESS_KEY", "UPBIT_SECRET_KEY")
    source = "\n".join(path.read_text(encoding="utf-8") for path in Path("src/autobit").rglob("*.py"))
    for token in forbidden:
        assert token not in source
```

- [ ] **Step 2: Run the new tests and verify failure**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_metrics.py tests/safety/test_no_live_surface.py -v`

Expected: metric import FAIL.

- [ ] **Step 3: Implement metrics, benchmark, report manifest, and CLI**

`PerformanceMetrics` must contain total return, annualized return, Sharpe, Sortino, Calmar, maximum drawdown and duration, profit factor, expectancy, win rate, average win/loss and ratio, trade count, mean/median holding bars, exposure, turnover, total fees, and total slippage.

The benchmark invests normalized equity at the first tradable next open and liquidates at the last close under the same cost config. The report bundle writes `summary.json`, `trades.csv`, `orders.csv`, `equity.csv`, `quality.json`, and `manifest.json` with config/data/code hashes.

Use `argparse` subcommands. `data-download --output DIR --end-utc TIMESTAMP --years 7` runs the public collector and stores its raw snapshot, checkpoint, exchange-rule snapshot, and manifest. `data-quality --input PATH --output PATH` runs canonicalization and writes a quality report. `backtest --input PATH --output DIR --slippage RATE` runs one scenario. Do not define `live`, `buy`, `sell`, `order`, or credential arguments.

- [ ] **Step 4: Run the entire core suite and golden regression**

The golden test runs `tests/fixtures/core_golden.csv` with zero cost and asserts the complete ordered tuple of entry time, exit time, entry price, exit price, exit reason, and final equity stored in `tests/fixtures/core_golden_expected.json`.

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/unit tests/integration tests/regression tests/safety -v
& '.\.venv\Scripts\python.exe' -m autobit.cli --help
```

Expected: all tests PASS; CLI help lists only offline research commands.

- [ ] **Step 5: Commit the complete core backtest deliverable**

```powershell
git add src/autobit/backtest src/autobit/reporting src/autobit/cli.py tests
git commit -m "feat: report reproducible KRW-BTC backtests"
```

## Plan 1 Completion Gate

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest --cov=autobit --cov-report=term-missing -v
git status --short
```

Required result: all tests pass, no live-order surface exists, the golden run is deterministic, and the worktree contains only intentional generated files ignored by Git. Do not start Plan 2 until this gate passes.
