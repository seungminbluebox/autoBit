"""Event-driven Backtrader adapter for enriched four-hour data."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math

import backtrader as bt
import pandas as pd

from autobit.config import CostConfig, RiskConfig, StrategyConfig
from autobit.domain.models import OrderStatus
from autobit.execution.backtest_broker import EventBacktestBroker, OneShotFractionalFiller
from autobit.risk.breakers import RiskDecision, evaluate_risk
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
        "force_flat_after_bar",
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
        ("force_flat_after_bar", "_force_flat_after_bar"),
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
        self._equity_peak = self._run_start_equity
        self._risk_equity_history: list[tuple[datetime, float]] = []
        self._daily_date = None
        self._daily_baseline_equity = self._run_start_equity
        self._last_equity = self._run_start_equity
        self._last_risk_time: datetime | None = None
        self._consecutive_losses = 0
        self._recovery_started_at: datetime | None = None
        self._daily_halt_started_at: datetime | None = None
        self._weekly_halt_started_at: datetime | None = None
        self._streak_halt_started_at: datetime | None = None
        self._profitable_trades_since_streak_halt = 0
        self._volatility_halted = False
        self._volatility_stable_bars = 0
        self._terminal_final_equity: float | None = None
        self.broker.set_pre_submit_hook(self._on_order_created)
        self.broker.set_same_bar_order_hook(self.notify_order)

    def next(self) -> None:
        now = self._now()
        equity = float(self.broker.getvalue())
        self.equity_points.append(EquityPoint(now, equity))
        if bool(self.data.force_flat_after_bar[0]):
            self._force_flat_for_gap()
            self._evaluate_current_risk(now, float(self.broker.getvalue()))
            return
        risk_decision = self._evaluate_current_risk(now, equity)

        if self._entry_partial_pending:
            self._cancel_entry_remainder()
        if self._exit_partial_pending:
            self._reissue_exit_remainder()
            return

        if self.position.size > 0.0:
            if self.stop_order is None:
                self._submit_stop()
            if self._requires_forced_exit(risk_decision):
                self._submit_forced_exit(risk_decision)
                return
            if self.exit_order is None and evaluate_close_exit(self._row()):
                self._submit_close_exit()
                return
            self.high_water = max(float(self.high_water), float(self.data.high[0]))
            if self.exit_order is None and self._stagnant_exit_due():
                self._submit_market_exit("STAGNANT_EXIT")
                return
            if self.exit_order is None and self._max_hold_exit_due():
                self._submit_market_exit("MAX_HOLD_EXIT")
                return
            self._update_trailing_stop()
            return
        if self.entry_order is not None:
            return

        row = self._row()
        if risk_decision.risk_rate <= 0.0 or risk_decision.exposure_cap <= 0.0:
            return
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
            risk_rate=risk_decision.risk_rate,
            exposure_cap=risk_decision.exposure_cap,
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
            execution_cap_enabled=True,
            execution_atr=atr,
            initial_atr_mult=self.adapter_config.strategy.initial_atr_mult,
            baseline_atr_pct=baseline_atr_pct,
            risk_rate=risk_decision.risk_rate,
            exposure_cap=risk_decision.exposure_cap,
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
        self.initial_stop = self.entry_price - initial_atr_mult * entry_atr
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

    def _force_flat_for_gap(self) -> None:
        """End broker/position state while retaining this phase's risk memory."""
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
            self._liquidate_at_observed_close("FORCED_GAP", terminal=False)
        self._reset_position_tracking()
        if abs(float(self.position.size)) > 1e-12 or self.broker.get_orders_open():
            raise RuntimeError("data-gap boundary left open broker state")

    def _liquidate_at_observed_close(self, reason: str, *, terminal: bool) -> None:
        """Close inventory natively at the current observed close."""
        quantity = float(self.position.size)
        reference_close = float(self.data.close[0])
        if (
            not math.isfinite(quantity)
            or quantity <= 0.0
            or not math.isfinite(reference_close)
            or reference_close <= 0.0
        ):
            return
        now = self._now()
        fill_price = reference_close * (1.0 - float(self.adapter_config.costs.slippage_rate))
        if not math.isfinite(fill_price):
            raise ValueError("terminal liquidation values must be finite")
        if fill_price <= 0.0:
            raise ValueError("terminal liquidation values must be nonnegative")
        self.exit_order = self.sell(
            size=quantity,
            signal_time=now.isoformat(),
            reason=reason,
            fill_fraction=1.0,
            terminal_reference_price=reference_close,
        )
        if self.exit_order is None or not self.broker.settle_terminal_market_order(
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

    def _submit_close_exit(self) -> None:
        self._submit_market_exit("CLOSE_EXIT")

    def _submit_market_exit(self, reason: str) -> None:
        signal_time = self._now()
        if self.stop_order is not None:
            self.cancel(self.stop_order)
            self.stop_order = None
        self.exit_order = self.sell(
            size=float(self.position.size),
            signal_time=signal_time.isoformat(),
            reason=reason,
            fill_fraction=self.adapter_config.exit_fill_fraction,
        )

    def _stagnant_exit_due(self) -> bool:
        if None in (
            self._entry_fill_bar,
            self.entry_price,
            self.initial_stop,
            self.high_water,
        ):
            return False
        completed_held_bars = len(self.data) - int(self._entry_fill_bar)
        if completed_held_bars < self.adapter_config.strategy.stagnant_bars:
            return False
        risk_per_unit = float(self.entry_price) - float(self.initial_stop)
        threshold = (
            float(self.entry_price)
            + self.adapter_config.strategy.stagnant_min_r * risk_per_unit
        )
        return float(self.high_water) < threshold

    def _max_hold_exit_due(self) -> bool:
        if self._entry_fill_bar is None:
            return False
        completed_held_bars = len(self.data) - self._entry_fill_bar
        return completed_held_bars >= self.adapter_config.strategy.max_holding_bars

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
            }
        )

    def _evaluate_current_risk(self, now: datetime, equity: float) -> RiskDecision:
        system_healthy = self._risk_inputs_are_healthy(now, equity)
        if math.isfinite(equity):
            self._equity_peak = max(self._equity_peak, equity)
        drawdown = (
            max(0.0, 1.0 - equity / self._equity_peak)
            if system_healthy and self._equity_peak > 0.0
            else math.nan
        )

        current_date = now.date()
        if self._daily_date is None:
            self._daily_date = current_date
        elif current_date != self._daily_date:
            self._daily_date = current_date
            self._daily_baseline_equity = self._last_equity
        daily_loss = _loss_from_baseline(equity, self._daily_baseline_equity)
        if (
            daily_loss >= self.adapter_config.risk.daily_loss_limit
            and self._daily_halt_started_at is None
        ):
            self._daily_halt_started_at = now

        self._risk_equity_history.append((now, equity))
        cutoff = now - timedelta(days=7)
        if now - self._risk_equity_history[0][0] < timedelta(days=7):
            weekly_baseline = self._run_start_equity
        else:
            weekly_baseline = next(
                value
                for timestamp, value in self._risk_equity_history
                if timestamp >= cutoff
            )
        weekly_loss = _loss_from_baseline(equity, weekly_baseline)
        if (
            drawdown >= self.adapter_config.risk.hard_drawdown
            and self._recovery_started_at is None
        ):
            self._recovery_started_at = now
        if (
            weekly_loss >= self.adapter_config.risk.weekly_halt_limit
            and self._weekly_halt_started_at is None
        ):
            self._weekly_halt_started_at = now
        if self._consecutive_losses >= 5 and self._streak_halt_started_at is None:
            self._streak_halt_started_at = now
            self._profitable_trades_since_streak_halt = 0

        close = float(self.data.close[0])
        atr = float(self.data.atr_value[0])
        baseline_atr_pct = float(self.data.baseline_atr_pct[0])
        volatility_ratio = (
            (atr / close) / baseline_atr_pct
            if close > 0.0 and baseline_atr_pct > 0.0
            else math.nan
        )
        volatility_bar_valid = (
            system_healthy
            and bool(self.data.entry_data_valid[0])
            and math.isfinite(volatility_ratio)
        )
        if self._volatility_halted:
            if volatility_bar_valid and volatility_ratio <= 1.5:
                self._volatility_stable_bars += 1
            else:
                self._volatility_stable_bars = 0
        elif math.isfinite(volatility_ratio) and volatility_ratio > 3.0:
            self._volatility_halted = True
            self._volatility_stable_bars = 0
        decision = evaluate_risk(
            now=now,
            drawdown=drawdown,
            daily_loss=daily_loss,
            weekly_loss=weekly_loss,
            consecutive_losses=self._consecutive_losses,
            volatility_ratio=volatility_ratio,
            system_healthy=system_healthy,
            config=self.adapter_config.risk,
            recovery_started_at=self._recovery_started_at,
            daily_halt_started_at=self._daily_halt_started_at,
            weekly_halt_started_at=self._weekly_halt_started_at,
            streak_halt_started_at=self._streak_halt_started_at,
            volatility_halted=self._volatility_halted,
            volatility_stable_bars=self._volatility_stable_bars,
            profitable_trades_since_streak_halt=(
                self._profitable_trades_since_streak_halt
            ),
        )
        if (
            self._recovery_started_at is not None
            and now >= self._recovery_started_at + timedelta(hours=72)
            and drawdown == 0.0
        ):
            self._recovery_started_at = None
        if (
            self._daily_halt_started_at is not None
            and now >= self._daily_halt_started_at + timedelta(hours=24)
            and daily_loss < self.adapter_config.risk.daily_loss_limit
        ):
            self._daily_halt_started_at = None
        if (
            self._weekly_halt_started_at is not None
            and now >= self._weekly_halt_started_at + timedelta(hours=48)
            and weekly_loss < self.adapter_config.risk.weekly_halt_limit
        ):
            self._weekly_halt_started_at = None
        if (
            self._streak_halt_started_at is not None
            and now >= self._streak_halt_started_at + timedelta(hours=48)
            and self._profitable_trades_since_streak_halt >= 2
        ):
            self._streak_halt_started_at = None
            self._profitable_trades_since_streak_halt = 0
        if self._volatility_halted and "volatility_halt" not in decision.reasons:
            self._volatility_halted = False
            self._volatility_stable_bars = 0
        self._last_equity = equity
        self._last_risk_time = now
        return decision

    @staticmethod
    def _requires_forced_exit(decision: RiskDecision) -> bool:
        return any(
            reason in {"drawdown_halt", "system_unhealthy", "invalid_input", "invalid_config"}
            for reason in decision.reasons
        )

    def _submit_forced_exit(self, decision: RiskDecision) -> None:
        if self.exit_order is not None:
            return
        if self.stop_order is not None:
            self.cancel(self.stop_order)
            self.stop_order = None
        reason = (
            "RISK_EXIT"
            if "drawdown_halt" in decision.reasons
            else "SYSTEM_EXIT"
        )
        self.exit_order = self.sell(
            size=float(self.position.size),
            signal_time=self._now().isoformat(),
            reason=reason,
            fill_fraction=self.adapter_config.exit_fill_fraction,
        )

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
            and (self._last_risk_time is None or now > self._last_risk_time)
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
        if self.trade_records[-1].net_pnl < 0.0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0
        if (
            self._streak_halt_started_at is not None
            and self.trade_records[-1].net_pnl > 0.0
            and self.trade_records[-1].exit_time > self._streak_halt_started_at
        ):
            self._profitable_trades_since_streak_halt += 1
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
    prepared["_warmup_complete"] = prepared["warmup_complete"]
    prepared["_entry_data_valid"] = prepared["entry_data_valid"]
    prepared["_ema_value"] = prepared[f"ema_{config.ema_period}"]
    prepared["_atr_value"] = prepared[f"atr_{config.atr_period}"]
    prepared["_force_flat_after_bar"] = (
        prepared["_force_flat_after_bar"]
        if "_force_flat_after_bar" in prepared
        else False
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


def _loss_from_baseline(equity: float, baseline: float) -> float:
    if not all(math.isfinite(value) for value in (equity, baseline)) or baseline <= 0.0:
        return math.nan
    return max(0.0, 1.0 - equity / baseline)
