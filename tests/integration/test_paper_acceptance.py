from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from autobit.cli import _PaperApplication
from autobit.execution.paper_broker import PaperBroker
from autobit.paper import service as paper_service
from autobit.paper.health import HealthMonitor, HealthStage
from autobit.persistence.sqlite_store import SQLiteStore
from autobit.risk.position_sizer import SizeDecision, calculate_size


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
    entry_submitted_quantity: float
    entry_filled_quantity: float


class _AcceptanceHarness:
    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def run(self, fixture_path: str) -> _AcceptanceResult:
        fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
        start = datetime.fromisoformat(fixture["start_end"].replace("Z", "+00:00"))
        restart_end = start + _FOUR_HOURS
        outage_end = restart_end + _FOUR_HOURS
        entry_end = outage_end + _FOUR_HOURS
        fill_end = entry_end + _FOUR_HOURS
        stop_end = fill_end + _FOUR_HOURS
        clock = _Clock(start + timedelta(minutes=10))
        entry_history = _history(entry_end, breakout=True, high_volatility=True)
        source = _SyntheticPublicCandleSource(
            [
                _history(start),
                _history(restart_end),
                *[RuntimeError("synthetic public API outage") for _ in range(fixture["api_failures"])],
                _history(outage_end),
                _history(outage_end),
                _history(outage_end),
                entry_history,
                _history(fill_end),
                _history(stop_end, hard_stop=True),
            ]
        )
        path = self._tmp_path / "paper.sqlite3"
        store = SQLiteStore(path)
        store.initialize(initial_equity=fixture["initial_equity"])
        first = _application(store, source, clock, "first")
        first.run_once()
        before_restart = store.replay_state()
        store.close()

        store = SQLiteStore(path)
        store.initialize(initial_equity=fixture["initial_equity"])
        assert store.replay_state() == before_restart
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

        clock.value = entry_end + timedelta(minutes=10)
        restarted.run_once()
        stages.append(HealthMonitor.from_store(store).current_action().stage.value)
        clock.value = fill_end + timedelta(minutes=10)
        restarted.run_once()
        clock.value = stop_end + timedelta(minutes=10)
        restarted.run_once()
        live = store.replay_state()
        broker = PaperBroker(store)
        reconciliation = broker.reconcile()

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

        halt_reasons = sorted(
            {
                reason
                for event in live.event_evidence
                if event.event_type == "HEALTH_STATE" and event.payload["stage"] == "HALTED"
                for reason in event.payload["reasons"]
            }
        )
        entry_order = next(order for order in reconciliation.fills if order.side == "BUY")
        submitted_entry = next(
            event.payload
            for event in live.event_evidence
            if event.event_type == "PAPER_ORDER" and event.payload["side"] == "BUY"
        )
        store.close()
        reopened_state = _reopen(path, fixture["initial_equity"])

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
            entry_submitted_quantity=float(submitted_entry["requested_quantity"]),
            entry_filled_quantity=entry_order.quantity,
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


def _history(
    end: datetime,
    *,
    breakout: bool = False,
    high_volatility: bool = False,
    hard_stop: bool = False,
) -> pd.DataFrame:
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
    if high_volatility:
        frame.iloc[-1, frame.columns.get_loc("close")] = 124.0
        frame.iloc[-1, frame.columns.get_loc("high")] = 124.0
        frame.iloc[-1, frame.columns.get_loc("low")] = 80.0
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


def _assert_causal_entry_sizing(
    result: _AcceptanceResult,
    calls: list[dict[str, object]],
) -> None:
    assert len(calls) == 1
    actual = calls[0]
    assert actual["risk_rate"] == pytest.approx(0.005)
    assert actual["exposure_cap"] == pytest.approx(0.175)
    assert float(actual["current_atr_pct"]) / float(actual["baseline_atr_pct"]) > 2.0

    expected = calculate_size(**actual)
    normal_risk_high_volatility = calculate_size(
        **{**actual, "risk_rate": 0.01, "exposure_cap": 0.35}
    )
    reduced_risk_baseline_volatility = calculate_size(
        **{**actual, "current_atr_pct": actual["baseline_atr_pct"]}
    )
    assert result.entry_submitted_quantity == pytest.approx(expected.quantity)
    assert result.entry_filled_quantity == pytest.approx(expected.quantity)
    assert expected.binding_constraint == "volatility"
    assert expected.quantity < normal_risk_high_volatility.quantity
    assert expected.quantity < reduced_risk_baseline_volatility.quantity


@pytest.fixture
def app_harness(tmp_path: Path) -> _AcceptanceHarness:
    return _AcceptanceHarness(tmp_path)


def test_paper_acceptance_scenario(
    app_harness: _AcceptanceHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    production_size = paper_service.calculate_size

    def capture_size(**kwargs: object):
        calls.append(kwargs)
        return production_size(**kwargs)

    monkeypatch.setattr(paper_service, "calculate_size", capture_size)
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
    _assert_causal_entry_sizing(result, calls)


def test_paper_acceptance_rejects_risk_agnostic_service_sizing(
    app_harness: _AcceptanceHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def constant_size(**kwargs: object) -> SizeDecision:
        calls.append(kwargs)
        return SizeDecision(quantity=0.5, binding_constraint="constant", estimated_loss=0.0)

    monkeypatch.setattr(paper_service, "calculate_size", constant_size)
    result = app_harness.run("tests/fixtures/paper_acceptance.json")

    with pytest.raises(AssertionError):
        _assert_causal_entry_sizing(result, calls)
