"""Deterministic orchestration for one completed KRW-BTC paper candle."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
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
from autobit.persistence.sqlite_store import (
    PaperSnapshot,
    SQLiteStore,
    StoreCorruptionError,
    StoredEvent,
)
from autobit.paper.health import (
    EmptyHealthEventError,
    HealthAction,
    HealthMonitor,
    HealthSnapshot,
    HealthStateError,
)
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
_RISK_STATE_VERSION = 2
_CYCLE_ATTEMPT_VERSION = 1
_UTC = timezone.utc
_ALERT_SOURCE_TYPES = frozenset({"HEALTH_STATE", "PAPER_CYCLE"})


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


@dataclass(frozen=True, slots=True)
class _HealthGate:
    action: HealthAction | None
    evidence_event_id: str | None
    evidence_sequence: int
    evidence_version: int


@dataclass(frozen=True, slots=True)
class _ValidatedRiskChain:
    projection: _RiskState
    latest_base: _RiskState
    latest_base_event: StoredEvent | None
    latest_risk_event: StoredEvent | None


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

    def oldest_required_end(self, latest_matured: datetime) -> datetime:
        """Resolve the oldest unfinished cycle exclusively from durable evidence."""
        latest = _require_cycle_end(latest_matured)
        snapshot = self._store.replay_state()
        risk_chain = _validate_health_and_risk_chain(snapshot)
        _validate_operational_alert_chain(snapshot.event_evidence)
        reconciliation = self._broker.reconcile()
        completed = _completed_cycle_ends(
            snapshot,
            self._broker,
            reconciliation,
            latest,
        )
        attempted = _validated_cycle_attempts(snapshot, completed, latest)

        if not snapshot.event_evidence:
            return latest

        observed: set[datetime] = set()
        obligations: set[datetime] = set()
        if completed:
            obligations.add(_first_uncompleted_after_cycle_chain(completed))
        obligations.update(attempted - completed)

        risk_state = risk_chain.projection
        if risk_state.last_risk_at is not None:
            observed.add(
                _observed_cycle_end(
                    risk_state.last_risk_at,
                    latest,
                    "risk observation",
                )
            )

        orders: dict[str, PaperOrder] = {
            order.order_id: order for order in reconciliation.active_orders
        }
        for event in snapshot.event_evidence:
            if event.event_type != "PAPER_ORDER":
                continue
            order_id = event.payload.get("order_id")
            if not isinstance(order_id, str) or not order_id:
                raise StoreCorruptionError("paper order identity is invalid")
            orders[order_id] = self._broker.order(order_id)
        for order in orders.values():
            eligible = order.eligible_open_utc
            if eligible is None:
                raise PaperServiceError("paper order has no eligible open evidence")
            signal_cycle = _observed_cycle_end(
                order.signal_at_utc,
                latest,
                "order signal",
            )
            observed.add(signal_cycle)
            if order.order_kind == "MARKET":
                if eligible != signal_cycle:
                    raise PaperServiceError("market order timing evidence is contradictory")
                obligations.add(
                    _future_obligation_end(
                        eligible,
                        latest,
                        "order execution",
                    )
                )
            elif order.order_kind == "STOP":
                if order.side != "SELL":
                    raise StoreCorruptionError("stop order side is invalid")
                observed.add(
                    _observed_cycle_end(
                        eligible,
                        latest,
                        "stop order trigger",
                    )
                )
            else:
                raise StoreCorruptionError("paper order kind is invalid")

        for fill in reconciliation.fills:
            observed.add(
                _observed_cycle_end(
                    fill.fill_time,
                    latest,
                    "fill observation",
                )
            )

        stop = reconciliation.active_stop
        if stop is not None:
            observed_cycle = _observed_cycle_end(
                stop.observed_at_utc,
                latest,
                "stop observation",
            )
            if stop.active_after_utc not in {stop.observed_at_utc, observed_cycle}:
                raise PaperServiceError("stop timing evidence is contradictory")
            observed.add(observed_cycle)
            obligations.add(
                _future_obligation_end(
                    stop.active_after_utc,
                    latest,
                    "stop activation",
                )
            )

        if completed:
            next_after_chain = _first_uncompleted_after_cycle_chain(completed)
            for direct in observed:
                if direct not in completed and direct != next_after_chain:
                    raise StoreCorruptionError(
                        "direct paper evidence contradicts completed cycle chronology"
                    )
            for obligation in obligations:
                if obligation not in completed and obligation < next_after_chain:
                    raise StoreCorruptionError(
                        "paper obligation would require a chronological rollback"
                    )
        elif len(observed) > 1:
            raise StoreCorruptionError(
                "direct paper evidence skips an uncompleted cycle chronology"
            )

        unfinished = sorted(
            end for end in observed | obligations if end not in completed
        )
        if not unfinished:
            operational_only = {
                "HEALTH_STATE",
                "ALERT_ATTEMPT",
                "ALERT_FAILURE",
                "PAPER_CYCLE_ATTEMPT",
            }
            if all(
                event.event_type in operational_only
                for event in snapshot.event_evidence
            ):
                return latest
            raise PaperServiceError("durable paper evidence has no resolvable cycle cursor")
        return unfinished[0]

    def process_completed_candle(self, end_utc: datetime) -> CycleResult:
        end = _require_cycle_end(end_utc)
        now = _clock_utc(self._clock)
        if now < end + _MATURITY_DELAY:
            raise ValueError("completed candle must be at least ten minutes old")
        latest_matured = _latest_matured_end(now)
        event_id = f"cycle:{_canonical_datetime(end)}"
        initial_snapshot = self._store.replay_state()
        _validate_health_and_risk_chain(initial_snapshot)
        _validate_operational_alert_chain(initial_snapshot.event_evidence)
        initial_reconciliation = self._broker.reconcile()
        initial_completed = _completed_cycle_ends(
            initial_snapshot,
            self._broker,
            initial_reconciliation,
            latest_matured,
        )
        initial_attempted = _validated_cycle_attempts(
            initial_snapshot,
            initial_completed,
            latest_matured,
        )
        if end in initial_completed:
            return CycleResult(CycleStatus.ALREADY_PROCESSED, end)
        _require_attempt_target(end, initial_attempted, initial_completed)

        expires = _safe_add(now, self._lease_ttl, "lease expiry is outside datetime range")
        if not self._store.acquire_cycle_lease(
            self._lease_owner,
            self._lease_token,
            now,
            expires,
        ):
            return CycleResult(CycleStatus.LEASE_HELD, end)

        try:
            leased_snapshot = self._store.replay_state()
            _validate_health_and_risk_chain(leased_snapshot)
            _validate_operational_alert_chain(leased_snapshot.event_evidence)
            leased_reconciliation = self._broker.reconcile()
            leased_completed = _completed_cycle_ends(
                leased_snapshot,
                self._broker,
                leased_reconciliation,
                latest_matured,
            )
            leased_attempted = _validated_cycle_attempts(
                leased_snapshot,
                leased_completed,
                latest_matured,
            )
            if end in leased_completed:
                return CycleResult(CycleStatus.ALREADY_PROCESSED, end)
            _require_attempt_target(end, leased_attempted, leased_completed)

            self._record_attempt(end, leased_attempted)
            self._fault("after_cycle_attempt")

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
            if _has_completed_cycle(
                self._validated_snapshot(),
                event_id,
                end,
                self._broker,
            ):
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

            # Legacy service orders predate immutable entry contexts.  Resolve
            # their public signal window before the fill mutation so observed
            # health/alert evidence cannot be rolled back with a failed fill.
            prepared_contexts = self._prepare_legacy_entry_contexts(bar_at, enriched)

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
            if _has_completed_cycle(
                self._validated_snapshot(),
                event_id,
                end,
                self._broker,
            ):
                return CycleResult(CycleStatus.ALREADY_PROCESSED, end)

            created_ids: list[str] = []
            filled_ids: list[str] = []
            with self._store.transaction():
                atomic_snapshot = self._validated_snapshot()
                _validate_operational_alert_chain(atomic_snapshot.event_evidence)
                if _has_completed_cycle(
                    atomic_snapshot,
                    event_id,
                    end,
                    self._broker,
                ):
                    return CycleResult(CycleStatus.ALREADY_PROCESSED, end)
                reconciliation = self._broker.reconcile()
                filled_entry_this_bar = False
                for order in reconciliation.active_orders:
                    eligible = order.eligible_open_utc
                    if eligible is None:
                        raise PaperServiceError("active market order has no eligible open")
                    if eligible < bar_at:
                        raise PaperServiceError("active market order missed its eligible open")
                    if eligible == bar_at:
                        if order.side == "BUY" and order.execution_context is None:
                            if order.order_id not in prepared_contexts:
                                raise PaperServiceError(
                                    "pending BUY changed during legacy context preparation"
                                )
                            context = prepared_contexts[order.order_id]
                            if context is not None:
                                self._broker.bind_entry_context(order.order_id, context)
                        fill = self._broker.process_open(
                            order.order_id,
                            bar_at,
                            open_price=float(row["open"]),
                        )
                        if fill is not None:
                            filled_ids.append(fill.order_id)
                            filled_entry_this_bar = fill.side == "BUY"

                if filled_entry_this_bar:
                    self._fault("after_pending_open")
                reconciliation = self._broker.reconcile()
                if (
                    reconciliation.btc_quantity > 0.0
                    and not reconciliation.active_orders
                    and reconciliation.active_stop is None
                ):
                    self._ensure_stop(
                        bar_at,
                        row,
                        reconciliation,
                        enriched,
                        active_after=(bar_at if filled_entry_this_bar else None),
                    )
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
            if reconciliation.btc_quantity > 0.0:
                self._observe_position(bar_at, reconciliation, enriched)
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
                        execution_context=self._entry_context(bar_at, row, risk_decision),
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

    def _validated_snapshot(self) -> PaperSnapshot:
        snapshot = self._store.replay_state()
        _validate_health_and_risk_chain(snapshot)
        return snapshot

    def _entry_context(
        self, bar_at: datetime, row: pd.Series, decision: RiskDecision,
    ) -> dict[str, object]:
        chain = _validate_health_and_risk_chain(self._store.replay_state())
        event = chain.latest_risk_event
        if event is None or chain.projection.last_risk_at != bar_at:
            raise PaperServiceError("entry sizing lacks same-signal risk evidence")
        return {
            "signal_price": float(row["close"]),
            "entry_atr": float(row[f"atr_{self._strategy.atr_period}"]),
            "initial_atr_mult": self._strategy.initial_atr_mult,
            "baseline_atr_pct": float(row["baseline_atr_pct"]),
            "risk_rate": decision.risk_rate, "exposure_cap": decision.exposure_cap,
            "risk_event_id": event.event_id,
        }

    def _observe_position(
        self, bar_at: datetime, reconciliation: PaperReconciliation, enriched: pd.DataFrame,
    ) -> None:
        entry = _open_entry(reconciliation)
        observation = self._broker.position_observation(entry.order_id)
        if observation is not None and observation.occurred_at_utc >= bar_at:
            if observation.occurred_at_utc > bar_at:
                raise PaperServiceError("position observation is ahead of current candle")
            return
        start = observation.occurred_at_utc + _FOUR_HOURS if observation else entry.fill_time
        end = bar_at + _FOUR_HOURS
        held = enriched.loc[(enriched.index >= start) & (enriched.index < end)]
        expected = pd.date_range(start, bar_at, freq="4h", tz="UTC")
        if not held.index.equals(expected):
            loader = getattr(self._source, "load_position_candles", None)
            if loader is None:
                raise PaperServiceError("public position-history backfill is unavailable")
            held = loader(start, end)
        held = _validated_position_frame(held, start, end)
        initial = (float(observation.payload["initial_stop"]) if observation
                   else self._broker.initial_stop(entry.order_id))
        if initial is None:
            raise PaperServiceError("position history lacks immutable initial protection")
        self._broker.observe_position(
            entry.order_id,
            [[_canonical_datetime(at.to_pydatetime()), float(row["high"])] for at, row in held.iterrows()],
            initial,
        )
        self._fault("after_position_observation")

    def _legacy_entry_context(
        self, order: PaperOrder, enriched: pd.DataFrame,
    ) -> dict[str, object] | None:
        snapshot = self._store.replay_state()
        # Direct broker orders retain their historical generic contract. A service
        # order is recognized by its canonical same-signal risk ancestry.
        events = [event for event in snapshot.event_evidence
                  if event.event_type == "BREAKER_STATE"
                  and event.payload.get("last_risk_at_utc") == _canonical_datetime(order.signal_at_utc)]
        if not events:
            return None
        risk = _risk_state_from_payload(events[-1].payload)
        signal_end = order.signal_at_utc + _FOUR_HOURS
        raw = self._source.load_completed_candles(signal_end)
        original = compute_trend_indicators(canonicalize_ohlcv(raw, signal_end).frame, self._strategy)
        if len(original) != self._strategy.warmup_bars + 1 or original.index[-1] != order.signal_at_utc:
            raise PaperServiceError("legacy entry requires its original complete signal window")
        signal = original.loc[pd.Timestamp(order.signal_at_utc)]
        return {
            "signal_price": float(signal["close"]),
            "entry_atr": float(signal[f"atr_{self._strategy.atr_period}"]),
            "initial_atr_mult": self._strategy.initial_atr_mult,
            "baseline_atr_pct": float(signal["baseline_atr_pct"]),
            "risk_rate": risk.decision.risk_rate, "exposure_cap": risk.decision.exposure_cap,
            "risk_event_id": events[-1].event_id,
        }

    def _prepare_legacy_entry_contexts(
        self, bar_at: datetime, enriched: pd.DataFrame,
    ) -> dict[str, dict[str, object] | None]:
        """Resolve eligible legacy BUY contexts outside a trading mutation."""
        prepared: dict[str, dict[str, object] | None] = {}
        for order in self._broker.reconcile().active_orders:
            eligible = order.eligible_open_utc
            if eligible is None:
                raise PaperServiceError("active market order has no eligible open")
            if eligible < bar_at:
                raise PaperServiceError("active market order missed its eligible open")
            if (
                eligible == bar_at
                and order.side == "BUY"
                and order.execution_context is None
            ):
                prepared[order.order_id] = self._legacy_entry_context(order, enriched)
        return prepared

    def _risk_decision(
        self,
        bar_at: datetime,
        row: pd.Series,
        equity: float,
        reconciliation: PaperReconciliation,
    ) -> RiskDecision:
        snapshot = self._store.replay_state()
        chain = _validate_health_and_risk_chain(snapshot)
        health = _health_gate_from_snapshot(snapshot)
        stored = chain.projection
        if stored.last_risk_at is not None:
            if stored.last_risk_at > bar_at:
                raise PaperServiceError("risk state timestamp is ahead of the current candle")
            if stored.last_risk_at == bar_at:
                base = chain.latest_base
                if base.last_risk_at != bar_at:
                    raise StoreCorruptionError("risk projection lacks its canonical base")
                final = _apply_health_action(base.decision, health.action)
                expected_overlay_id = None
                if health.evidence_event_id is not None:
                    expected_overlay_id = _health_risk_followup_id(bar_at, health)
                current_is_bound = (
                    chain.latest_risk_event is not None
                    and chain.latest_risk_event.event_id == expected_overlay_id
                    and stored.decision == final
                )
                if stored.decision != final or (
                    final != base.decision and not current_is_bound
                ):
                    _persist_health_risk_followup(
                        self._store,
                        bar_at=bar_at,
                        stored=base,
                        final=final,
                        health=health,
                    )
                    self._fault("after_health_risk_followup")
                return final

        base = _advance_risk_state(
            stored,
            bar_at=bar_at,
            equity=equity,
            row=row,
            completed_trades=reconciliation.completed_trades,
            risk_config=self._risk,
            system_healthy=(
                health.action is not None and not health.action.halt_entries
            ),
        )
        self._store.append_event(
            f"risk:{_canonical_datetime(bar_at)}",
            "BREAKER_STATE",
            bar_at,
            _risk_state_payload(base),
        )
        final = _apply_health_action(base.decision, health.action)
        if final != base.decision:
            _persist_health_risk_followup(
                self._store,
                bar_at=bar_at,
                stored=base,
                final=final,
                health=health,
            )
            self._fault("after_health_risk_followup")
        return final

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
        active_after: datetime | None = None,
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
                active_after=active_after,
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

    def _record_attempt(
        self,
        end: datetime,
        attempted: frozenset[datetime],
    ) -> None:
        if end in attempted:
            return
        try:
            observed_at = end - _FOUR_HOURS
        except OverflowError as error:
            raise ValueError("cycle attempt is outside datetime range") from error
        self._store.append_event(
            _cycle_attempt_id(end),
            "PAPER_CYCLE_ATTEMPT",
            observed_at,
            {
                "end_utc": _canonical_datetime(end),
                "version": _CYCLE_ATTEMPT_VERSION,
            },
        )

    def _fault(self, boundary: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(boundary)


def _has_completed_cycle(
    snapshot: PaperSnapshot,
    event_id: str,
    end: datetime,
    broker: PaperBroker,
    reconciliation: PaperReconciliation | None = None,
) -> bool:
    matches = tuple(event for event in snapshot.event_evidence if event.event_id == event_id)
    if not matches:
        return False
    if len(matches) != 1:
        raise StoreCorruptionError("cycle completion identity is duplicated")
    _validate_cycle_event(
        snapshot,
        matches[0],
        end,
        broker,
        reconciliation,
    )
    return True


def _validate_cycle_event(
    snapshot: PaperSnapshot,
    event: StoredEvent,
    end: datetime,
    broker: PaperBroker,
    reconciliation: PaperReconciliation | None,
) -> None:
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
    normalized_lists: dict[str, tuple[str, ...]] = {}
    for key in ("created_order_ids", "filled_order_ids", "reason_codes"):
        values = payload[key]
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise StoreCorruptionError("cycle completion list evidence is invalid")
        if any(not isinstance(value, str) or not value for value in values):
            raise StoreCorruptionError("cycle completion list evidence is invalid")
        normalized_lists[key] = tuple(values)

    created = normalized_lists["created_order_ids"]
    filled = normalized_lists["filled_order_ids"]
    if len(set(created)) != len(created) or len(set(filled)) != len(filled):
        raise StoreCorruptionError("cycle mutation identities must be unique")
    if payload["status"] == CycleStatus.UNSAFE_DATA.value and (created or filled):
        raise StoreCorruptionError("unsafe cycle cannot claim mutation identities")

    account = reconciliation if reconciliation is not None else broker.reconcile()
    bar_at = end - _FOUR_HOURS
    for order_id in created:
        metadata = tuple(
            candidate
            for candidate in snapshot.event_evidence
            if candidate.event_type == "PAPER_ORDER"
            and candidate.payload.get("order_id") == order_id
            and candidate.sequence < event.sequence
        )
        if len(metadata) != 1:
            raise StoreCorruptionError(
                "cycle created order lacks prior paper order evidence"
            )
        try:
            order = broker.order(order_id)
        except ValueError as error:
            raise StoreCorruptionError("cycle created order does not exist") from error
        if order.order_kind != "MARKET" or order.parent_order_id is not None:
            raise StoreCorruptionError("cycle created order kind is invalid")
        if order.signal_at_utc != bar_at:
            raise StoreCorruptionError(
                "cycle created order does not belong to its signal bar"
            )

    for order_id in filled:
        metadata = tuple(
            candidate
            for candidate in snapshot.event_evidence
            if candidate.event_type == "PAPER_FILL"
            and candidate.payload.get("order_id") == order_id
            and candidate.occurred_at_utc == bar_at
            and candidate.sequence < event.sequence
        )
        matching_fills = tuple(
            fill
            for fill in account.fills
            if fill.order_id == order_id and fill.fill_time == bar_at
        )
        if len(metadata) != 1 or len(matching_fills) != 1:
            raise StoreCorruptionError(
                "cycle filled order lacks broker fill evidence for its fill bar"
            )
        try:
            order = broker.order(order_id)
        except ValueError as error:
            raise StoreCorruptionError("cycle filled order does not exist") from error
        if order.order_kind not in {"MARKET", "STOP"}:
            raise StoreCorruptionError("cycle filled order kind is invalid")
        if order.order_kind == "STOP" and order.side != "SELL":
            raise StoreCorruptionError("cycle stop fill side is invalid")


def _completed_cycle_ends(
    snapshot: PaperSnapshot,
    broker: PaperBroker,
    reconciliation: PaperReconciliation,
    latest_matured: datetime,
) -> frozenset[datetime]:
    completed: set[datetime] = set()
    for event in snapshot.event_evidence:
        if event.event_type != "PAPER_CYCLE" and not event.event_id.startswith("cycle:"):
            continue
        try:
            end = _require_cycle_end(event.occurred_at_utc)
        except ValueError as error:
            raise StoreCorruptionError("cycle completion boundary is invalid") from error
        expected_id = f"cycle:{_canonical_datetime(end)}"
        if event.event_id != expected_id:
            raise StoreCorruptionError("cycle completion identity is inconsistent")
        if end > latest_matured:
            raise StoreCorruptionError("cycle completion is later than latest matured end")
        _validate_cycle_event(snapshot, event, end, broker, reconciliation)
        if end in completed:
            raise StoreCorruptionError("cycle completion boundary is duplicated")
        completed.add(end)
    ordered = sorted(completed)
    if any(current - previous != _FOUR_HOURS for previous, current in zip(ordered, ordered[1:])):
        raise StoreCorruptionError("cycle completion chronology is not contiguous")
    return frozenset(completed)


def _cycle_attempt_id(end: datetime) -> str:
    return f"paper-cycle-attempt:{_canonical_datetime(end)}"


def _validated_cycle_attempts(
    snapshot: PaperSnapshot,
    completed: frozenset[datetime],
    latest_matured: datetime,
) -> frozenset[datetime]:
    latest = _require_cycle_end(latest_matured)
    attempts: dict[datetime, StoredEvent] = {}
    reserved_prefix = "paper-cycle-attempt:"
    for event in snapshot.event_evidence:
        if event.event_type != "PAPER_CYCLE_ATTEMPT":
            if event.event_id.startswith(reserved_prefix):
                raise StoreCorruptionError("cycle attempt identity has the wrong event type")
            continue
        payload = event.payload
        if (
            set(payload) != {"end_utc", "version"}
            or type(payload.get("version")) is not int
            or payload["version"] != _CYCLE_ATTEMPT_VERSION
        ):
            raise StoreCorruptionError("cycle attempt payload is invalid")
        try:
            target = _require_cycle_end(_strict_stored_datetime(payload["end_utc"]))
            observed_at = target - _FOUR_HOURS
        except (TypeError, ValueError, OverflowError) as error:
            raise StoreCorruptionError("cycle attempt target is invalid") from error
        if event.event_id != _cycle_attempt_id(target):
            raise StoreCorruptionError("cycle attempt identity is inconsistent")
        if event.occurred_at_utc != observed_at:
            raise StoreCorruptionError("cycle attempt envelope is inconsistent")
        if target > latest:
            raise StoreCorruptionError("cycle attempt target is not yet mature")
        if target in attempts:
            raise StoreCorruptionError("cycle attempt target is duplicated")
        attempts[target] = event

    cycle_events = {
        event.occurred_at_utc: event
        for event in snapshot.event_evidence
        if event.event_type == "PAPER_CYCLE"
    }
    for target, attempt in attempts.items():
        if target not in completed:
            continue
        terminal = cycle_events.get(target)
        if terminal is None or attempt.sequence >= terminal.sequence:
            raise StoreCorruptionError("cycle attempt is not prior to its terminal cycle")

    unfinished = sorted(set(attempts) - completed)
    if len(unfinished) > 1:
        raise StoreCorruptionError("cycle attempts contain multiple unfinished targets")
    if completed and unfinished:
        expected = _first_uncompleted_after_cycle_chain(completed)
        if unfinished[0] != expected:
            raise StoreCorruptionError("cycle attempt contradicts completed chronology")
    return frozenset(attempts)


def _require_attempt_target(
    requested: datetime,
    attempted: frozenset[datetime],
    completed: frozenset[datetime],
) -> None:
    unfinished = attempted - completed
    if unfinished and requested != min(unfinished):
        raise PaperServiceError("requested cycle skips the durable cycle attempt")


def _first_uncompleted_after_cycle_chain(
    completed: frozenset[datetime],
) -> datetime:
    ordered = sorted(completed)
    if not ordered:
        raise ValueError("completed cycle chain cannot be empty")
    return _safe_add(
        ordered[-1],
        _FOUR_HOURS,
        "cycle cursor is outside datetime range",
    )


def _observed_cycle_end(
    observed_at: datetime,
    latest_matured: datetime,
    label: str,
) -> datetime:
    cycle_end = _safe_add(
        _require_cycle_end(observed_at),
        _FOUR_HOURS,
        f"{label} cycle is outside datetime range",
    )
    if cycle_end > latest_matured:
        raise StoreCorruptionError(f"{label} is later than latest matured evidence")
    return cycle_end


def _future_obligation_end(
    basis: datetime,
    latest_matured: datetime,
    label: str,
) -> datetime:
    obligation = _safe_add(
        _require_cycle_end(basis),
        _FOUR_HOURS,
        f"{label} cycle is outside datetime range",
    )
    if obligation <= latest_matured:
        return obligation
    next_end = _safe_add(
        latest_matured,
        _FOUR_HOURS,
        "latest matured successor is outside datetime range",
    )
    if obligation != next_end:
        raise StoreCorruptionError(f"{label} skips beyond the next cycle")
    return obligation


def _open_position(
    broker: PaperBroker,
    reconciliation: PaperReconciliation,
    enriched: pd.DataFrame,
    bar_at: datetime,
    config: StrategyConfig,
) -> _OpenPosition:
    open_entry = _open_entry(reconciliation)
    order = broker.order(open_entry.order_id)
    observation = broker.position_observation(order.order_id)
    if observation is not None and observation.occurred_at_utc == bar_at:
        initial = float(observation.payload["initial_stop"])
        active = reconciliation.active_stop
        return _OpenPosition(
            order, open_entry, open_entry.fill_price, initial,
            active.stop_price if active else initial, float(observation.payload["high_water"]),
            int((bar_at - open_entry.fill_time) / _FOUR_HOURS),
        )
    initial_stop = broker.initial_stop(order.order_id)
    if initial_stop is None and order.execution_context is not None:
        atr = float(order.execution_context["entry_atr"])
        initial_stop = open_entry.fill_price - config.initial_atr_mult * atr
    elif initial_stop is None:
        signal_at = pd.Timestamp(order.signal_at_utc)
        if signal_at not in enriched.index:
            raise PaperServiceError("entry signal candle is absent from public history")
        signal_row = enriched.loc[signal_at]
        atr = _positive_finite(signal_row.get(f"atr_{config.atr_period}"), "entry ATR")
        initial_stop = open_entry.fill_price - config.initial_atr_mult * atr
    if not math.isfinite(initial_stop) or initial_stop <= 0.0:
        raise PaperServiceError("reconstructed initial stop is invalid")
    held = enriched.loc[(enriched.index >= open_entry.fill_time) & (enriched.index <= bar_at)]
    highs = pd.to_numeric(held["high"], errors="coerce")
    if held.empty or not highs.notna().all():
        raise PaperServiceError("held candle history is incomplete")
    active = reconciliation.active_stop
    return _OpenPosition(
        order, open_entry, open_entry.fill_price, initial_stop,
        active.stop_price if active else initial_stop, float(highs.max()),
        int((bar_at - open_entry.fill_time) / _FOUR_HOURS),
    )


def _open_entry(reconciliation: PaperReconciliation) -> PaperFill:
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

    return open_entry


def _validated_position_frame(frame: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    expected = pd.date_range(start, end - _FOUR_HOURS, freq="4h", tz="UTC")
    if (not isinstance(frame, pd.DataFrame) or not isinstance(frame.index, pd.DatetimeIndex)
        or not frame.index.equals(expected)):
        raise PaperServiceError("position history must cover its exact contiguous interval")
    canonical = canonicalize_ohlcv(frame, end).frame
    if not canonical.index.equals(expected) or not all(_safe_execution_row(row) for _, row in canonical.iterrows()):
        raise PaperServiceError("position history contains unsafe candle evidence")
    return canonical


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
    if (
        set(payload) != expected
        or type(payload.get("version")) is not int
        or payload["version"] not in (1, _RISK_STATE_VERSION)
    ):
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


def _health_gate_from_snapshot(snapshot: PaperSnapshot) -> _HealthGate:
    """Bind strict typed health to the immutable event that supplied it."""
    health_events = tuple(
        event
        for event in snapshot.event_evidence
        if event.event_type == "HEALTH_STATE"
    )
    gate = _health_gate_from_events(health_events)
    if gate.evidence_event_id is None:
        if snapshot.health_state:
            raise StoreCorruptionError("health projection has no event evidence")
        return gate
    if gate.action is None:
        return gate
    projected = HealthSnapshot.from_mapping(snapshot.health_state)
    latest = HealthSnapshot.from_event_mapping(health_events[-1].payload)
    if projected != latest:
        raise StoreCorruptionError("health projection contradicts event evidence")
    return gate


def _health_gate_from_events(events: Sequence[StoredEvent]) -> _HealthGate:
    latest_snapshot: HealthSnapshot | None = None
    latest_event: StoredEvent | None = None
    for event in events:
        try:
            latest_snapshot = HealthSnapshot.from_event_mapping(event.payload)
        except EmptyHealthEventError:
            raise
        except HealthStateError:
            return _HealthGate(None, event.event_id, event.sequence, 0)
        latest_event = event
    if latest_snapshot is None or latest_event is None:
        return _HealthGate(HealthMonitor().current_action(), None, 0, 1)
    return _HealthGate(
        action=HealthMonitor(latest_snapshot).current_action(),
        evidence_event_id=latest_event.event_id,
        evidence_sequence=latest_event.sequence,
        evidence_version=latest_snapshot.version,
    )


def _persist_health_risk_followup(
    store: SQLiteStore,
    *,
    bar_at: datetime,
    stored: _RiskState,
    final: RiskDecision,
    health: _HealthGate,
) -> None:
    if health.evidence_event_id is None or health.evidence_sequence <= 0:
        raise StoreCorruptionError("changed health decision lacks event evidence")
    corrected = replace(stored, decision=final)
    snapshot = store.replay_state()
    event_id = _health_risk_followup_id(bar_at, health)
    matches = tuple(
        event for event in snapshot.event_evidence if event.event_id == event_id
    )
    if matches:
        if len(matches) != 1:
            raise StoreCorruptionError("health risk followup identity is duplicated")
        existing = matches[0]
        if (
            existing.event_type != "BREAKER_STATE"
            or _risk_state_from_payload(existing.payload) != corrected
        ):
            raise StoreCorruptionError("health risk followup identity conflicts")
        return
    occurred_at = bar_at
    if snapshot.event_evidence:
        occurred_at = max(occurred_at, snapshot.event_evidence[-1].occurred_at_utc)
    store.append_event(
        event_id,
        "BREAKER_STATE",
        occurred_at,
        _risk_state_payload(corrected),
    )


def _health_risk_followup_id(bar_at: datetime, health: _HealthGate) -> str:
    if health.evidence_event_id is None or health.evidence_sequence <= 0:
        raise StoreCorruptionError("health risk followup lacks event identity")
    digest = sha256(health.evidence_event_id.encode("utf-8")).hexdigest()
    return (
        f"risk-health:{_canonical_datetime(bar_at)}:"
        f"{health.evidence_sequence}:v{health.evidence_version}:{digest}"
    )


def _validate_health_and_risk_chain(snapshot: PaperSnapshot) -> _ValidatedRiskChain:
    """Validate stored risk provenance and health projections before mutation.

    V1 omitted raw volatility and pre-transition inputs, so historical base
    decisions cannot be fully recomputed. Preserve their strict evidence checks
    without applying today's policy to immutable historical decisions.
    """
    _health_gate_from_snapshot(snapshot)
    health_events: list[StoredEvent] = []
    latest_base = _RiskState()
    projection = _RiskState()
    latest_base_event: StoredEvent | None = None
    latest_risk_event: StoredEvent | None = None
    previous_event: StoredEvent | None = None
    latest_risk_version = 1

    for event in snapshot.event_evidence:
        if event.event_type == "HEALTH_STATE":
            health_events.append(event)
        if event.event_type != "BREAKER_STATE":
            previous_event = event
            continue

        if not event.payload:
            raise StoreCorruptionError("persisted breaker event has an empty schema")
        candidate = _risk_state_from_payload(event.payload)
        version = event.payload["version"]
        if version < latest_risk_version:
            raise StoreCorruptionError("breaker policy version downgrade is invalid")
        latest_risk_version = version
        if event.event_id.startswith("risk-health:"):
            if latest_base_event is None or latest_base.last_risk_at is None:
                raise StoreCorruptionError("health risk followup lacks canonical base")
            if candidate.last_risk_at != latest_base.last_risk_at:
                raise StoreCorruptionError("health risk followup changes the risk cursor")
            gate = _health_gate_from_events(tuple(health_events))
            if (
                gate.evidence_event_id is None
                or _health_risk_followup_id(latest_base.last_risk_at, gate)
                != event.event_id
            ):
                raise StoreCorruptionError(
                    "health risk followup is not bound to the latest health event"
                )
            expected = replace(
                latest_base,
                decision=_apply_health_action(latest_base.decision, gate.action),
            )
            if candidate != expected:
                raise StoreCorruptionError("health risk followup decision is inconsistent")
            expected_occurred_at = latest_base.last_risk_at
            if previous_event is not None:
                expected_occurred_at = max(
                    expected_occurred_at,
                    previous_event.occurred_at_utc,
                )
            if event.occurred_at_utc != expected_occurred_at:
                raise StoreCorruptionError("health risk followup timestamp is inconsistent")
            projection = candidate
        else:
            if candidate.last_risk_at is None:
                raise StoreCorruptionError("canonical risk event lacks its cursor")
            expected_id = f"risk:{_canonical_datetime(candidate.last_risk_at)}"
            if event.event_id != expected_id:
                raise StoreCorruptionError("breaker event identity is not canonical")
            if event.occurred_at_utc != candidate.last_risk_at:
                raise StoreCorruptionError("risk cursor contradicts its event envelope")
            if "health_recovery_reduced" in candidate.decision.reasons:
                raise StoreCorruptionError("canonical risk event contains an unbound health marker")
            if (
                latest_base.last_risk_at is not None
                and candidate.last_risk_at <= latest_base.last_risk_at
            ):
                raise StoreCorruptionError("canonical risk cursors are not strictly increasing")
            latest_base = candidate
            projection = candidate
            latest_base_event = event

        latest_risk_event = event
        previous_event = event

    if latest_risk_event is None:
        if snapshot.breaker_state:
            raise StoreCorruptionError("breaker projection has no event evidence")
    elif (
        _risk_state_from_payload(snapshot.breaker_state) != projection
        or snapshot.breaker_state["version"] != latest_risk_version
    ):
        raise StoreCorruptionError("breaker projection contradicts event evidence")
    return _ValidatedRiskChain(
        projection=projection,
        latest_base=latest_base,
        latest_base_event=latest_base_event,
        latest_risk_event=latest_risk_event,
    )


def _apply_health_action(
    decision: RiskDecision,
    health: HealthAction | None,
) -> RiskDecision:
    if health is None or health.halt_entries:
        if decision.reasons == ("system_unhealthy",):
            return decision
        return RiskDecision(0.0, 0.0, None, ("system_unhealthy",))
    if not health.resume_reduced:
        return decision
    if (
        decision.halted_until is not None
        or decision.risk_rate <= 0.0
        or decision.exposure_cap <= 0.0
    ):
        return decision
    return RiskDecision(
        risk_rate=decision.risk_rate / 2.0,
        exposure_cap=decision.exposure_cap / 2.0,
        halted_until=decision.halted_until,
        reasons=decision.reasons + ("health_recovery_reduced",),
    )


def _loss_from_baseline(equity: float, baseline: float) -> float:
    if baseline <= 0.0:
        return 0.0
    return max(0.0, 1.0 - equity / baseline)


def _validate_operational_alert_chain(events: Sequence[StoredEvent]) -> None:
    """Accept only notifier evidence produced from an earlier durable source."""
    seen: dict[str, StoredEvent] = {}
    for event in events:
        if event.event_type not in {"ALERT_ATTEMPT", "ALERT_FAILURE"}:
            seen[event.event_id] = event
            continue

        payload = dict(event.payload)
        common_keys = {
            "source_event_id",
            "source_event_type",
            "version",
        }
        expected_keys = (
            common_keys
            if event.event_type == "ALERT_ATTEMPT"
            else common_keys | {"failure_code"}
        )
        if set(payload) != expected_keys or type(payload.get("version")) is not int:
            raise StoreCorruptionError("alert evidence payload is invalid")
        if payload["version"] != 1:
            raise StoreCorruptionError("alert evidence version is invalid")
        source_id = payload.get("source_event_id")
        source_type = payload.get("source_event_type")
        if (
            not isinstance(source_id, str)
            or not source_id
            or source_type not in _ALERT_SOURCE_TYPES
        ):
            raise StoreCorruptionError("alert evidence source identity is invalid")
        source = seen.get(source_id)
        if source is None or source.event_type != source_type:
            raise StoreCorruptionError("alert evidence has no matching prior source")

        digest = sha256(f"{source_id}|{source_type}".encode("utf-8")).hexdigest()
        attempt_id = f"alert-attempt:{digest}"
        if event.event_type == "ALERT_ATTEMPT":
            if event.event_id != attempt_id:
                raise StoreCorruptionError("alert evidence identity is invalid")
        else:
            if (
                payload.get("failure_code") != "DELIVERY_FAILED"
                or event.event_id != f"alert-failure:{digest}"
            ):
                raise StoreCorruptionError("alert evidence failure payload is invalid")
            attempt = seen.get(attempt_id)
            if attempt is None or attempt.event_type != "ALERT_ATTEMPT":
                raise StoreCorruptionError("alert evidence failure has no prior attempt")
        seen[event.event_id] = event


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


def _latest_matured_end(now: datetime) -> datetime:
    try:
        eligible = now - _MATURITY_DELAY
        return eligible.replace(
            hour=eligible.hour - eligible.hour % 4,
            minute=0,
            second=0,
            microsecond=0,
        )
    except (OverflowError, ValueError) as error:
        raise ValueError("clock is outside the supported cycle range") from error


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
