from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path

import pandas as pd
import pytest

from autobit.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    EquityPoint,
    OrderRecord,
    TradeRecord,
    run_backtest,
)
from autobit.config import CostConfig, StrategyConfig
from autobit.domain.models import OrderStatus, PositionState
from autobit.indicators.trend import compute_trend_indicators
from autobit.validation.models import CostScenario, FoldWindow, TrialConfig, WalkForwardConfig
from autobit.validation.runner import (
    BacktestRequest,
    _run_request,
    core_backtest,
    run_walk_forward,
)
from autobit.validation.splits import build_rolling_folds


FIXTURES = Path(__file__).parents[1] / "fixtures"


def _frame(periods: int = 5_000) -> pd.DataFrame:
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
    canonical = tuple(build_rolling_folds(frame.index, WalkForwardConfig()))
    assert len(canonical) >= count
    return canonical[:count]


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


def _order_lifecycle(
    request: BacktestRequest,
    *,
    order_id: str,
    side: str,
    requested_quantity: float,
    terminal_status: OrderStatus = OrderStatus.COMPLETED,
    terminal_filled_quantity: float | None = None,
    signal_time: object | None = None,
) -> tuple[OrderRecord, ...]:
    """Build a native-shaped lifecycle with cumulative quantity semantics."""
    signal = (
        request.frame.index[-2].to_pydatetime()
        if signal_time is None
        else signal_time
    )
    occurred = request.frame.index[-1].to_pydatetime()
    filled = requested_quantity if terminal_filled_quantity is None else terminal_filled_quantity
    remainder = requested_quantity - filled
    common = dict(
        order_id=order_id,
        side=side,
        requested_quantity=requested_quantity,
        occurred_at=occurred,
        signal_time=signal,
    )
    return (
        OrderRecord(
            status=OrderStatus.CREATED,
            filled_quantity=0.0,
            remainder_quantity=requested_quantity,
            **common,
        ),
        OrderRecord(
            status=OrderStatus.SUBMITTED,
            filled_quantity=0.0,
            remainder_quantity=requested_quantity,
            **common,
        ),
        OrderRecord(
            status=OrderStatus.ACCEPTED,
            filled_quantity=0.0,
            remainder_quantity=requested_quantity,
            **common,
        ),
        OrderRecord(
            status=terminal_status,
            filled_quantity=filled,
            remainder_quantity=remainder,
            fill_time=occurred if filled > 0.0 else None,
            fill_price=100.0 if filled > 0.0 else None,
            **common,
        ),
    )


def _fixture(name: str) -> pd.DataFrame:
    return pd.read_csv(FIXTURES / name, parse_dates=["timestamp"], index_col="timestamp")


def _real_core_request(frame: pd.DataFrame, config: BacktestConfig) -> BacktestRequest:
    return BacktestRequest(
        phase="OOS",
        fold_id="fold-000",
        trial_id="baseline",
        trial=TrialConfig("baseline", 200, 50, 20, 14, 2.5),
        cost_id="zero",
        cost=CostScenario("zero", 0.0, 0.0),
        frame=frame,
        config=config,
        initial_equity=100.0,
        initial_state=PositionState.FLAT,
        pending_orders=0,
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


def test_runner_rejects_a_canonical_fold_prefix_before_any_backtest_call() -> None:
    """A shorter hand-picked schedule could hide unfavorable completed OOS folds."""
    frame = _frame(5_600)
    canonical = _folds(frame, count=2)
    calls: list[BacktestRequest] = []

    def fake_backtest(request: BacktestRequest) -> BacktestResult:
        calls.append(request)
        return _result(request)

    with pytest.raises(ValueError, match="complete canonical"):
        run_walk_forward(frame, canonical[:1], fake_backtest)
    assert calls == []


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


def test_runner_retains_malformed_nested_backtest_evidence_as_failed_rows() -> None:
    """Nested ledger corruption must never reach a COMPLETED validation result."""
    frame = _frame()
    folds = _folds(frame)
    malformed = {
        ("baseline", "zero"): "nan_fee",
        ("baseline", "baseline"): "infinite_trade",
        ("ema_150", "zero"): "quantity_lifecycle",
        ("ema_150", "baseline"): "timestamp_and_id",
    }

    def fake_backtest(request: BacktestRequest) -> BacktestResult:
        result = _result(request)
        kind = malformed.get((request.trial_id, request.cost_id))
        if request.phase != "OOS" or kind is None:
            return result
        start = request.frame.index[-2].to_pydatetime()
        end = request.frame.index[-1].to_pydatetime()
        if kind == "nan_fee":
            return replace(
                result,
                orders=(
                    OrderRecord(
                        order_id="order-1",
                        status=OrderStatus.COMPLETED,
                        side="BUY",
                        requested_quantity=1.0,
                        filled_quantity=1.0,
                        remainder_quantity=0.0,
                        occurred_at=start,
                        signal_time=start,
                        fill_time=end,
                        fill_price=100.0,
                        fee=math.nan,
                    ),
                ),
            )
        if kind == "infinite_trade":
            return replace(
                result,
                trades=(
                    TradeRecord(
                        entry_time=start,
                        exit_time=end,
                        quantity=1.0,
                        entry_price=math.inf,
                        exit_price=101.0,
                        gross_pnl=math.inf,
                        net_pnl=math.inf,
                        fees=0.0,
                        exit_reason="FORCED_END",
                    ),
                ),
            )
        if kind == "quantity_lifecycle":
            return replace(
                result,
                orders=(
                    OrderRecord(
                        order_id="order-2",
                        status=OrderStatus.COMPLETED,
                        side="SELL",
                        requested_quantity=1.0,
                        filled_quantity=2.0,
                        remainder_quantity=-1.0,
                        occurred_at=end,
                        signal_time=start,
                        fill_time=end,
                        fill_price=100.0,
                    ),
                ),
            )
        return replace(
            result,
            orders=(
                OrderRecord(
                    order_id="",
                    status=OrderStatus.COMPLETED,
                    side="BUY",
                    requested_quantity=1.0,
                    filled_quantity=1.0,
                    remainder_quantity=0.0,
                    occurred_at=start.replace(tzinfo=None),
                    signal_time=start.replace(tzinfo=None),
                    fill_time=end.replace(tzinfo=None),
                    fill_price=100.0,
                ),
            ),
        )

    result = run_walk_forward(frame, folds, fake_backtest)

    failed = {
        (run.trial_id, run.cost_id)
        for run in result.runs
        if run.phase == "OOS" and run.status == "FAILED"
    }
    assert failed == set(malformed)
    for trial_id, cost_id in malformed:
        stitched = next(
            item
            for item in result.stitched_oos
            if item.trial_id == trial_id and item.cost_id == cost_id
        )
        assert stitched.status == "INCOMPLETE"
        assert stitched.equity_curve == ()


def test_runner_retains_a_rebased_phase_curve_as_failed_and_incomplete() -> None:
    """A fold must report its own fresh-100 execution path, not an arbitrary rebasing."""
    frame = _frame()
    folds = _folds(frame)

    def fake_backtest(request: BacktestRequest) -> BacktestResult:
        if (
            request.phase == "OOS"
            and request.trial_id == "baseline"
            and request.cost_id == "zero"
        ):
            return _result(request, (50.0, 60.0, 60.0))
        return _result(request)

    result = run_walk_forward(frame, folds, fake_backtest)
    failed = next(
        run
        for run in result.runs
        if run.phase == "OOS" and run.trial_id == "baseline" and run.cost_id == "zero"
    )
    stitched = next(
        item
        for item in result.stitched_oos
        if item.trial_id == "baseline" and item.cost_id == "zero"
    )

    assert failed.status == "FAILED"
    assert "first phase equity" in (failed.error or "")
    assert stitched.status == "INCOMPLETE"
    assert stitched.equity_curve == ()


def test_runner_accepts_tiny_initial_equity_floating_point_drift() -> None:
    """The strict initial-equity guard must not reject harmless binary rounding."""
    frame = _frame()
    folds = _folds(frame)

    def fake_backtest(request: BacktestRequest) -> BacktestResult:
        if (
            request.phase == "OOS"
            and request.trial_id == "baseline"
            and request.cost_id == "zero"
        ):
            return _result(request, (100.0 + 5e-11, 110.0, 121.0))
        return _result(request)

    result = run_walk_forward(frame, folds, fake_backtest)
    run = next(
        item
        for item in result.runs
        if item.phase == "OOS" and item.trial_id == "baseline" and item.cost_id == "zero"
    )
    assert run.status == "COMPLETED"


def test_runner_retains_economically_inconsistent_order_and_trade_evidence() -> None:
    """A well-shaped ledger must still conserve inventory and closed trade quantity."""
    frame = _frame()
    folds = _folds(frame)
    invalid = {
        ("baseline", "zero"): "zero_completed",
        ("baseline", "baseline"): "undersized_completed",
        ("ema_150", "zero"): "negative_inventory",
        ("ema_150", "baseline"): "trade_quantity_mismatch",
        ("ema_250", "zero"): "mutated_signal",
        ("ema_250", "baseline"): "fee_without_fill",
    }

    def fake_backtest(request: BacktestRequest) -> BacktestResult:
        result = _result(request)
        kind = invalid.get((request.trial_id, request.cost_id))
        if request.phase != "OOS" or kind is None:
            return result
        if kind == "zero_completed":
            return replace(
                result,
                orders=_order_lifecycle(
                    request,
                    order_id="zero-completed",
                    side="BUY",
                    requested_quantity=1.0,
                    terminal_filled_quantity=0.0,
                ),
            )
        if kind == "undersized_completed":
            return replace(
                result,
                orders=_order_lifecycle(
                    request,
                    order_id="undersized-completed",
                    side="BUY",
                    requested_quantity=1.0,
                    terminal_filled_quantity=0.5,
                ),
            )
        if kind == "negative_inventory":
            return replace(
                result,
                orders=_order_lifecycle(
                    request,
                    order_id="sell-without-buy",
                    side="SELL",
                    requested_quantity=1.0,
                ),
            )
        if kind == "trade_quantity_mismatch":
            end = request.frame.index[-1].to_pydatetime()
            return replace(
                result,
                orders=(
                    *_order_lifecycle(
                        request,
                        order_id="buy",
                        side="BUY",
                        requested_quantity=1.0,
                    ),
                    *_order_lifecycle(
                        request,
                        order_id="sell",
                        side="SELL",
                        requested_quantity=1.0,
                    ),
                ),
                trades=(
                    TradeRecord(
                        entry_time=end,
                        exit_time=end,
                        quantity=0.5,
                        entry_price=100.0,
                        exit_price=100.0,
                        gross_pnl=0.0,
                        net_pnl=0.0,
                        fees=0.0,
                        exit_reason="FORCED_END",
                    ),
                ),
            )
        if kind == "fee_without_fill":
            buy = list(_order_lifecycle(
                request,
                order_id="buy-with-phantom-fee",
                side="BUY",
                requested_quantity=1.0,
            ))
            buy[2] = replace(buy[2], fee=1.0)
            buy[3] = replace(buy[3], fee=1.0)
            end = request.frame.index[-1].to_pydatetime()
            return replace(
                result,
                orders=(
                    *buy,
                    *_order_lifecycle(
                        request,
                        order_id="sell-after-phantom-fee",
                        side="SELL",
                        requested_quantity=1.0,
                    ),
                ),
                trades=(
                    TradeRecord(
                        entry_time=end,
                        exit_time=end,
                        quantity=1.0,
                        entry_price=100.0,
                        exit_price=100.0,
                        gross_pnl=0.0,
                        net_pnl=-1.0,
                        fees=1.0,
                        exit_reason="FORCED_END",
                    ),
                ),
                total_fees=1.0,
            )
        lifecycle = list(_order_lifecycle(
            request,
            order_id="signal-mutated",
            side="BUY",
            requested_quantity=1.0,
        ))
        lifecycle[1] = replace(
            lifecycle[1],
            signal_time=request.frame.index[-3].to_pydatetime(),
        )
        return replace(result, orders=tuple(lifecycle))

    result = run_walk_forward(frame, folds, fake_backtest)
    failed = {
        (run.trial_id, run.cost_id)
        for run in result.runs
        if run.phase == "OOS" and run.status == "FAILED"
    }

    assert failed == set(invalid)
    phantom_fee = next(
        run
        for run in result.runs
        if run.phase == "OOS" and run.trial_id == "ema_250" and run.cost_id == "baseline"
    )
    assert "fill increments" in (phantom_fee.error or "")
    for trial_id, cost_id in invalid:
        stitched = next(
            item
            for item in result.stitched_oos
            if item.trial_id == trial_id and item.cost_id == cost_id
        )
        assert stitched.status == "INCOMPLETE"


@pytest.mark.parametrize(
    ("fixture_name", "config"),
    (
        ("entry_next_open.csv", BacktestConfig(force_liquidate_at_end=True)),
        (
            "partial_entry.csv",
            BacktestConfig(entry_fill_fraction=0.5, force_liquidate_at_end=True),
        ),
        (
            "partial_exit.csv",
            BacktestConfig(exit_fill_fraction=0.5, force_liquidate_at_end=True),
        ),
        ("gap_stop.csv", BacktestConfig(force_liquidate_at_end=True)),
    ),
)
def test_runner_phase_validation_accepts_real_core_lifecycles(
    fixture_name: str,
    config: BacktestConfig,
) -> None:
    """Plan 1 full/partial/stop and forced terminal lifecycles remain valid evidence."""
    request = _real_core_request(_fixture(fixture_name), config)

    run = _run_request(request, core_backtest)

    assert run.status == "COMPLETED", run.error
    assert run.result is not None
    assert run.result.equity_curve[0].equity == pytest.approx(request.initial_equity)


def test_runner_phase_validation_accepts_real_core_same_bar_stop() -> None:
    """Same-bar entry and protective-stop fills remain economically ordered evidence."""
    frame = _fixture("entry_next_open.csv").iloc[:612].copy()
    frame.loc[pd.Timestamp("2025-01-05T04:00:00Z"), ["open", "high", "low", "close"]] = [
        100.0,
        101.0,
        94.0,
        100.0,
    ]
    request = _real_core_request(
        frame,
        BacktestConfig(
            costs=CostConfig(fee_rate=0.0005, slippage_rate=0.001),
            entry_fill_fraction=0.5,
            force_liquidate_at_end=True,
        ),
    )

    run = _run_request(request, core_backtest)

    assert run.status == "COMPLETED", run.error


def test_runner_uses_only_pre_test_context_and_excludes_future_and_embargo_rows() -> None:
    """Passing a full enriched frame would allow test metrics to see future observations."""
    frame = _frame()
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
    frame = _frame(5_600)
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
        folds[0].test_index[-3].to_pydatetime(),
        folds[0].test_index[-2].to_pydatetime(),
        folds[0].test_index[-1].to_pydatetime(),
        folds[1].test_index[-2].to_pydatetime(),
        folds[1].test_index[-1].to_pydatetime(),
    ]
    assert len({point.timestamp for point in stitched.equity_curve}) == len(stitched.equity_curve)


def test_stitched_oos_preserves_a_complete_bankruptcy_path_without_dividing_by_zero() -> None:
    """A fold that reaches zero stays zero, while later fold returns remain observable."""
    frame = _frame(5_600)
    folds = _folds(frame, count=2)

    def fake_backtest(request: BacktestRequest) -> BacktestResult:
        if request.phase == "OOS" and request.fold_id == "fold-000":
            return _result(request, (100.0, 0.0, 0.0))
        return _result(request)

    result = run_walk_forward(frame, folds, fake_backtest)
    stitched = next(
        item for item in result.stitched_oos
        if item.trial_id == "baseline" and item.cost_id == "zero"
    )

    assert stitched.status == "COMPLETE"
    assert [point.value for point in stitched.returns] == pytest.approx([-1.0, 0.0, 0.1, 0.1])
    assert [point.equity for point in stitched.equity_curve] == pytest.approx(
        [100.0, 0.0, 0.0, 0.0, 0.0]
    )


def test_runner_retains_zero_equity_recovery_as_a_failed_oos_cell() -> None:
    """A zero-to-positive recovery is impossible for a fresh 100-equity fold path."""
    frame = _frame()
    folds = _folds(frame)

    def fake_backtest(request: BacktestRequest) -> BacktestResult:
        if (
            request.phase == "OOS"
            and request.trial_id == "baseline"
            and request.cost_id == "zero"
        ):
            return _result(request, (100.0, 0.0, 1.0))
        return _result(request)

    result = run_walk_forward(frame, folds, fake_backtest)
    failed = next(
        run
        for run in result.runs
        if run.phase == "OOS" and run.trial_id == "baseline" and run.cost_id == "zero"
    )
    stitched = next(
        item for item in result.stitched_oos
        if item.trial_id == "baseline" and item.cost_id == "zero"
    )

    assert failed.status == "FAILED"
    assert "zero-equity recovery" in (failed.error or "")
    assert stitched.status == "INCOMPLETE"


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
    frame = _frame(5_600)
    folds = _folds(frame, count=2)
    non_utc = frame.copy()
    non_utc.index = non_utc.index.tz_convert("Asia/Seoul")
    nonfinite = frame.copy()
    nonfinite.iloc[0, nonfinite.columns.get_loc("close")] = math.inf
    overlap_test_start = folds[0].test_start + pd.Timedelta(hours=4)
    overlap_train_end = overlap_test_start - pd.DateOffset(days=5)
    overlap_train_start = overlap_train_end - pd.DateOffset(years=2)
    overlap = replace(
        folds[1],
        train_start=overlap_train_start,
        train_end=overlap_train_end,
        test_start=overlap_test_start,
        train_index=frame.index[
            (frame.index >= overlap_train_start) & (frame.index < overlap_train_end)
        ],
        test_index=frame.index[
            (frame.index >= overlap_test_start) & (frame.index < folds[1].test_end)
        ],
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
