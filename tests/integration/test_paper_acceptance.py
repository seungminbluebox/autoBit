from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from autobit.cli import _PaperApplication
from autobit.config import CostConfig, StrategyConfig
from autobit.execution.paper_broker import PaperBroker
from autobit.indicators.trend import compute_trend_indicators
from autobit.paper.health import HealthMonitor, HealthStage
from autobit.persistence.sqlite_store import SQLiteStore
from autobit.risk.position_sizer import calculate_size


UTC = timezone.utc
_FOUR_HOURS = timedelta(hours=4)


@dataclass
class _Clock:
    value: datetime

    def now(self) -> datetime:
        return self.value


class _Sleeper:
    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def sleep(self, seconds: float) -> None:
        self._clock.value += timedelta(seconds=seconds)


class _SyntheticPublicCandleSource:
    def __init__(self, values: list[pd.DataFrame | BaseException]) -> None:
        self._values = iter(values)
        self.calls: list[datetime] = []

    def load_completed_candles(self, end_utc: datetime) -> pd.DataFrame:
        self.calls.append(end_utc)
        value = next(self._values)
        if isinstance(value, BaseException):
            raise value
        return value.copy(deep=True)


@dataclass(frozen=True)
class _AcceptanceResult:
    duplicate_orders: int
    negative_cash_events: int
    oversell_events: int
    halt_reasons: list[str]
    automatic_recoveries: int
    final_state: str
    replay_matches_live_state: bool
    entry_orders: int
    hard_stops: int
    stages: tuple[str, ...]
    volatility_size: float
    normal_size: float
    volatility_binding: str
    volatility_ratio: float


class _AcceptanceHarness:
    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def run(self, fixture_path: str) -> _AcceptanceResult:
        fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
        start = datetime.fromisoformat(fixture["start_end"].replace("Z", "+00:00"))
        restart_end = start + _FOUR_HOURS
        stop_end = restart_end + _FOUR_HOURS
        recovery_end = stop_end + _FOUR_HOURS
        clock = _Clock(start + timedelta(minutes=10))
        stop_history = _history(stop_end, hard_stop=True)
        source = _SyntheticPublicCandleSource(
            [
                _history(start, breakout=True),
                _history(restart_end),
                *[RuntimeError("synthetic public API outage") for _ in range(fixture["api_failures"])],
                stop_history,
                stop_history,
                stop_history,
                _history(recovery_end),
            ]
        )
        path = self._tmp_path / "paper.sqlite3"
        store = SQLiteStore(path)
        store.initialize(initial_equity=fixture["initial_equity"])
        first = _application(store, source, clock, "first")
        first.run_once()
        store.close()

        store = SQLiteStore(path)
        store.initialize(initial_equity=fixture["initial_equity"])
        restarted = _application(store, source, clock, "restarted")
        clock.value = restart_end + timedelta(minutes=10)
        restarted.run_once()

        for attempt in range(fixture["api_failures"]):
            with pytest.raises(RuntimeError, match="synthetic public API outage"):
                restarted.run_once()
            clock.value += timedelta(seconds=attempt + 1)
        assert HealthMonitor.from_store(store).current_action().stage is HealthStage.HALTED

        stages = [HealthMonitor.from_store(store).current_action().stage.value]
        for _ in range(fixture["api_successes"]):
            restarted.run_once()
            stages.append(HealthMonitor.from_store(store).current_action().stage.value)
        assert stages == ["HALTED", "HALTED", "HALTED", "REDUCED"]

        clock.value = recovery_end + timedelta(minutes=10)
        restarted.run_once()
        stages.append(HealthMonitor.from_store(store).current_action().stage.value)
        live = store.replay_state()
        broker = PaperBroker(store)
        reconciliation = broker.reconcile()
        reopened_state = _reopen(path, fixture["initial_equity"])

        orders = [
            event.payload["order_id"]
            for event in live.event_evidence
            if event.event_type == "PAPER_ORDER"
        ]
        fills = reconciliation.fills
        cash = fixture["initial_equity"]
        held = 0.0
        negative_cash_events = 0
        oversell_events = 0
        for fill in fills:
            if fill.side == "BUY":
                cash -= fill.quantity * fill.fill_price + fill.fee
                held += fill.quantity
            else:
                if fill.quantity > held + 1e-12:
                    oversell_events += 1
                held -= fill.quantity
                cash += fill.quantity * fill.fill_price - fill.fee
            if cash < -1e-12:
                negative_cash_events += 1

        risk_events = [
            event.payload
            for event in live.event_evidence
            if event.event_type == "BREAKER_STATE"
        ]
        reduced_risk = next(
            payload for payload in risk_events if payload["decision_reasons"] == ("health_recovery_reduced",)
        )
        stop_row = compute_trend_indicators(stop_history, StrategyConfig()).iloc[-1]
        current_atr_pct = float(stop_row["atr_14"] / stop_row["close"])
        baseline_atr_pct = float(stop_row["baseline_atr_pct"])
        volatility_size = calculate_size(
            equity=100.0,
            cash=100.0,
            entry=100.0,
            stop=95.0,
            current_atr_pct=current_atr_pct,
            baseline_atr_pct=baseline_atr_pct,
            risk_rate=float(reduced_risk["decision_risk_rate"]),
            exposure_cap=float(reduced_risk["decision_exposure_cap"]),
            costs=CostConfig(),
        )
        normal_size = calculate_size(
            equity=100.0,
            cash=100.0,
            entry=100.0,
            stop=95.0,
            current_atr_pct=baseline_atr_pct,
            baseline_atr_pct=baseline_atr_pct,
            risk_rate=0.02,
            exposure_cap=0.70,
            costs=CostConfig(),
        ).quantity
        halt_reasons = sorted(
            {
                reason
                for event in live.event_evidence
                if event.event_type == "HEALTH_STATE" and event.payload["stage"] == "HALTED"
                for reason in event.payload["reasons"]
            }
        )

        return _AcceptanceResult(
            duplicate_orders=len(orders) - len(set(orders)),
            negative_cash_events=negative_cash_events,
            oversell_events=oversell_events,
            halt_reasons=halt_reasons,
            automatic_recoveries=sum(
                prior == "REDUCED" and current == "NORMAL"
                for prior, current in zip(stages, stages[1:], strict=False)
            ),
            final_state=reconciliation.position_state.value,
            replay_matches_live_state=live == reopened_state,
            entry_orders=sum(fill.side == "BUY" for fill in fills),
            hard_stops=sum(fill.reason == "HARD_STOP" for fill in fills),
            stages=tuple(stages),
            volatility_size=volatility_size.quantity,
            normal_size=normal_size,
            volatility_binding=volatility_size.binding_constraint,
            volatility_ratio=current_atr_pct / baseline_atr_pct,
        )


def _application(
    store: SQLiteStore,
    source: _SyntheticPublicCandleSource,
    clock: _Clock,
    owner: str,
) -> _PaperApplication:
    return _PaperApplication(
        source=source,
        store=store,
        clock=clock,
        sleeper=_Sleeper(clock),
        notifier=None,
        lease_owner=owner,
        lease_token=f"{owner}-token",
    )


def _history(end: datetime, *, breakout: bool = False, hard_stop: bool = False) -> pd.DataFrame:
    index = pd.date_range(
        end=pd.Timestamp(end) - pd.Timedelta(hours=4),
        periods=601,
        freq="4h",
        tz="UTC",
    )
    frame = pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1.0,
        },
        index=index,
    )
    if breakout:
        frame.iloc[-1, frame.columns.get_loc("close")] = 102.0
        frame.iloc[-1, frame.columns.get_loc("high")] = 103.0
    if hard_stop:
        frame.iloc[-1, frame.columns.get_loc("high")] = 120.0
        frame.iloc[-1, frame.columns.get_loc("low")] = 80.0
    return frame


def _reopen(path: Path, initial_equity: float):
    reopened = SQLiteStore(path)
    reopened.initialize(initial_equity=initial_equity)
    state = reopened.replay_state()
    reopened.close()
    return state


@pytest.fixture
def app_harness(tmp_path: Path) -> _AcceptanceHarness:
    return _AcceptanceHarness(tmp_path)


def test_paper_acceptance_scenario(app_harness: _AcceptanceHarness) -> None:
    result = app_harness.run("tests/fixtures/paper_acceptance.json")

    assert result.duplicate_orders == 0
    assert result.negative_cash_events == 0
    assert result.oversell_events == 0
    assert result.halt_reasons == ["API_FAILURES"]
    assert result.automatic_recoveries == 1
    assert result.final_state == "FLAT"
    assert result.replay_matches_live_state
    assert result.entry_orders == 1
    assert result.hard_stops == 1
    assert result.stages == ("HALTED", "HALTED", "HALTED", "REDUCED", "NORMAL")
    assert result.volatility_size < result.normal_size
    assert result.volatility_binding == "volatility"
    assert result.volatility_ratio > 2.0
