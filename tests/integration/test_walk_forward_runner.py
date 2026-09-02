from __future__ import annotations

from dataclasses import replace
import math

import pandas as pd
import pytest

from autobit.backtest.engine import BacktestConfig, BacktestResult, EquityPoint, run_backtest
from autobit.config import CostConfig, StrategyConfig
from autobit.domain.models import PositionState
from autobit.indicators.trend import compute_trend_indicators
from autobit.validation.models import CostScenario, FoldWindow, TrialConfig
from autobit.validation.runner import (
    BacktestRequest,
    core_backtest,
    run_walk_forward,
)


def _frame(periods: int = 608) -> pd.DataFrame:
    index = pd.date_range("2022-01-01", periods=periods, freq="4h", tz="UTC")
    prices = pd.Series(range(periods), index=index, dtype="float64") + 100.0
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices + 2.0,
            "low": prices - 1.0,
            "close": prices + 1.0,
            "volume": 10.0,
            "is_filled": False,
            "is_quarantined": False,
            "anomaly_spike": False,
            "anomaly_flat": False,
            "segment_id": 0,
            "entry_data_valid": True,
        },
        index=index,
    )


def _folds(frame: pd.DataFrame, *, count: int = 1) -> tuple[FoldWindow, ...]:
    test_width = 3
    result: list[FoldWindow] = []
    for number in range(count):
        test_start_position = 601 + number * test_width
        test_end_position = test_start_position + test_width
        test_index = frame.index[test_start_position:test_end_position]
        train_end_position = test_start_position - 1
        train_index = frame.index[:train_end_position]
        result.append(
            FoldWindow(
                fold_id=f"fold-{number:03d}",
                train_start=train_index[0],
                train_end=frame.index[train_end_position],
                test_start=test_index[0],
                test_end=frame.index[test_end_position],
                train_index=train_index.copy(),
                test_index=test_index.copy(),
            )
        )
    return tuple(result)


def _result(request: BacktestRequest, values: tuple[float, ...] = (100.0, 110.0, 121.0)) -> BacktestResult:
    points = tuple(
        EquityPoint(timestamp.to_pydatetime(), value)
        for timestamp, value in zip(request.frame.index[-len(values):], values, strict=True)
    )
    return BacktestResult(
        equity_curve=points,
        orders=(),
        trades=(),
        final_equity=values[-1],
        total_fees=0.0,
        total_slippage=0.0,
    )


def test_runner_executes_each_oos_cell_once_with_a_fresh_flat_request() -> None:
    """Sharing a broker, frame, or config would leak state between trial cells."""
    frame = _frame()
    folds = _folds(frame)
    requests: list[BacktestRequest] = []

    def fake_backtest(request: BacktestRequest) -> BacktestResult:
        assert request.initial_equity == 100.0
        assert request.initial_state is PositionState.FLAT
        assert request.pending_orders == 0
        assert request.config.initial_equity == 100.0
        assert request.config.force_liquidate_at_end is True
        assert request.frame.index.equals(
            folds[0].train_index if request.phase == "TRAIN" else folds[0].test_index
        )
        assert request.frame.iloc[0]["open"] > 0.0
        requests.append(request)
        request.frame.iloc[0, request.frame.columns.get_loc("open")] = -999.0
        return _result(request)

    result = run_walk_forward(frame, folds, fake_backtest)

    expected_oos = [
        ("fold-000", trial_id, cost_id)
        for trial_id in (
            "baseline", "ema_150", "ema_250", "entry_40", "entry_60",
            "exit_15", "exit_25", "stop_2_0", "stop_3_0",
        )
        for cost_id in ("zero", "baseline", "stress_10bps", "stress_20bps")
    ]
    oos_requests = [request for request in requests if request.phase == "OOS"]
    assert [(request.fold_id, request.trial.trial_id, request.cost.cost_id) for request in oos_requests] == expected_oos
    assert len(requests) == 2 * len(expected_oos)
    assert len(result.runs) == len(requests)
    assert len({id(request.frame) for request in requests}) == len(requests)
    assert len({id(request.config) for request in requests}) == len(requests)
    assert frame["open"].gt(0.0).all()


def test_train_failure_is_retained_without_suppressing_its_oos_cell() -> None:
    """Dropping an error or skipping the OOS retry would create a biased result matrix."""
    frame = _frame()
    folds = _folds(frame)
    calls: list[tuple[str, str, str, str]] = []

    def fake_backtest(request: BacktestRequest) -> BacktestResult:
        calls.append((request.phase, request.fold_id, request.trial.trial_id, request.cost.cost_id))
        if request.phase == "TRAIN" and request.trial.trial_id == "ema_150":
            raise RuntimeError("train only failure")
        if request.phase == "OOS" and request.trial.trial_id == "exit_15":
            return replace(_result(request), final_equity=math.nan)
        return _result(request)

    result = run_walk_forward(frame, folds, fake_backtest)

    failed = [run for run in result.runs if run.status == "FAILED"]
    assert [(run.phase, run.trial_id, run.cost_id) for run in failed] == [
        ("TRAIN", "ema_150", cost_id)
        for cost_id in ("zero", "baseline", "stress_10bps", "stress_20bps")
    ] + [
        ("OOS", "exit_15", cost_id)
        for cost_id in ("zero", "baseline", "stress_10bps", "stress_20bps")
    ]
    assert sum(phase == "OOS" for phase, *_ in calls) == 36
    assert sum(phase == "TRAIN" for phase, *_ in calls) == 36
    stitched = next(
        item for item in result.stitched_oos
        if item.trial_id == "exit_15" and item.cost_id == "zero"
    )
    assert stitched.status == "INCOMPLETE"
    assert stitched.equity_curve == ()
    train_failed_stitched = next(
        item for item in result.stitched_oos
        if item.trial_id == "ema_150" and item.cost_id == "zero"
    )
    assert train_failed_stitched.status == "INCOMPLETE"
    assert train_failed_stitched.equity_curve


def test_runner_uses_only_pre_test_context_and_excludes_future_and_embargo_rows() -> None:
    """Passing a full enriched frame would allow test metrics to see future observations."""
    frame = _frame(609)
    folds = _folds(frame)
    captured: list[pd.DataFrame] = []

    def fake_backtest(request: BacktestRequest) -> BacktestResult:
        if request.phase == "OOS" and request.trial.trial_id == "baseline" and request.cost.cost_id == "zero":
            captured.append(request.frame.copy())
        return _result(request)

    run_walk_forward(frame, folds, fake_backtest)
    first = captured[0]
    changed_future = frame.copy()
    changed_future.loc[changed_future.index[-1], ["open", "high", "low", "close"]] = 9_999_999.0
    run_walk_forward(changed_future, folds, fake_backtest)
    second = captured[1]

    assert first.index.equals(folds[0].test_index)
    assert second.index.equals(folds[0].test_index)
    pd.testing.assert_frame_equal(first, second)
    assert first.index.min() == folds[0].test_start
    assert first.index.max() < folds[0].test_end


def test_stitched_oos_compounds_within_fold_returns_without_reset_or_boundary_duplicates() -> None:
    """Concatenating fresh-100 curves would create a false drawdown and duplicate timestamps."""
    frame = _frame(612)
    folds = _folds(frame, count=2)

    def fake_backtest(request: BacktestRequest) -> BacktestResult:
        if request.phase == "OOS" and request.fold_id == "fold-001":
            return _result(request, (100.0, 90.0, 99.0))
        return _result(request)

    result = run_walk_forward(frame, folds, fake_backtest)
    stitched = next(
        item for item in result.stitched_oos
        if item.trial_id == "baseline" and item.cost_id == "zero"
    )

    assert stitched.status == "COMPLETE"
    assert [point.equity for point in stitched.equity_curve] == pytest.approx(
        [100.0, 110.0, 121.0, 108.9, 119.79]
    )
    assert [point.timestamp for point in stitched.equity_curve] == [
        folds[0].test_index[0].to_pydatetime(),
        folds[0].test_index[1].to_pydatetime(),
        folds[0].test_index[2].to_pydatetime(),
        folds[1].test_index[1].to_pydatetime(),
        folds[1].test_index[2].to_pydatetime(),
    ]
    assert len({point.timestamp for point in stitched.equity_curve}) == len(stitched.equity_curve)


def test_runner_rejects_unknown_fold_ids_and_indexes_outside_half_open_windows() -> None:
    """Accepting an altered fold ID or index could silently change the result matrix."""
    frame = _frame()
    fold, = _folds(frame)

    with pytest.raises(ValueError, match="fold IDs"):
        run_walk_forward(frame, (replace(fold, fold_id="experiment-1"),), _result)
    with pytest.raises(ValueError, match="exactly match"):
        run_walk_forward(
            frame,
            (replace(fold, test_index=fold.test_index[:-1]),),
            _result,
        )


def test_runner_applies_exact_scenario_costs_to_each_isolated_request() -> None:
    """Any fee/slippage drift would make the published stress comparison false."""
    frame = _frame()
    folds = _folds(frame)
    observed: list[tuple[str, float, float]] = []

    def fake_backtest(request: BacktestRequest) -> BacktestResult:
        if request.phase == "OOS" and request.trial_id == "baseline":
            observed.append(
                (
                    request.cost_id,
                    request.config.costs.fee_rate,
                    request.config.costs.slippage_rate,
                )
            )
        return _result(request)

    run_walk_forward(frame, folds, fake_backtest)

    assert observed == [
        ("zero", 0.0, 0.0),
        ("baseline", 0.0005, 0.0005),
        ("stress_10bps", 0.0005, 0.001),
        ("stress_20bps", 0.0005, 0.002),
    ]


def test_runner_rejects_noncanonical_frame_and_overlapping_oos_folds() -> None:
    """Non-UTC data or overlapping OOS periods would invalidate chronological evidence."""
    frame = _frame(612)
    folds = _folds(frame, count=2)
    non_utc = frame.copy()
    non_utc.index = non_utc.index.tz_convert("Asia/Seoul")
    nonfinite = frame.copy()
    nonfinite.iloc[0, nonfinite.columns.get_loc("close")] = math.inf
    overlap = replace(
        folds[1],
        train_end=frame.index[601],
        test_start=frame.index[602],
        train_index=frame.index[:601],
        test_index=frame.index[602:607],
    )

    with pytest.raises(ValueError, match="canonical UTC"):
        run_walk_forward(non_utc, folds, _result)
    with pytest.raises(ValueError, match="finite"):
        run_walk_forward(nonfinite, folds, _result)
    with pytest.raises(ValueError, match="must not overlap"):
        run_walk_forward(frame, (folds[0], overlap), _result)


def test_core_adapter_executes_the_real_core_backtest_with_the_request_config() -> None:
    """A runner adapter that bypassed the core engine would not preserve execution semantics."""
    frame = compute_trend_indicators(_frame(604), StrategyConfig())
    request = BacktestRequest(
        phase="OOS",
        fold_id="fold-000",
        trial_id="baseline",
        trial=TrialConfig("baseline", 200, 50, 20, 14, 2.5),
        cost_id="zero",
        cost=CostScenario("zero", 0.0, 0.0),
        frame=frame.iloc[600:603].copy(),
        config=BacktestConfig(
            strategy=StrategyConfig(),
            costs=CostConfig(fee_rate=0.0, slippage_rate=0.0),
            force_liquidate_at_end=True,
        ),
        initial_equity=100.0,
        initial_state=PositionState.FLAT,
        pending_orders=0,
    )

    assert core_backtest(request) == run_backtest(request.frame, request.config)
