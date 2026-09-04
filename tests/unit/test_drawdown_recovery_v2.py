from datetime import datetime, timedelta, timezone

import pytest

from autobit.config import RiskConfig
from autobit.risk.breakers import evaluate_risk


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("hours", [72, 240])
@pytest.mark.parametrize("drawdown", [0.15, 0.16])
def test_drawdown_can_resume_without_erasing_historical_loss(hours, drawdown):
    result = evaluate_risk(
        now=START + timedelta(hours=hours), drawdown=drawdown,
        daily_loss=0.0, weekly_loss=0.0, consecutive_losses=0,
        volatility_ratio=1.0, system_healthy=True, config=RiskConfig(),
        recovery_started_at=START,
    )
    assert (result.risk_rate, result.exposure_cap) == (0.0025, 0.15)
    assert "drawdown_halt" not in result.reasons
    assert "recovery" in result.reasons


def _recover(**overrides):
    inputs = dict(
        now=START + timedelta(hours=72), drawdown=0.16,
        daily_loss=0.0, weekly_loss=0.0, consecutive_losses=0,
        volatility_ratio=1.0, system_healthy=True, config=RiskConfig(),
        recovery_started_at=START,
    )
    return evaluate_risk(**(inputs | overrides))


def test_recovery_stays_halted_one_second_before_expiry():
    decision = _recover(now=START + timedelta(hours=72, seconds=-1))
    assert (decision.risk_rate, decision.exposure_cap) == (0.0, 0.0)
    assert decision.halted_until == START + timedelta(hours=72)
    assert decision.reasons == ("drawdown_halt",)


@pytest.mark.parametrize("overrides, reason", [
    ({"daily_loss": .04}, "daily_loss_halt"),
    ({"weekly_loss": .07}, "weekly_loss_halt"),
    ({"consecutive_losses": 5}, "loss_streak_halt"),
    ({"volatility_ratio": 3.01}, "volatility_halt"),
    ({"system_healthy": False}, "system_unhealthy"),
])
def test_recovery_does_not_override_independent_halts(overrides, reason):
    decision = _recover(**overrides)
    assert (decision.risk_rate, decision.exposure_cap) == (0.0, 0.0)
    assert reason in decision.reasons
    assert "drawdown_halt" not in decision.reasons


def test_recovery_combines_simultaneous_reductions_once():
    decision = _recover(
        daily_loss=.04, daily_halt_started_at=START, weekly_loss=.05,
        consecutive_losses=3, volatility_ratio=2.5,
    )
    assert (decision.risk_rate, decision.exposure_cap) == (.00125, .075)
    assert decision.reasons == (
        "recovery", "daily_loss_reduced", "weekly_loss_reduced",
        "loss_streak_reduced", "volatility_reduced",
    )


def test_recovery_combines_simultaneous_halts():
    decision = _recover(daily_loss=.04, weekly_loss=.07, volatility_ratio=3.1)
    assert (decision.risk_rate, decision.exposure_cap) == (0.0, 0.0)
    assert decision.halted_until is None
    assert decision.reasons == (
        "recovery", "daily_loss_halt", "weekly_loss_halt", "volatility_halt",
    )


@pytest.mark.parametrize("hours", [72, 240])
@pytest.mark.parametrize("drawdown", [.15, .16])
def test_unhealthy_system_dominates_all_simultaneous_halts_after_expiry(hours, drawdown):
    decision = _recover(
        now=START + timedelta(hours=hours), drawdown=drawdown,
        daily_loss=.04, weekly_loss=.07, consecutive_losses=5,
        volatility_ratio=3.1, system_healthy=False,
    )
    assert (decision.risk_rate, decision.exposure_cap) == (0.0, 0.0)
    assert decision.halted_until is None
    assert decision.reasons == ("system_unhealthy",)
