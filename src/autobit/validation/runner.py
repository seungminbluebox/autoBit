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
_RECONCILIATION_TOLERANCE = 1e-10
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
_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
_QUALITY_BOOLEAN_COLUMNS = (
    "is_filled",
    "is_quarantined",
    "anomaly_spike",
    "anomaly_flat",
    "entry_data_valid",
)
_QUALITY_COLUMNS = (*_QUALITY_BOOLEAN_COLUMNS, "segment_id")


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


@dataclass(frozen=True, slots=True)
class _ValidatedOrderEvent:
    ordinal: int
    record: OrderRecord
    occurred_at: datetime
    signal_time: datetime
    fill_time: datetime | None


@dataclass(frozen=True, slots=True)
class _FillEvidence:
    timestamp: datetime
    side: str
    quantity: float
    price: float
    notional: float
    fee: float
    slippage: float


@dataclass(frozen=True, slots=True)
class _ClosedTradeCycle:
    entry_time: datetime
    exit_time: datetime
    quantity: float
    entry_notional: float
    exit_notional: float
    fees: float
    slippage: float


@dataclass(frozen=True, slots=True)
class _OrderEvidence:
    final_orders: tuple[OrderRecord, ...]
    fills: tuple[_FillEvidence, ...]
    buy_quantity: float
    sell_quantity: float
    fill_fees: float
    fill_slippage: float
    total_fees: float
    total_slippage: float
    cycles: tuple[_ClosedTradeCycle, ...]


BacktestFn = Callable[[BacktestRequest], BacktestResult]


def core_backtest(request: BacktestRequest) -> BacktestResult:
    """Run one phase with flat execution boundaries and continuous account risk."""
    segment_column = (
        "_execution_segment_id"
        if "_execution_segment_id" in request.frame
        else "segment_id"
    )
    execution = request.frame.copy(deep=True)
    execution["_force_flat_after_bar"] = False
    if segment_column in execution:
        following_segment = execution[segment_column].shift(-1)
        execution["_force_flat_after_bar"] = (
            following_segment.notna()
            & execution[segment_column].ne(following_segment)
        )
    return run_backtest(execution, request.config)


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
    validated_frame = validate_walk_forward_frame(frame)
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
    available = execution.loc[:, _OHLCV_COLUMNS].notna().all(axis=1)
    execution["_execution_segment_id"] = (~available).cumsum()
    execution = execution.loc[available].copy(deep=True)
    if execution.empty:
        raise ValueError("walk-forward phase has no executable quality segment")
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


def validate_walk_forward_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate finite candles plus canonical Plan 1 unavailable-price rows.

    A missing-price row is accepted only when the complete quality surface
    proves it is either quarantined or belongs to a segment-breaking long gap.
    All five OHLCV values must be finite together or unavailable together.
    """
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("frame must be a pandas DataFrame")
    missing = tuple(column for column in _OHLCV_COLUMNS if column not in frame.columns)
    if missing:
        raise ValueError(f"frame is missing required columns: {', '.join(missing)}")
    index = _validated_utc_index(frame.index, "frame")
    if index.empty:
        raise ValueError("frame must not be empty")
    raw = frame.loc[:, _OHLCV_COLUMNS]
    numeric = raw.apply(pd.to_numeric, errors="coerce")
    coerced_missing = raw.notna() & numeric.isna()
    if coerced_missing.any().any():
        raise ValueError("frame OHLCV values must be numeric or canonically unavailable")
    if any(
        not math.isfinite(float(value))
        for value in numeric.to_numpy().flat
        if not pd.isna(value)
    ):
        raise ValueError("frame OHLCV values must be finite when available")
    all_available = numeric.notna().all(axis=1)
    all_unavailable = numeric.isna().all(axis=1)
    if not (all_available | all_unavailable).all():
        raise ValueError("frame OHLCV rows cannot be partially unavailable")
    _validate_available_ohlcv(numeric, all_available)

    present_quality = tuple(column for column in _QUALITY_COLUMNS if column in frame)
    if present_quality and set(present_quality) != set(_QUALITY_COLUMNS):
        raise ValueError("frame quality surface must be complete")
    if all_unavailable.any() and not present_quality:
        raise ValueError("unavailable OHLCV rows require canonical quality flags")
    if present_quality:
        _validate_quality_surface(frame, numeric, all_available, all_unavailable)

    result = frame.copy(deep=True)
    result.loc[:, _OHLCV_COLUMNS] = numeric
    return result


def _validated_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Backward-compatible private alias used by focused adversarial tests."""
    return validate_walk_forward_frame(frame)


def _validate_available_ohlcv(
    numeric: pd.DataFrame, available: pd.Series
) -> None:
    rows = numeric.loc[available]
    if (rows.loc[:, ("open", "high", "low", "close")] <= 0.0).any().any():
        raise ValueError("available OHLC prices must be positive")
    if (rows["volume"] < 0.0).any():
        raise ValueError("available volume must be nonnegative")
    if (
        (rows["high"] < rows[["open", "close", "low"]].max(axis=1)).any()
        or (rows["low"] > rows[["open", "close", "high"]].min(axis=1)).any()
    ):
        raise ValueError("available OHLC geometry is invalid")


def _validate_quality_surface(
    frame: pd.DataFrame,
    numeric: pd.DataFrame,
    available: pd.Series,
    unavailable: pd.Series,
) -> None:
    for column in _QUALITY_BOOLEAN_COLUMNS:
        if not pd.api.types.is_bool_dtype(frame[column].dtype) or frame[column].isna().any():
            raise ValueError(f"quality flag {column} must contain strict booleans")
    segments = pd.to_numeric(frame["segment_id"], errors="coerce")
    if (
        segments.isna().any()
        or any(not math.isfinite(float(value)) for value in segments)
        or (segments < 0).any()
        or not (segments == segments.round()).all()
    ):
        raise ValueError("quality segment_id must be a nonnegative integer")
    segment_values = tuple(int(value) for value in segments)
    flags = frame.loc[:, _QUALITY_BOOLEAN_COLUMNS]
    filled = flags["is_filled"]
    quarantined = flags["is_quarantined"]
    spike = flags["anomaly_spike"]
    flat = flags["anomaly_flat"]
    entry_valid = flags["entry_data_valid"]
    if (filled & quarantined).any() or (spike & flat).any():
        raise ValueError("quality flags contain an impossible combination")
    if (unavailable & (filled | spike | flat)).any():
        raise ValueError("unavailable rows cannot be filled or anomalous candles")
    if (available & quarantined).any():
        raise ValueError("quarantined rows must clear all OHLCV values")
    filled_rows = numeric.loc[filled]
    if not filled_rows.empty and (
        not (filled_rows["volume"] == 0.0).all()
        or not (
            filled_rows["open"] == filled_rows["high"]
        ).all()
        or not (
            filled_rows["open"] == filled_rows["low"]
        ).all()
        or not (
            filled_rows["open"] == filled_rows["close"]
        ).all()
    ):
        raise ValueError("filled quality rows must be flat zero-volume candles")

    long_gap = unavailable & ~quarantined
    if segment_values[0] != 0:
        raise ValueError("quality segment_id must start at zero")
    current_segment = 0
    position = 0
    while position < len(frame):
        if segment_values[position] != current_segment:
            raise ValueError("quality segment_id transition does not follow a long-gap region")
        if not bool(long_gap.iloc[position]):
            position += 1
            continue

        gap_start = position
        if gap_start == 0 or not bool(available.iloc[gap_start - 1]):
            raise ValueError("a long-gap region must have a preceding finite canonical row")
        while position < len(frame) and bool(long_gap.iloc[position]):
            if segment_values[position] != current_segment:
                raise ValueError("quality segment_id must remain stable inside a long-gap region")
            position += 1
        if position - gap_start < 2:
            raise ValueError("a long-gap region must contain at least two unavailable rows")
        if position == len(frame) or not bool(available.iloc[position]):
            raise ValueError("a long-gap region must have a following finite canonical row")
        current_segment += 1
        if segment_values[position] != current_segment:
            raise ValueError("quality segment_id transition after a long-gap region must increment once")

    contamination = filled | quarantined | spike | flat
    expected_entry = available & ~(
        contamination.groupby(segments, sort=False)
        .transform(lambda rows: rows.rolling(200, min_periods=1).max())
        .astype(bool)
    )
    if not entry_valid.equals(expected_entry.astype(bool)):
        raise ValueError("quality entry_data_valid is inconsistent with contamination")


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
    if request.config.force_liquidate_at_end is not True:
        raise ValueError("walk-forward backtests must force liquidate at phase end")
    scalars = (result.final_equity, result.total_fees, result.total_slippage)
    if not all(math.isfinite(float(value)) for value in scalars):
        raise ValueError("backtest result values must be finite")
    if result.final_equity < 0.0 or result.total_fees < 0.0 or result.total_slippage < 0.0:
        raise ValueError("backtest result costs and equity must be nonnegative")
    if not result.equity_curve:
        raise ValueError("backtest result must contain phase equity")
    timestamps: list[datetime] = []
    previous_equity: float | None = None
    for ordinal, point in enumerate(result.equity_curve):
        if not isinstance(point, EquityPoint) or not _is_finite_number(point.equity) or point.equity < 0.0:
            raise ValueError("backtest equity must be finite and nonnegative")
        if ordinal == 0 and not _close_enough(point.equity, request.initial_equity):
            raise ValueError("first phase equity must match request initial equity")
        if previous_equity == 0.0 and point.equity > 0.0:
            raise ValueError("zero-equity recovery is not a valid backtest path")
        previous_equity = float(point.equity)
        timestamps.append(_phase_timestamp(point.timestamp, "backtest equity", request))
    if any(right <= left for left, right in zip(timestamps, timestamps[1:], strict=False)):
        raise ValueError("backtest equity timestamps must be strictly increasing")
    if not math.isclose(result.equity_curve[-1].equity, result.final_equity, rel_tol=0.0, abs_tol=1e-10):
        raise ValueError("backtest final equity must match the final phase equity")
    order_evidence = _validate_orders(result.orders, request)
    realized_net_pnl = _validate_trades(result.trades, request, order_evidence)
    _validate_reconciliation(result, order_evidence)
    if not _close_enough(result.final_equity, request.initial_equity + realized_net_pnl):
        raise ValueError("final phase equity must reconcile with closed trade cycles")


def _validate_orders(orders: tuple[OrderRecord, ...], request: BacktestRequest) -> _OrderEvidence:
    """Validate native order lifecycles and reconstruct non-short BTC inventory."""
    groups: dict[str, list[_ValidatedOrderEvent]] = {}
    occurred: list[datetime] = []
    for ordinal, order in enumerate(orders):
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
        fill_time: datetime | None = None
        if filled > 0.0:
            if order.fill_time is None or order.fill_price is None:
                raise ValueError("filled backtest orders require time and price")
            fill_time = _phase_timestamp(order.fill_time, "backtest order fill_time", request)
            if (
                fill_time < signal_time
                or fill_time > occurred_at
                or not _is_finite_number(order.fill_price)
                or order.fill_price <= 0.0
            ):
                raise ValueError("backtest order fills must be finite and ordered")
        elif order.fill_time is not None or order.fill_price is not None:
            raise ValueError("unfilled backtest orders must not have fill data")
        if order.stop_price is not None and (
            not _is_finite_number(order.stop_price) or order.stop_price <= 0.0
        ):
            raise ValueError("backtest stop_price must be finite and positive")
        groups.setdefault(order.order_id, []).append(
            _ValidatedOrderEvent(
                ordinal=ordinal,
                record=order,
                occurred_at=occurred_at,
                signal_time=signal_time,
                fill_time=fill_time,
            )
        )
    if any(right < left for left, right in zip(occurred, occurred[1:], strict=False)):
        raise ValueError("backtest order records must be chronologically ordered")
    fills_by_ordinal: dict[int, _FillEvidence] = {}
    final_orders: list[OrderRecord] = []
    for lifecycle in groups.values():
        final_order, fills = _validate_order_lifecycle(lifecycle)
        final_orders.append(final_order)
        for ordinal, fill in fills.items():
            fills_by_ordinal[ordinal] = fill

    inventory = 0.0
    fills: list[_FillEvidence] = []
    buy_quantity = 0.0
    sell_quantity = 0.0
    for ordinal, order in enumerate(orders):
        fill = fills_by_ordinal.get(ordinal)
        if fill is None:
            continue
        fills.append(fill)
        if order.side == "BUY":
            inventory += fill.quantity
            buy_quantity += fill.quantity
        else:
            inventory -= fill.quantity
            sell_quantity += fill.quantity
        if inventory < -_RECONCILIATION_TOLERANCE:
            raise ValueError("backtest fills must not create negative BTC inventory")
    total_fees = sum(order.fee for order in final_orders)
    fill_fees = sum(fill.fee for fill in fills)
    if not _close_enough(fill_fees, total_fees):
        raise ValueError("backtest order fees must reconcile with fill increments")
    total_slippage = sum(order.slippage for order in final_orders)
    fill_slippage = sum(fill.slippage for fill in fills)
    if not _close_enough(fill_slippage, total_slippage):
        raise ValueError("backtest order slippage must reconcile with fill increments")
    cycles = _reconstruct_closed_trade_cycles(fills)
    return _OrderEvidence(
        final_orders=tuple(final_orders),
        fills=tuple(fills),
        buy_quantity=buy_quantity,
        sell_quantity=sell_quantity,
        fill_fees=fill_fees,
        fill_slippage=fill_slippage,
        total_fees=total_fees,
        total_slippage=total_slippage,
        cycles=cycles,
    )


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


def _reconstruct_closed_trade_cycles(
    fills: Sequence[_FillEvidence],
) -> tuple[_ClosedTradeCycle, ...]:
    """Build exact flat-to-flat economics from chronological native fill increments."""
    inventory = 0.0
    entry_time: datetime | None = None
    entry_quantity = 0.0
    entry_notional = 0.0
    entry_fees = 0.0
    entry_slippage = 0.0
    exit_quantity = 0.0
    exit_notional = 0.0
    exit_fees = 0.0
    exit_slippage = 0.0
    cycles: list[_ClosedTradeCycle] = []

    for fill in fills:
        if fill.side == "BUY":
            if _close_enough(inventory, 0.0):
                entry_time = fill.timestamp
            inventory += fill.quantity
            entry_quantity += fill.quantity
            entry_notional += fill.notional
            entry_fees += fill.fee
            entry_slippage += fill.slippage
            continue

        inventory -= fill.quantity
        exit_quantity += fill.quantity
        exit_notional += fill.notional
        exit_fees += fill.fee
        exit_slippage += fill.slippage
        if inventory < -_RECONCILIATION_TOLERANCE:
            raise ValueError("backtest fills must not create negative BTC inventory")
        if not _close_enough(inventory, 0.0):
            continue
        if entry_time is None or not _close_enough(entry_quantity, exit_quantity):
            raise ValueError("backtest trade cycles must close matched BTC quantity")
        cycles.append(
            _ClosedTradeCycle(
                entry_time=entry_time,
                exit_time=fill.timestamp,
                quantity=entry_quantity,
                entry_notional=entry_notional,
                exit_notional=exit_notional,
                fees=entry_fees + exit_fees,
                slippage=entry_slippage + exit_slippage,
            )
        )
        entry_time = None
        entry_quantity = 0.0
        entry_notional = 0.0
        entry_fees = 0.0
        entry_slippage = 0.0
        exit_quantity = 0.0
        exit_notional = 0.0
        exit_fees = 0.0
        exit_slippage = 0.0

    if not _close_enough(inventory, 0.0):
        raise ValueError("forced-liquidation phase must finish with zero BTC inventory")
    return tuple(cycles)


def _validate_order_lifecycle(
    lifecycle: list[_ValidatedOrderEvent],
) -> tuple[OrderRecord, dict[int, _FillEvidence]]:
    first = lifecycle[0]
    if first.record.status is not OrderStatus.CREATED:
        raise ValueError("backtest order lifecycle must begin CREATED")
    if (
        not _close_enough(first.record.filled_quantity, 0.0)
        or not _close_enough(first.record.remainder_quantity, first.record.requested_quantity)
        or not _close_enough(first.record.fee, 0.0)
        or not _close_enough(first.record.slippage, 0.0)
    ):
        raise ValueError("backtest CREATED lifecycle state must be unfilled")
    terminal = {
        OrderStatus.COMPLETED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.INSUFFICIENT_CASH,
        OrderStatus.REJECTED,
    }
    transitions = {
        OrderStatus.CREATED: {OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.CANCELED},
        OrderStatus.SUBMITTED: {OrderStatus.ACCEPTED} | terminal,
        OrderStatus.ACCEPTED: terminal | {OrderStatus.PARTIAL},
        OrderStatus.PARTIAL: terminal | {OrderStatus.PARTIAL},
    }
    fills: dict[int, _FillEvidence] = {}
    pending_slippage = 0.0
    previous = lifecycle[0]
    for current in lifecycle[1:]:
        if (
            current.record.side != previous.record.side
            or not _close_enough(current.record.requested_quantity, previous.record.requested_quantity)
            or current.signal_time != previous.signal_time
        ):
            raise ValueError("backtest order lifecycle fields must remain stable")
        if current.record.status not in transitions.get(previous.record.status, set()):
            raise ValueError("backtest order lifecycle transition is invalid")
        if (
            current.record.filled_quantity + _RECONCILIATION_TOLERANCE < previous.record.filled_quantity
            or current.record.remainder_quantity
            > previous.record.remainder_quantity + _RECONCILIATION_TOLERANCE
            or current.record.fee + _RECONCILIATION_TOLERANCE < previous.record.fee
            or current.record.slippage + _RECONCILIATION_TOLERANCE < previous.record.slippage
        ):
            raise ValueError("backtest order lifecycle quantities must be monotonic")
        fill_delta = current.record.filled_quantity - previous.record.filled_quantity
        fee_delta = current.record.fee - previous.record.fee
        slippage_delta = current.record.slippage - previous.record.slippage
        if current.record.status in terminal and current.record.status is not OrderStatus.COMPLETED:
            if fill_delta > _RECONCILIATION_TOLERANCE:
                raise ValueError("terminal order states must not add fills")
        if current.record.status in {OrderStatus.CREATED, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED} and (
            fill_delta > _RECONCILIATION_TOLERANCE
        ):
            raise ValueError("unfilled order lifecycle states must not add fills")
        if current.record.status is OrderStatus.PARTIAL and (
            fill_delta <= _RECONCILIATION_TOLERANCE
            or current.record.filled_quantity >= current.record.requested_quantity - _RECONCILIATION_TOLERANCE
        ):
            raise ValueError("PARTIAL order states require a proper incremental fill")
        if current.record.status is OrderStatus.COMPLETED and (
            current.record.filled_quantity <= _RECONCILIATION_TOLERANCE
            or not _close_enough(current.record.filled_quantity, current.record.requested_quantity)
            or not _close_enough(current.record.remainder_quantity, 0.0)
            or fill_delta <= _RECONCILIATION_TOLERANCE
        ):
            raise ValueError("COMPLETED orders must fully settle exactly once")
        if fill_delta <= _RECONCILIATION_TOLERANCE:
            pending_slippage += max(0.0, slippage_delta)
        if fill_delta > _RECONCILIATION_TOLERANCE:
            if current.fill_time is None:
                raise ValueError("incremental backtest fills require a fill timestamp")
            previous_notional = _cumulative_order_notional(previous.record)
            current_notional = _cumulative_order_notional(current.record)
            notional_delta = current_notional - previous_notional
            if not math.isfinite(notional_delta) or notional_delta <= _RECONCILIATION_TOLERANCE:
                raise ValueError("incremental backtest fills require positive notional")
            fills[current.ordinal] = _FillEvidence(
                timestamp=current.fill_time,
                side=current.record.side,
                quantity=fill_delta,
                price=notional_delta / fill_delta,
                notional=notional_delta,
                fee=max(0.0, fee_delta),
                slippage=pending_slippage + max(0.0, slippage_delta),
            )
            pending_slippage = 0.0
        previous = current
    if lifecycle[-1].record.status not in terminal:
        raise ValueError("backtest order lifecycle must be terminal")
    if pending_slippage > _RECONCILIATION_TOLERANCE:
        raise ValueError("backtest order slippage requires a fill increment")
    return lifecycle[-1].record, fills


def _cumulative_order_notional(order: OrderRecord) -> float:
    if order.filled_quantity <= _RECONCILIATION_TOLERANCE:
        return 0.0
    if order.fill_price is None or not _is_finite_number(order.fill_price):
        raise ValueError("filled backtest orders require finite fill notional")
    return float(order.filled_quantity) * float(order.fill_price)


def _validate_trades(
    trades: tuple[TradeRecord, ...],
    request: BacktestRequest,
    order_evidence: _OrderEvidence,
) -> float:
    """Require finite, closed, internally consistent trade evidence."""
    buy_fill_times = {fill.timestamp for fill in order_evidence.fills if fill.side == "BUY"}
    sell_fill_times = {fill.timestamp for fill in order_evidence.fills if fill.side == "SELL"}
    previous_exit: datetime | None = None
    trade_quantity = 0.0
    trade_fees = 0.0
    realized_net_pnl = 0.0
    if len(trades) != len(order_evidence.cycles):
        raise ValueError("backtest trade cycles must match closed fill cycles")
    for trade, cycle in zip(trades, order_evidence.cycles, strict=True):
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
        if previous_exit is not None and entry_time < previous_exit:
            raise ValueError("backtest trade chronology must not overlap")
        if entry_time not in buy_fill_times or exit_time not in sell_fill_times:
            raise ValueError("backtest trade times must match order fill evidence")
        expected_entry_price = cycle.entry_notional / cycle.quantity
        expected_exit_price = cycle.exit_notional / cycle.quantity
        expected_gross_pnl = cycle.exit_notional - cycle.entry_notional
        expected_net_pnl = expected_gross_pnl - cycle.fees
        if (
            entry_time != cycle.entry_time
            or exit_time != cycle.exit_time
            or not _close_enough(trade.quantity, cycle.quantity)
            or not _close_enough(trade.entry_price, expected_entry_price)
            or not _close_enough(trade.exit_price, expected_exit_price)
            or not _close_enough(trade.fees, cycle.fees)
            or not _close_enough(trade.gross_pnl, expected_gross_pnl)
            or not _close_enough(trade.net_pnl, expected_net_pnl)
        ):
            raise ValueError("backtest trade must reconcile with its closed fill cycle")
        gross = trade.quantity * (trade.exit_price - trade.entry_price)
        if not math.isclose(trade.gross_pnl, gross, rel_tol=0.0, abs_tol=1e-10):
            raise ValueError("backtest trade gross PnL must reconcile")
        if not math.isclose(trade.net_pnl, trade.gross_pnl - trade.fees, rel_tol=0.0, abs_tol=1e-10):
            raise ValueError("backtest trade net PnL must reconcile")
        if not isinstance(trade.exit_reason, str) or not trade.exit_reason:
            raise ValueError("backtest trade exit_reason must be nonempty")
        previous_exit = exit_time
        trade_quantity += trade.quantity
        trade_fees += trade.fees
        realized_net_pnl += trade.net_pnl
    if not _close_enough(trade_quantity, order_evidence.sell_quantity):
        raise ValueError("closed trade quantity must reconcile with sell fills")
    if not _close_enough(trade_fees, order_evidence.total_fees):
        raise ValueError("closed trade fees must reconcile with order fills")
    return realized_net_pnl


def _validate_reconciliation(result: BacktestResult, order_evidence: _OrderEvidence) -> None:
    """Cross-check cumulative ledger costs against top-level totals."""
    if not _close_enough(result.total_fees, order_evidence.total_fees):
        raise ValueError("backtest total_fees must reconcile with orders")
    if not _close_enough(result.total_slippage, order_evidence.total_slippage):
        raise ValueError("backtest total_slippage must reconcile with orders")


def _close_enough(left: object, right: object) -> bool:
    return (
        _is_finite_number(left)
        and _is_finite_number(right)
        and math.isclose(
            float(left),
            float(right),
            rel_tol=0.0,
            abs_tol=_RECONCILIATION_TOLERANCE,
        )
    )


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
