"""Hand-checked contracts for the policy used by every execution mode."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util

import pytest

from autobit.config import CostConfig, RiskConfig, StrategyConfig
from autobit.risk.breakers import RiskDecision


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
NORMAL = RiskDecision(.02, .70, None, ())
ROW = dict(close=100., high=101., ema_200=90., entry_high=99.,
           previous_close=99., previous_entry_high=99., atr_14=2.,
           baseline_atr_pct=.02, exit_low=80., warmup_complete=True,
           entry_data_valid=True)


def core():
    assert importlib.util.find_spec("autobit.core") is not None, "shared policy is not implemented"
    from autobit.core.engine import StrategyEngine
    from autobit.core.models import DecisionInput, PositionContext
    from autobit.core.risk_state import RiskState, RiskObservation, ClosedTradeObservation, advance_risk_state
    return StrategyEngine, DecisionInput, PositionContext, RiskState, RiskObservation, ClosedTradeObservation, advance_risk_state


def snapshot(*, long=False, **changes):
    Engine, Input, Position, *_ = core()
    position = Position(100., 95., 95., 101., .4, 1) if long else None
    return Engine(StrategyConfig(), CostConfig(0, 0)), replace(
        Input(ROW, 100., 100., position, False, NORMAL), **changes)


def test_flat_entry_has_hand_calculated_risk_limited_quantity():
    engine, value = snapshot()
    decision = engine.decide(value)
    # 2% of 100 / (100 - 95) = .4, below .7 exposure and 1 cash.
    assert (decision.action, decision.reason, decision.quantity, decision.next_stop) == ("buy", "ENTRY", .4, 95.)
    assert decision.risk == NORMAL


@pytest.mark.parametrize("held,high,action,reason", [
    (59, 101., "hold", None), (60, 101., "sell", "STAGNANT_EXIT"),
    (1094, 106., "hold", None), (1095, 106., "sell", "MAX_HOLD_EXIT"),
])
def test_completed_holding_boundaries(held, high, action, reason):
    engine, value = snapshot(long=True)
    value = replace(value, position=replace(value.position, held_bars=held, high_water=high))
    decision = engine.decide(value)
    assert (decision.action, decision.reason) == (action, reason)
    assert decision.quantity == (.4 if action == "sell" else 0.)


def test_close_exit_precedes_holding_exit():
    engine, value = snapshot(long=True, row={**ROW, "exit_low": 101.})
    value = replace(value, position=replace(value.position, held_bars=1095))
    assert engine.decide(value).reason == "CLOSE_EXIT"


@pytest.mark.parametrize("pending", [False, True])
def test_forced_exit_precedes_pending_order_suppression(pending):
    risk = RiskDecision(0., 0., NOW + timedelta(hours=72), ("drawdown_halt",))
    engine, value = snapshot(long=True, risk=risk, has_pending_order=pending)
    result = engine.decide(value)
    assert (result.action, result.reason, result.quantity) == ("sell", "RISK_EXIT", .4)


def test_pending_entry_never_creates_duplicate_exposure():
    engine, value = snapshot(has_pending_order=True)
    assert engine.decide(value).action == "hold"


def test_trailing_stop_is_monotone_and_does_not_mutate_active_stop():
    engine, value = snapshot(long=True, row={**ROW, "high": 112.})
    decision = engine.decide(value)
    assert decision.action == "hold"
    assert decision.next_stop == 106.
    assert value.position.current_stop == 95.
    assert not engine.protect(value.position, 100.)
    later = replace(value, position=replace(value.position, current_stop=106., high_water=112.), row=ROW)
    assert engine.decide(later).next_stop == 106.
    assert engine.protect(later.position, 106.)


@pytest.mark.parametrize("changes", [
    {"cash": float("nan")}, {"equity": -1.}, {"row": {**ROW, "atr_14": 0.}},
    {"row": {**ROW, "close": float("nan")}}, {"row": {}},
])
def test_invalid_snapshot_cannot_open_exposure(changes):
    engine, value = snapshot(**changes)
    assert engine.decide(value).action == "hold"


def test_protection_fails_closed_on_invalid_observation_or_position():
    engine, value = snapshot(long=True)
    assert engine.protect(value.position, float("nan"))
    assert engine.protect(replace(value.position, current_stop=float("nan")), 100.)


def test_risk_reducer_uses_actual_initial_equity_and_no_duplicate_bar():
    _, _, _, State, Observation, _, advance = core()
    state = State(initial_equity=10000., equity_peak=10000., daily_baseline_equity=10000., last_equity=10000.)
    observation = Observation(NOW, 9400., (), 1., True, True)
    state = advance(state, observation, config=RiskConfig())
    assert state.decision.exposure_cap <= .50
    assert "weekly_loss_reduced" in state.decision.reasons
    assert state.equity_history == ((NOW, 9400.),)
    assert advance(state, observation, config=RiskConfig()) == state
    with pytest.raises(ValueError):
        advance(state, replace(observation, now=NOW - timedelta(hours=4)), config=RiskConfig())


def test_risk_trade_cursor_and_loss_progression():
    _, _, _, State, Observation, Trade, advance = core()
    trades = tuple(Trade(-1., NOW - timedelta(hours=5-i)) for i in range(5))
    observation = Observation(NOW, 100., trades, 1., True, True)
    state = advance(State(), observation, config=RiskConfig())
    assert state.consecutive_losses == 5
    assert state.processed_trade_count == 5
    assert state.streak_halt_started_at == NOW
    assert advance(state, observation, config=RiskConfig()).consecutive_losses == 5
    with pytest.raises(ValueError):
        advance(state, replace(observation, now=NOW + timedelta(hours=4), closed_trades=trades[:-1]), config=RiskConfig())
    wins = (*trades, Trade(1., NOW + timedelta(hours=4)), Trade(1., NOW + timedelta(hours=8)))
    state = advance(state, replace(observation, now=NOW + timedelta(hours=48), closed_trades=wins), config=RiskConfig())
    assert state.consecutive_losses == 0
    assert state.streak_halt_started_at is None


def test_same_bar_health_followup_cannot_count_a_second_recovery_bar():
    _, _, _, State, Observation, _, advance = core()
    observation = Observation(NOW, 100., (), 1., True, True)
    state = advance(State(volatility_halted=True), observation, config=RiskConfig())
    assert state.volatility_stable_bars == 1
    followup = advance(state, replace(observation, system_healthy=False), config=RiskConfig())
    assert followup.equity_history == state.equity_history
    assert followup.volatility_stable_bars == 1
    assert followup.decision.reasons == ("system_unhealthy",)


def test_shared_health_recovery_scales_once_and_preserves_other_halts():
    core()
    from autobit.core.risk_state import apply_health_recovery
    reduced = apply_health_recovery(NORMAL, system_healthy=True, resume_reduced=True)
    assert (reduced.risk_rate, reduced.exposure_cap) == (.01, .35)
    halted = RiskDecision(0., 0., NOW, ("daily_halt",))
    assert apply_health_recovery(halted, system_healthy=True, resume_reduced=True) == halted
    assert apply_health_recovery(NORMAL, system_healthy=False, resume_reduced=False).reasons == ("system_unhealthy",)


def test_risk_dates_are_utc_and_history_is_bounded_without_losing_start():
    _, _, _, State, Observation, _, advance = core()
    offset = timezone(timedelta(hours=9))
    at = datetime(2026, 1, 2, 1, tzinfo=offset)  # January 1, 16:00 UTC
    state = advance(State(), Observation(at, 100., (), 1., True, True), config=RiskConfig())
    assert state.daily_date == NOW.date()
    assert state.last_risk_at == NOW + timedelta(hours=16)
    for index in range(1, 121):
        state = advance(state, Observation(at + timedelta(hours=4 * index), 100., (), 1., True, True), config=RiskConfig())
    assert len(state.equity_history) == 43
    assert state.risk_started_at == NOW + timedelta(hours=16)
    assert state.equity_peak == 100.
    assert state.decision == NORMAL


@pytest.mark.parametrize("price", ["100", True, float("inf"), 0., None])
def test_non_numeric_protection_observation_fails_closed(price):
    engine, value = snapshot(long=True)
    assert engine.protect(value.position, price)


@pytest.mark.parametrize("trades", [
    [(-1., 2), (-1., 1)], [(-1., 5)], [(float("nan"), 1)],
])
def test_invalid_closed_trade_order_never_advances_state(trades):
    _, _, _, State, Observation, Trade, advance = core()
    values = tuple(Trade(pnl, NOW + timedelta(hours=hours)) for pnl, hours in trades)
    with pytest.raises(ValueError):
        advance(State(), Observation(NOW + timedelta(hours=4), 100., values, 1., True, True), config=RiskConfig())
