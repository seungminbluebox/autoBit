"""Deterministic performance metrics for completed historical simulations."""

from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import math
import statistics

from autobit.backtest.engine import EquityPoint, OrderRecord, TradeRecord


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    """Finite report metrics; average loss is stored as a negative value."""

    total_return: float = 0.0
    annualized_return: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_duration_bars: int = 0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    win_rate: float = 0.0
    average_win: float = 0.0
    average_loss: float = 0.0
    average_win_loss_ratio: float = 0.0
    trade_count: int = 0
    mean_holding_bars: float = 0.0
    median_holding_bars: float = 0.0
    exposure: float = 0.0
    turnover: float = 0.0
    total_fees: float = 0.0
    total_slippage: float = 0.0


@dataclass(frozen=True, slots=True)
class _FillEvent:
    timestamp: datetime
    signed_quantity: float
    notional: float


def calculate_metrics(
    closed_pnls: Sequence[float] | None = None,
    *,
    equity_curve: Sequence[float | EquityPoint] | None = None,
    trades: Sequence[TradeRecord] | None = None,
    orders: Sequence[OrderRecord] | None = None,
    holding_bars: Sequence[float] | None = None,
    periods_per_year: int = 2190,
    exposure: float | None = None,
    turnover: float | None = None,
    total_fees: float | None = None,
    total_slippage: float = 0.0,
) -> PerformanceMetrics:
    """Calculate bar-based metrics without treating trades as elapsed periods.

    ``turnover`` is the two-sided traded notional divided by mean equity. When
    omitted with real trades, it is derived using entry plus exit notional.
    Exposure is the fraction of observed equity bars with a nonzero position.
    Order lifecycle records are de-duplicated into fill increments,
    so terminal open positions and partial fills contribute to exposure and
    turnover without changing closed-trade statistics. Filled orders require
    timestamped equity points unless exposure is explicit. Drawdown duration
    counts consecutive underwater equity bars. All undefined ratios use ``0.0``
    so serialized reports never contain NaN or Infinity.
    """
    if isinstance(periods_per_year, bool) or not isinstance(periods_per_year, int) or periods_per_year <= 0:
        raise ValueError("periods_per_year must be a positive integer")

    trade_records = tuple(trades or ())
    fill_events = _order_fill_events(tuple(orders or ()))
    if closed_pnls is None:
        pnl_values = tuple(float(trade.net_pnl) for trade in trade_records)
    else:
        pnl_values = _finite_values(closed_pnls, "closed PnL")
    _ensure_finite(pnl_values, "closed PnL")

    equity_values, equity_times = _equity_values(equity_curve or ())
    if holding_bars is None:
        holding_values = _trade_holding_bars(trade_records, equity_times)
    else:
        holding_values = _finite_values(holding_bars, "holding_bars")
        if any(value < 0.0 for value in holding_values):
            raise ValueError("holding_bars must be nonnegative")

    fee_value = (
        sum(float(trade.fees) for trade in trade_records)
        if total_fees is None
        else _finite_scalar(total_fees, "total_fees", nonnegative=True)
    )
    fee_value = _finite_scalar(fee_value, "total_fees", nonnegative=True)
    slippage_value = _finite_scalar(total_slippage, "total_slippage", nonnegative=True)

    if exposure is None:
        if fill_events and not equity_times:
            raise ValueError(
                "order-derived exposure requires timestamped equity or explicit exposure"
            )
        exposure_value = (
            _order_exposure(fill_events, equity_times)
            if fill_events
            else _derived_exposure(holding_values, len(equity_values))
        )
    else:
        exposure_value = _finite_scalar(exposure, "exposure")
    if not 0.0 <= exposure_value <= 1.0:
        raise ValueError("exposure must be in [0, 1]")

    if turnover is None:
        turnover_value = (
            _fill_turnover(fill_events, equity_values)
            if fill_events
            else _derived_turnover(trade_records, equity_values)
        )
    else:
        turnover_value = _finite_scalar(turnover, "turnover", nonnegative=True)
    turnover_value = _finite_scalar(turnover_value, "turnover", nonnegative=True)

    wins = tuple(value for value in pnl_values if value > 0.0)
    losses = tuple(value for value in pnl_values if value < 0.0)
    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    average_win = _finite_mean(wins) if wins else 0.0
    average_loss = _finite_mean(losses) if losses else 0.0

    returns = _period_returns(equity_values)
    total_return = equity_values[-1] / equity_values[0] - 1.0 if len(equity_values) >= 2 else 0.0
    total_return = _derived_finite(total_return)
    annualized_return = _annualized_return(total_return, len(returns), periods_per_year)
    max_drawdown, max_duration = _drawdown(equity_values)
    sharpe = _sharpe(returns, periods_per_year)
    sortino = _sortino(returns, periods_per_year)

    metrics = PerformanceMetrics(
        total_return=total_return,
        annualized_return=annualized_return,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=annualized_return / max_drawdown if max_drawdown > 0.0 else 0.0,
        max_drawdown=max_drawdown,
        max_drawdown_duration_bars=max_duration,
        profit_factor=gross_wins / gross_losses if gross_losses > 0.0 else 0.0,
        expectancy=_finite_mean(pnl_values) if pnl_values else 0.0,
        win_rate=len(wins) / len(pnl_values) if pnl_values else 0.0,
        average_win=average_win,
        average_loss=average_loss,
        average_win_loss_ratio=average_win / abs(average_loss) if average_loss < 0.0 else 0.0,
        trade_count=len(pnl_values),
        mean_holding_bars=_finite_mean(holding_values) if holding_values else 0.0,
        median_holding_bars=statistics.median(holding_values) if holding_values else 0.0,
        exposure=exposure_value,
        turnover=turnover_value,
        total_fees=fee_value,
        total_slippage=slippage_value,
    )
    if any(
        not math.isfinite(float(getattr(metrics, field.name)))
        for field in fields(metrics)
    ):
        raise ValueError("derived metrics must be finite")
    return metrics


def _finite_values(values: Sequence[float], name: str) -> tuple[float, ...]:
    try:
        converted = tuple(float(value) for value in values)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} values must be finite") from error
    _ensure_finite(converted, name)
    return converted


def _ensure_finite(values: Sequence[float], name: str) -> None:
    if any(not math.isfinite(value) for value in values):
        raise ValueError(f"{name} values must be finite")


def _finite_scalar(value: float, name: str, *, nonnegative: bool = False) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be finite") from error
    if isinstance(value, bool) or not math.isfinite(converted):
        raise ValueError(f"{name} must be finite")
    if nonnegative and converted < 0.0:
        raise ValueError(f"{name} must be nonnegative")
    return converted


def _equity_values(
    curve: Sequence[float | EquityPoint],
) -> tuple[tuple[float, ...], tuple[datetime, ...]]:
    if not curve:
        return (), ()
    points = tuple(curve)
    if all(isinstance(point, EquityPoint) for point in points):
        typed_points = tuple(point for point in points if isinstance(point, EquityPoint))
        times = tuple(point.timestamp for point in typed_points)
        if any(timestamp.tzinfo is None for timestamp in times):
            raise ValueError("equity timestamps must be timezone-aware")
        if any(right <= left for left, right in zip(times, times[1:])):
            raise ValueError("equity curve must be time-ordered")
        values = tuple(float(point.equity) for point in typed_points)
    elif any(isinstance(point, EquityPoint) for point in points):
        raise ValueError("equity curve cannot mix points and numbers")
    else:
        values = tuple(float(point) for point in points)
        times = ()
    _ensure_finite(values, "equity")
    if any(value < 0.0 for value in values) or (values and values[0] <= 0.0):
        raise ValueError("equity values must be nonnegative with a positive initial value")
    return values, times


def _period_returns(equity: Sequence[float]) -> tuple[float, ...]:
    returns: list[float] = []
    for previous, current in zip(equity, equity[1:]):
        if previous == 0.0:
            returns.append(0.0)
        else:
            returns.append(current / previous - 1.0)
    return tuple(returns)


def _annualized_return(total_return: float, periods: int, periods_per_year: int) -> float:
    if periods == 0 or total_return == 0.0:
        return 0.0
    if total_return <= -1.0:
        return -1.0
    try:
        result = (1.0 + total_return) ** (periods_per_year / periods) - 1.0
    except OverflowError as error:
        raise ValueError("derived metrics must be finite") from error
    return _derived_finite(result)


def _sharpe(returns: Sequence[float], periods_per_year: int) -> float:
    if not returns:
        return 0.0
    try:
        deviation = statistics.pstdev(returns)
    except OverflowError as error:
        raise ValueError("derived metrics must be finite") from error
    result = _finite_mean(returns) / deviation * math.sqrt(periods_per_year) if deviation > 0.0 else 0.0
    return _derived_finite(result)


def _sortino(returns: Sequence[float], periods_per_year: int) -> float:
    if not returns:
        return 0.0
    try:
        downside = math.sqrt(statistics.fmean(min(value, 0.0) ** 2 for value in returns))
    except OverflowError as error:
        raise ValueError("derived metrics must be finite") from error
    result = _finite_mean(returns) / downside * math.sqrt(periods_per_year) if downside > 0.0 else 0.0
    return _derived_finite(result)


def _drawdown(equity: Sequence[float]) -> tuple[float, int]:
    if not equity:
        return 0.0, 0
    peak = equity[0]
    max_drawdown = 0.0
    duration = 0
    max_duration = 0
    for value in equity[1:]:
        if value >= peak:
            peak = value
            duration = 0
            continue
        duration += 1
        max_duration = max(max_duration, duration)
        max_drawdown = max(max_drawdown, (peak - value) / peak)
    return max_drawdown, max_duration


def _trade_holding_bars(
    trades: Sequence[TradeRecord], equity_times: Sequence[datetime]
) -> tuple[float, ...]:
    if not trades:
        return ()
    if len(equity_times) >= 2:
        seconds = [
            (right - left).total_seconds()
            for left, right in zip(equity_times, equity_times[1:])
        ]
        bar_seconds = statistics.median(seconds)
    else:
        bar_seconds = 4.0 * 60.0 * 60.0
    if bar_seconds <= 0.0:
        raise ValueError("equity timestamps must have positive spacing")
    result = tuple(
        (trade.exit_time - trade.entry_time).total_seconds() / bar_seconds
        for trade in trades
    )
    if any(value < 0.0 for value in result):
        raise ValueError("trade exit_time must not precede entry_time")
    return result


def _order_fill_events(orders: Sequence[OrderRecord]) -> tuple[_FillEvent, ...]:
    cumulative: dict[str, tuple[str, float, float]] = {}
    events: list[_FillEvent] = []
    for order in orders:
        filled = _finite_scalar(
            order.filled_quantity, "order filled_quantity", nonnegative=True
        )
        prior_side, prior_quantity, prior_notional = cumulative.get(
            order.order_id, (order.side, 0.0, 0.0)
        )
        if order.side not in {"BUY", "SELL"} or order.side != prior_side:
            raise ValueError("order side must remain BUY or SELL")
        if filled + 1e-12 < prior_quantity:
            raise ValueError("order filled_quantity must be cumulative")
        if filled <= prior_quantity + 1e-12:
            continue
        if order.fill_time is None or order.fill_price is None:
            raise ValueError("new order fills require fill_time and fill_price")
        if order.fill_time.tzinfo is None or order.fill_time.utcoffset() is None:
            raise ValueError("order fill_time must be timezone-aware")
        price = _finite_scalar(order.fill_price, "order fill_price", nonnegative=True)
        cumulative_notional = filled * price
        delta_quantity = filled - prior_quantity
        delta_notional = cumulative_notional - prior_notional
        if delta_notional < 0.0 or not math.isfinite(delta_notional):
            raise ValueError("order fill notional must be finite and cumulative")
        cumulative[order.order_id] = (
            order.side,
            filled,
            cumulative_notional,
        )
        events.append(
            _FillEvent(
                timestamp=order.fill_time.astimezone(timezone.utc),
                signed_quantity=delta_quantity if order.side == "BUY" else -delta_quantity,
                notional=delta_notional,
            )
        )
    return tuple(sorted(events, key=lambda event: event.timestamp))


def _order_exposure(
    events: Sequence[_FillEvent], equity_times: Sequence[datetime]
) -> float:
    if not events or not equity_times:
        return 0.0
    position = 0.0
    event_index = 0
    exposed_bars = 0
    for timestamp in equity_times:
        while event_index < len(events) and events[event_index].timestamp <= timestamp:
            position += events[event_index].signed_quantity
            event_index += 1
        if position < -1e-9:
            raise ValueError("sell fills exceed accumulated buy fills")
        if position > 1e-12:
            exposed_bars += 1
    return exposed_bars / len(equity_times)


def _fill_turnover(
    events: Sequence[_FillEvent], equity: Sequence[float]
) -> float:
    if not events or not equity:
        return 0.0
    mean_equity = _finite_mean(equity)
    if mean_equity <= 0.0:
        return 0.0
    return _derived_finite(sum(event.notional for event in events) / mean_equity)


def _derived_exposure(holding_bars: Sequence[float], equity_count: int) -> float:
    if equity_count == 0:
        return 0.0
    return min(1.0, sum(holding_bars) / equity_count)


def _derived_turnover(trades: Sequence[TradeRecord], equity: Sequence[float]) -> float:
    if not trades or not equity:
        return 0.0
    mean_equity = _finite_mean(equity)
    if mean_equity <= 0.0:
        return 0.0
    traded_notional = sum(
        trade.quantity * (trade.entry_price + trade.exit_price) for trade in trades
    )
    return traded_notional / mean_equity


def _finite_mean(values: Sequence[float]) -> float:
    try:
        result = statistics.fmean(values)
    except OverflowError as error:
        raise ValueError("derived metrics must be finite") from error
    return _derived_finite(result)


def _derived_finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("derived metrics must be finite")
    return value
