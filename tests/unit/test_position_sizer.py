from dataclasses import FrozenInstanceError
import math

import pytest

from autobit.config import CostConfig
from autobit.risk.position_sizer import SizeDecision, calculate_size


def _calculate(**overrides: object) -> SizeDecision:
    inputs: dict[str, object] = {
        "equity": 100.0,
        "cash": 1_000.0,
        "entry": 100.0,
        "stop": 90.0,
        "current_atr_pct": 0.02,
        "baseline_atr_pct": 0.02,
        "risk_rate": 0.02,
        "exposure_cap": 0.70,
        "costs": CostConfig(),
    }
    inputs.update(overrides)
    return calculate_size(**inputs)  # type: ignore[arg-type]


def test_size_accounts_for_expected_fill_slippage_and_both_fees() -> None:
    decision = _calculate(costs=CostConfig(fee_rate=0.01, slippage_rate=0.01))

    # Buy 101.0, stop sell 89.1, fees 1.01 + 0.891: loss/BTC = 13.801.
    assert decision.quantity == pytest.approx(2.0 / 13.801)
    assert decision.estimated_loss == pytest.approx(2.0)
    assert decision.binding_constraint == "fixed"


def test_high_volatility_candidate_is_the_exact_minimum() -> None:
    decision = _calculate(
        costs=CostConfig(fee_rate=0.0, slippage_rate=0.0),
        current_atr_pct=0.04,
        baseline_atr_pct=0.02,
        exposure_cap=1.0,
    )

    assert decision == SizeDecision(
        quantity=0.1,
        binding_constraint="volatility",
        estimated_loss=1.0,
    )


def test_exposure_candidate_can_bind() -> None:
    decision = _calculate(
        equity=100.0,
        risk_rate=0.50,
        exposure_cap=0.10,
        costs=CostConfig(fee_rate=0.0, slippage_rate=0.0),
    )

    assert decision.quantity == pytest.approx(0.1)
    assert decision.binding_constraint == "exposure"
    assert decision.estimated_loss == pytest.approx(1.0)


def test_cash_candidate_includes_entry_fee_and_can_bind() -> None:
    decision = _calculate(
        equity=1_000.0,
        cash=10.0,
        costs=CostConfig(fee_rate=0.01, slippage_rate=0.0),
    )

    assert decision.quantity == pytest.approx(10.0 / 101.0)
    assert decision.binding_constraint == "cash"
    assert decision.quantity * 100.0 * 1.01 == pytest.approx(10.0)


def test_equal_fixed_and_volatility_candidates_bind_fixed_deterministically() -> None:
    decision = _calculate(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0))

    assert decision.quantity == pytest.approx(0.2)
    assert decision.binding_constraint == "fixed"


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("equity", 0.0),
        ("equity", math.nan),
        ("cash", 0.0),
        ("entry", 0.0),
        ("stop", 0.0),
        ("stop", 100.0),
        ("current_atr_pct", 0.0),
        ("baseline_atr_pct", -0.01),
        ("risk_rate", 0.0),
        ("exposure_cap", -0.1),
    ],
)
def test_invalid_numeric_inputs_fail_closed_with_a_stable_reason(override: str, value: float) -> None:
    decision = _calculate(**{override: value})

    assert decision == SizeDecision(0.0, "invalid_input", 0.0)


@pytest.mark.parametrize(
    "costs",
    [
        CostConfig(fee_rate=-0.001, slippage_rate=0.0),
        CostConfig(fee_rate=1.0, slippage_rate=0.0),
        CostConfig(fee_rate=0.0, slippage_rate=-0.001),
        CostConfig(fee_rate=0.0, slippage_rate=1.0),
        CostConfig(fee_rate=math.inf, slippage_rate=0.0),
    ],
)
def test_invalid_cost_rates_fail_closed(costs: CostConfig) -> None:
    assert _calculate(costs=costs) == SizeDecision(0.0, "invalid_input", 0.0)


def test_size_decision_is_frozen_and_slotted() -> None:
    decision = _calculate(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0))

    assert not hasattr(decision, "__dict__")
    with pytest.raises(FrozenInstanceError):
        decision.quantity = 99.0
