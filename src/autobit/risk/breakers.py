"""Automatic risk restrictions and persisted cooldown evaluation."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from numbers import Integral

from autobit.config import RiskConfig


_BASE_RISK_RATE = 0.02
_MAX_EXPOSURE = 0.70
_HARD_DRAWDOWN = 0.15
_DAILY_LOSS_LIMIT = 0.04
_WEEKLY_REDUCE_LIMIT = 0.05
_WEEKLY_HALT_LIMIT = 0.07
_APPROVED_CONFIG = (
    _BASE_RISK_RATE,
    _MAX_EXPOSURE,
    _HARD_DRAWDOWN,
    _DAILY_LOSS_LIMIT,
    _WEEKLY_REDUCE_LIMIT,
    _WEEKLY_HALT_LIMIT,
)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """The most restrictive risk state active for the supplied snapshot."""

    risk_rate: float
    exposure_cap: float
    halted_until: datetime | None
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))


def evaluate_risk(
    *,
    now: datetime,
    drawdown: float,
    daily_loss: float,
    weekly_loss: float,
    consecutive_losses: int,
    volatility_ratio: float,
    system_healthy: bool,
    config: RiskConfig,
    recovery_started_at: datetime | None = None,
    daily_halt_started_at: datetime | None = None,
    weekly_halt_started_at: datetime | None = None,
    streak_halt_started_at: datetime | None = None,
    volatility_halted: bool = False,
    volatility_stable_bars: int = 0,
    profitable_trades_since_streak_halt: int = 0,
) -> RiskDecision:
    """Evaluate all active rules and fail closed on an invalid snapshot.

    Persisted halt starts are event-scoped. Callers must store each start once
    while that event remains active, then clear it when the trigger ends before
    a later event. Aggregate loss/streak inputs cannot distinguish two events.
    """
    if system_healthy is False:
        return RiskDecision(0.0, 0.0, None, ("system_unhealthy",))
    if system_healthy is not True or not isinstance(volatility_halted, bool):
        return _invalid_decision()

    now_utc = _as_utc(now)
    snapshot_times = tuple(
        _as_utc(value) if value is not None else None
        for value in (
            recovery_started_at,
            daily_halt_started_at,
            weekly_halt_started_at,
            streak_halt_started_at,
        )
    )
    if now_utc is None or any(
        supplied is not None and normalized is None
        for supplied, normalized in zip(
            (
                recovery_started_at,
                daily_halt_started_at,
                weekly_halt_started_at,
                streak_halt_started_at,
            ),
            snapshot_times,
            strict=True,
        )
    ):
        return _invalid_decision()
    recovery_start, daily_start, weekly_start, streak_start = snapshot_times

    numeric_inputs = _finite_nonnegative_values(drawdown, daily_loss, weekly_loss, volatility_ratio)
    if numeric_inputs is None:
        return _invalid_decision()
    drawdown_value, daily_loss_value, weekly_loss_value, volatility_value = numeric_inputs
    if not all(
        _is_nonnegative_integer(value)
        for value in (
            consecutive_losses,
            volatility_stable_bars,
            profitable_trades_since_streak_halt,
        )
    ):
        return _invalid_decision()
    if not _valid_config(config):
        return _invalid_config_decision()

    reasons: list[str] = []
    halts: list[datetime | None] = []

    risk_rate, exposure_cap = _drawdown_ladder(drawdown_value)
    if recovery_start is None:
        if drawdown_value >= _HARD_DRAWDOWN:
            reasons.append("drawdown_halt")
            halts.append(now_utc + timedelta(hours=72))
    else:
        recovery_expiry = recovery_start + timedelta(hours=72)
        if now_utc < recovery_expiry:
            reasons.append("drawdown_halt")
            halts.append(recovery_expiry)
        else:
            risk_rate, exposure_cap = _recovery_ladder(drawdown_value)
            if drawdown_value > 0.0:
                reasons.append("recovery")

    reduced_risk_rate = risk_rate / 2.0
    reduced_exposure_cap = exposure_cap / 2.0

    daily_expiry = daily_start + timedelta(hours=24) if daily_start is not None else None
    if daily_expiry is not None and now_utc < daily_expiry:
        reasons.append("daily_loss_halt")
        halts.append(daily_expiry)
    elif daily_start is None and daily_loss_value >= _DAILY_LOSS_LIMIT:
        reasons.append("daily_loss_halt")
        halts.append(now_utc + timedelta(hours=24))
    elif daily_start is not None and daily_loss_value >= _DAILY_LOSS_LIMIT:
        risk_rate = min(risk_rate, reduced_risk_rate)
        exposure_cap = min(exposure_cap, reduced_exposure_cap)
        reasons.append("daily_loss_reduced")

    weekly_expiry = weekly_start + timedelta(hours=48) if weekly_start is not None else None
    if weekly_expiry is not None and now_utc < weekly_expiry:
        reasons.append("weekly_loss_halt")
        halts.append(weekly_expiry)
    elif weekly_start is None and weekly_loss_value >= _WEEKLY_HALT_LIMIT:
        reasons.append("weekly_loss_halt")
        halts.append(now_utc + timedelta(hours=48))
    elif weekly_loss_value >= _WEEKLY_REDUCE_LIMIT:
        risk_rate = min(risk_rate, reduced_risk_rate)
        exposure_cap = min(exposure_cap, reduced_exposure_cap)
        reasons.append("weekly_loss_reduced")

    streak_expiry = streak_start + timedelta(hours=48) if streak_start is not None else None
    if streak_expiry is not None and now_utc < streak_expiry:
        reasons.append("loss_streak_halt")
        halts.append(streak_expiry)
    elif streak_start is None and consecutive_losses >= 5:
        reasons.append("loss_streak_halt")
        halts.append(now_utc + timedelta(hours=48))
    elif streak_start is not None and profitable_trades_since_streak_halt < 2:
        risk_rate = min(risk_rate, reduced_risk_rate)
        exposure_cap = min(exposure_cap, reduced_exposure_cap)
        reasons.append("loss_streak_reduced")
    elif streak_start is None and consecutive_losses >= 3:
        risk_rate = min(risk_rate, reduced_risk_rate)
        exposure_cap = min(exposure_cap, reduced_exposure_cap)
        reasons.append("loss_streak_reduced")

    if volatility_halted:
        if not (volatility_value <= 1.5 and volatility_stable_bars >= 3):
            reasons.append("volatility_halt")
            halts.append(None)
    elif volatility_value > 3.0:
        reasons.append("volatility_halt")
        halts.append(None)
    elif volatility_value > 2.0:
        risk_rate = min(risk_rate, reduced_risk_rate)
        exposure_cap = min(exposure_cap, reduced_exposure_cap)
        reasons.append("volatility_reduced")

    if halts:
        finite_halts = [halt for halt in halts if halt is not None]
        halted_until = None if len(finite_halts) != len(halts) else max(finite_halts)
        return RiskDecision(0.0, 0.0, halted_until, tuple(reasons))
    return RiskDecision(risk_rate, exposure_cap, None, tuple(reasons))


def _drawdown_ladder(drawdown: float) -> tuple[float, float]:
    if drawdown >= 0.10:
        return 0.005, 0.30
    if drawdown >= 0.05:
        return 0.01, 0.50
    return _BASE_RISK_RATE, _MAX_EXPOSURE


def _recovery_ladder(drawdown: float) -> tuple[float, float]:
    if drawdown >= 0.10:
        return 0.0025, 0.15
    if drawdown >= 0.05:
        return 0.005, 0.30
    if drawdown > 0.0:
        return 0.01, 0.50
    return _BASE_RISK_RATE, _MAX_EXPOSURE


def _finite_nonnegative_values(*values: object) -> tuple[float, ...] | None:
    converted: list[float] = []
    for value in values:
        if isinstance(value, bool):
            return None
        try:
            converted_value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(converted_value) or converted_value < 0.0:
            return None
        converted.append(converted_value)
    return tuple(converted)


def _is_nonnegative_integer(value: object) -> bool:
    return isinstance(value, Integral) and not isinstance(value, bool) and value >= 0


def _as_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _valid_config(config: object) -> bool:
    try:
        values = (
            config.base_risk_rate,
            config.max_exposure,
            config.hard_drawdown,
            config.daily_loss_limit,
            config.weekly_reduce_limit,
            config.weekly_halt_limit,
        )
    except AttributeError:
        return False
    return values == _APPROVED_CONFIG


def _invalid_decision() -> RiskDecision:
    return RiskDecision(0.0, 0.0, None, ("invalid_input",))


def _invalid_config_decision() -> RiskDecision:
    return RiskDecision(0.0, 0.0, None, ("invalid_config",))
