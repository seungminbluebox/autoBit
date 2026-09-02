"""Conservative, cost-aware position sizing for long entries."""

from dataclasses import dataclass
import math

from autobit.config import CostConfig


@dataclass(frozen=True, slots=True)
class SizeDecision:
    """The final quantity and the constraint that produced it."""

    quantity: float
    binding_constraint: str
    estimated_loss: float


def calculate_size(
    *,
    equity: float,
    cash: float,
    entry: float,
    stop: float,
    current_atr_pct: float,
    baseline_atr_pct: float,
    risk_rate: float,
    exposure_cap: float,
    costs: CostConfig,
) -> SizeDecision:
    """Return the exact conservative minimum of all approved size limits."""
    values = _finite_values(
        equity,
        cash,
        entry,
        stop,
        current_atr_pct,
        baseline_atr_pct,
        risk_rate,
        exposure_cap,
    )
    if values is None:
        return _invalid_decision()
    (
        equity_value,
        cash_value,
        entry_value,
        stop_value,
        current_atr_value,
        baseline_atr_value,
        risk_rate_value,
        exposure_cap_value,
    ) = values
    if (
        any(value < 0.0 for value in (equity_value, cash_value, risk_rate_value, exposure_cap_value))
        or any(
            value <= 0.0
            for value in (entry_value, stop_value, current_atr_value, baseline_atr_value)
        )
        or stop_value >= entry_value
    ):
        return _invalid_decision()

    try:
        cost_values = _finite_values(costs.fee_rate, costs.slippage_rate)
    except (AttributeError, TypeError):
        return _invalid_decision()
    if cost_values is None:
        return _invalid_decision()
    fee_rate, slippage_rate = cost_values
    if not (0.0 <= fee_rate < 1.0 and 0.0 <= slippage_rate < 1.0):
        return _invalid_decision()

    expected_buy_fill = entry_value * (1.0 + slippage_rate)
    expected_stop_fill = stop_value * (1.0 - slippage_rate)
    effective_loss = (
        expected_buy_fill
        - expected_stop_fill
        + expected_buy_fill * fee_rate
        + expected_stop_fill * fee_rate
    )
    if not math.isfinite(effective_loss) or effective_loss <= 0.0:
        return _invalid_decision()

    fixed = equity_value * risk_rate_value / effective_loss
    volatility = fixed * min(1.0, baseline_atr_value / current_atr_value)
    exposure = equity_value * exposure_cap_value / expected_buy_fill
    cash_limit = cash_value / (expected_buy_fill * (1.0 + fee_rate))
    candidates = (
        ("fixed", fixed),
        ("volatility", volatility),
        ("exposure", exposure),
        ("cash", cash_limit),
    )
    if any(not math.isfinite(quantity) or quantity < 0.0 for _, quantity in candidates):
        return _invalid_decision()

    binding_constraint, quantity = min(candidates, key=lambda candidate: candidate[1])
    estimated_loss = quantity * effective_loss
    if not math.isfinite(estimated_loss):
        return _invalid_decision()
    if quantity == 0.0:
        quantity = 0.0
        estimated_loss = 0.0
    return SizeDecision(quantity, binding_constraint, estimated_loss)


def _finite_values(*values: object) -> tuple[float, ...] | None:
    converted: list[float] = []
    for value in values:
        if isinstance(value, bool):
            return None
        try:
            converted_value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(converted_value):
            return None
        converted.append(converted_value)
    return tuple(converted)


def _invalid_decision() -> SizeDecision:
    return SizeDecision(0.0, "invalid_input", 0.0)
