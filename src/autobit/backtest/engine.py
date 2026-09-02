"""Event-driven Backtrader adapter for enriched four-hour data."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math

import backtrader as bt
import pandas as pd

from autobit.config import CostConfig, RiskConfig, StrategyConfig
from autobit.domain.models import OrderStatus
from autobit.execution.backtest_broker import EventBacktestBroker, OneShotFractionalFiller
from autobit.risk.position_sizer import calculate_size
from autobit.strategy.donchian_trend import (
    PositionSnapshot,
    evaluate_close_exit,
    evaluate_entry,
    next_stop,
)


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    strategy: StrategyConfig = StrategyConfig()
    risk: RiskConfig = RiskConfig()
    costs: CostConfig = CostConfig()
    initial_equity: float = 100.0
    entry_fill_fraction: float = field(default=1.0, kw_only=True)
    exit_fill_fraction: float = field(default=1.0, kw_only=True)

    def __post_init__(self) -> None:
        try:
            initial_equity = float(self.initial_equity)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("initial equity must be finite and positive") from error
        if (
            isinstance(self.initial_equity, bool)
            or not math.isfinite(initial_equity)
            or initial_equity <= 0.0
        ):
            raise ValueError("initial equity must be finite and positive")

        for value in (self.entry_fill_fraction, self.exit_fill_fraction):
            try:
                fraction = float(value)
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError("fill fraction must be finite and in (0, 1]") from error
            if isinstance(value, bool) or not math.isfinite(fraction) or not 0.0 < fraction <= 1.0:
                raise ValueError("fill fraction must be finite and in (0, 1]")


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: datetime
    equity: float


@dataclass(frozen=True, slots=True)
class OrderRecord:
    order_id: str
    status: OrderStatus
    side: str
    requested_quantity: float
    filled_quantity: float
    remainder_quantity: float
    occurred_at: datetime
    signal_time: datetime
    fill_time: datetime | None = None
    fill_price: float | None = None
    fee: float = 0.0
    slippage: float = 0.0
    stop_price: float | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class TradeRecord:
    entry_time: datetime
    exit_time: datetime
    quantity: float
    entry_price: float
    exit_price: float
    gross_pnl: float
    net_pnl: float
    fees: float
    exit_reason: str


@dataclass(frozen=True, slots=True)
class BacktestResult:
    equity_curve: tuple[EquityPoint, ...]
    orders: tuple[OrderRecord, ...]
    trades: tuple[TradeRecord, ...]
    final_equity: float
    total_fees: float
    total_slippage: float


class EnrichedPandasData(bt.feeds.PandasData):
    """Pandas feed whose strategy inputs were computed before Backtrader."""

    lines = (
        "warmup_complete",
        "entry_data_valid",
        "ema_value",
        "entry_high",
        "previous_close",
        "previous_entry_high",
        "atr_value",
        "exit_low",
        "baseline_atr_pct",
    )
    params = (
        ("warmup_complete", "_warmup_complete"),
        ("entry_data_valid", "_entry_data_valid"),
        ("ema_value", "_ema_value"),
        ("entry_high", "entry_high"),
        ("previous_close", "previous_close"),
        ("previous_entry_high", "previous_entry_high"),
        ("atr_value", "_atr_value"),
        ("exit_low", "exit_low"),
        ("baseline_atr_pct", "baseline_atr_pct"),
    )


class _DonchianBacktestStrategy(bt.Strategy):
    params = (("adapter_config", None), ("reference_opens", None))

    def __init__(self) -> None:
        self.adapter_config: BacktestConfig = self.p.adapter_config
        self.order_records: list[OrderRecord] = []
        self.equity_points: list[EquityPoint] = []
        self.trade_records: list[TradeRecord] = []
        self.total_fees = 0.0
        self.total_slippage = 0.0
        self._seen_execution_bits: dict[int, int] = {}
        self._entry_fill_bits: list[tuple[datetime, float, float, float]] = []
        self._exit_fill_bits: list[tuple[datetime, float, float, float]] = []
        self._entry_partial_pending = False
        self._exit_partial_pending = False
        self.entry_order: bt.Order | None = None
        self.exit_order: bt.Order | None = None
        self.stop_order: bt.Order | None = None
        self.initial_stop: float | None = None
        self.current_stop: float | None = None
        self.entry_price: float | None = None
        self.high_water: float | None = None
        self.entry_signal_time: datetime | None = None

    def next(self) -> None:
        self.equity_points.append(EquityPoint(self._now(), float(self.broker.getvalue())))

        if self._entry_partial_pending:
            self._cancel_entry_remainder()
        if self._exit_partial_pending:
            self._reissue_exit_remainder()
            return

        if self.position.size > 0.0:
            if self.stop_order is None:
                self._submit_stop()
            if self.exit_order is None and evaluate_close_exit(self._row()):
                self._submit_close_exit()
                return
            self._update_trailing_stop()
            return
        if self.entry_order is not None:
            return

        row = self._row()
        if not evaluate_entry(row, is_flat=True, config=self.adapter_config.strategy):
            return

        entry_reference = float(self.data.close[0])
        atr = float(self.data.atr_value[0])
        stop = entry_reference - self.adapter_config.strategy.initial_atr_mult * atr
        baseline_atr_pct = float(self.data.baseline_atr_pct[0])
        size = calculate_size(
            equity=float(self.broker.getvalue()),
            cash=float(self.broker.getcash()),
            entry=entry_reference,
            stop=stop,
            current_atr_pct=atr / entry_reference,
            baseline_atr_pct=baseline_atr_pct,
            risk_rate=self.adapter_config.risk.base_risk_rate,
            exposure_cap=self.adapter_config.risk.max_exposure,
            costs=self.adapter_config.costs,
        ).quantity
        if size <= 0.0:
            return

        self.initial_stop = stop
        self.current_stop = stop
        self.entry_signal_time = self._now()
        self.entry_order = self.buy(
            size=size,
            signal_time=self.entry_signal_time.isoformat(),
            reason="ENTRY",
            fill_fraction=self.adapter_config.entry_fill_fraction,
        )
        self._record_created(self.entry_order)

    def notify_order(self, order: bt.Order) -> None:
        self._capture_new_fills(order)
        self._record_callback(order)
        if _same_order(order, self.entry_order) and order.status == order.Completed:
            self.entry_price = float(order.executed.price)
            self.high_water = self.entry_price
            self.entry_order = None
        if _same_order(order, self.entry_order) and order.status == order.Partial:
            self.entry_price = float(order.executed.price)
            self.high_water = self.entry_price
            self._entry_partial_pending = True
        if _same_order(order, self.entry_order) and order.status in (
            order.Canceled,
            order.Expired,
            order.Margin,
            order.Rejected,
        ):
            self.entry_order = None
        if _same_order(order, self.stop_order) and order.status in (
            order.Completed,
            order.Canceled,
            order.Expired,
            order.Margin,
            order.Rejected,
        ):
            self.stop_order = None
        if _same_order(order, self.exit_order) and order.status in (
            order.Completed,
            order.Canceled,
            order.Expired,
            order.Margin,
            order.Rejected,
        ):
            self.exit_order = None
        if order.status == order.Partial and (
            _same_order(order, self.exit_order) or _same_order(order, self.stop_order)
        ):
            self._exit_partial_pending = True

    def _submit_stop(self) -> None:
        if self.current_stop is None or self.entry_signal_time is None:
            return
        self.stop_order = self.sell(
            size=float(self.position.size),
            exectype=bt.Order.Stop,
            price=self.current_stop,
            signal_time=self.entry_signal_time.isoformat(),
            stop_price=self.current_stop,
            reason="HARD_STOP",
            fill_fraction=self.adapter_config.exit_fill_fraction,
        )
        self._record_created(self.stop_order)

    def _update_trailing_stop(self) -> None:
        if None in (self.entry_price, self.initial_stop, self.current_stop, self.high_water):
            return
        row = self._row()
        candidate = next_stop(
            PositionSnapshot(
                entry_price=float(self.entry_price),
                initial_stop=float(self.initial_stop),
                current_stop=float(self.current_stop),
                high_water=float(self.high_water),
            ),
            row,
            self.adapter_config.strategy,
        )
        self.high_water = max(float(self.high_water), float(self.data.high[0]))
        if candidate <= float(self.current_stop):
            return
        if self.stop_order is not None:
            self.cancel(self.stop_order)
            self.stop_order = None
        self.current_stop = candidate
        signal_time = self._now()
        self.stop_order = self.sell(
            size=float(self.position.size),
            exectype=bt.Order.Stop,
            price=candidate,
            signal_time=signal_time.isoformat(),
            stop_price=candidate,
            reason="TRAILING_STOP",
            fill_fraction=self.adapter_config.exit_fill_fraction,
        )
        self._record_created(self.stop_order)

    def _submit_close_exit(self) -> None:
        signal_time = self._now()
        if self.stop_order is not None:
            self.cancel(self.stop_order)
            self.stop_order = None
        self.exit_order = self.sell(
            size=float(self.position.size),
            signal_time=signal_time.isoformat(),
            reason="CLOSE_EXIT",
            fill_fraction=self.adapter_config.exit_fill_fraction,
        )
        self._record_created(self.exit_order)

    def _cancel_entry_remainder(self) -> None:
        self._entry_partial_pending = False
        if self.entry_order is not None and self.entry_order.alive():
            self.cancel(self.entry_order)
        self.entry_order = None

    def _reissue_exit_remainder(self) -> None:
        self._exit_partial_pending = False
        original = self.exit_order if self.exit_order is not None else self.stop_order
        if original is None:
            return
        remainder = min(
            abs(float(original.executed.remsize)),
            max(0.0, float(self.position.size)),
        )
        if original.alive():
            self.cancel(original)
        if _same_order(original, self.stop_order):
            self.stop_order = None
        if _same_order(original, self.exit_order):
            self.exit_order = None
        if remainder <= 0.0:
            return
        self.exit_order = self.sell(
            size=remainder,
            signal_time=original.info.signal_time,
            reason=original.info.reason,
            stop_price=original.info.get("stop_price"),
            fill_fraction=1.0,
            is_remainder=True,
        )
        self._record_created(self.exit_order)

    def _row(self) -> pd.Series:
        config = self.adapter_config.strategy
        return pd.Series(
            {
                "close": float(self.data.close[0]),
                "high": float(self.data.high[0]),
                "warmup_complete": bool(self.data.warmup_complete[0]),
                "entry_data_valid": bool(self.data.entry_data_valid[0]),
                f"ema_{config.ema_period}": float(self.data.ema_value[0]),
                "entry_high": float(self.data.entry_high[0]),
                "previous_close": float(self.data.previous_close[0]),
                "previous_entry_high": float(self.data.previous_entry_high[0]),
                f"atr_{config.atr_period}": float(self.data.atr_value[0]),
                "exit_low": float(self.data.exit_low[0]),
            }
        )

    def _record_created(self, order: bt.Order) -> None:
        self.order_records.append(self._order_record(order, OrderStatus.CREATED))

    def _record_callback(self, order: bt.Order) -> None:
        statuses = {
            order.Submitted: OrderStatus.SUBMITTED,
            order.Accepted: OrderStatus.ACCEPTED,
            order.Partial: OrderStatus.PARTIAL,
            order.Completed: OrderStatus.COMPLETED,
            order.Canceled: OrderStatus.CANCELED,
            order.Expired: OrderStatus.EXPIRED,
            order.Margin: OrderStatus.INSUFFICIENT_CASH,
            order.Rejected: OrderStatus.REJECTED,
        }
        status = statuses.get(order.status)
        if status is not None:
            self.order_records.append(self._order_record(order, status))

    def _order_record(self, order: bt.Order, status: OrderStatus) -> OrderRecord:
        signal_time = _utc_datetime(order.info.signal_time)
        has_fill = bool(order.executed.size)
        fill_time = _bt_utc(order.executed.dt) if has_fill else None
        return OrderRecord(
            order_id=str(order.ref),
            status=status,
            side="BUY" if order.isbuy() else "SELL",
            requested_quantity=abs(float(order.created.size)),
            filled_quantity=abs(float(order.executed.size)),
            remainder_quantity=abs(float(order.executed.remsize)),
            occurred_at=self._now(),
            signal_time=signal_time,
            fill_time=fill_time,
            fill_price=float(order.executed.price) if has_fill else None,
            fee=float(order.executed.comm),
            slippage=self._execution_slippage(order),
            stop_price=order.info.get("stop_price"),
            reason=order.info.get("reason"),
        )

    def _capture_new_fills(self, order: bt.Order) -> None:
        seen = self._seen_execution_bits.get(order.ref, 0)
        bits = list(order.executed.exbits)
        for bit in bits[seen:]:
            self.total_fees += float(bit.comm)
            self.total_slippage += self._bit_slippage(order, bit)
            self._capture_trade_fill(order, bit)
        self._seen_execution_bits[order.ref] = len(bits)

    def _capture_trade_fill(self, order: bt.Order, bit: object) -> None:
        fill = (
            _bt_utc(float(bit.dt)),
            abs(float(bit.size)),
            float(bit.price),
            float(bit.comm),
        )
        if order.isbuy():
            self._entry_fill_bits.append(fill)
            return
        self._exit_fill_bits.append(fill)
        if abs(float(bit.psize)) > 1e-12:
            return

        entry_quantity = sum(item[1] for item in self._entry_fill_bits)
        exit_quantity = sum(item[1] for item in self._exit_fill_bits)
        if entry_quantity <= 0.0 or exit_quantity <= 0.0:
            return
        entry_value = sum(item[1] * item[2] for item in self._entry_fill_bits)
        exit_value = sum(item[1] * item[2] for item in self._exit_fill_bits)
        fees = sum(item[3] for item in self._entry_fill_bits + self._exit_fill_bits)
        gross_pnl = exit_value - entry_value
        self.trade_records.append(
            TradeRecord(
                entry_time=self._entry_fill_bits[0][0],
                exit_time=self._exit_fill_bits[-1][0],
                quantity=entry_quantity,
                entry_price=entry_value / entry_quantity,
                exit_price=exit_value / exit_quantity,
                gross_pnl=gross_pnl,
                net_pnl=gross_pnl - fees,
                fees=fees,
                exit_reason=str(order.info.reason),
            )
        )
        self._entry_fill_bits.clear()
        self._exit_fill_bits.clear()

    def _execution_slippage(self, order: bt.Order) -> float:
        return sum(self._bit_slippage(order, bit) for bit in order.executed.exbits)

    def _bit_slippage(self, order: bt.Order, bit: object) -> float:
        fill_time = pd.Timestamp(_bt_utc(float(bit.dt)))
        reference_open = float(self.p.reference_opens[fill_time])
        if order.exectype == bt.Order.Stop:
            trigger = float(order.info.stop_price)
            reference = min(reference_open, trigger) if order.issell() else max(reference_open, trigger)
        else:
            reference = reference_open
        quantity = abs(float(bit.size))
        if order.isbuy():
            return max(0.0, float(bit.price) - reference) * quantity
        return max(0.0, reference - float(bit.price)) * quantity

    def _now(self) -> datetime:
        return _bt_utc(float(self.data.datetime[0]))


def run_backtest(frame: pd.DataFrame, config: BacktestConfig = BacktestConfig()) -> BacktestResult:
    prepared = _prepare_frame(frame, config.strategy)
    cerebro = bt.Cerebro(cheat_on_open=False, stdstats=False)
    broker = EventBacktestBroker()
    cerebro.setbroker(broker)
    broker.set_coc(False)
    broker.setcash(float(config.initial_equity))
    broker.setcommission(commission=float(config.costs.fee_rate))
    broker.set_filler(OneShotFractionalFiller())
    broker.set_slippage_perc(
        float(config.costs.slippage_rate),
        slip_open=True,
        slip_limit=True,
        slip_match=True,
        slip_out=False,
    )
    cerebro.adddata(EnrichedPandasData(dataname=prepared))
    reference_opens = {
        pd.Timestamp(timestamp): float(open_price)
        for timestamp, open_price in prepared["open"].items()
    }
    cerebro.addstrategy(
        _DonchianBacktestStrategy,
        adapter_config=config,
        reference_opens=reference_opens,
    )
    strategy = cerebro.run()[0]
    return BacktestResult(
        equity_curve=tuple(strategy.equity_points),
        orders=tuple(strategy.order_records),
        trades=tuple(strategy.trade_records),
        final_equity=float(broker.getvalue()),
        total_fees=float(strategy.total_fees),
        total_slippage=float(strategy.total_slippage),
    )


def _prepare_frame(frame: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    prepared = frame.copy()
    prepared["_warmup_complete"] = prepared["warmup_complete"]
    prepared["_entry_data_valid"] = prepared["entry_data_valid"]
    prepared["_ema_value"] = prepared[f"ema_{config.ema_period}"]
    prepared["_atr_value"] = prepared[f"atr_{config.atr_period}"]
    return prepared


def _utc_datetime(value: object) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.to_pydatetime()


def _bt_utc(value: float) -> datetime:
    if value is None or not math.isfinite(float(value)):
        raise ValueError("Backtrader timestamp is unavailable")
    return bt.num2date(float(value), tz=timezone.utc, naive=False)


def _same_order(left: bt.Order, right: bt.Order | None) -> bool:
    return right is not None and left.ref == right.ref
