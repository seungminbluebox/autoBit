"""Isolated, deterministic execution of pre-registered walk-forward cells."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Literal

import pandas as pd

from autobit.backtest.analyzers import PerformanceMetrics, calculate_metrics
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
from autobit.validation.models import (
    CostScenario,
    FoldWindow,
    ReturnPoint,
    StitchedOOSResult,
    TrialConfig,
    WalkForwardResult,
    WalkForwardRun,
    WalkForwardConfig,
)
from autobit.validation.splits import build_rolling_folds
from autobit.validation.trials import registered_cost_scenarios, registered_trials


_CANDLE_FREQUENCY = pd.Timedelta(hours=4)
_PHASES: tuple[Literal["TRAIN", "OOS"], ...] = ("TRAIN", "OOS")
_TRIAL_IDS = (
    "baseline",
    "ema_150",
    "ema_250",
    "entry_40",
    "entry_60",
    "exit_15",
    "exit_25",
    "stop_2_0",
    "stop_3_0",
)
_COST_IDS = ("zero", "baseline", "stress_10bps", "stress_20bps")


@dataclass(frozen=True, slots=True)
class BacktestRequest:
    """The entire isolated state visible to a train or OOS backtest invocation."""

    phase: Literal["TRAIN", "OOS"]
    fold_id: str
    trial_id: str
    trial: TrialConfig
    cost_id: str
    cost: CostScenario
    frame: pd.DataFrame
    config: BacktestConfig
    initial_equity: float
    initial_state: PositionState
    pending_orders: int

    def __post_init__(self) -> None:
        if self.phase not in _PHASES:
            raise ValueError("phase must be TRAIN or OOS")
        if not self.fold_id:
            raise ValueError("fold_id must not be empty")
        if self.trial_id != self.trial.trial_id:
            raise ValueError("trial_id must match trial")
        if self.cost_id != self.cost.cost_id:
            raise ValueError("cost_id must match cost")
        if not isinstance(self.frame, pd.DataFrame):
            raise ValueError("frame must be a pandas DataFrame")
        if self.initial_equity != 100.0 or self.config.initial_equity != 100.0:
            raise ValueError("walk-forward requests must start with equity 100")
        if self.initial_state is not PositionState.FLAT:
            raise ValueError("walk-forward requests must start FLAT")
        if self.pending_orders != 0:
            raise ValueError("walk-forward requests must have no pending orders")


BacktestFn = Callable[[BacktestRequest], BacktestResult]


def core_backtest(request: BacktestRequest) -> BacktestResult:
    """Production adapter retaining the core engine as the execution authority."""
    return run_backtest(request.frame, request.config)


def run_walk_forward(
    frame: pd.DataFrame,
    folds: Sequence[FoldWindow],
    backtest_fn: BacktestFn = core_backtest,
) -> WalkForwardResult:
    """Run one isolated train and OOS simulation for every fixed matrix cell.

    Each OOS invocation begins with a newly instantiated core configuration and
    an execution frame restricted to that fold's half-open OOS interval. The
    indicator calculation sees only the prefix that ends before the relevant
    phase end; it never observes later rows. Exceptions and invalid results
    are retained as failed rows rather than being removed from the matrix.
    """
    validated_frame = _validated_frame(frame)
    validated_folds = _validated_folds(validated_frame.index, folds)
    if not callable(backtest_fn):
        raise ValueError("backtest_fn must be callable")
    trials = tuple(registered_trials())
    costs = tuple(registered_cost_scenarios())
    _validate_registry(trials, costs)

    runs: list[WalkForwardRun] = []
    for fold in validated_folds:
        for trial in trials:
            for cost in costs:
                for phase in _PHASES:
                    request = _request_for_phase(
                        validated_frame,
                        fold=fold,
                        trial=trial,
                        cost=cost,
                        phase=phase,
                    )
                    runs.append(_run_request(request, backtest_fn))

    stitched = tuple(
        _stitch_oos(runs, validated_folds, trial, cost)
        for trial in trials
        for cost in costs
    )
    return WalkForwardResult(runs=tuple(runs), stitched_oos=stitched)


def _request_for_phase(
    frame: pd.DataFrame,
    *,
    fold: FoldWindow,
    trial: TrialConfig,
    cost: CostScenario,
    phase: Literal["TRAIN", "OOS"],
) -> BacktestRequest:
    phase_index = fold.train_index if phase == "TRAIN" else fold.test_index
    phase_end = fold.train_end if phase == "TRAIN" else fold.test_end
    strategy = StrategyConfig(
        ema_period=trial.ema_period,
        entry_period=trial.entry_period,
        exit_period=trial.exit_period,
        atr_period=trial.atr_period,
        initial_atr_mult=trial.stop_atr_mult,
    )
    context = frame.loc[frame.index < phase_end].copy(deep=True)
    enriched = compute_trend_indicators(context, strategy)
    execution = enriched.reindex(phase_index).copy(deep=True)
    return BacktestRequest(
        phase=phase,
        fold_id=fold.fold_id,
        trial_id=trial.trial_id,
        trial=trial,
        cost_id=cost.cost_id,
        cost=cost,
        frame=execution,
        config=BacktestConfig(
            strategy=strategy,
            costs=CostConfig(fee_rate=cost.fee_rate, slippage_rate=cost.slippage_rate),
            initial_equity=100.0,
            force_liquidate_at_end=True,
        ),
        initial_equity=100.0,
        initial_state=PositionState.FLAT,
        pending_orders=0,
    )


def _run_request(request: BacktestRequest, backtest_fn: BacktestFn) -> WalkForwardRun:
    try:
        result = backtest_fn(request)
        _validate_backtest_result(result, request)
        metrics = calculate_metrics(
            equity_curve=result.equity_curve,
            trades=result.trades,
            orders=result.orders,
            total_fees=result.total_fees,
            total_slippage=result.total_slippage,
        )
    except Exception as error:
        return WalkForwardRun(
            phase=request.phase,
            fold_id=request.fold_id,
            trial_id=request.trial_id,
            cost_id=request.cost_id,
            status="FAILED",
            result=None,
            metrics=None,
            error=f"{type(error).__name__}: {error}",
        )
    return WalkForwardRun(
        phase=request.phase,
        fold_id=request.fold_id,
        trial_id=request.trial_id,
        cost_id=request.cost_id,
        status="COMPLETED",
        result=result,
        metrics=metrics,
    )


def _stitch_oos(
    runs: Sequence[WalkForwardRun],
    folds: Sequence[FoldWindow],
    trial: TrialConfig,
    cost: CostScenario,
) -> StitchedOOSResult:
    keyed = {
        (run.fold_id, run.phase, run.trial_id, run.cost_id): run
        for run in runs
    }
    returns: list[ReturnPoint] = []
    equity: list[EquityPoint] = []
    trades: list[TradeRecord] = []
    complete = True
    compounded_equity = 100.0

    for fold in folds:
        train_run = keyed.get((fold.fold_id, "TRAIN", trial.trial_id, cost.cost_id))
        if train_run is None or train_run.status != "COMPLETED":
            complete = False
        run = keyed.get((fold.fold_id, "OOS", trial.trial_id, cost.cost_id))
        if run is None or run.status != "COMPLETED" or run.result is None:
            complete = False
            continue
        points = run.result.equity_curve
        if not points:
            complete = False
            continue
        trades.extend(run.result.trades)
        first = points[0]
        if not equity:
            equity.append(EquityPoint(first.timestamp, 100.0))
        for previous, current in zip(points, points[1:], strict=False):
            period_return = _within_fold_return(previous.equity, current.equity)
            returns.append(ReturnPoint(current.timestamp, period_return))
            compounded_equity *= 1.0 + period_return
            equity.append(EquityPoint(current.timestamp, compounded_equity))

    if len({point.timestamp for point in equity}) != len(equity):
        raise ValueError("stitched OOS equity timestamps must be unique")
    metrics = (
        calculate_metrics(
            equity_curve=tuple(equity),
            trades=tuple(trades),
            total_slippage=sum(
                run.result.total_slippage
                for run in keyed.values()
                if run.phase == "OOS"
                and run.trial_id == trial.trial_id
                and run.cost_id == cost.cost_id
                and run.status == "COMPLETED"
                and run.result is not None
            ),
        )
        if equity
        else None
    )
    return StitchedOOSResult(
        trial_id=trial.trial_id,
        cost_id=cost.cost_id,
        status="COMPLETE" if complete else "INCOMPLETE",
        returns=tuple(returns),
        equity_curve=tuple(equity),
        metrics=metrics,
    )


def _within_fold_return(previous_equity: float, current_equity: float) -> float:
    """Convert a fresh-100 fold curve to returns, including terminal bankruptcy."""
    if previous_equity > 0.0:
        return current_equity / previous_equity - 1.0
    if previous_equity == 0.0 and current_equity == 0.0:
        return 0.0
    raise ValueError("zero-equity recovery is not a valid OOS return path")


def _validated_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("frame must be a pandas DataFrame")
    required = ("open", "high", "low", "close", "volume")
    missing = tuple(column for column in required if column not in frame.columns)
    if missing:
        raise ValueError(f"frame is missing required columns: {', '.join(missing)}")
    index = _validated_utc_index(frame.index, "frame")
    if index.empty:
        raise ValueError("frame must not be empty")
    numeric = frame.loc[:, required].apply(pd.to_numeric, errors="coerce")
    if not math.isfinite(float(numeric.to_numpy().sum())) or not numeric.notna().all().all():
        raise ValueError("frame OHLCV values must be finite")
    return frame.copy(deep=True)


def _validated_folds(index: pd.DatetimeIndex, folds: Sequence[FoldWindow]) -> tuple[FoldWindow, ...]:
    if not isinstance(folds, Sequence) or isinstance(folds, (str, bytes)):
        raise ValueError("folds must be a sequence")
    result = tuple(folds)
    if not result:
        raise ValueError("folds must not be empty")
    ids: set[str] = set()
    previous_test_end: pd.Timestamp | None = None
    for ordinal, fold in enumerate(result):
        if (
            not isinstance(fold, FoldWindow)
            or fold.fold_id != f"fold-{ordinal:03d}"
            or fold.fold_id in ids
        ):
            raise ValueError("fold IDs must be known and unique")
        ids.add(fold.fold_id)
        train_start = _utc_timestamp(fold.train_start, "fold train_start")
        train_end = _utc_timestamp(fold.train_end, "fold train_end")
        test_start = _utc_timestamp(fold.test_start, "fold test_start")
        test_end = _utc_timestamp(fold.test_end, "fold test_end")
        if not train_start < train_end < test_start < test_end:
            raise ValueError("fold boundaries must be strictly ordered")
        if previous_test_end is not None and test_start < previous_test_end:
            raise ValueError("fold OOS windows must not overlap")
        previous_test_end = test_end
        expected_train = index[(index >= train_start) & (index < train_end)]
        expected_test = index[(index >= test_start) & (index < test_end)]
        if expected_train.empty or expected_test.empty:
            raise ValueError("fold intervals must be contained in frame")
        train_index = _validated_utc_index(fold.train_index, "fold train_index")
        test_index = _validated_utc_index(fold.test_index, "fold test_index")
        if not train_index.equals(expected_train) or not test_index.equals(expected_test):
            raise ValueError("fold indexes must exactly match half-open frame intervals")
    canonical = tuple(build_rolling_folds(index, WalkForwardConfig()))
    if len(result) != len(canonical) or any(
        not _same_fold(supplied, expected)
        for supplied, expected in zip(result, canonical, strict=True)
    ):
        raise ValueError("folds must match the complete canonical schedule")
    return canonical


def _same_fold(left: FoldWindow, right: FoldWindow) -> bool:
    """Compare fold content without pandas index truth-value ambiguity."""
    return (
        left.fold_id == right.fold_id
        and left.train_start == right.train_start
        and left.train_end == right.train_end
        and left.test_start == right.test_start
        and left.test_end == right.test_end
        and left.train_index.equals(right.train_index)
        and left.test_index.equals(right.test_index)
    )


def _validated_utc_index(index: object, name: str) -> pd.DatetimeIndex:
    if not isinstance(index, pd.DatetimeIndex):
        raise ValueError(f"{name} must be a pandas DatetimeIndex")
    if index.tz is None or str(index.tz) != "UTC":
        raise ValueError(f"{name} must use canonical UTC")
    if index.hasnans or not index.is_unique or not index.is_monotonic_increasing:
        raise ValueError(f"{name} must be sorted, unique, and nonmissing")
    if len(index) > 1 and not (
        index[1:].asi8 - index[:-1].asi8 == _CANDLE_FREQUENCY.value
    ).all():
        raise ValueError(f"{name} must have contiguous four-hour spacing")
    if (index.asi8 % _CANDLE_FREQUENCY.value != 0).any():
        raise ValueError(f"{name} must align to UTC four-hour boundaries")
    return index


def _utc_timestamp(value: object, name: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None or str(timestamp.tzinfo) != "UTC":
        raise ValueError(f"{name} must use canonical UTC")
    return timestamp.tz_convert("UTC")


def _validate_registry(trials: Sequence[TrialConfig], costs: Sequence[CostScenario]) -> None:
    trial_ids = tuple(trial.trial_id for trial in trials)
    cost_ids = tuple(cost.cost_id for cost in costs)
    if trial_ids != _TRIAL_IDS or len(set(trial_ids)) != len(trial_ids):
        raise ValueError("trial registry contains unknown or duplicate IDs")
    if cost_ids != _COST_IDS or len(set(cost_ids)) != len(cost_ids):
        raise ValueError("cost registry contains unknown or duplicate IDs")
    for cost in costs:
        if not all(math.isfinite(value) and value >= 0.0 for value in (cost.fee_rate, cost.slippage_rate)):
            raise ValueError("cost registry values must be finite and nonnegative")


def _validate_backtest_result(result: object, request: BacktestRequest) -> None:
    if not isinstance(result, BacktestResult):
        raise ValueError("backtest_fn must return BacktestResult")
    scalars = (result.final_equity, result.total_fees, result.total_slippage)
    if not all(math.isfinite(float(value)) for value in scalars):
        raise ValueError("backtest result values must be finite")
    if result.final_equity < 0.0 or result.total_fees < 0.0 or result.total_slippage < 0.0:
        raise ValueError("backtest result costs and equity must be nonnegative")
    if not result.equity_curve:
        raise ValueError("backtest result must contain phase equity")
    timestamps: list[datetime] = []
    previous_equity: float | None = None
    for point in result.equity_curve:
        if not isinstance(point, EquityPoint) or not _is_finite_number(point.equity) or point.equity < 0.0:
            raise ValueError("backtest equity must be finite and nonnegative")
        if previous_equity == 0.0 and point.equity > 0.0:
            raise ValueError("zero-equity recovery is not a valid backtest path")
        previous_equity = float(point.equity)
        timestamps.append(_phase_timestamp(point.timestamp, "backtest equity", request))
    if any(right <= left for left, right in zip(timestamps, timestamps[1:], strict=False)):
        raise ValueError("backtest equity timestamps must be strictly increasing")
    if not math.isclose(result.equity_curve[-1].equity, result.final_equity, rel_tol=0.0, abs_tol=1e-10):
        raise ValueError("backtest final equity must match the final phase equity")
    _validate_orders(result.orders, request)
    _validate_trades(result.trades, request)
    _validate_reconciliation(result)


def _validate_orders(orders: tuple[OrderRecord, ...], request: BacktestRequest) -> None:
    """Reject malformed order lifecycle evidence before metrics can consume it."""
    groups: dict[str, list[OrderRecord]] = {}
    occurred: list[datetime] = []
    for order in orders:
        if not isinstance(order, OrderRecord):
            raise ValueError("backtest orders must be OrderRecord values")
        if not isinstance(order.order_id, str) or not order.order_id:
            raise ValueError("backtest order IDs must be nonempty strings")
        if not isinstance(order.status, OrderStatus):
            raise ValueError("backtest order status must be valid")
        if order.side not in {"BUY", "SELL"}:
            raise ValueError("backtest order side must be BUY or SELL")
        requested, filled, remainder, fee, slippage = _finite_order_values(order)
        if requested <= 0.0 or filled < 0.0 or remainder < 0.0 or fee < 0.0 or slippage < 0.0:
            raise ValueError("backtest order quantities and costs must be nonnegative")
        if filled > requested + 1e-10 or not math.isclose(
            requested, filled + remainder, rel_tol=0.0, abs_tol=1e-10
        ):
            raise ValueError("backtest order quantities must reconcile")
        occurred_at = _phase_timestamp(order.occurred_at, "backtest order occurred_at", request)
        signal_time = _phase_timestamp(order.signal_time, "backtest order signal_time", request)
        if signal_time > occurred_at:
            raise ValueError("backtest order signal_time must not follow occurred_at")
        occurred.append(occurred_at)
        if filled > 0.0:
            if order.fill_time is None or order.fill_price is None:
                raise ValueError("filled backtest orders require time and price")
            fill_time = _phase_timestamp(order.fill_time, "backtest order fill_time", request)
            if fill_time < signal_time or not _is_finite_number(order.fill_price) or order.fill_price <= 0.0:
                raise ValueError("backtest order fills must be finite and ordered")
        elif order.fill_time is not None or order.fill_price is not None:
            raise ValueError("unfilled backtest orders must not have fill data")
        if order.stop_price is not None and (
            not _is_finite_number(order.stop_price) or order.stop_price <= 0.0
        ):
            raise ValueError("backtest stop_price must be finite and positive")
        groups.setdefault(order.order_id, []).append(order)
    if any(right < left for left, right in zip(occurred, occurred[1:], strict=False)):
        raise ValueError("backtest order records must be chronologically ordered")
    for lifecycle in groups.values():
        _validate_order_lifecycle(lifecycle)


def _finite_order_values(order: OrderRecord) -> tuple[float, float, float, float, float]:
    values = (
        order.requested_quantity,
        order.filled_quantity,
        order.remainder_quantity,
        order.fee,
        order.slippage,
    )
    if not all(_is_finite_number(value) for value in values):
        raise ValueError("backtest order values must be finite")
    return tuple(float(value) for value in values)  # type: ignore[return-value]


def _validate_order_lifecycle(lifecycle: list[OrderRecord]) -> None:
    first = lifecycle[0]
    if first.status is not OrderStatus.CREATED:
        raise ValueError("backtest order lifecycle must begin CREATED")
    terminal = {
        OrderStatus.COMPLETED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.INSUFFICIENT_CASH,
        OrderStatus.REJECTED,
    }
    transitions = {
        OrderStatus.CREATED: {OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.CANCELED},
        OrderStatus.SUBMITTED: {OrderStatus.ACCEPTED, OrderStatus.CANCELED, OrderStatus.REJECTED},
        OrderStatus.ACCEPTED: terminal | {OrderStatus.PARTIAL},
        OrderStatus.PARTIAL: {OrderStatus.PARTIAL, OrderStatus.COMPLETED, OrderStatus.CANCELED},
    }
    previous = lifecycle[0]
    for current in lifecycle[1:]:
        if current.side != previous.side or not math.isclose(
            current.requested_quantity, previous.requested_quantity, rel_tol=0.0, abs_tol=1e-10
        ):
            raise ValueError("backtest order lifecycle fields must remain stable")
        if current.status not in transitions.get(previous.status, set()):
            raise ValueError("backtest order lifecycle transition is invalid")
        if current.filled_quantity + 1e-10 < previous.filled_quantity or current.remainder_quantity > previous.remainder_quantity + 1e-10:
            raise ValueError("backtest order lifecycle quantities must be monotonic")
        previous = current
    if lifecycle[-1].status not in terminal:
        raise ValueError("backtest order lifecycle must be terminal")


def _validate_trades(trades: tuple[TradeRecord, ...], request: BacktestRequest) -> None:
    """Require finite, closed, internally consistent trade evidence."""
    for trade in trades:
        if not isinstance(trade, TradeRecord):
            raise ValueError("backtest trades must be TradeRecord values")
        values = (
            trade.quantity,
            trade.entry_price,
            trade.exit_price,
            trade.gross_pnl,
            trade.net_pnl,
            trade.fees,
        )
        if not all(_is_finite_number(value) for value in values):
            raise ValueError("backtest trade values must be finite")
        if trade.quantity <= 0.0 or trade.entry_price <= 0.0 or trade.exit_price <= 0.0 or trade.fees < 0.0:
            raise ValueError("backtest trade quantity, prices, and fees must be valid")
        entry_time = _phase_timestamp(trade.entry_time, "backtest trade entry_time", request)
        exit_time = _phase_timestamp(trade.exit_time, "backtest trade exit_time", request)
        if entry_time > exit_time:
            raise ValueError("backtest trade exit_time must not precede entry_time")
        gross = trade.quantity * (trade.exit_price - trade.entry_price)
        if not math.isclose(trade.gross_pnl, gross, rel_tol=0.0, abs_tol=1e-10):
            raise ValueError("backtest trade gross PnL must reconcile")
        if not math.isclose(trade.net_pnl, trade.gross_pnl - trade.fees, rel_tol=0.0, abs_tol=1e-10):
            raise ValueError("backtest trade net PnL must reconcile")
        if not isinstance(trade.exit_reason, str) or not trade.exit_reason:
            raise ValueError("backtest trade exit_reason must be nonempty")


def _validate_reconciliation(result: BacktestResult) -> None:
    """Cross-check cumulative ledger costs against top-level totals."""
    final_orders: dict[str, OrderRecord] = {}
    for order in result.orders:
        final_orders[order.order_id] = order
    if final_orders:
        order_fees = sum(order.fee for order in final_orders.values())
        order_slippage = sum(order.slippage for order in final_orders.values())
        if not math.isclose(result.total_fees, order_fees, rel_tol=0.0, abs_tol=1e-10):
            raise ValueError("backtest total_fees must reconcile with orders")
        if not math.isclose(result.total_slippage, order_slippage, rel_tol=0.0, abs_tol=1e-10):
            raise ValueError("backtest total_slippage must reconcile with orders")
    if result.trades and not math.isclose(
        result.total_fees,
        sum(trade.fees for trade in result.trades),
        rel_tol=0.0,
        abs_tol=1e-10,
    ):
        raise ValueError("backtest total_fees must reconcile with trades")


def _phase_timestamp(value: object, name: str, request: BacktestRequest) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    timestamp = value.astimezone(timezone.utc)
    if timestamp not in request.frame.index:
        raise ValueError(f"{name} must remain inside the execution phase")
    return timestamp


def _is_finite_number(value: object) -> bool:
    try:
        return not isinstance(value, bool) and math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False
