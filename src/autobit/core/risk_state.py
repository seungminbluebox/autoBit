"""Immutable, execution-independent risk accumulation and health arithmetic."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math

from autobit.config import RiskConfig
from autobit.risk.breakers import RiskDecision, evaluate_risk


@dataclass(frozen=True, slots=True)
class RiskState:
    equity_peak: float = 100.0
    daily_date: date | None = None
    daily_baseline_equity: float = 100.0
    equity_history: tuple[tuple[datetime, float], ...] = ()
    risk_started_at: datetime | None = None
    last_equity: float = 100.0
    last_risk_at: datetime | None = None
    consecutive_losses: int = 0
    processed_trade_count: int = 0
    recovery_started_at: datetime | None = None
    daily_halt_started_at: datetime | None = None
    weekly_halt_started_at: datetime | None = None
    streak_halt_started_at: datetime | None = None
    profitable_trades_since_streak_halt: int = 0
    volatility_halted: bool = False
    volatility_stable_bars: int = 0
    decision: RiskDecision = RiskDecision(0.02, 0.70, None, ())
    initial_equity: float = 100.0


@dataclass(frozen=True, slots=True)
class ClosedTradeObservation:
    net_pnl: float
    exit_time: datetime


@dataclass(frozen=True, slots=True)
class RiskObservation:
    now: datetime
    equity: float
    closed_trades: tuple[ClosedTradeObservation, ...]
    volatility_ratio: float
    volatility_bar_valid: bool
    system_healthy: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "closed_trades", tuple(self.closed_trades))


def advance_risk_state(state: RiskState, observation: RiskObservation, *, config: RiskConfig) -> RiskState:
    """Advance completed-bar facts once. Reject reversed/skipped trade cursors.

    Same-bar calls may change health only. Adapters retain durable canonical
    decisions and use apply_health_recovery for provenance-bound overlays.
    """
    prior = state
    _validate_observation(prior, observation)
    bar_at, equity = observation.now.astimezone(timezone.utc), observation.equity
    completed_trades, risk_config = observation.closed_trades, config
    system_healthy = observation.system_healthy
    volatility_ratio = observation.volatility_ratio
    volatility_bar_valid = observation.volatility_bar_valid
    new_bar = prior.last_risk_at != bar_at
    consecutive_losses = prior.consecutive_losses
    profitable_since_halt = prior.profitable_trades_since_streak_halt
    for trade in completed_trades[prior.processed_trade_count :]:
        if trade.net_pnl < 0.0:
            consecutive_losses += 1
        else:
            consecutive_losses = 0
        if (
            prior.streak_halt_started_at is not None
            and trade.net_pnl > 0.0
            and trade.exit_time > prior.streak_halt_started_at
        ):
            profitable_since_halt += 1

    equity_peak = max(prior.equity_peak, equity)
    drawdown = max(0.0, 1.0 - equity / equity_peak) if equity_peak > 0.0 else math.nan
    daily_date = prior.daily_date or bar_at.date()
    daily_baseline = prior.daily_baseline_equity
    if bar_at.date() != daily_date:
        daily_date = bar_at.date()
        daily_baseline = prior.last_equity
    daily_loss = _loss_from_baseline(equity, daily_baseline)

    risk_started_at = prior.risk_started_at or bar_at
    history = (*prior.equity_history, (bar_at, equity)) if new_bar else prior.equity_history
    cutoff = bar_at - timedelta(days=7)
    recent_history = tuple(item for item in history if item[0] >= cutoff)
    if bar_at - risk_started_at < timedelta(days=7):
        weekly_baseline = prior.initial_equity
    else:
        weekly_baseline = recent_history[0][1]
    weekly_loss = _loss_from_baseline(equity, weekly_baseline)

    recovery_start = prior.recovery_started_at
    daily_start = prior.daily_halt_started_at
    weekly_start = prior.weekly_halt_started_at
    streak_start = prior.streak_halt_started_at
    if drawdown >= risk_config.hard_drawdown and recovery_start is None:
        recovery_start = bar_at
    if daily_loss >= risk_config.daily_loss_limit and daily_start is None:
        daily_start = bar_at
    if weekly_loss >= risk_config.weekly_halt_limit and weekly_start is None:
        weekly_start = bar_at
    if consecutive_losses >= 5 and streak_start is None:
        streak_start = bar_at
        profitable_since_halt = 0

    volatility_halted = prior.volatility_halted
    stable_bars = prior.volatility_stable_bars
    if volatility_halted and new_bar:
        stable_bars = stable_bars + 1 if volatility_bar_valid and volatility_ratio <= 1.5 else 0
    elif not volatility_halted and volatility_ratio > 3.0:
        volatility_halted = True
        stable_bars = 0

    decision = evaluate_risk(
        now=bar_at,
        drawdown=drawdown,
        daily_loss=daily_loss,
        weekly_loss=weekly_loss,
        consecutive_losses=consecutive_losses,
        volatility_ratio=volatility_ratio,
        system_healthy=system_healthy,
        config=risk_config,
        recovery_started_at=recovery_start,
        daily_halt_started_at=daily_start,
        weekly_halt_started_at=weekly_start,
        streak_halt_started_at=streak_start,
        volatility_halted=volatility_halted,
        volatility_stable_bars=stable_bars,
        profitable_trades_since_streak_halt=profitable_since_halt,
    )

    if recovery_start is not None and bar_at >= recovery_start + timedelta(hours=72) and drawdown == 0.0:
        recovery_start = None
    if daily_start is not None and bar_at >= daily_start + timedelta(hours=24) and daily_loss < risk_config.daily_loss_limit:
        daily_start = None
    if weekly_start is not None and bar_at >= weekly_start + timedelta(hours=48) and weekly_loss < risk_config.weekly_halt_limit:
        weekly_start = None
    if streak_start is not None and bar_at >= streak_start + timedelta(hours=48) and profitable_since_halt >= 2:
        streak_start = None
        profitable_since_halt = 0
    if volatility_halted and volatility_bar_valid and volatility_ratio <= 1.5 and stable_bars >= 3:
        volatility_halted = False
        stable_bars = 0

    return RiskState(
        initial_equity=prior.initial_equity,
        equity_peak=equity_peak,
        daily_date=daily_date,
        daily_baseline_equity=daily_baseline,
        equity_history=recent_history,
        risk_started_at=risk_started_at,
        last_equity=equity,
        last_risk_at=bar_at,
        consecutive_losses=consecutive_losses,
        processed_trade_count=len(completed_trades),
        recovery_started_at=recovery_start,
        daily_halt_started_at=daily_start,
        weekly_halt_started_at=weekly_start,
        streak_halt_started_at=streak_start,
        profitable_trades_since_streak_halt=profitable_since_halt,
        volatility_halted=volatility_halted,
        volatility_stable_bars=stable_bars,
        decision=decision,
    )


def apply_health_recovery(
    decision: RiskDecision, *, system_healthy: bool, resume_reduced: bool,
) -> RiskDecision:
    """Apply current operational health to a canonical (not overlaid) decision."""
    if system_healthy is not True or type(resume_reduced) is not bool:
        return RiskDecision(0., 0., None, ("system_unhealthy",))
    if not resume_reduced or decision.halted_until is not None or decision.risk_rate <= 0. or decision.exposure_cap <= 0.:
        return decision
    return RiskDecision(decision.risk_rate / 2., decision.exposure_cap / 2.,
                        decision.halted_until, decision.reasons + ("health_recovery_reduced",))


def _validate_observation(prior: RiskState, observation: RiskObservation) -> None:
    now = observation.now
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("risk timestamp must be timezone aware")
    if not math.isfinite(observation.equity) or observation.equity < 0.:
        raise ValueError("mark-to-market equity is invalid")
    if not math.isfinite(prior.initial_equity) or prior.initial_equity <= 0.:
        raise ValueError("initial equity is invalid")
    if type(observation.volatility_bar_valid) is not bool or type(observation.system_healthy) is not bool:
        raise ValueError("risk validity flags must be boolean")
    trades = observation.closed_trades
    if type(prior.processed_trade_count) is not int or not 0 <= prior.processed_trade_count <= len(trades):
        raise ValueError("risk trade cursor is ahead of broker history")
    previous = None
    for index, trade in enumerate(trades):
        if (not isinstance(trade.exit_time, datetime) or trade.exit_time.tzinfo is None
            or trade.exit_time.utcoffset() is None or not math.isfinite(trade.net_pnl)
            or trade.exit_time > now or (previous is not None and trade.exit_time < previous)):
            raise ValueError("completed trade chronology is invalid")
        if index >= prior.processed_trade_count and prior.last_risk_at is not None and trade.exit_time < prior.last_risk_at:
            raise ValueError("new trade precedes processed risk history")
        previous = trade.exit_time
    if prior.last_risk_at is not None:
        if now < prior.last_risk_at:
            raise ValueError("risk timestamp is reversed")
        if now == prior.last_risk_at and (observation.equity != prior.last_equity or len(trades) != prior.processed_trade_count):
            raise ValueError("same-bar followup may not change equity or trades")


def _loss_from_baseline(equity: float, baseline: float) -> float:
    return max(0., 1. - equity / baseline) if baseline > 0. else 0.
