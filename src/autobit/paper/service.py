"""Deterministic orchestration for one completed KRW-BTC paper candle."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import math
from typing import Protocol

import pandas as pd

from autobit.config import CostConfig, RiskConfig, StrategyConfig
from autobit.data.quality import canonicalize_ohlcv
from autobit.execution.paper_broker import (
    PaperBroker,
    PaperFill,
    PaperOrder,
    PaperReconciliation,
    PaperTrade,
)
from autobit.indicators.trend import compute_trend_indicators
from autobit.persistence.sqlite_store import PaperSnapshot, SQLiteStore, StoreCorruptionError
from autobit.risk.breakers import RiskDecision, evaluate_risk
from autobit.risk.position_sizer import calculate_size
from autobit.strategy.donchian_trend import (
    PositionSnapshot,
    evaluate_close_exit,
    evaluate_entry,
    next_stop,
)


_FOUR_HOURS = timedelta(hours=4)
_MATURITY_DELAY = timedelta(minutes=10)
_DEFAULT_LEASE_TTL = timedelta(minutes=5)
_RISK_STATE_VERSION = 1
_UTC = timezone.utc


class CompletedCandleSource(Protocol):
    """Public-data-only source for raw candles before an exclusive UTC end."""

    def load_completed_candles(self, end_utc: datetime) -> pd.DataFrame: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class CycleStatus(str, Enum):
    PROCESSED = "PROCESSED"
    ALREADY_PROCESSED = "ALREADY_PROCESSED"
    LEASE_HELD = "LEASE_HELD"
    UNSAFE_DATA = "UNSAFE_DATA"


@dataclass(frozen=True, slots=True)
class CycleResult:
    status: CycleStatus
    end_utc: datetime
    created_order_ids: tuple[str, ...] = ()
    filled_order_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    equity: float | None = None


class PaperServiceError(RuntimeError):
    """Raised when a cycle cannot safely reconcile durable evidence."""


@dataclass(frozen=True, slots=True)
class _OpenPosition:
    order: PaperOrder
    entry_fill: PaperFill
    entry_price: float
    initial_stop: float
    current_stop: float
    high_water: float
    held_bars: int


@dataclass(frozen=True, slots=True)
class _RiskState:
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


class PaperService:
    """Run exactly one restart-safe decision cycle for a completed candle."""

    def __init__(
        self,
        *,
        source: CompletedCandleSource,
        store: SQLiteStore,
        broker: PaperBroker,
        clock: Clock,
        lease_owner: str,
        lease_token: str,
        strategy_config: StrategyConfig = StrategyConfig(),
        risk_config: RiskConfig = RiskConfig(),
        costs: CostConfig = CostConfig(),
        lease_ttl: timedelta = _DEFAULT_LEASE_TTL,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        if not isinstance(lease_owner, str) or not lease_owner:
            raise ValueError("lease owner must be non-empty")
        if not isinstance(lease_token, str) or not lease_token:
            raise ValueError("lease token must be non-empty")
        if not isinstance(lease_ttl, timedelta) or lease_ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")
        self._source = source
        self._store = store
        self._broker = broker
        self._clock = clock
        self._lease_owner = lease_owner
        self._lease_token = lease_token
        self._strategy = strategy_config
        self._risk = risk_config
        self._costs = costs
        self._lease_ttl = lease_ttl
        self._fault_hook = fault_hook

    def process_completed_candle(self, end_utc: datetime) -> CycleResult:
        end = _require_cycle_end(end_utc)
        now = _clock_utc(self._clock)
        if now < end + _MATURITY_DELAY:
            raise ValueError("completed candle must be at least ten minutes old")
        event_id = f"cycle:{_canonical_datetime(end)}"
        if _has_completed_cycle(self._store.replay_state(), event_id, end):
            return CycleResult(CycleStatus.ALREADY_PROCESSED, end)

        expires = _safe_add(now, self._lease_ttl, "lease expiry is outside datetime range")
        if not self._store.acquire_cycle_lease(
            self._lease_owner,
            self._lease_token,
            now,
            expires,
        ):
            return CycleResult(CycleStatus.LEASE_HELD, end)

        try:
            if _has_completed_cycle(self._store.replay_state(), event_id, end):
                return CycleResult(CycleStatus.ALREADY_PROCESSED, end)

            raw = self._source.load_completed_candles(end)
            if not isinstance(raw, pd.DataFrame):
                raise TypeError("completed candle source must return a pandas DataFrame")
            quality = canonicalize_ohlcv(raw, end)
            enriched = compute_trend_indicators(quality.frame, self._strategy)
            bar_at = end - _FOUR_HOURS
            row = enriched.loc[bar_at] if bar_at in enriched.index else None

            renewed_at = _clock_utc(self._clock)
            renewed_expiry = _safe_add(
                renewed_at,
                self._lease_ttl,
                "lease expiry is outside datetime range",
            )
            if not self._store.acquire_cycle_lease(
                self._lease_owner,
                self._lease_token,
                renewed_at,
                renewed_expiry,
            ):
                return CycleResult(CycleStatus.LEASE_HELD, end)
            if _has_completed_cycle(self._store.replay_state(), event_id, end):
                return CycleResult(CycleStatus.ALREADY_PROCESSED, end)

            if row is None or not _safe_execution_row(row):
                unsafe_state = self._broker.reconcile()
                if (
                    unsafe_state.btc_quantity > 0.0
                    or unsafe_state.active_orders
                    or self._store.replay_state().pending_orders
                ):
                    raise PaperServiceError(
                        "unsafe latest candle prevents pending execution or protection obligation"
                    )
                result = CycleResult(
                    CycleStatus.UNSAFE_DATA,
                    end,
                    reasons=("LATEST_CANDLE_UNSAFE",),
                )
                self._record_completion(event_id, result)
                return result

            created_ids: list[str] = []
            filled_ids: list[str] = []
            reconciliation = self._broker.reconcile()
            for order in reconciliation.active_orders:
                eligible = order.eligible_open_utc
                if eligible is None:
                    raise PaperServiceError("active market order has no eligible open")
                if eligible < bar_at:
                    raise PaperServiceError("active market order missed its eligible open")
                if eligible == bar_at:
                    fill = self._broker.process_open(
                        order.order_id,
                        bar_at,
                        open_price=float(row["open"]),
                    )
                    if fill is not None:
                        filled_ids.append(fill.order_id)

            reconciliation = self._broker.reconcile()
            if reconciliation.btc_quantity > 0.0 and not reconciliation.active_orders:
                stop_fill = self._broker.process_intrabar_stop(
                    bar_at,
                    open_price=float(row["open"]),
                    low_price=float(row["low"]),
                )
                if stop_fill is not None:
                    filled_ids.append(stop_fill.order_id)

            reconciliation = self._broker.reconcile()
            equity = _mark_to_market(reconciliation, float(row["close"]))
            risk_decision = self._risk_decision(
                bar_at,
                row,
                equity,
                reconciliation,
            )
            reconciliation = self._broker.reconcile()

            if reconciliation.btc_quantity > 0.0 and not reconciliation.active_orders:
                exit_reason = self._exit_reason(
                    bar_at,
                    row,
                    reconciliation,
                    risk_decision,
                    enriched,
                )
                if exit_reason is not None:
                    order = self._broker.submit_exit(
                        bar_at,
                        quantity=reconciliation.btc_quantity,
                        owned_quantity=reconciliation.btc_quantity,
                        reason=exit_reason,
                    )
                    created_ids.append(order.order_id)
                else:
                    self._ensure_stop(bar_at, row, reconciliation, enriched)
            elif (
                reconciliation.btc_quantity == 0.0
                and not reconciliation.active_orders
                and risk_decision.risk_rate > 0.0
                and risk_decision.exposure_cap > 0.0
                and evaluate_entry(row, is_flat=True, config=self._strategy)
            ):
                close = float(row["close"])
                atr = float(row[f"atr_{self._strategy.atr_period}"])
                initial_stop = close - self._strategy.initial_atr_mult * atr
                size = calculate_size(
                    equity=equity,
                    cash=reconciliation.cash,
                    entry=close,
                    stop=initial_stop,
                    current_atr_pct=atr / close,
                    baseline_atr_pct=float(row["baseline_atr_pct"]),
                    risk_rate=risk_decision.risk_rate,
                    exposure_cap=risk_decision.exposure_cap,
                    costs=self._costs,
                )
                if size.quantity > 0.0:
                    order = self._broker.submit_entry(
                        bar_at,
                        quantity=size.quantity,
                        reason="ENTRY",
                    )
                    created_ids.append(order.order_id)
                    self._fault("after_order_acceptance")

            result = CycleResult(
                CycleStatus.PROCESSED,
                end,
                created_order_ids=tuple(created_ids),
                filled_order_ids=tuple(filled_ids),
                reasons=risk_decision.reasons,
                equity=equity,
            )
            self._record_completion(event_id, result)
            return result
        finally:
            self._store.release_cycle_lease(self._lease_owner, self._lease_token)

    def _risk_decision(
        self,
        bar_at: datetime,
        row: pd.Series,
        equity: float,
        reconciliation: PaperReconciliation,
    ) -> RiskDecision:
        stored = _risk_state_from_payload(self._store.replay_state().breaker_state)
        if stored.last_risk_at is not None:
            if stored.last_risk_at > bar_at:
                raise PaperServiceError("risk state timestamp is ahead of the current candle")
            if stored.last_risk_at == bar_at:
                return stored.decision

        state = _advance_risk_state(
            stored,
            bar_at=bar_at,
            equity=equity,
            row=row,
            completed_trades=reconciliation.completed_trades,
            risk_config=self._risk,
            system_healthy=not bool(self._store.replay_state().health_state.get("halt_entries", False)),
        )
        self._store.append_event(
            f"risk:{_canonical_datetime(bar_at)}",
            "BREAKER_STATE",
            bar_at,
            _risk_state_payload(state),
        )
        return state.decision

    def _exit_reason(
        self,
        bar_at: datetime,
        row: pd.Series,
        reconciliation: PaperReconciliation,
        risk: RiskDecision,
        enriched: pd.DataFrame,
    ) -> str | None:
        forced = _forced_exit_reason(risk)
        if forced is not None:
            return forced
        position = _open_position(
            self._broker,
            reconciliation,
            enriched,
            bar_at,
            self._strategy,
        )
        if evaluate_close_exit(row):
            return "CLOSE_EXIT"
        risk_unit = position.entry_price - position.initial_stop
        if (
            position.held_bars >= self._strategy.stagnant_bars
            and risk_unit > 0.0
            and position.high_water
            < position.entry_price + self._strategy.stagnant_min_r * risk_unit
        ):
            return "STAGNANT_EXIT"
        if position.held_bars >= self._strategy.max_holding_bars:
            return "MAX_HOLD_EXIT"
        return None

    def _ensure_stop(
        self,
        bar_at: datetime,
        row: pd.Series,
        reconciliation: PaperReconciliation,
        enriched: pd.DataFrame,
    ) -> None:
        position = _open_position(
            self._broker,
            reconciliation,
            enriched,
            bar_at,
            self._strategy,
        )
        active = reconciliation.active_stop
        if active is None:
            self._broker.set_stop(
                bar_at,
                position.initial_stop,
                reason="HARD_STOP",
                source_id=position.order.order_id,
            )
            return
        candidate = next_stop(
            PositionSnapshot(
                entry_price=position.entry_price,
                initial_stop=position.initial_stop,
                current_stop=position.current_stop,
                high_water=position.high_water,
            ),
            row,
            self._strategy,
        )
        if Decimal(str(candidate)) > Decimal(str(active.stop_price)):
            self._broker.set_stop(
                bar_at,
                candidate,
                reason="TRAILING_STOP",
                source_id=position.order.order_id,
            )

    def _record_completion(self, event_id: str, result: CycleResult) -> None:
        self._store.append_event(
            event_id,
            "PAPER_CYCLE",
            result.end_utc,
            {
                "created_order_ids": list(result.created_order_ids),
                "end_utc": _canonical_datetime(result.end_utc),
                "filled_order_ids": list(result.filled_order_ids),
                "reason_codes": list(result.reasons),
                "status": result.status.value,
            },
        )

    def _fault(self, boundary: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(boundary)


def _has_completed_cycle(snapshot: PaperSnapshot, event_id: str, end: datetime) -> bool:
    matches = tuple(event for event in snapshot.event_evidence if event.event_id == event_id)
    if not matches:
        return False
    if len(matches) != 1:
        raise StoreCorruptionError("cycle completion identity is duplicated")
    event = matches[0]
    if event.event_type != "PAPER_CYCLE" or event.occurred_at_utc != end:
        raise StoreCorruptionError("cycle completion evidence is inconsistent")
    payload = event.payload
    expected_keys = {
        "created_order_ids",
        "end_utc",
        "filled_order_ids",
        "reason_codes",
        "status",
    }
    if set(payload) != expected_keys or payload["end_utc"] != _canonical_datetime(end):
        raise StoreCorruptionError("cycle completion payload is inconsistent")
    if payload["status"] not in {CycleStatus.PROCESSED.value, CycleStatus.UNSAFE_DATA.value}:
        raise StoreCorruptionError("cycle completion status is invalid")
    for key in ("created_order_ids", "filled_order_ids", "reason_codes"):
        values = payload[key]
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise StoreCorruptionError("cycle completion list evidence is invalid")
        if any(not isinstance(value, str) or not value for value in values):
            raise StoreCorruptionError("cycle completion list evidence is invalid")
    return True


def _open_position(
    broker: PaperBroker,
    reconciliation: PaperReconciliation,
    enriched: pd.DataFrame,
    bar_at: datetime,
    config: StrategyConfig,
) -> _OpenPosition:
    if reconciliation.btc_quantity <= 0.0:
        raise PaperServiceError("cannot reconstruct a flat position")
    inventory = Decimal("0")
    open_entry: PaperFill | None = None
    for fill in reconciliation.fills:
        quantity = Decimal(str(fill.quantity))
        if fill.side == "BUY":
            if inventory != 0:
                raise PaperServiceError("open position contains pyramiding")
            open_entry = fill
            inventory += quantity
        else:
            inventory -= quantity
            if inventory < 0:
                raise PaperServiceError("open position fill history oversells")
            if inventory == 0:
                open_entry = None
    if open_entry is None or inventory != Decimal(str(reconciliation.btc_quantity)):
        raise PaperServiceError("open position does not reconcile to paper fills")

    order = broker.order(open_entry.order_id)
    signal_at = pd.Timestamp(order.signal_at_utc)
    if signal_at not in enriched.index:
        raise PaperServiceError("entry signal candle is absent from public history")
    signal_row = enriched.loc[signal_at]
    atr = _positive_finite(signal_row.get(f"atr_{config.atr_period}"), "entry ATR")
    initial_stop = open_entry.fill_price - config.initial_atr_mult * atr
    if not math.isfinite(initial_stop) or initial_stop <= 0.0:
        raise PaperServiceError("reconstructed initial stop is invalid")

    held = enriched.loc[
        (enriched.index >= pd.Timestamp(open_entry.fill_time))
        & (enriched.index <= pd.Timestamp(bar_at))
    ]
    highs = pd.to_numeric(held["high"], errors="coerce")
    if held.empty or not highs.notna().all():
        raise PaperServiceError("held candle history is incomplete")
    high_water = float(highs.max())
    active = reconciliation.active_stop
    current_stop = active.stop_price if active is not None else initial_stop
    return _OpenPosition(
        order=order,
        entry_fill=open_entry,
        entry_price=open_entry.fill_price,
        initial_stop=initial_stop,
        current_stop=current_stop,
        high_water=high_water,
        held_bars=len(held) - 1,
    )


def _advance_risk_state(
    prior: _RiskState,
    *,
    bar_at: datetime,
    equity: float,
    row: pd.Series,
    completed_trades: tuple[PaperTrade, ...],
    risk_config: RiskConfig,
    system_healthy: bool,
) -> _RiskState:
    if not math.isfinite(equity) or equity < 0.0:
        raise PaperServiceError("mark-to-market equity is invalid")
    if prior.processed_trade_count > len(completed_trades):
        raise PaperServiceError("risk trade cursor is ahead of broker history")

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
    history = (*prior.equity_history, (bar_at, equity))
    cutoff = bar_at - timedelta(days=7)
    recent_history = tuple(item for item in history if item[0] >= cutoff)
    if bar_at - risk_started_at < timedelta(days=7):
        weekly_baseline = 100.0
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

    close = _positive_finite(row.get("close"), "risk close")
    atr = _positive_finite(row.get(f"atr_{StrategyConfig().atr_period}"), "risk ATR")
    baseline_atr_pct = _positive_finite(row.get("baseline_atr_pct"), "baseline ATR percent")
    volatility_ratio = (atr / close) / baseline_atr_pct
    volatility_halted = prior.volatility_halted
    stable_bars = prior.volatility_stable_bars
    volatility_bar_valid = bool(row.get("warmup_complete")) and bool(
        row.get("entry_data_valid")
    )
    if volatility_halted:
        stable_bars = stable_bars + 1 if volatility_bar_valid and volatility_ratio <= 1.5 else 0
    elif volatility_ratio > 3.0:
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

    return _RiskState(
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


def _risk_state_payload(state: _RiskState) -> dict[str, object]:
    return {
        "consecutive_losses": state.consecutive_losses,
        "daily_baseline_equity": state.daily_baseline_equity,
        "daily_date": state.daily_date.isoformat() if state.daily_date is not None else None,
        "daily_halt_started_at_utc": _optional_datetime(state.daily_halt_started_at),
        "decision_exposure_cap": state.decision.exposure_cap,
        "decision_halted_until_utc": _optional_datetime(state.decision.halted_until),
        "decision_reasons": list(state.decision.reasons),
        "decision_risk_rate": state.decision.risk_rate,
        "equity_history": [
            {"at_utc": _canonical_datetime(at), "equity": value}
            for at, value in state.equity_history
        ],
        "equity_peak": state.equity_peak,
        "halt_entries": state.decision.risk_rate <= 0.0,
        "last_equity": state.last_equity,
        "last_risk_at_utc": _optional_datetime(state.last_risk_at),
        "processed_trade_count": state.processed_trade_count,
        "profitable_trades_since_streak_halt": state.profitable_trades_since_streak_halt,
        "recovery_started_at_utc": _optional_datetime(state.recovery_started_at),
        "risk_started_at_utc": _optional_datetime(state.risk_started_at),
        "streak_halt_started_at_utc": _optional_datetime(state.streak_halt_started_at),
        "version": _RISK_STATE_VERSION,
        "volatility_halted": state.volatility_halted,
        "volatility_stable_bars": state.volatility_stable_bars,
        "weekly_halt_started_at_utc": _optional_datetime(state.weekly_halt_started_at),
    }


def _risk_state_from_payload(payload: Mapping[str, object]) -> _RiskState:
    if not payload:
        return _RiskState()
    expected = set(_risk_state_payload(_RiskState()))
    if set(payload) != expected or payload.get("version") != _RISK_STATE_VERSION:
        raise StoreCorruptionError("persisted breaker state has an invalid schema")
    try:
        daily_value = payload["daily_date"]
        daily_date = date.fromisoformat(daily_value) if isinstance(daily_value, str) else None
        history_value = payload["equity_history"]
        if not isinstance(history_value, Sequence) or isinstance(history_value, (str, bytes)):
            raise ValueError("invalid equity history")
        history: list[tuple[datetime, float]] = []
        for item in history_value:
            if not isinstance(item, Mapping) or set(item) != {"at_utc", "equity"}:
                raise ValueError("invalid equity history item")
            history.append(
                (
                    _strict_stored_datetime(item["at_utc"]),
                    _finite_nonnegative(item["equity"], "history equity"),
                )
            )
        if any(current[0] <= previous[0] for previous, current in zip(history, history[1:])):
            raise ValueError("risk history is not strictly increasing")
        reasons = payload["decision_reasons"]
        if not isinstance(reasons, Sequence) or isinstance(reasons, (str, bytes)):
            raise ValueError("invalid risk reasons")
        normalized_reasons = tuple(reasons)
        if any(not isinstance(reason, str) or not reason for reason in normalized_reasons):
            raise ValueError("invalid risk reason")
        decision = RiskDecision(
            _finite_nonnegative(payload["decision_risk_rate"], "decision risk rate"),
            _finite_nonnegative(payload["decision_exposure_cap"], "decision exposure cap"),
            _optional_stored_datetime(payload["decision_halted_until_utc"]),
            normalized_reasons,
        )
        state = _RiskState(
            equity_peak=_finite_nonnegative(payload["equity_peak"], "equity peak"),
            daily_date=daily_date,
            daily_baseline_equity=_finite_nonnegative(
                payload["daily_baseline_equity"], "daily baseline"
            ),
            equity_history=tuple(history),
            risk_started_at=_optional_stored_datetime(payload["risk_started_at_utc"]),
            last_equity=_finite_nonnegative(payload["last_equity"], "last equity"),
            last_risk_at=_optional_stored_datetime(payload["last_risk_at_utc"]),
            consecutive_losses=_nonnegative_int(payload["consecutive_losses"], "loss count"),
            processed_trade_count=_nonnegative_int(
                payload["processed_trade_count"], "trade cursor"
            ),
            recovery_started_at=_optional_stored_datetime(payload["recovery_started_at_utc"]),
            daily_halt_started_at=_optional_stored_datetime(
                payload["daily_halt_started_at_utc"]
            ),
            weekly_halt_started_at=_optional_stored_datetime(
                payload["weekly_halt_started_at_utc"]
            ),
            streak_halt_started_at=_optional_stored_datetime(
                payload["streak_halt_started_at_utc"]
            ),
            profitable_trades_since_streak_halt=_nonnegative_int(
                payload["profitable_trades_since_streak_halt"], "profitable trade count"
            ),
            volatility_halted=_strict_bool(payload["volatility_halted"], "volatility halt"),
            volatility_stable_bars=_nonnegative_int(
                payload["volatility_stable_bars"], "volatility stable bars"
            ),
            decision=decision,
        )
        if _strict_bool(payload["halt_entries"], "halt entries") != (
            decision.risk_rate <= 0.0
        ):
            raise ValueError("halt flag contradicts risk decision")
        if state.last_risk_at is not None and history and state.last_risk_at != history[-1][0]:
            raise ValueError("risk cursor contradicts equity history")
        return state
    except (TypeError, ValueError, OverflowError, KeyError) as error:
        raise StoreCorruptionError("persisted breaker state is invalid") from error


def _safe_execution_row(row: pd.Series) -> bool:
    values: list[float] = []
    for key in ("open", "high", "low", "close", "volume"):
        try:
            value = float(row[key])
        except (TypeError, ValueError, KeyError):
            return False
        if not math.isfinite(value) or (key != "volume" and value <= 0.0) or value < 0.0:
            return False
        values.append(value)
    open_price, high, low, close, _ = values
    return (
        high >= max(open_price, close, low)
        and low <= min(open_price, close, high)
        and not bool(row.get("is_quarantined"))
        and not bool(row.get("is_filled"))
    )


def _mark_to_market(reconciliation: PaperReconciliation, close: float) -> float:
    value = Decimal(str(reconciliation.cash)) + Decimal(
        str(reconciliation.btc_quantity)
    ) * Decimal(str(close))
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise PaperServiceError("mark-to-market equity is invalid")
    return result


def _forced_exit_reason(decision: RiskDecision) -> str | None:
    if "drawdown_halt" in decision.reasons:
        return "RISK_EXIT"
    if any(reason in {"system_unhealthy", "invalid_input", "invalid_config"} for reason in decision.reasons):
        return "SYSTEM_EXIT"
    return None


def _loss_from_baseline(equity: float, baseline: float) -> float:
    if baseline <= 0.0:
        return 0.0
    return max(0.0, 1.0 - equity / baseline)


def _require_cycle_end(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("end_utc must be timezone-aware UTC")
    try:
        if value.utcoffset() != timedelta(0):
            raise ValueError("end_utc must use UTC")
        result = value.astimezone(_UTC)
    except (OverflowError, ValueError) as error:
        raise ValueError("end_utc must use UTC") from error
    if result.minute or result.second or result.microsecond or result.hour % 4:
        raise ValueError("end_utc must be an exact four-hour UTC boundary")
    return result


def _clock_utc(clock: Clock) -> datetime:
    value = clock.now()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    try:
        if value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(_UTC)
    except (OverflowError, ValueError) as error:
        raise ValueError("clock returned an invalid datetime") from error


def _safe_add(value: datetime, duration: timedelta, message: str) -> datetime:
    try:
        return value + duration
    except OverflowError as error:
        raise ValueError(message) from error


def _canonical_datetime(value: datetime) -> str:
    return value.astimezone(_UTC).isoformat().replace("+00:00", "Z")


def _optional_datetime(value: datetime | None) -> str | None:
    return _canonical_datetime(value) if value is not None else None


def _strict_stored_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("stored datetime must be canonical UTC")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if _canonical_datetime(parsed) != value:
        raise ValueError("stored datetime must be canonical UTC")
    return parsed


def _optional_stored_datetime(value: object) -> datetime | None:
    return None if value is None else _strict_stored_datetime(value)


def _positive_finite(value: object, label: str) -> float:
    converted = _finite_nonnegative(value, label)
    if converted <= 0.0:
        raise PaperServiceError(f"{label} must be positive")
    return converted


def _finite_nonnegative(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite and non-negative")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0.0:
        raise ValueError(f"{label} must be finite and non-negative")
    return converted


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _strict_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a bool")
    return value
