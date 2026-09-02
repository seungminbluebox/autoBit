from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
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
from autobit.data.quality import canonicalize_ohlcv
from autobit.domain.models import OrderStatus, PositionState
from autobit.indicators.trend import compute_trend_indicators
from autobit.validation.models import CostScenario, FoldWindow, TrialConfig, WalkForwardConfig
from autobit.validation.runner import (
    BacktestRequest,
    _run_request,
    core_backtest,
    run_walk_forward,
    validate_walk_forward_frame,
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


def _result(request: BacktestRequest, values: tuple[float, ...] = (100.0, 100.0, 100.0)) -> BacktestResult:
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


def _filled_lifecycle(
    *,
    order_id: str,
    side: str,
    quantity: float,
    occurred_at: object,
    fill_price: float,
    fee: float = 0.0,
    slippage: float = 0.0,
) -> tuple[OrderRecord, ...]:
    """Construct one fully filled native-shaped order at a known economic price."""
    common = dict(
        order_id=order_id,
        side=side,
        requested_quantity=quantity,
        occurred_at=occurred_at,
        signal_time=occurred_at,
    )
    return (
        OrderRecord(
            status=OrderStatus.CREATED,
            filled_quantity=0.0,
            remainder_quantity=quantity,
            **common,
        ),
        OrderRecord(
            status=OrderStatus.SUBMITTED,
            filled_quantity=0.0,
            remainder_quantity=quantity,
            **common,
        ),
        OrderRecord(
            status=OrderStatus.ACCEPTED,
            filled_quantity=0.0,
            remainder_quantity=quantity,
            **common,
        ),
        OrderRecord(
            status=OrderStatus.COMPLETED,
            filled_quantity=quantity,
            remainder_quantity=0.0,
            fill_time=occurred_at,
            fill_price=fill_price,
            fee=fee,
            slippage=slippage,
            **common,
        ),
    )


def _ledger_result(
    request: BacktestRequest,
    *,
    values: tuple[float, ...],
    orders: tuple[OrderRecord, ...],
    trades: tuple[TradeRecord, ...],
    total_fees: float,
    total_slippage: float = 0.0,
) -> BacktestResult:
    return replace(
        _result(request, values),
        orders=orders,
        trades=trades,
        total_fees=total_fees,
        total_slippage=total_slippage,
    )


def _single_cycle_result(
    request: BacktestRequest,
    values: tuple[float, ...],
    *,
    entry_price: float,
    exit_price: float,
    fees: float = 0.0,
) -> BacktestResult:
    """Return a phase curve with one evidence-backed flat-to-flat BTC cycle."""
    entry_time = request.frame.index[-len(values)].to_pydatetime()
    exit_time = request.frame.index[-1].to_pydatetime()
    gross_pnl = exit_price - entry_price
    net_pnl = gross_pnl - fees
    return _ledger_result(
        request,
        values=values,
        orders=(
            *_filled_lifecycle(
                order_id="single-cycle-buy",
                side="BUY",
                quantity=1.0,
                occurred_at=entry_time,
                fill_price=entry_price,
            ),
            *_filled_lifecycle(
                order_id="single-cycle-sell",
                side="SELL",
                quantity=1.0,
                occurred_at=exit_time,
                fill_price=exit_price,
                fee=fees,
            ),
        ),
        trades=(
            TradeRecord(
                entry_time=entry_time,
                exit_time=exit_time,
                quantity=1.0,
                entry_price=entry_price,
                exit_price=exit_price,
                gross_pnl=gross_pnl,
                net_pnl=net_pnl,
                fees=fees,
                exit_reason="FORCED_END",
            ),
        ),
        total_fees=fees,
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
            return _result(request, (100.0 + 5e-11, 100.0, 100.0))
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


def test_runner_retains_trade_records_that_conflict_with_fill_cycles() -> None:
    """Aggregates alone cannot prove a trade used the prices, lots, or PnL it claims."""
    frame = _frame()
    folds = _folds(frame)
    invalid = {
        ("baseline", "zero"): "forged_price_and_pnl",
        ("baseline", "baseline"): "swapped_two_cycle_economics",
        ("ema_150", "zero"): "final_equity_gap",
    }

    def fake_backtest(request: BacktestRequest) -> BacktestResult:
        kind = invalid.get((request.trial_id, request.cost_id))
        if request.phase != "OOS" or kind is None:
            return _result(request, (100.0, 100.0, 100.0))
        first = request.frame.index[-9].to_pydatetime()
        second = request.frame.index[-8].to_pydatetime()
        third = request.frame.index[-7].to_pydatetime()
        fourth = request.frame.index[-6].to_pydatetime()
        if kind == "forged_price_and_pnl":
            return _ledger_result(
                request,
                values=(100.0, 100.0, 100.0),
                orders=(
                    *_filled_lifecycle(
                        order_id="buy-100",
                        side="BUY",
                        quantity=1.0,
                        occurred_at=first,
                        fill_price=100.0,
                    ),
                    *_filled_lifecycle(
                        order_id="sell-100",
                        side="SELL",
                        quantity=1.0,
                        occurred_at=second,
                        fill_price=100.0,
                    ),
                ),
                trades=(
                    TradeRecord(
                        entry_time=first,
                        exit_time=second,
                        quantity=1.0,
                        entry_price=1.0,
                        exit_price=2.0,
                        gross_pnl=1.0,
                        net_pnl=1.0,
                        fees=0.0,
                        exit_reason="FORCED_END",
                    ),
                ),
                total_fees=0.0,
            )
        if kind == "swapped_two_cycle_economics":
            return _ledger_result(
                request,
                values=(100.0, 102.8, 102.8),
                orders=(
                    *_filled_lifecycle(
                        order_id="cycle-one-buy",
                        side="BUY",
                        quantity=1.0,
                        occurred_at=first,
                        fill_price=100.0,
                        fee=0.1,
                    ),
                    *_filled_lifecycle(
                        order_id="cycle-one-sell",
                        side="SELL",
                        quantity=1.0,
                        occurred_at=second,
                        fill_price=101.0,
                        fee=0.2,
                    ),
                    *_filled_lifecycle(
                        order_id="cycle-two-buy",
                        side="BUY",
                        quantity=2.0,
                        occurred_at=third,
                        fill_price=100.0,
                        fee=0.4,
                    ),
                    *_filled_lifecycle(
                        order_id="cycle-two-sell",
                        side="SELL",
                        quantity=2.0,
                        occurred_at=fourth,
                        fill_price=102.0,
                        fee=0.5,
                    ),
                ),
                trades=(
                    TradeRecord(
                        entry_time=first,
                        exit_time=second,
                        quantity=2.0,
                        entry_price=100.0,
                        exit_price=101.0,
                        gross_pnl=2.0,
                        net_pnl=1.1,
                        fees=0.9,
                        exit_reason="CLOSE_EXIT",
                    ),
                    TradeRecord(
                        entry_time=third,
                        exit_time=fourth,
                        quantity=1.0,
                        entry_price=100.0,
                        exit_price=102.0,
                        gross_pnl=2.0,
                        net_pnl=1.7,
                        fees=0.3,
                        exit_reason="CLOSE_EXIT",
                    ),
                ),
                total_fees=1.2,
            )
        return _ledger_result(
            request,
            values=(100.0, 100.0, 100.0),
            orders=(
                *_filled_lifecycle(
                    order_id="buy-then-profit",
                    side="BUY",
                    quantity=1.0,
                    occurred_at=first,
                    fill_price=100.0,
                ),
                *_filled_lifecycle(
                    order_id="sell-then-profit",
                    side="SELL",
                    quantity=1.0,
                    occurred_at=second,
                    fill_price=101.0,
                ),
            ),
            trades=(
                TradeRecord(
                    entry_time=first,
                    exit_time=second,
                    quantity=1.0,
                    entry_price=100.0,
                    exit_price=101.0,
                    gross_pnl=1.0,
                    net_pnl=1.0,
                    fees=0.0,
                    exit_reason="CLOSE_EXIT",
                ),
            ),
            total_fees=0.0,
        )

    result = run_walk_forward(frame, folds, fake_backtest)
    failed = {
        (run.trial_id, run.cost_id)
        for run in result.runs
        if run.phase == "OOS" and run.status == "FAILED"
    }

    assert failed == set(invalid)
    for trial_id, cost_id in invalid:
        failed_run = next(
            run
            for run in result.runs
            if run.phase == "OOS" and run.trial_id == trial_id and run.cost_id == cost_id
        )
        assert "cycle" in (failed_run.error or "")
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
        if request.phase != "OOS":
            return _result(request)
        if request.fold_id == "fold-001":
            return _single_cycle_result(
                request,
                (100.0, 90.0, 99.0),
                entry_price=100.0,
                exit_price=99.0,
            )
        return _single_cycle_result(
            request,
            (100.0, 110.0, 121.0),
            entry_price=100.0,
            exit_price=121.0,
        )

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
        if request.phase != "OOS":
            return _result(request)
        if request.fold_id == "fold-000":
            return _single_cycle_result(
                request,
                (100.0, 0.0, 0.0),
                entry_price=100.0,
                exit_price=1.0,
                fees=1.0,
            )
        return _single_cycle_result(
            request,
            (100.0, 110.0, 121.0),
            entry_price=100.0,
            exit_price=121.0,
        )

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


def test_runner_accepts_real_canonical_gap_and_quarantine_without_crossing_them() -> None:
    """Plan 1 gaps stay on-grid but never become executable or seed a new segment."""
    raw = _frame(5_600).loc[:, ["open", "high", "low", "close", "volume"]]
    gap_times = tuple(raw.index[100:102])
    quarantine_time = raw.index[300]
    after_gap = raw.index[102]
    after_quarantine = raw.index[301]
    raw = raw.drop(index=list(gap_times))
    raw.loc[quarantine_time, "high"] = raw.loc[quarantine_time, "low"] - 1.0
    quality = canonicalize_ohlcv(
        raw,
        (raw.index[-1] + timedelta(hours=8)).to_pydatetime(),
    )
    canonical = validate_walk_forward_frame(quality.frame)
    folds = tuple(build_rolling_folds(canonical.index, WalkForwardConfig()))
    observed: list[pd.DataFrame] = []

    def fake_backtest(request: BacktestRequest) -> BacktestResult:
        if request.phase == "TRAIN" and request.trial_id == "baseline" and request.cost_id == "zero":
            observed.append(request.frame.copy(deep=True))
        assert request.frame[["open", "high", "low", "close", "volume"]].notna().all().all()
        return _result(request)

    result = run_walk_forward(canonical, folds, fake_backtest)

    assert result.runs
    first = observed[0]
    assert not set(gap_times).intersection(first.index)
    assert quarantine_time not in first.index
    assert int(first.loc[after_gap, "segment_id"]) == 1
    assert pd.isna(first.loc[after_gap, "ema_200"])
    assert pd.isna(first.loc[after_gap, "entry_high"])
    assert pd.isna(first.loc[after_gap, "previous_close"])
    post_gap_execution = first.loc[after_gap:]
    assert not post_gap_execution["warmup_complete"].iloc[:600].any()
    assert bool(post_gap_execution["warmup_complete"].iloc[600])
    assert not bool(first.loc[after_quarantine, "entry_data_valid"])


@pytest.mark.parametrize("malformation", ("unflagged_nan", "entry_true", "infinity", "bad_segment"))
def test_gap_acceptance_rejects_unflagged_or_malformed_invalid_rows(
    malformation: str,
) -> None:
    raw = _frame(20).loc[:, ["open", "high", "low", "close", "volume"]]
    gap_times = tuple(raw.index[5:7])
    raw = raw.drop(index=list(gap_times))
    canonical = canonicalize_ohlcv(
        raw,
        (raw.index[-1] + timedelta(hours=8)).to_pydatetime(),
    ).frame
    malformed = canonical.copy(deep=True)
    if malformation == "unflagged_nan":
        malformed = malformed.loc[:, ["open", "high", "low", "close", "volume"]]
    elif malformation == "entry_true":
        malformed.loc[gap_times[0], "entry_data_valid"] = True
    elif malformation == "infinity":
        malformed.loc[gap_times[0], "close"] = math.inf
    else:
        malformed.loc[raw.index[5]:, "segment_id"] = 0

    with pytest.raises(ValueError, match="quality|finite|segment|entry_data_valid"):
        validate_walk_forward_frame(malformed)


def _quality_topology_frame() -> pd.DataFrame:
    """Return a finite, flag-complete frame suitable for topology mutations."""
    return _frame(12)


def _set_filled_quality_row(frame: pd.DataFrame, position: int) -> pd.Timestamp:
    timestamp = frame.index[position]
    prior_close = (
        float(frame.iloc[:position]["close"].dropna().iloc[-1])
        if position
        else float(frame.iloc[0]["close"])
    )
    frame.loc[timestamp, ["open", "high", "low", "close", "volume"]] = [
        prior_close,
        prior_close,
        prior_close,
        prior_close,
        0.0,
    ]
    frame.loc[timestamp, "is_filled"] = True
    frame.loc[timestamp, "is_quarantined"] = False
    frame.loc[timestamp, "anomaly_spike"] = False
    frame.loc[timestamp, "anomaly_flat"] = False
    return timestamp


def test_real_canonical_single_gap_accepts_one_observed_bounded_fill() -> None:
    raw = _frame(20).loc[:, ["open", "high", "low", "close", "volume"]]
    missing_time = raw.index[5]
    predecessor = raw.index[4]
    successor = raw.index[6]
    prior_close = float(raw.loc[predecessor, "close"])
    canonical = canonicalize_ohlcv(
        raw.drop(index=missing_time),
        (raw.index[-1] + timedelta(hours=8)).to_pydatetime(),
    ).frame

    validated = validate_walk_forward_frame(canonical)

    assert validated.index[validated["is_filled"]].equals(pd.DatetimeIndex([missing_time]))
    assert validated.loc[missing_time, ["open", "high", "low", "close"]].eq(prior_close).all()
    assert float(validated.loc[missing_time, "volume"]) == 0.0
    assert not bool(validated.loc[missing_time, "entry_data_valid"])
    assert not bool(validated.loc[successor, "entry_data_valid"])
    assert validated.loc[[predecessor, missing_time, successor], "segment_id"].tolist() == [0, 0, 0]


@pytest.mark.parametrize(
    "malformation",
    (
        "filled_predecessor_of_long_gap",
        "filled_successor_of_long_gap",
        "consecutive_filled",
        "leading_filled",
        "trailing_filled",
        "segment_transition_through_filled",
        "filled_anomaly_flag",
    ),
)
def test_filled_topology_rejects_noncanonical_synthetic_rows(malformation: str) -> None:
    frame = _quality_topology_frame()
    if malformation in {
        "filled_predecessor_of_long_gap",
        "filled_successor_of_long_gap",
    }:
        gap_index = frame.index[5:7]
        frame.loc[gap_index, ["open", "high", "low", "close", "volume"]] = math.nan
        frame.loc[gap_index, "entry_data_valid"] = False
        frame.loc[frame.index[7]:, "segment_id"] = 1
        fill_position = 4 if malformation.startswith("filled_predecessor") else 7
        filled_time = _set_filled_quality_row(frame, fill_position)
        if fill_position == 4:
            frame.loc[filled_time, "entry_data_valid"] = False
        else:
            frame.loc[filled_time:, "entry_data_valid"] = False
    elif malformation == "consecutive_filled":
        _set_filled_quality_row(frame, 4)
        _set_filled_quality_row(frame, 5)
        frame.loc[frame.index[4]:, "entry_data_valid"] = False
    elif malformation == "leading_filled":
        _set_filled_quality_row(frame, 0)
        frame.loc[:, "entry_data_valid"] = False
    elif malformation == "trailing_filled":
        filled_time = _set_filled_quality_row(frame, len(frame) - 1)
        frame.loc[filled_time, "entry_data_valid"] = False
    elif malformation == "segment_transition_through_filled":
        filled_time = _set_filled_quality_row(frame, 5)
        frame.loc[filled_time, "entry_data_valid"] = False
        frame.loc[frame.index[6]:, "segment_id"] = 1
    else:
        filled_time = _set_filled_quality_row(frame, 5)
        frame.loc[filled_time:, "entry_data_valid"] = False
        frame.loc[filled_time, "anomaly_flat"] = True

    with pytest.raises(ValueError, match="filled"):
        validate_walk_forward_frame(frame)


@pytest.mark.parametrize(
    "malformation",
    (
        "single_gap",
        "leading_gap",
        "trailing_gap",
        "repeated_transition",
        "misplaced_transition",
        "transition_on_unobserved_boundary",
    ),
)
def test_long_gap_topology_rejects_fabricated_or_misplaced_transitions(
    malformation: str,
) -> None:
    """Only a bounded 2+ row canonical long gap can increment a segment."""
    frame = _quality_topology_frame()
    gap_start = 4
    gap_end = 6
    if malformation == "single_gap":
        gap_end = 5
    elif malformation == "leading_gap":
        gap_start = 0
        gap_end = 2
    elif malformation == "trailing_gap":
        gap_start = len(frame) - 2
        gap_end = len(frame)

    gap_index = frame.index[gap_start:gap_end]
    frame.loc[gap_index, ["open", "high", "low", "close", "volume"]] = math.nan
    frame.loc[gap_index, "entry_data_valid"] = False
    if gap_end < len(frame):
        frame.loc[frame.index[gap_end]:, "segment_id"] = 1

    if malformation == "repeated_transition":
        frame.loc[frame.index[gap_end + 1]:, "segment_id"] = 2
    elif malformation == "misplaced_transition":
        frame.loc[frame.index[gap_start - 1]:frame.index[gap_end - 1], "segment_id"] = 1
        frame.loc[frame.index[gap_end]:, "segment_id"] = 2
    elif malformation == "transition_on_unobserved_boundary":
        boundary = frame.index[gap_end]
        frame.loc[boundary, ["open", "high", "low", "close", "volume"]] = math.nan
        frame.loc[boundary, "entry_data_valid"] = False

    with pytest.raises(ValueError, match="long-gap|segment_id transition"):
        validate_walk_forward_frame(frame)


@pytest.mark.parametrize("boundary_side", ("predecessor", "successor"))
def test_real_canonical_long_gap_accepts_an_observed_quarantine_boundary(
    boundary_side: str,
) -> None:
    """An invalid raw candle remains observed even though canonical OHLCV is cleared."""
    raw = _frame(20).loc[:, ["open", "high", "low", "close", "volume"]]
    gap_times = tuple(raw.index[5:7])
    boundary_time = raw.index[4] if boundary_side == "predecessor" else raw.index[7]
    raw = raw.drop(index=list(gap_times))
    raw.loc[boundary_time, "high"] = raw.loc[boundary_time, "low"] - 1.0

    canonical = canonicalize_ohlcv(
        raw,
        (raw.index[-1] + timedelta(hours=8)).to_pydatetime(),
    ).frame
    validated = validate_walk_forward_frame(canonical)

    assert bool(validated.loc[boundary_time, "is_quarantined"])
    assert validated.loc[boundary_time, ["open", "high", "low", "close", "volume"]].isna().all()
    assert not validated.loc[list(gap_times), "is_quarantined"].any()
    assert validated.loc[list(gap_times), ["open", "high", "low", "close", "volume"]].isna().all().all()
    assert int(validated.loc[gap_times[-1], "segment_id"]) == 0
    assert int(validated.loc[raw.index[5], "segment_id"]) == 1


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


def _segmented_risk_scenario(*, lose_before_gap: bool) -> tuple[pd.DataFrame, pd.Timestamp, pd.Timestamp]:
    base = _fixture("entry_next_open.csv").iloc[:610].copy(deep=True)
    start = base.index[-1] + pd.Timedelta(hours=4)
    defaults: dict[str, object] = {
        "open": 99.0,
        "high": 100.0,
        "low": 98.0,
        "close": 99.0,
        "volume": 1.0,
        "warmup_complete": True,
        "entry_data_valid": True,
        "ema_200": 90.0,
        "entry_high": 110.0,
        "previous_close": 99.0,
        "previous_entry_high": 99.0,
        "atr_14": 2.0,
        "exit_low": 80.0,
        "baseline_atr_pct": 0.02,
    }
    signal = {
        "open": 99.0,
        "high": 101.0,
        "low": 98.0,
        "close": 100.0,
        "entry_high": 99.0,
        "previous_close": 99.0,
        "previous_entry_high": 99.0,
    }
    boundary = (
        {"open": 89.0, "high": 91.0, "low": 88.0, "close": 90.0}
        if lose_before_gap
        else {"open": 100.0, "high": 101.0, "low": 96.0, "close": 100.0}
    )
    records = [
        {**defaults, **signal},
        {**defaults, "open": 100.0, "high": 101.0, "low": 96.0, "close": 100.0},
        {**defaults, **boundary},
        {**defaults, **(signal if lose_before_gap else {})},
        {**defaults, "open": 100.0, "high": 101.0, "low": 96.0, "close": 100.0},
    ]
    full_index = pd.date_range(start, periods=7, freq="4h", tz="UTC")
    observed_index = full_index[[0, 1, 2, 5, 6]]
    appended = pd.DataFrame(records, index=observed_index).loc[:, base.columns]
    frame = pd.concat([base, appended])
    gap_boundary = observed_index[2]
    post_gap = observed_index[3]
    frame["_execution_segment_id"] = 0
    frame.loc[post_gap:, "_execution_segment_id"] = 1
    return frame, gap_boundary, post_gap


def test_gap_preserves_daily_loss_cooldown_but_a_fresh_fold_resets_it() -> None:
    """Only a new phase may clear account-level breaker memory after a loss."""
    frame, gap_boundary, post_gap = _segmented_risk_scenario(lose_before_gap=True)
    config = BacktestConfig(
        costs=CostConfig(fee_rate=0.0, slippage_rate=0.0),
        force_liquidate_at_end=True,
    )
    result = core_backtest(_real_core_request(frame, config))

    assert any(trade.net_pnl <= -4.0 for trade in result.trades)
    assert not any(
        order.side == "BUY"
        and order.status == OrderStatus.COMPLETED
        and order.signal_time >= post_gap.to_pydatetime()
        for order in result.orders
    )

    carried_equity = next(
        point.equity for point in result.equity_curve
        if point.timestamp == gap_boundary.to_pydatetime()
    )
    fresh = run_backtest(
        frame.loc[post_gap:].copy(deep=True),
        replace(config, initial_equity=carried_equity),
    )
    assert any(
        order.side == "BUY" and order.status == OrderStatus.COMPLETED
        for order in fresh.orders
    )


def test_gap_invalid_bar_preserves_volatility_halt_and_unstable_ratio_resets_recovery() -> None:
    """Only three consecutive valid stable ratios may release a pre-gap halt."""
    base = _fixture("entry_next_open.csv").iloc[:610].copy(deep=True)
    start = base.index[-1] + pd.Timedelta(hours=4)
    defaults: dict[str, object] = {
        "open": 99.0,
        "high": 101.0,
        "low": 98.0,
        "close": 100.0,
        "volume": 1.0,
        "warmup_complete": True,
        "entry_data_valid": True,
        "ema_200": 90.0,
        "entry_high": 99.0,
        "previous_close": 99.0,
        "previous_entry_high": 99.0,
        "atr_14": 2.0,
        "exit_low": 80.0,
        "baseline_atr_pct": 0.02,
    }
    invalid_after_gap = {
        **defaults,
        "warmup_complete": False,
        "entry_data_valid": False,
        "ema_200": math.nan,
        "entry_high": math.nan,
        "previous_close": math.nan,
        "previous_entry_high": math.nan,
        "atr_14": math.nan,
        "exit_low": math.nan,
        "baseline_atr_pct": math.nan,
    }
    high_volatility = {**defaults, "atr_14": 10.0}
    renewed_unstable = {**defaults, "atr_14": 4.0}
    records = [
        high_volatility,
        invalid_after_gap,
        defaults,
        defaults,
        renewed_unstable,
        defaults,
        defaults,
        defaults,
        defaults,
    ]
    full_index = pd.date_range(start, periods=11, freq="4h", tz="UTC")
    observed_index = full_index[[0, 3, 4, 5, 6, 7, 8, 9, 10]]
    frame = pd.concat(
        [base, pd.DataFrame(records, index=observed_index).loc[:, base.columns]]
    )
    frame["_execution_segment_id"] = 0
    frame.loc[observed_index[1]:, "_execution_segment_id"] = 1

    result = core_backtest(
        _real_core_request(
            frame,
            BacktestConfig(
                costs=CostConfig(fee_rate=0.0, slippage_rate=0.0),
                force_liquidate_at_end=True,
            ),
        )
    )

    entries = [
        order
        for order in result.orders
        if order.side == "BUY" and order.status == OrderStatus.CREATED
    ]
    assert len(entries) == 1
    assert entries[0].signal_time == observed_index[7].to_pydatetime()


def test_gap_force_flat_has_native_costed_ledger_and_cancels_the_stop() -> None:
    """A live position and stop terminate at the last observed close, never after a gap."""
    frame, gap_boundary, _ = _segmented_risk_scenario(lose_before_gap=False)
    config = BacktestConfig(
        costs=CostConfig(fee_rate=0.001, slippage_rate=0.002),
        force_liquidate_at_end=True,
    )

    completed = _run_request(_real_core_request(frame, config), core_backtest)

    assert completed.status == "COMPLETED"
    assert completed.result is not None
    result = completed.result
    trade = next(trade for trade in result.trades if trade.exit_reason == "FORCED_GAP")
    forced_orders = [order for order in result.orders if order.reason == "FORCED_GAP"]
    assert [order.status for order in forced_orders] == [
        OrderStatus.CREATED,
        OrderStatus.SUBMITTED,
        OrderStatus.ACCEPTED,
        OrderStatus.COMPLETED,
    ]
    forced_fill = forced_orders[-1]
    assert forced_fill.fill_time == gap_boundary.to_pydatetime()
    assert forced_fill.fill_price == pytest.approx(float(frame.loc[gap_boundary, "close"]) * 0.998)
    assert forced_fill.fee == pytest.approx(
        forced_fill.filled_quantity * float(forced_fill.fill_price) * 0.001
    )
    assert forced_fill.slippage == pytest.approx(
        forced_fill.filled_quantity
        * (float(frame.loc[gap_boundary, "close"]) - float(forced_fill.fill_price))
    )
    assert trade.exit_time == forced_fill.fill_time
    assert trade.exit_price == pytest.approx(forced_fill.fill_price)
    assert trade.fees == pytest.approx(
        sum(
            order.fee
            for order in result.orders
            if order.status == OrderStatus.COMPLETED
        )
    )
    canceled_stops = [
        order for order in result.orders
        if order.status == OrderStatus.CANCELED and order.stop_price is not None
    ]
    assert canceled_stops
    assert all(order.reason == "DATA_GAP" for order in canceled_stops)
    assert all(order.occurred_at <= gap_boundary.to_pydatetime() for order in canceled_stops)
    assert result.total_fees == pytest.approx(trade.fees)
    assert result.total_slippage == pytest.approx(
        sum(
            order.slippage
            for order in result.orders
            if order.status == OrderStatus.COMPLETED
        )
    )


def test_real_core_completes_canonical_gap_and_quarantine_segments_flat() -> None:
    """The production engine closes before every unavailable-price region."""
    raw = _frame(1_250).loc[:, ["open", "high", "low", "close", "volume"]]
    gap_times = tuple(raw.index[620:622])
    quarantine_time = raw.index[900]
    raw = raw.drop(index=list(gap_times))
    raw.loc[quarantine_time, "high"] = raw.loc[quarantine_time, "low"] - 1.0
    canonical = canonicalize_ohlcv(
        raw, (raw.index[-1] + timedelta(hours=8)).to_pydatetime()
    ).frame
    enriched = compute_trend_indicators(canonical, StrategyConfig())
    available = enriched[["open", "high", "low", "close", "volume"]].notna().all(axis=1)
    enriched["_execution_segment_id"] = (~available).cumsum()
    execution = enriched.loc[available].copy(deep=True)
    request = BacktestRequest(
        phase="OOS", fold_id="fold-000", trial_id="baseline",
        trial=TrialConfig("baseline", 200, 50, 20, 14, 2.5),
        cost_id="baseline", cost=CostScenario("baseline", 0.0005, 0.0005),
        frame=execution,
        config=BacktestConfig(
            strategy=StrategyConfig(), costs=CostConfig(0.0005, 0.0005),
            force_liquidate_at_end=True,
        ),
        initial_equity=100.0, initial_state=PositionState.FLAT, pending_orders=0,
    )

    completed = _run_request(request, core_backtest)

    assert completed.status == "COMPLETED"
    assert completed.result is not None
    unavailable_datetimes = {
        timestamp.to_pydatetime() for timestamp in (*gap_times, quarantine_time)
    }
    assert not unavailable_datetimes.intersection(
        point.timestamp for point in completed.result.equity_curve
    )
    for unavailable_time in (gap_times[-1], quarantine_time):
        before = max(
            (
                point for point in completed.result.equity_curve
                if point.timestamp < unavailable_time.to_pydatetime()
            ),
            key=lambda point: point.timestamp,
        )
        after = min(
            (
                point for point in completed.result.equity_curve
                if point.timestamp > unavailable_time.to_pydatetime()
            ),
            key=lambda point: point.timestamp,
        )
        assert after.equity == pytest.approx(before.equity)
    assert all(
        not (
            trade.entry_time < unavailable.to_pydatetime() < trade.exit_time
        )
        for unavailable in (*gap_times, quarantine_time)
        for trade in completed.result.trades
    )
