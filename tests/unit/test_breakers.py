from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import math

import pytest

from autobit.config import RiskConfig
from autobit.risk.breakers import RiskDecision, evaluate_risk


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _evaluate(**overrides: object) -> RiskDecision:
    inputs: dict[str, object] = {
        "now": NOW,
        "drawdown": 0.0,
        "daily_loss": 0.0,
        "weekly_loss": 0.0,
        "consecutive_losses": 0,
        "volatility_ratio": 1.0,
        "system_healthy": True,
        "config": RiskConfig(),
    }
    inputs.update(overrides)
    return evaluate_risk(**inputs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("drawdown", "risk_rate", "exposure_cap"),
    [
        (0.0, 0.02, 0.70),
        (0.049999, 0.02, 0.70),
        (0.05, 0.01, 0.50),
        (0.099999, 0.01, 0.50),
        (0.10, 0.005, 0.30),
        (0.149999, 0.005, 0.30),
    ],
)
def test_drawdown_tiers_use_literal_inclusive_boundaries(
    drawdown: float,
    risk_rate: float,
    exposure_cap: float,
) -> None:
    decision = _evaluate(drawdown=drawdown)

    assert decision.risk_rate == pytest.approx(risk_rate)
    assert decision.exposure_cap == pytest.approx(exposure_cap)
    assert decision.halted_until is None
    assert decision.reasons == ()


def test_fifteen_percent_drawdown_starts_a_72_hour_halt() -> None:
    decision = _evaluate(drawdown=0.15)

    assert decision == RiskDecision(0.0, 0.0, NOW + timedelta(hours=72), ("drawdown_halt",))


@pytest.mark.parametrize(
    ("drawdown", "risk_rate", "exposure_cap"),
    [
        (0.14, 0.0025, 0.15),
        (0.10, 0.0025, 0.15),
        (0.099999, 0.005, 0.30),
        (0.05, 0.005, 0.30),
        (0.049999, 0.01, 0.50),
        (0.000001, 0.01, 0.50),
        (0.0, 0.02, 0.70),
    ],
)
def test_expired_drawdown_cooldown_uses_recovery_ladder(
    drawdown: float,
    risk_rate: float,
    exposure_cap: float,
) -> None:
    decision = _evaluate(
        now=NOW + timedelta(hours=72),
        drawdown=drawdown,
        recovery_started_at=NOW,
    )

    assert decision.risk_rate == pytest.approx(risk_rate)
    assert decision.exposure_cap == pytest.approx(exposure_cap)
    assert decision.halted_until is None
    assert decision.reasons == ("recovery",)


def test_drawdown_recovery_does_not_release_before_exact_expiry() -> None:
    decision = _evaluate(
        now=NOW + timedelta(hours=71, minutes=59),
        drawdown=0.10,
        recovery_started_at=NOW,
    )

    assert decision == RiskDecision(0.0, 0.0, NOW + timedelta(hours=72), ("drawdown_halt",))


def test_drawdown_recovery_stays_halted_if_drawdown_remains_fifteen_percent() -> None:
    decision = _evaluate(
        now=NOW + timedelta(hours=73),
        drawdown=0.15,
        recovery_started_at=NOW,
    )

    assert decision == RiskDecision(0.0, 0.0, None, ("drawdown_halt",))


def test_daily_loss_halt_persists_for_24_hours_even_if_rolling_loss_clears() -> None:
    started = _evaluate(daily_loss=0.04)
    active = _evaluate(
        now=NOW + timedelta(hours=23),
        daily_loss=0.0,
        daily_halt_started_at=NOW,
    )

    expected = RiskDecision(0.0, 0.0, NOW + timedelta(hours=24), ("daily_loss_halt",))
    assert started == expected
    assert active == expected


def test_daily_loss_expiry_reduces_while_loss_remains_and_clears_otherwise() -> None:
    reduced = _evaluate(
        now=NOW + timedelta(hours=24),
        daily_loss=0.04,
        daily_halt_started_at=NOW,
    )
    cleared = _evaluate(
        now=NOW + timedelta(hours=24),
        daily_loss=0.039999,
        daily_halt_started_at=NOW,
    )

    assert reduced == RiskDecision(0.01, 0.35, None, ("daily_loss_reduced",))
    assert cleared == RiskDecision(0.02, 0.70, None, ())


@pytest.mark.parametrize("weekly_loss", [0.05, 0.069999])
def test_weekly_loss_from_five_to_seven_percent_halves_risk(weekly_loss: float) -> None:
    assert _evaluate(weekly_loss=weekly_loss) == RiskDecision(
        0.01,
        0.35,
        None,
        ("weekly_loss_reduced",),
    )


def test_weekly_seven_percent_halt_has_48_hour_cooldown_then_reduces() -> None:
    started = _evaluate(weekly_loss=0.07)
    active = _evaluate(
        now=NOW + timedelta(hours=47),
        weekly_loss=0.0,
        weekly_halt_started_at=NOW,
    )
    expired = _evaluate(
        now=NOW + timedelta(hours=48),
        weekly_loss=0.07,
        weekly_halt_started_at=NOW,
    )

    expected_halt = RiskDecision(0.0, 0.0, NOW + timedelta(hours=48), ("weekly_loss_halt",))
    assert started == expected_halt
    assert active == expected_halt
    assert expired == RiskDecision(0.01, 0.35, None, ("weekly_loss_reduced",))


@pytest.mark.parametrize("consecutive_losses", [3, 4])
def test_three_or_four_consecutive_losses_halves_risk(consecutive_losses: int) -> None:
    assert _evaluate(consecutive_losses=consecutive_losses) == RiskDecision(
        0.01,
        0.35,
        None,
        ("loss_streak_reduced",),
    )


def test_five_loss_streak_cools_for_48_hours_then_needs_two_profitable_trades() -> None:
    started = _evaluate(consecutive_losses=5)
    active = _evaluate(
        now=NOW + timedelta(hours=48),
        consecutive_losses=0,
        streak_halt_started_at=NOW,
        profitable_trades_since_streak_halt=1,
    )
    cleared = _evaluate(
        now=NOW + timedelta(hours=48),
        consecutive_losses=5,
        streak_halt_started_at=NOW,
        profitable_trades_since_streak_halt=2,
    )

    assert started == RiskDecision(0.0, 0.0, NOW + timedelta(hours=48), ("loss_streak_halt",))
    assert active == RiskDecision(0.01, 0.35, None, ("loss_streak_reduced",))
    assert cleared == RiskDecision(0.02, 0.70, None, ())


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (0.0, RiskDecision(0.02, 0.70, None, ())),
        (2.0, RiskDecision(0.02, 0.70, None, ())),
        (2.000001, RiskDecision(0.01, 0.35, None, ("volatility_reduced",))),
        (3.0, RiskDecision(0.01, 0.35, None, ("volatility_reduced",))),
        (3.000001, RiskDecision(0.0, 0.0, None, ("volatility_halt",))),
    ],
)
def test_volatility_boundaries_are_strict(ratio: float, expected: RiskDecision) -> None:
    assert _evaluate(volatility_ratio=ratio) == expected


def test_persisted_volatility_halt_requires_three_stable_bars_at_or_below_one_point_five() -> None:
    ratio_high = _evaluate(
        volatility_ratio=1.500001,
        volatility_halted=True,
        volatility_stable_bars=3,
    )
    bars_low = _evaluate(
        volatility_ratio=1.5,
        volatility_halted=True,
        volatility_stable_bars=2,
    )
    released = _evaluate(
        volatility_ratio=1.5,
        volatility_halted=True,
        volatility_stable_bars=3,
    )

    assert ratio_high == RiskDecision(0.0, 0.0, None, ("volatility_halt",))
    assert bars_low == RiskDecision(0.0, 0.0, None, ("volatility_halt",))
    assert released == RiskDecision(0.02, 0.70, None, ())


def test_simultaneous_non_halt_rules_take_one_most_restrictive_half_and_keep_reason_order() -> None:
    decision = _evaluate(
        now=NOW + timedelta(hours=48),
        drawdown=0.06,
        daily_loss=0.04,
        daily_halt_started_at=NOW,
        weekly_loss=0.06,
        consecutive_losses=4,
        volatility_ratio=2.5,
    )

    assert decision == RiskDecision(
        0.005,
        0.25,
        None,
        (
            "daily_loss_reduced",
            "weekly_loss_reduced",
            "loss_streak_reduced",
            "volatility_reduced",
        ),
    )


def test_simultaneous_halts_return_latest_finite_expiry_and_deterministic_reasons() -> None:
    decision = _evaluate(
        drawdown=0.15,
        daily_loss=0.04,
        weekly_loss=0.07,
        consecutive_losses=5,
    )

    assert decision == RiskDecision(
        0.0,
        0.0,
        NOW + timedelta(hours=72),
        ("drawdown_halt", "daily_loss_halt", "weekly_loss_halt", "loss_streak_halt"),
    )


def test_system_unhealthy_has_precedence_over_every_rule_and_invalid_input() -> None:
    decision = _evaluate(
        now=datetime(2026, 1, 1),
        drawdown=-1.0,
        daily_loss=math.nan,
        weekly_loss=-1.0,
        consecutive_losses=-1,
        volatility_ratio=-1.0,
        system_healthy=False,
    )

    assert decision == RiskDecision(0.0, 0.0, None, ("system_unhealthy",))


@pytest.mark.parametrize(
    "overrides",
    [
        {"now": datetime(2026, 1, 1)},
        {"drawdown": -0.01},
        {"daily_loss": math.nan},
        {"weekly_loss": math.inf},
        {"consecutive_losses": -1},
        {"volatility_ratio": -0.000001},
        {"volatility_stable_bars": -1},
        {"profitable_trades_since_streak_halt": -1},
        {"daily_halt_started_at": datetime(2026, 1, 1)},
    ],
)
def test_invalid_inputs_fail_closed(overrides: dict[str, object]) -> None:
    assert _evaluate(**overrides) == RiskDecision(0.0, 0.0, None, ("invalid_input",))


def test_timezone_aware_non_utc_inputs_are_normalized_for_cooldowns() -> None:
    plus_nine = timezone(timedelta(hours=9))
    local_start = datetime(2026, 1, 1, 9, tzinfo=plus_nine)

    decision = _evaluate(
        now=datetime(2026, 1, 2, 8, tzinfo=plus_nine),
        daily_loss=0.0,
        daily_halt_started_at=local_start,
    )

    assert decision.halted_until == NOW + timedelta(hours=24)


def test_risk_decision_is_frozen_slotted_and_has_immutable_reasons() -> None:
    decision = _evaluate()
    normalized = RiskDecision(0.0, 0.0, None, ["volatility_halt"])  # type: ignore[arg-type]

    assert isinstance(decision.reasons, tuple)
    assert normalized.reasons == ("volatility_halt",)
    assert not hasattr(decision, "__dict__")
    with pytest.raises(FrozenInstanceError):
        decision.risk_rate = 0.0
