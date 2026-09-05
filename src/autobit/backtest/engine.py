"""Event-driven Backtrader adapter for enriched four-hour data."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math

import backtrader as bt
import pandas as pd

from autobit.config import CostConfig, RiskConfig, StrategyConfig
from autobit.domain.models import OrderStatus
from autobit.execution.backtest_broker import EventBacktestBroker, OneShotFractionalFiller
from autobit.risk.breakers import RiskDecision
from autobit.core.engine import StrategyEngine, forced_exit_reason
from autobit.core.models import DecisionInput, PositionContext
from autobit.core.risk_state import RiskState, RiskObservation, ClosedTradeObservation, advance_risk_state
from autobit.strategy.donchian_trend import initial_stop_price


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    strategy: StrategyConfig = StrategyConfig()
    risk: RiskConfig = RiskConfig()
    costs: CostConfig = CostConfig()
    initial_equity: float = 100.0
    entry_fill_fraction: float = field(default=1.0, kw_only=True)
    exit_fill_fraction: float = field(default=1.0, kw_only=True)
    force_liquidate_at_end: bool = field(default=False, kw_only=True)

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
        if not isinstance(self.force_liquidate_at_end, bool):
            raise ValueError("force_liquidate_at_end must be boolean")


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
        "gap_before_current_bar",
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
        ("gap_before_current_bar", "_gap_before_current_bar"),
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
        self._recorded_event_keys: set[tuple[int, OrderStatus, float]] = set()
        self._tracked_orders: dict[int, bt.Order] = {}
        self._run_order_ids: dict[int, str] = {}
        self._next_run_order_id = 1
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
        self._entry_fill_bar: int | None = None
        self._run_start_equity = float(self.broker.getvalue())
        self._engine = StrategyEngine(self.adapter_config.strategy, self.adapter_config.costs)
        self._risk_state = RiskState(
            initial_equity=self._run_start_equity, equity_peak=self._run_start_equity,
            daily_baseline_equity=self._run_start_equity, last_equity=self._run_start_equity,
        )
        self._gap_handled_current_bar = False
        self._terminal_final_equity: float | None = None
        self.broker.set_pre_submit_hook(self._on_order_created)
        self.broker.set_pre_match_hook(self._handle_gap_before_current_bar)
        self.broker.set_same_bar_order_hook(self.notify_order)

    def next(self) -> None:
        now = self._now()
        equity = float(self.broker.getvalue())
        if self.equity_points and self.equity_points[-1].timestamp == now:
            self.equity_points[-1] = EquityPoint(now, equity)
        else:
            self.equity_points.append(EquityPoint(now, equity))
        if self._gap_handled_current_bar:
            self._gap_handled_current_bar = False
            self._evaluate_current_risk(now, equity)
            return
        risk_decision = self._evaluate_current_risk(now, equity)

        if self._entry_partial_pending:
            self._cancel_entry_remainder()
        if self._exit_partial_pending:
            self._reissue_exit_remainder()
            return

        row = self._row()
        position = self._position_context()
        decision = self._engine.decide(DecisionInput(
            row=row, cash=float(self.broker.getcash()), equity=equity,
            position=position, has_pending_order=self.entry_order is not None or self.exit_order is not None,
            risk=risk_decision,
        ))
        if position is not None:
            if self.stop_order is None and self.exit_order is None:
                self._submit_stop()
            if decision.action == "sell":
                if self.exit_order is None:
                    self._cancel_entry_remainder()
                    self._submit_market_exit(decision.reason, quantity=decision.quantity)
            elif decision.next_stop is not None and self.exit_order is None:
                self._update_trailing_stop(decision.next_stop)
            self.high_water = max(float(self.high_water), float(self.data.high[0]))
            return
        if decision.action != "buy":
            return

        stop, size = decision.next_stop, decision.quantity
        atr = float(self.data.atr_value[0])
        baseline_atr_pct = float(self.data.baseline_atr_pct[0])
        self.initial_stop = stop
        self.current_stop = stop
        self.entry_signal_time = self._now()
        self.entry_order = self.buy(
            size=size,
            signal_time=self.entry_signal_time.isoformat(),
            reason=decision.reason,
            fill_fraction=self.adapter_config.entry_fill_fraction,
            execution_cap_enabled=True,
            execution_atr=atr,
            initial_atr_mult=self.adapter_config.strategy.initial_atr_mult,
            baseline_atr_pct=baseline_atr_pct,
            risk_rate=decision.risk.risk_rate,
            exposure_cap=decision.risk.exposure_cap,
            fee_rate=self.adapter_config.costs.fee_rate,
        )

    def notify_order(self, order: bt.Order) -> None:
        self._capture_new_fills(order)
        self._record_callback(order)
        if _same_order(order, self.entry_order) and order.status == order.Completed:
            self._apply_entry_fill(order)
            self.entry_order = None
        if _same_order(order, self.entry_order) and order.status == order.Partial:
            self._apply_entry_fill(order)
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
        if order.issell() and order.status == order.Completed and self.position.size == 0.0:
            self._reset_position_tracking()

    def _apply_entry_fill(self, order: bt.Order) -> None:
        self.entry_price = float(order.executed.price)
        entry_atr = float(order.info.execution_atr)
        initial_atr_mult = float(order.info.initial_atr_mult)
        self.initial_stop = initial_stop_price(self.entry_price, entry_atr, initial_atr_mult)
        self.current_stop = self.initial_stop
        self.high_water = self.entry_price
        if self._entry_fill_bar is None:
            self._entry_fill_bar = len(self.data)
        self._submit_stop(reconcile_current_bar=True)

    def stop(self) -> None:
        for order in self._tracked_orders.values():
            self._record_native_status(order)
            if order.alive():
                self.broker.cancel_end_of_data(order)
                self._record_native_status(order)
        if self.adapter_config.force_liquidate_at_end and self.position.size > 0.0:
            self._force_liquidate_at_end()

    def _force_liquidate_at_end(self) -> None:
        """Close remaining inventory at the final observed close for isolated runs."""
        self._liquidate_at_observed_close("FORCED_END", terminal=True)

    def _handle_gap_before_current_bar(self) -> None:
        """Flatten at a newly observed segment before the broker can match orders."""
        if not bool(self.data.gap_before_current_bar[0]):
            return
        self._gap_handled_current_bar = True
        for order in tuple(self._tracked_orders.values()):
            self._record_native_status(order)
            if order.alive():
                self.broker.cancel_end_of_data(order, terminal_reason="DATA_GAP")
                self._record_native_status(order)
        self.entry_order = None
        self.exit_order = None
        self.stop_order = None
        self._entry_partial_pending = False
        self._exit_partial_pending = False
        if self.position.size > 0.0:
            self._liquidate_at_current_open("FORCED_GAP")
        else:
            self._entry_fill_bits.clear()
            self._exit_fill_bits.clear()
        self._reset_position_tracking()
        if abs(float(self.position.size)) > 1e-12 or self.broker.get_orders_open():
            raise RuntimeError("data-gap boundary left open broker state")

    def _liquidate_at_observed_close(self, reason: str, *, terminal: bool) -> None:
        """Close inventory natively at the current observed close."""
        self._liquidate_at_reference(
            reason,
            reference_price=float(self.data.close[0]),
            terminal=terminal,
        )

    def _liquidate_at_current_open(self, reason: str) -> None:
        """Close inventory natively at the newly observed post-gap open."""
        self._liquidate_at_reference(
            reason,
            reference_price=float(self.data.open[0]),
            terminal=False,
        )

    def _liquidate_at_reference(
        self,
        reason: str,
        *,
        reference_price: float,
        terminal: bool,
    ) -> None:
        quantity = float(self.position.size)
        if (
            not math.isfinite(quantity)
            or quantity <= 0.0
            or not math.isfinite(reference_price)
            or reference_price <= 0.0
        ):
            return
        now = self._now()
        fill_price = reference_price * (1.0 - float(self.adapter_config.costs.slippage_rate))
        if not math.isfinite(fill_price):
            raise ValueError("terminal liquidation values must be finite")
        if fill_price <= 0.0:
            raise ValueError("terminal liquidation values must be nonnegative")
        self.exit_order = self.sell(
            size=quantity,
            signal_time=now.isoformat(),
            reason=reason,
            fill_fraction=1.0,
            terminal_reference_price=reference_price,
        )
        if self.exit_order is None or not self.broker.settle_immediate_market_order(
            self.exit_order, fill_price
        ):
            raise RuntimeError("terminal liquidation could not settle natively")
        if abs(float(self.position.size)) > 1e-12 or self.broker.get_orders_open():
            raise RuntimeError("terminal liquidation left open broker state")
        final_equity = float(self.broker.getvalue())
        if not math.isfinite(final_equity) or final_equity < 0.0:
            raise ValueError("terminal liquidation equity must be finite and nonnegative")
        if terminal:
            self._terminal_final_equity = final_equity
        if self.equity_points and self.equity_points[-1].timestamp == now:
            self.equity_points[-1] = EquityPoint(now, final_equity)
        else:
            self.equity_points.append(EquityPoint(now, final_equity))

    def _submit_stop(self, *, reconcile_current_bar: bool = False) -> None:
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
        if (
            reconcile_current_bar
            and self.stop_order is not None
            and float(self.data.low[0]) <= self.current_stop
        ):
            self.broker.reconcile_same_bar_stop(self.stop_order)

    def _update_trailing_stop(self, candidate: float) -> None:
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

    def _submit_market_exit(self, reason: str, *, quantity: float | None = None) -> None:
        signal_time = self._now()
        if self.stop_order is not None:
            self.cancel(self.stop_order)
            self.stop_order = None
        self.exit_order = self.sell(
            size=float(self.position.size) if quantity is None else quantity,
            signal_time=signal_time.isoformat(),
            reason=reason,
            fill_fraction=self.adapter_config.exit_fill_fraction,
        )

    def _position_context(self) -> PositionContext | None:
        if self.position.size <= 0.:
            return None
        return PositionContext(
            float(self.entry_price), float(self.initial_stop), float(self.current_stop),
            float(self.high_water), float(self.position.size),
            len(self.data) - int(self._entry_fill_bar),
        )

    def _reset_position_tracking(self) -> None:
        self.initial_stop = None
        self.current_stop = None
        self.entry_price = None
        self.high_water = None
        self.entry_signal_time = None
        self._entry_fill_bar = None

    def _cancel_entry_remainder(self) -> None:
        self._entry_partial_pending = False
        if self.entry_order is not None and self.entry_order.alive():
            if self._is_last_bar():
                self.entry_order.addinfo(terminal_reason="END_OF_DATA")
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
            if self._is_last_bar():
                original.addinfo(terminal_reason="END_OF_DATA")
            self.cancel(original)
        if _same_order(original, self.stop_order):
            self.stop_order = None
        if _same_order(original, self.exit_order):
            self.exit_order = None
        if remainder <= 0.0:
            return
        if self._is_last_bar():
            return
        self.exit_order = self.sell(
            size=remainder,
            signal_time=original.info.signal_time,
            reason=original.info.reason,
            stop_price=original.info.get("stop_price"),
            fill_fraction=1.0,
            is_remainder=True,
        )

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
                "baseline_atr_pct": float(self.data.baseline_atr_pct[0]),
            }
        )

    def _evaluate_current_risk(self, now: datetime, equity: float) -> RiskDecision:
        healthy = self._risk_inputs_are_healthy(now, equity)
        close, atr, baseline = (float(self.data.close[0]), float(self.data.atr_value[0]),
                                float(self.data.baseline_atr_pct[0]))
        ratio = (atr / close) / baseline if close > 0. and baseline > 0. else math.nan
        if not math.isfinite(equity) or equity < 0.:
            return RiskDecision(0., 0., None, ("system_unhealthy",))
        self._risk_state = advance_risk_state(self._risk_state, RiskObservation(
            now=now, equity=equity,
            closed_trades=tuple(ClosedTradeObservation(t.net_pnl, t.exit_time) for t in self.trade_records),
            volatility_ratio=ratio,
            volatility_bar_valid=healthy and bool(self.data.warmup_complete[0])
                                 and bool(self.data.entry_data_valid[0]) and math.isfinite(ratio),
            system_healthy=healthy,
        ), config=self.adapter_config.risk)
        return self._risk_state.decision

    @staticmethod
    def _requires_forced_exit(decision: RiskDecision) -> bool:
        """Compatibility predicate; current policy lives in the common engine."""
        return forced_exit_reason(decision) is not None

    def _risk_inputs_are_healthy(self, now: datetime, equity: float) -> bool:
        prices = tuple(
            float(line[0])
            for line in (self.data.open, self.data.high, self.data.low, self.data.close)
        )
        open_price, high, low, close = prices
        return (
            all(math.isfinite(value) and value > 0.0 for value in prices)
            and high >= max(open_price, low, close)
            and low <= min(open_price, high, close)
            and math.isfinite(equity)
            and equity >= 0.0
            and (self._risk_state.last_risk_at is None or now > self._risk_state.last_risk_at)
        )

    def _on_order_created(self, order: bt.Order) -> None:
        if order.status != order.Created:
            raise RuntimeError("pre-submit hook received a non-Created order")
        self._tracked_orders[order.ref] = order
        self._run_order_ids[order.ref] = str(self._next_run_order_id)
        self._next_run_order_id += 1
        self._append_order_record(order, OrderStatus.CREATED)

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
            self._append_order_record(order, status)

    def _record_native_status(self, order: bt.Order) -> None:
        self._record_callback(order)

    def _append_order_record(self, order: bt.Order, status: OrderStatus) -> None:
        key = (order.ref, status, abs(float(order.executed.size)))
        if key in self._recorded_event_keys:
            return
        self._recorded_event_keys.add(key)
        self.order_records.append(self._order_record(order, status))

    def _order_record(self, order: bt.Order, status: OrderStatus) -> OrderRecord:
        signal_time = _utc_datetime(order.info.signal_time)
        has_fill = bool(order.executed.size)
        fill_time = _bt_utc(order.executed.dt) if has_fill else None
        return OrderRecord(
            order_id=self._run_order_ids[order.ref],
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
            reason=self._event_reason(order, status),
        )

    def _event_reason(self, order: bt.Order, status: OrderStatus) -> str | None:
        if status == OrderStatus.REJECTED:
            return order.info.get("rejection_reason", order.info.get("reason"))
        if status == OrderStatus.CANCELED:
            return order.info.get(
                "terminal_reason",
                order.info.get("partial_reason", order.info.get("reason")),
            )
        if status == OrderStatus.PARTIAL:
            return order.info.get("partial_reason", order.info.get("reason"))
        return order.info.get("reason")

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

        self._record_completed_trade(str(order.info.reason))

    def _record_completed_trade(self, exit_reason: str) -> None:
        """Record one complete entry/exit sequence from accumulated native fills."""

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
                exit_reason=exit_reason,
            )
        )
        self._entry_fill_bits.clear()
        self._exit_fill_bits.clear()

    def _execution_slippage(self, order: bt.Order) -> float:
        return sum(self._bit_slippage(order, bit) for bit in order.executed.exbits)

    def _bit_slippage(self, order: bt.Order, bit: object) -> float:
        terminal_reference = order.info.get("terminal_reference_price")
        if terminal_reference is None:
            fill_time = pd.Timestamp(_bt_utc(float(bit.dt)))
            reference_open = float(self.p.reference_opens[fill_time])
        else:
            reference_open = float(terminal_reference)
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

    def _is_last_bar(self) -> bool:
        return len(self.data) >= self.data.buflen()


def run_backtest(frame: pd.DataFrame, config: BacktestConfig = BacktestConfig()) -> BacktestResult:
    prepared = _prepare_frame(frame, config.strategy)
    cerebro = bt.Cerebro(cheat_on_open=False, stdstats=False)
    broker = EventBacktestBroker()
    cerebro.setbroker(broker)
    broker.set_coc(False)
    broker.setcash(float(config.initial_equity))
    broker.setcommission(commission=float(config.costs.fee_rate))
    broker.set_filler(OneShotFractionalFiller(broker))
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
    final_equity = (
        float(strategy._terminal_final_equity)
        if strategy._terminal_final_equity is not None
        else float(broker.getvalue())
    )
    return BacktestResult(
        equity_curve=tuple(strategy.equity_points),
        orders=tuple(strategy.order_records),
        trades=tuple(strategy.trade_records),
        final_equity=final_equity,
        total_fees=float(strategy.total_fees),
        total_slippage=float(strategy.total_slippage),
    )


def _prepare_frame(frame: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    prepared = frame.copy()
    executable = prepared.loc[:, ["open", "high", "low", "close", "volume"]].notna().all(axis=1)
    explicit_gap = (
        prepared["_gap_before_current_bar"].fillna(False).astype(bool)
        if "_gap_before_current_bar" in prepared
        else pd.Series(False, index=prepared.index, dtype=bool)
    )
    segment_column = next(
        (
            candidate
            for candidate in ("_execution_segment_id", "segment_id")
            if candidate in prepared
        ),
        None,
    )
    prepared = prepared.loc[executable].copy()
    if prepared.empty:
        raise ValueError("backtest frame has no executable OHLCV rows")
    timestamp_gap = prepared.index.to_series().diff().gt(pd.Timedelta(hours=4))
    derived_gap = timestamp_gap
    if segment_column is not None:
        prior_segment = prepared[segment_column].shift(1)
        derived_gap = derived_gap | (
            prior_segment.notna() & prepared[segment_column].ne(prior_segment)
        )
    prepared["_warmup_complete"] = prepared["warmup_complete"]
    prepared["_entry_data_valid"] = prepared["entry_data_valid"]
    prepared["_ema_value"] = prepared[f"ema_{config.ema_period}"]
    prepared["_atr_value"] = prepared[f"atr_{config.atr_period}"]
    prepared["_gap_before_current_bar"] = (
        explicit_gap.reindex(prepared.index, fill_value=False) | derived_gap.fillna(False)
    )
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
