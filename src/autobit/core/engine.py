"""One pure policy for entry, exit priority, protective stops and sizing."""

import math
from numbers import Real

from autobit.config import CostConfig, StrategyConfig
from autobit.core.models import Decision, DecisionInput, PositionContext
from autobit.risk.breakers import RiskDecision
from autobit.risk.position_sizer import calculate_size
from autobit.strategy.donchian_trend import (
    PositionSnapshot, evaluate_close_exit, evaluate_entry,
    initial_stop_price, next_stop,
)


def forced_exit_reason(risk: RiskDecision) -> str | None:
    if "drawdown_halt" in risk.reasons:
        return "RISK_EXIT"
    if any(reason in {"system_unhealthy", "invalid_input", "invalid_config"} for reason in risk.reasons):
        return "SYSTEM_EXIT"
    return None


class StrategyEngine:
    def __init__(self, strategy: StrategyConfig, costs: CostConfig) -> None:
        self.strategy = strategy
        self.costs = costs

    def initial_stop(self, entry_price: float, entry_atr: float) -> float:
        return initial_stop_price(entry_price, entry_atr, self.strategy.initial_atr_mult)

    def protect(self, position: PositionContext, observed_price: float) -> bool:
        """Use the active stop only; invalid protection inputs require an exit."""
        return (not _valid_position(position) or not _positive(observed_price)
                or observed_price <= position.current_stop)

    def decide(self, snapshot: DecisionInput) -> Decision:
        risk, position, row = snapshot.risk, snapshot.position, snapshot.row
        hold = Decision("hold", None, 0., None, risk)
        if position is not None:
            if not _positive(position.quantity):
                return hold
            reason = forced_exit_reason(risk)
            if reason is not None:
                return Decision("sell", reason, position.quantity, None, risk)
            if not _valid_position(position):
                return Decision("sell", "SYSTEM_EXIT", position.quantity, None, risk)
            if snapshot.has_pending_order:
                return hold
            if evaluate_close_exit(row):
                return Decision("sell", "CLOSE_EXIT", position.quantity, None, risk)
            high_water = max(position.high_water, float(row["high"])) if _positive(row.get("high")) else position.high_water
            risk_unit = position.entry_price - position.initial_stop
            if (position.held_bars >= self.strategy.stagnant_bars and risk_unit > 0.
                and high_water < position.entry_price + self.strategy.stagnant_min_r * risk_unit):
                return Decision("sell", "STAGNANT_EXIT", position.quantity, None, risk)
            if position.held_bars >= self.strategy.max_holding_bars:
                return Decision("sell", "MAX_HOLD_EXIT", position.quantity, None, risk)
            candidate = next_stop(PositionSnapshot(position.entry_price, position.initial_stop,
                                                  position.current_stop, high_water), row, self.strategy)
            return Decision("hold", None, 0., candidate, risk)
        if (snapshot.has_pending_order is not False
            or not _nonnegative(snapshot.cash) or not _positive(snapshot.equity)
            or risk.halted_until is not None or risk.risk_rate <= 0. or risk.exposure_cap <= 0.
            or not evaluate_entry(row, is_flat=True, config=self.strategy)):
            return hold
        close, atr = row.get("close"), row.get(f"atr_{self.strategy.atr_period}")
        if not _positive(close) or not _positive(atr):
            return hold
        close, atr = float(close), float(atr)
        stop = self.initial_stop(close, atr)
        size = calculate_size(equity=snapshot.equity, cash=snapshot.cash, entry=close, stop=stop,
                              current_atr_pct=atr / close, baseline_atr_pct=row.get("baseline_atr_pct"),
                              risk_rate=risk.risk_rate, exposure_cap=risk.exposure_cap, costs=self.costs)
        return Decision("buy", "ENTRY", size.quantity, stop, risk) if size.quantity > 0. else hold


def _nonnegative(value: object) -> bool:
    try:
        return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value)) and float(value) >= 0.
    except (TypeError, ValueError, OverflowError):
        return False


def _positive(value: object) -> bool:
    return _nonnegative(value) and float(value) > 0.


def _valid_position(position: PositionContext) -> bool:
    return (all(_positive(value) for value in (position.entry_price, position.initial_stop,
                position.current_stop, position.high_water, position.quantity))
            and position.initial_stop < position.entry_price
            and position.current_stop >= position.initial_stop
            and type(position.held_bars) is int and position.held_bars >= 0)
