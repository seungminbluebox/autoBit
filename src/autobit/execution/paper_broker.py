"""Deterministic, restart-safe execution adapter for normalized paper trading.

The broker has deliberately no exchange or network dependency.  SQLite events
are the sole authority: every public mutation is wrapped by the store's outer
transaction and every view is rebuilt from immutable evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import math
import re

from autobit.config import CostConfig
from autobit.domain.models import OrderStatus, PositionState
from autobit.persistence.sqlite_store import (
    IdempotencyConflictError,
    PaperSnapshot,
    SQLiteStore,
    StoredEvent,
)


_MARKET = "KRW-BTC"
_ACTIVE = frozenset(
    {
        OrderStatus.CREATED,
        OrderStatus.SUBMITTED,
        OrderStatus.ACCEPTED,
        OrderStatus.PARTIAL,
    }
)
_UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_FOUR_HOURS = timedelta(hours=4)


class PaperReconciliationError(RuntimeError):
    """Raised when paper evidence cannot be reconciled without guessing."""


@dataclass(frozen=True, slots=True)
class PaperOrder:
    order_id: str
    idempotency_key: str
    side: str
    reason: str
    signal_at_utc: datetime
    requested_quantity: float
    filled_quantity: float
    remainder_quantity: float
    status: OrderStatus
    order_kind: str = "MARKET"
    parent_order_id: str | None = None
    eligible_open_utc: datetime | None = None
    fee_rate: float = 0.0
    slippage_rate: float = 0.0


@dataclass(frozen=True, slots=True)
class PaperFill:
    fill_id: str
    order_id: str
    side: str
    reason: str
    quantity: float
    reference_price: float
    fill_price: float
    fee: float
    slippage: float
    fill_time: datetime


@dataclass(frozen=True, slots=True)
class PaperStop:
    stop_id: str
    stop_price: float
    reason: str
    observed_at_utc: datetime
    active_after_utc: datetime
    source_id: str
    fee_rate: float = 0.0
    slippage_rate: float = 0.0


@dataclass(frozen=True, slots=True)
class PaperTrade:
    entry_time: datetime
    exit_time: datetime
    quantity: float
    entry_price: float
    exit_price: float
    gross_pnl: float
    net_pnl: float
    fees: float
    exit_reason: str


@dataclass(frozen=True, slots=True)
class PaperReconciliation:
    cash: float
    btc_quantity: float
    equity: float
    position_state: PositionState
    active_orders: tuple[PaperOrder, ...]
    fills: tuple[PaperFill, ...]
    completed_trades: tuple[PaperTrade, ...]
    total_fees: float
    total_slippage: float
    active_stop: PaperStop | None


@dataclass(frozen=True, slots=True)
class _Ledger:
    snapshot: PaperSnapshot
    orders: Mapping[str, PaperOrder]
    active_orders: tuple[PaperOrder, ...]
    fills: tuple[PaperFill, ...]
    trades: tuple[PaperTrade, ...]
    active_stop: PaperStop | None
    open_stops: tuple[PaperStop, ...]
    total_fees: float
    total_slippage: float


class PaperBroker:
    """Simulate market execution against completed public candles only."""

    def __init__(
        self,
        store: SQLiteStore,
        costs: CostConfig = CostConfig(),
        *,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        fee_rate = _nonnegative_rate(costs.fee_rate)
        slippage_rate = _nonnegative_rate(costs.slippage_rate)
        snapshot = store.replay_state()
        if snapshot.market != _MARKET or snapshot.initial_equity != 100.0:
            raise ValueError("paper broker requires normalized-100 KRW-BTC state")
        self._store = store
        self._fee_rate = fee_rate
        self._slippage_rate = slippage_rate
        self._fault_hook = fault_hook

    def submit(
        self,
        signal_at: str | datetime,
        *,
        side: str,
        quantity: float,
        reason: str,
        owned_quantity: float | None = None,
        parent_order_id: str | None = None,
    ) -> PaperOrder:
        """Submit one accepted deterministic paper market order."""
        normalized_side = _side(side)
        if normalized_side == "BUY":
            if owned_quantity is not None:
                raise ValueError("owned quantity is only valid for a sell")
            return self.submit_entry(signal_at, quantity=quantity, reason=reason)
        if owned_quantity is None:
            owned_quantity = self._store.replay_state().btc_quantity
        return self.submit_exit(
            signal_at,
            quantity=quantity,
            owned_quantity=owned_quantity,
            reason=reason,
            parent_order_id=parent_order_id,
        )

    def submit_entry(
        self,
        signal_at: str | datetime,
        *,
        quantity: float,
        reason: str = "ENTRY",
    ) -> PaperOrder:
        signal_text, signal_time = _timestamp(signal_at)
        normalized_quantity = _positive(quantity, "quantity")
        normalized_reason = _reason(reason)
        key, order_id = _order_identity(
            signal_text,
            "BUY",
            normalized_reason,
            parent_order_id=None,
        )
        with self._store.transaction():
            ledger = self._build_ledger()
            existing = ledger.orders.get(order_id)
            if existing is not None:
                _assert_same_order(
                    existing,
                    key=key,
                    side="BUY",
                    reason=normalized_reason,
                    signal_at=signal_time,
                    quantity=normalized_quantity,
                    parent_order_id=None,
                )
                return existing
            if ledger.snapshot.btc_quantity > 0.0 or ledger.snapshot.pending_orders:
                raise ValueError("entry requires a flat account with no active order")
            return self._submit_order_in_transaction(
                signal_text=signal_text,
                signal_time=signal_time,
                occurred_at=signal_text,
                side="BUY",
                quantity=normalized_quantity,
                reason=normalized_reason,
                parent_order_id=None,
                key=key,
                order_id=order_id,
                order_kind="MARKET",
                eligible_open=_next_boundary(signal_time),
            )

    def submit_exit(
        self,
        signal_at: str | datetime,
        *,
        quantity: float,
        owned_quantity: float,
        reason: str,
        parent_order_id: str | None = None,
    ) -> PaperOrder:
        signal_text, signal_time = _timestamp(signal_at)
        normalized_quantity = _positive(quantity, "quantity")
        normalized_owned = _nonnegative(owned_quantity, "owned quantity")
        normalized_reason = _reason(reason)
        key, order_id = _order_identity(
            signal_text,
            "SELL",
            normalized_reason,
            parent_order_id=parent_order_id,
        )
        with self._store.transaction():
            ledger = self._build_ledger()
            existing = ledger.orders.get(order_id)
            if existing is not None:
                _assert_same_order(
                    existing,
                    key=key,
                    side="SELL",
                    reason=normalized_reason,
                    signal_at=signal_time,
                    quantity=normalized_quantity,
                    parent_order_id=parent_order_id,
                )
                return existing
            if parent_order_id is not None:
                raise ValueError("remainder child orders are broker-managed")
            actual_owned = ledger.snapshot.btc_quantity
            if not _exact(normalized_owned, actual_owned):
                raise ValueError("owned quantity does not match paper ledger")
            if _decimal(normalized_quantity) > _decimal(normalized_owned):
                raise ValueError("sell quantity exceeds position")
            if ledger.snapshot.pending_orders:
                raise ValueError("an active exit already holds reserved position")
            return self._submit_order_in_transaction(
                signal_text=signal_text,
                signal_time=signal_time,
                occurred_at=signal_text,
                side="SELL",
                quantity=normalized_quantity,
                reason=normalized_reason,
                parent_order_id=parent_order_id,
                key=key,
                order_id=order_id,
                order_kind="MARKET",
                eligible_open=_next_boundary(signal_time),
            )

    def process_open(
        self,
        order_id: str,
        candle_at: str | datetime,
        *,
        open_price: float,
        actual_quantity: float | None = None,
    ) -> PaperFill | None:
        """Fill one accepted market order at a strictly later candle open."""
        normalized_id = _text(order_id, "order id")
        candle_text, candle_time = _timestamp(candle_at)
        reference = _positive(open_price, "open price")
        if actual_quantity is not None:
            normalized_actual = _positive(actual_quantity, "actual quantity")
        else:
            normalized_actual = None

        with self._store.transaction():
            ledger = self._build_ledger()
            order = ledger.orders.get(normalized_id)
            if order is None:
                raise ValueError(f"unknown order id: {normalized_id}")
            if order.status not in _ACTIVE:
                return None
            if candle_time <= order.signal_at_utc:
                raise ValueError("market fill candle must be strictly later than signal")
            if order.order_kind != "MARKET":
                raise ValueError("process_open requires a market order")
            if order.eligible_open_utc is None or candle_time != order.eligible_open_utc:
                raise ValueError("market order is eligible only at the immediately following open")
            if (
                order.parent_order_id is not None
                and order.parent_order_id.startswith("paper-order:")
                and normalized_actual is not None
                and not _exact(normalized_actual, order.remainder_quantity)
            ):
                raise ValueError("remainder child must fill completely")
            return self._process_order_in_transaction(
                order,
                candle_text=candle_text,
                candle_time=candle_time,
                reference_price=reference,
                actual_quantity=normalized_actual,
                protective_stops=ledger.open_stops,
            )

    def process_intrabar_stop(
        self,
        candle_at: str | datetime,
        *,
        open_price: float,
        low_price: float,
    ) -> PaperFill | None:
        """Trigger only a stop that was active before this candle began."""
        candle_text, candle_time = _timestamp(candle_at)
        _require_boundary(candle_time, "stop candle")
        open_value = _positive(open_price, "open price")
        low_value = _positive(low_price, "low price")
        if low_value > open_value:
            raise ValueError("low price cannot exceed open price")

        with self._store.transaction():
            ledger = self._build_ledger()
            eligible_stops = tuple(
                stop
                for stop in ledger.open_stops
                if stop.active_after_utc <= candle_time
            )
            if not eligible_stops:
                return None
            stop = eligible_stops[-1]
            if open_value <= stop.stop_price:
                reference = open_value
            elif low_value <= stop.stop_price:
                reference = stop.stop_price
            else:
                return None
            if ledger.snapshot.btc_quantity <= 0.0:
                raise PaperReconciliationError("active stop has no owned BTC")
            if ledger.snapshot.pending_orders:
                raise PaperReconciliationError("stop trigger conflicts with an active order")

            key, order_id = _order_identity(
                _canonical_datetime(stop.observed_at_utc),
                "SELL",
                stop.reason,
                parent_order_id=stop.stop_id,
            )
            order = self._submit_order_in_transaction(
                signal_text=_canonical_datetime(stop.observed_at_utc),
                signal_time=stop.observed_at_utc,
                occurred_at=candle_text,
                side="SELL",
                quantity=ledger.snapshot.btc_quantity,
                reason=stop.reason,
                parent_order_id=stop.stop_id,
                key=key,
                order_id=order_id,
                order_kind="STOP",
                eligible_open=candle_time,
                fee_rate=stop.fee_rate,
                slippage_rate=stop.slippage_rate,
            )
            fill = self._process_order_in_transaction(
                order,
                candle_text=candle_text,
                candle_time=candle_time,
                reference_price=reference,
                actual_quantity=None,
                protective_stops=(),
            )
            for open_stop in ledger.open_stops:
                self._deactivate_stop_in_transaction(
                    open_stop,
                    candle_text,
                    action=("TRIGGERED" if open_stop.stop_id == stop.stop_id else "CANCELED"),
                    identity=order_id,
                )
            return fill

    def set_stop(
        self,
        observed_at: str | datetime,
        stop_price: float,
        *,
        active_after: str | datetime | None = None,
        reason: str = "HARD_STOP",
        source_id: str | None = None,
    ) -> PaperStop:
        """Persist a stop whose low-price trigger is effective after its source candle."""
        observed_text, observed_time = _timestamp(observed_at)
        _require_boundary(observed_time, "stop observation")
        required_active = _next_boundary(observed_time)
        if active_after is None:
            active_time = required_active
            active_text = _canonical_datetime(required_active)
        else:
            active_text, active_time = _timestamp(active_after)
            if active_time != required_active:
                raise ValueError("stop active time must be the next four-hour boundary")
        price = _positive(stop_price, "stop price")
        normalized_reason = _reason(reason)

        with self._store.transaction():
            ledger = self._build_ledger()
            normalized_source = (
                _text(source_id, "stop source id")
                if source_id is not None
                else _open_entry_identity(ledger)
            )
            if normalized_source != _open_entry_identity(ledger):
                raise ValueError("stop source must be the currently open paper entry")
            identity_material = "|".join(
                (_MARKET, normalized_source, observed_text, normalized_reason)
            )
            stop_id = f"paper-stop:{sha256(identity_material.encode('utf-8')).hexdigest()}"
            event_id = f"paper-stop-set:{sha256(stop_id.encode('utf-8')).hexdigest()}"
            candidate = PaperStop(
                stop_id,
                price,
                normalized_reason,
                observed_time,
                active_time,
                normalized_source,
                self._fee_rate,
                self._slippage_rate,
            )
            matching = next(
                (event for event in ledger.snapshot.event_evidence if event.event_id == event_id),
                None,
            )
            if matching is not None:
                persisted = _stop_from_event(matching)
                if not (
                    persisted.stop_id == candidate.stop_id
                    and _exact(persisted.stop_price, candidate.stop_price)
                    and persisted.reason == candidate.reason
                    and persisted.observed_at_utc == candidate.observed_at_utc
                    and persisted.active_after_utc == candidate.active_after_utc
                    and persisted.source_id == candidate.source_id
                ):
                    raise IdempotencyConflictError("stop identity has conflicting evidence")
                return persisted
            if ledger.snapshot.btc_quantity <= 0.0:
                raise ValueError("stop requires owned BTC")
            if ledger.active_stop is not None:
                if _decimal(price) < _decimal(ledger.active_stop.stop_price):
                    raise ValueError("protective stop cannot loosen")
                if _exact(price, ledger.active_stop.stop_price):
                    return ledger.active_stop
            self._store.append_event(
                event_id,
                "PAPER_STOP",
                observed_text,
                {
                    "action": "SET",
                    "active_after_utc": active_text,
                    "fee_rate": self._fee_rate,
                    "reason": normalized_reason,
                    "source_id": normalized_source,
                    "stop_id": stop_id,
                    "stop_price": price,
                    "slippage_rate": self._slippage_rate,
                },
            )
            return candidate

    def active_stop(self) -> PaperStop | None:
        return self._build_ledger().active_stop

    def order(self, order_id: str) -> PaperOrder:
        normalized_id = _text(order_id, "order id")
        order = self._build_ledger().orders.get(normalized_id)
        if order is None:
            raise ValueError(f"unknown order id: {normalized_id}")
        return order

    def reconcile(self) -> PaperReconciliation:
        """Verify immutable evidence and return a read-only paper account view."""
        ledger = self._build_ledger()
        active_orders = tuple(
            sorted(
                (order for order in ledger.orders.values() if order.status in _ACTIVE),
                key=lambda item: (item.signal_at_utc, item.order_id),
            )
        )
        return PaperReconciliation(
            cash=ledger.snapshot.cash,
            btc_quantity=ledger.snapshot.btc_quantity,
            equity=ledger.snapshot.equity,
            position_state=ledger.snapshot.position_state,
            active_orders=active_orders,
            fills=ledger.fills,
            completed_trades=ledger.trades,
            total_fees=ledger.total_fees,
            total_slippage=ledger.total_slippage,
            active_stop=ledger.active_stop,
        )

    def _submit_order_in_transaction(
        self,
        *,
        signal_text: str,
        signal_time: datetime,
        occurred_at: str,
        side: str,
        quantity: float,
        reason: str,
        parent_order_id: str | None,
        key: str,
        order_id: str,
        order_kind: str,
        eligible_open: datetime,
        fee_rate: float | None = None,
        slippage_rate: float | None = None,
    ) -> PaperOrder:
        bound_fee_rate = self._fee_rate if fee_rate is None else fee_rate
        bound_slippage_rate = (
            self._slippage_rate if slippage_rate is None else slippage_rate
        )
        created = self._store.record_order_once(
            key,
            side,
            quantity,
            order_id=order_id,
            occurred_at=occurred_at,
            status=OrderStatus.CREATED,
        )
        if not created:
            existing = self._build_ledger().orders.get(order_id)
            if existing is None:
                raise PaperReconciliationError("order retry lacks broker metadata")
            return existing
        self._fault("created")
        self._store.transition_order_status(
            order_id,
            f"{key}:submitted",
            OrderStatus.SUBMITTED,
            occurred_at,
        )
        self._fault("submitted")
        self._store.transition_order_status(
            order_id,
            f"{key}:accepted",
            OrderStatus.ACCEPTED,
            occurred_at,
        )
        self._fault("accepted")
        self._store.append_event(
            f"paper-order-meta:{sha256(order_id.encode('utf-8')).hexdigest()}",
            "PAPER_ORDER",
            occurred_at,
            {
                "idempotency_key": key,
                "eligible_open_utc": _canonical_datetime(eligible_open),
                "fee_rate": bound_fee_rate,
                "order_id": order_id,
                "order_kind": order_kind,
                "parent_order_id": parent_order_id,
                "reason": reason,
                "requested_quantity": quantity,
                "side": side,
                "signal_at_utc": signal_text,
                "slippage_rate": bound_slippage_rate,
            },
        )
        return PaperOrder(
            order_id=order_id,
            idempotency_key=key,
            side=side,
            reason=reason,
            signal_at_utc=signal_time,
            requested_quantity=quantity,
            filled_quantity=0.0,
            remainder_quantity=quantity,
            status=OrderStatus.ACCEPTED,
            order_kind=order_kind,
            parent_order_id=parent_order_id,
            eligible_open_utc=eligible_open,
            fee_rate=bound_fee_rate,
            slippage_rate=bound_slippage_rate,
        )

    def _process_order_in_transaction(
        self,
        order: PaperOrder,
        *,
        candle_text: str,
        candle_time: datetime,
        reference_price: float,
        actual_quantity: float | None,
        protective_stops: tuple[PaperStop, ...],
    ) -> PaperFill | None:
        remainder = order.remainder_quantity
        quantity = remainder if actual_quantity is None else actual_quantity
        quantity_decimal = Decimal(str(quantity))
        remainder_decimal = Decimal(str(remainder))
        if quantity_decimal > remainder_decimal:
            raise ValueError("actual quantity exceeds order remainder")
        fill_price = _computed_fill_price(
            reference_price,
            order.slippage_rate,
            order.side,
        )
        if not math.isfinite(fill_price) or fill_price <= 0.0:
            raise ValueError("slippage produces a non-positive fill price")
        fee = _computed_fee(quantity, fill_price, order.fee_rate)
        current = self._store.replay_state()
        cash_required = _decimal(quantity) * _decimal(fill_price) + _decimal(fee)
        if order.side == "BUY" and cash_required > _decimal(current.cash):
            self._store.transition_order_status(
                order.order_id,
                f"{order.idempotency_key}:insufficient:{candle_text}",
                OrderStatus.INSUFFICIENT_CASH,
                candle_text,
                reason="INSUFFICIENT_CASH",
            )
            return None
        if order.side == "SELL" and quantity_decimal > _decimal(current.btc_quantity):
            raise ValueError("sell fill exceeds owned BTC")

        fill_identity = "|".join((order.order_id, candle_text, repr(quantity)))
        fill_id = f"paper-fill:{sha256(fill_identity.encode('utf-8')).hexdigest()}"
        appended = self._store.append_fill(
            order.order_id,
            order.side,
            quantity,
            fill_price,
            fee,
            candle_text,
            fill_id=fill_id,
        )
        if not appended:
            return None
        next_status = (
            OrderStatus.COMPLETED
            if quantity_decimal == remainder_decimal
            else OrderStatus.PARTIAL
        )
        self._store.transition_order_status(
            order.order_id,
            f"{order.idempotency_key}:{next_status.value.lower()}:{candle_text}",
            next_status,
            candle_text,
            reason=order.reason,
        )
        slippage = _computed_slippage(quantity, fill_price, reference_price)
        self._store.append_event(
            f"paper-fill-meta:{sha256(fill_id.encode('utf-8')).hexdigest()}",
            "PAPER_FILL",
            candle_text,
            {
                "fee": fee,
                "fee_rate": order.fee_rate,
                "fill_id": fill_id,
                "fill_price": fill_price,
                "order_id": order.order_id,
                "quantity": quantity,
                "reason": order.reason,
                "reference_price": reference_price,
                "side": order.side,
                "slippage": slippage,
                "slippage_rate": order.slippage_rate,
            },
        )
        fill = PaperFill(
            fill_id=fill_id,
            order_id=order.order_id,
            side=order.side,
            reason=order.reason,
            quantity=quantity,
            reference_price=reference_price,
            fill_price=fill_price,
            fee=fee,
            slippage=slippage,
            fill_time=candle_time,
        )

        if next_status is OrderStatus.PARTIAL:
            self._store.transition_order_status(
                order.order_id,
                f"{order.idempotency_key}:cancel-remainder:{candle_text}",
                OrderStatus.CANCELED,
                candle_text,
                reason="PARTIAL_REMAINDER",
            )
            if order.side == "SELL":
                if order.parent_order_id is not None and order.parent_order_id.startswith(
                    "paper-order:"
                ):
                    raise PaperReconciliationError(
                        "remainder child cannot create another remainder child"
                    )
                remaining_owned_decimal = remainder_decimal - quantity_decimal
                if remaining_owned_decimal <= 0:
                    raise PaperReconciliationError("partial sell left no remainder")
                remaining_owned = float(remaining_owned_decimal)
                child_key, child_id = _order_identity(
                    candle_text,
                    "SELL",
                    order.reason,
                    parent_order_id=order.order_id,
                )
                self._submit_order_in_transaction(
                    signal_text=candle_text,
                    signal_time=candle_time,
                    occurred_at=candle_text,
                    side="SELL",
                    quantity=remaining_owned,
                    reason=order.reason,
                    parent_order_id=order.order_id,
                    key=child_key,
                    order_id=child_id,
                    order_kind="MARKET",
                    eligible_open=_next_boundary(candle_time),
                    fee_rate=order.fee_rate,
                    slippage_rate=order.slippage_rate,
                )
        if (
            order.side == "SELL"
            and order.order_kind == "MARKET"
            and _decimal(self._store.replay_state().btc_quantity) == 0
        ):
            for stop in protective_stops:
                self._deactivate_stop_in_transaction(
                    stop,
                    candle_text,
                    action="CANCELED",
                    identity=order.order_id,
                )
        self._fault("fill")
        return fill

    def _deactivate_stop_in_transaction(
        self,
        stop: PaperStop,
        occurred_at: str,
        *,
        action: str,
        identity: str,
    ) -> None:
        event_id = (
            f"paper-stop-{action.lower()}:"
            f"{sha256((stop.stop_id + '|' + identity).encode('utf-8')).hexdigest()}"
        )
        self._store.append_event(
            event_id,
            "PAPER_STOP",
            occurred_at,
            {
                "action": action,
                "identity": identity,
                "stop_id": stop.stop_id,
            },
        )

    def _fault(self, boundary: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(boundary)

    def _build_ledger(self) -> _Ledger:
        snapshot = self._store.replay_state()
        created: dict[str, StoredEvent] = {}
        status_events: dict[str, list[StoredEvent]] = {}
        fills_by_order: dict[str, list[StoredEvent]] = {}
        raw_fills: dict[str, StoredEvent] = {}
        order_metadata: dict[str, StoredEvent] = {}
        fill_metadata: dict[str, StoredEvent] = {}
        active_stop: PaperStop | None = None
        known_stops: dict[str, PaperStop] = {}
        open_stops: dict[str, PaperStop] = {}
        stop_set_events: dict[str, StoredEvent] = {}
        stop_terminals: list[StoredEvent] = []

        for event in snapshot.event_evidence:
            payload = event.payload
            if event.event_type == "ORDER_CREATED":
                order_id = _payload_text(payload, "order_id")
                if order_id in created:
                    raise PaperReconciliationError("duplicate order creation evidence")
                created[order_id] = event
            elif event.event_type == "ORDER_STATUS":
                order_id = _payload_text(payload, "order_id")
                if order_id not in created:
                    raise PaperReconciliationError("order status lacks creation evidence")
                status_events.setdefault(order_id, []).append(event)
            elif event.event_type == "FILL":
                if event.event_id in raw_fills:
                    raise PaperReconciliationError("duplicate fill evidence")
                raw_fills[event.event_id] = event
                order_id = _payload_text(payload, "order_id")
                fills_by_order.setdefault(order_id, []).append(event)
            elif event.event_type == "PAPER_ORDER":
                order_id = _payload_text(payload, "order_id")
                if order_id in order_metadata:
                    raise PaperReconciliationError("duplicate paper order metadata")
                order_metadata[order_id] = event
            elif event.event_type == "PAPER_FILL":
                fill_id = _payload_text(payload, "fill_id")
                if fill_id in fill_metadata:
                    raise PaperReconciliationError("duplicate paper fill metadata")
                fill_metadata[fill_id] = event
            elif event.event_type == "PAPER_STOP":
                action = _payload_text(payload, "action")
                if action == "SET":
                    candidate = _stop_from_event(event)
                    if candidate.stop_id in known_stops:
                        raise PaperReconciliationError("duplicate paper stop identity")
                    if active_stop is not None:
                        if candidate.source_id != active_stop.source_id:
                            raise PaperReconciliationError("stop update changes position source")
                        if _decimal(candidate.stop_price) <= _decimal(active_stop.stop_price):
                            raise PaperReconciliationError(
                                "persisted protective stop is not tighter"
                            )
                    known_stops[candidate.stop_id] = candidate
                    open_stops[candidate.stop_id] = candidate
                    stop_set_events[candidate.stop_id] = event
                    active_stop = candidate
                elif action in {"CANCELED", "TRIGGERED"}:
                    stop_id = _payload_text(payload, "stop_id")
                    terminal_stop = open_stops.get(stop_id)
                    if terminal_stop is None:
                        raise PaperReconciliationError("stop terminal event has no active stop")
                    _validate_stop_terminal(event, terminal_stop)
                    stop_terminals.append(event)
                    del open_stops[stop_id]
                    active_stop = next(reversed(open_stops.values()), None)
                else:
                    raise PaperReconciliationError("unknown paper stop action")

        orders: dict[str, PaperOrder] = {}
        for order_id, metadata in order_metadata.items():
            creation = created.get(order_id)
            if creation is None:
                raise PaperReconciliationError("paper order metadata lacks creation evidence")
            orders[order_id] = _paper_order_from_evidence(
                order_id=order_id,
                creation=creation,
                status_events=tuple(status_events.get(order_id, ())),
                raw_fill_events=tuple(fills_by_order.get(order_id, ())),
                metadata_event=metadata,
                fill_metadata=fill_metadata,
            )

        paper_fill_ids = {
            event.event_id
            for order_id in orders
            for event in fills_by_order.get(order_id, ())
        }
        if set(fill_metadata) != paper_fill_ids:
            raise PaperReconciliationError(
                "paper fill metadata does not match paper order fills"
            )

        children: dict[str, list[PaperOrder]] = {}
        for order in orders.values():
            parent_id = order.parent_order_id
            if parent_id is None:
                continue
            if parent_id.startswith("paper-order:"):
                parent = orders.get(parent_id)
                if parent is None:
                    raise PaperReconciliationError("remainder child lacks its paper parent")
                if parent.parent_order_id is not None and parent.parent_order_id.startswith(
                    "paper-order:"
                ):
                    raise PaperReconciliationError("remainder order has recursive ancestry")
                if parent.side != "SELL" or order.side != "SELL":
                    raise PaperReconciliationError("remainder ancestry must be SELL orders")
                if parent.status is not OrderStatus.CANCELED or parent.filled_quantity <= 0:
                    raise PaperReconciliationError(
                        "remainder parent is not a partial canceled exit"
                    )
                if order.reason != parent.reason:
                    raise PaperReconciliationError("remainder child changes exit reason")
                parent_fills = fills_by_order.get(parent_id, ())
                if len(parent_fills) != 1:
                    raise PaperReconciliationError("remainder parent fill is ambiguous")
                if order.signal_at_utc != parent_fills[0].occurred_at_utc:
                    raise PaperReconciliationError(
                        "remainder child time is not parent fill time"
                    )
                if not _exact(order.requested_quantity, parent.remainder_quantity):
                    raise PaperReconciliationError("remainder child quantity is not exact")
                if not (
                    _exact(order.fee_rate, parent.fee_rate)
                    and _exact(order.slippage_rate, parent.slippage_rate)
                ):
                    raise PaperReconciliationError("remainder child changes cost binding")
                children.setdefault(parent_id, []).append(order)
            elif parent_id.startswith("paper-stop:"):
                stop = known_stops.get(parent_id)
                if order.order_kind != "STOP" or stop is None:
                    raise PaperReconciliationError("stop order ancestry is invalid")
                if not (
                    order.reason == stop.reason
                    and order.signal_at_utc == stop.observed_at_utc
                    and order.eligible_open_utc is not None
                    and order.eligible_open_utc >= stop.active_after_utc
                    and _exact(order.fee_rate, stop.fee_rate)
                    and _exact(order.slippage_rate, stop.slippage_rate)
                ):
                    raise PaperReconciliationError(
                        "stop order changes persisted stop binding"
                    )
                stop_fills = fills_by_order.get(order.order_id, ())
                if order.status is not OrderStatus.COMPLETED or len(stop_fills) != 1:
                    raise PaperReconciliationError("stop exit must complete in one fill")
                stop_fill = stop_fills[0]
                inventory_before = _inventory_before_sequence(
                    stop_fill.sequence,
                    snapshot.event_evidence,
                )
                fill_quantity = _decimal(
                    _payload_number(stop_fill.payload, "quantity")
                )
                if (
                    _decimal(order.requested_quantity) != inventory_before
                    or fill_quantity != inventory_before
                    or inventory_before - fill_quantity != 0
                ):
                    raise PaperReconciliationError(
                        "stop exit does not cover the exact full inventory"
                    )
                stop_fill_meta = fill_metadata.get(stop_fill.event_id)
                if stop_fill_meta is None or _decimal(
                    _payload_number(stop_fill_meta.payload, "reference_price")
                ) > _decimal(stop.stop_price):
                    raise PaperReconciliationError(
                        "stop exit reference is inconsistent with the stop price"
                    )
            else:
                raise PaperReconciliationError("paper order has unknown parent identity")
        if any(len(items) != 1 for items in children.values()):
            raise PaperReconciliationError("partial exit has duplicate remainder children")
        for order in orders.values():
            if (
                order.side == "SELL"
                and order.status is OrderStatus.CANCELED
                and order.filled_quantity > 0
                and order.remainder_quantity > 0
                and order.order_id not in children
            ):
                raise PaperReconciliationError("partial exit lacks its exact remainder child")

        paper_fills: list[PaperFill] = []
        cash = Decimal("100")
        btc = Decimal("0")
        total_fees = Decimal("0")
        total_slippage = Decimal("0")
        for fill_id, raw in raw_fills.items():
            payload = raw.payload
            quantity = _decimal(_payload_number(payload, "quantity"))
            fill_price = _decimal(_payload_number(payload, "price"))
            fee = _decimal(_payload_number(payload, "fee"))
            side = _payload_text(payload, "side")
            if side == "BUY":
                cash -= quantity * fill_price + fee
                btc += quantity
            elif side == "SELL":
                cash += quantity * fill_price - fee
                btc -= quantity
            else:
                raise PaperReconciliationError("invalid fill side")
            if cash < 0 or btc < 0:
                raise PaperReconciliationError("paper ledger has negative balance")
            if fill_id in paper_fill_ids:
                order = orders[_payload_text(payload, "order_id")]
                fill = _paper_fill_from_evidence(
                    raw,
                    fill_metadata[fill_id],
                    order,
                )
                total_fees += _decimal(fill.fee)
                total_slippage += _decimal(fill.slippage)
                paper_fills.append(fill)

        if float(cash) != snapshot.cash:
            raise PaperReconciliationError("cash does not reconcile to immutable fills")
        if float(btc) != snapshot.btc_quantity:
            raise PaperReconciliationError("BTC does not reconcile to immutable fills")

        active_orders = tuple(order for order in orders.values() if order.status in _ACTIVE)
        projected_active = {order.order_id: order for order in snapshot.pending_orders}
        paper_projected = {order_id for order_id in projected_active if order_id in orders}
        if paper_projected != {order.order_id for order in active_orders}:
            raise PaperReconciliationError(
                "active paper order projection contradicts broker evidence"
            )
        for order in active_orders:
            projected = projected_active[order.order_id]
            if not (
                projected.idempotency_key == order.idempotency_key
                and projected.side == order.side
                and projected.status is order.status
                and _exact(projected.requested_quantity, order.requested_quantity)
                and _exact(projected.filled_quantity, order.filled_quantity)
            ):
                raise PaperReconciliationError(
                    "active paper order projection contradicts broker evidence"
                )
        if len(active_orders) > 1:
            raise PaperReconciliationError("more than one active paper order")
        reserved = sum(
            (_decimal(order.remainder_quantity) for order in active_orders if order.side == "SELL"),
            Decimal("0"),
        )
        if reserved > btc:
            raise PaperReconciliationError("active exits reserve more BTC than owned")
        if any(order.side == "BUY" for order in active_orders) and btc > 0:
            raise PaperReconciliationError("active entry would pyramid an existing position")
        if active_stop is not None and btc <= 0:
            raise PaperReconciliationError("active stop has no owned BTC")

        paper_fills.sort(key=lambda item: (item.fill_time, item.fill_id))
        trades = _completed_trades(tuple(paper_fills))
        if active_orders:
            derived_position = (
                PositionState.ENTRY_PENDING
                if active_orders[0].side == "BUY"
                else PositionState.EXIT_PENDING
            )
        elif btc > 0:
            derived_position = PositionState.LONG
        else:
            derived_position = PositionState.FLAT
        generic_evidence = bool(set(created) - set(orders)) or any(
            _payload_text(event.payload, "order_id") not in orders
            for event in raw_fills.values()
        )
        if (
            not generic_evidence
            and snapshot.position_state is not PositionState.HALTED
            and snapshot.position_state is not derived_position
        ):
            raise PaperReconciliationError("position state contradicts paper evidence")
        for stop in known_stops.values():
            source = orders.get(stop.source_id)
            if source is None or source.side != "BUY" or source.filled_quantity <= 0:
                raise PaperReconciliationError("paper stop source is not a filled entry")
            if _paper_source_at_sequence(
                stop_set_events[stop.stop_id].sequence,
                snapshot.event_evidence,
                orders,
            ) != stop.source_id:
                raise PaperReconciliationError("paper stop source was not open when observed")
        triggered_orders: set[str] = set()
        for event in stop_terminals:
            identity = _payload_text(event.payload, "identity")
            related = orders.get(identity)
            if related is None or related.side != "SELL":
                raise PaperReconciliationError("paper stop terminal lacks its exit order")
            related_fills = fills_by_order.get(identity, ())
            if related.status is not OrderStatus.COMPLETED or len(related_fills) != 1:
                raise PaperReconciliationError(
                    "paper stop terminal exit lifecycle is incomplete"
                )
            related_fill = related_fills[0]
            related_fill_meta = fill_metadata.get(related_fill.event_id)
            if related_fill_meta is None or not (
                order_metadata[identity].sequence
                < related_fill.sequence
                < related_fill_meta.sequence
                < event.sequence
            ):
                raise PaperReconciliationError(
                    "paper stop terminal sequence is not causal"
                )
            if event.occurred_at_utc != related_fill.occurred_at_utc:
                raise PaperReconciliationError("paper stop terminal time is not causal")
            if _inventory_before_sequence(
                event.sequence,
                snapshot.event_evidence,
            ) != 0:
                raise PaperReconciliationError(
                    "paper stop terminal did not leave inventory exactly flat"
                )
            action = _payload_text(event.payload, "action")
            stop_id = _payload_text(event.payload, "stop_id")
            if action == "TRIGGERED":
                if related.parent_order_id != stop_id:
                    raise PaperReconciliationError(
                        "triggered stop exit ancestry is invalid"
                    )
                earlier_terminal_ids = {
                    _payload_text(terminal.payload, "stop_id")
                    for terminal in stop_terminals
                    if terminal.sequence < related_fill.sequence
                }
                eligible_versions = tuple(
                    candidate.stop_id
                    for candidate in known_stops.values()
                    if stop_set_events[candidate.stop_id].sequence
                    < related_fill.sequence
                    and candidate.stop_id not in earlier_terminal_ids
                    and candidate.active_after_utc <= related_fill.occurred_at_utc
                )
                if not eligible_versions or eligible_versions[-1] != stop_id:
                    raise PaperReconciliationError(
                        "triggered stop is not the latest eligible open version"
                    )
                triggered_orders.add(identity)
            if action == "CANCELED" and related.parent_order_id == stop_id:
                raise PaperReconciliationError("canceled stop is bound to a trigger order")
        stop_order_ids = {
            order.order_id for order in orders.values() if order.order_kind == "STOP"
        }
        if triggered_orders != stop_order_ids:
            raise PaperReconciliationError(
                "stop exit lifecycle lacks exactly one triggered terminal"
            )

        return _Ledger(
            snapshot=snapshot,
            orders=orders,
            active_orders=tuple(
                sorted(active_orders, key=lambda item: (item.signal_at_utc, item.order_id))
            ),
            fills=tuple(paper_fills),
            trades=trades,
            active_stop=active_stop,
            open_stops=tuple(open_stops.values()),
            total_fees=float(total_fees),
            total_slippage=float(total_slippage),
        )


def _paper_order_from_evidence(
    *,
    order_id: str,
    creation: StoredEvent,
    status_events: tuple[StoredEvent, ...],
    raw_fill_events: tuple[StoredEvent, ...],
    metadata_event: StoredEvent,
    fill_metadata: Mapping[str, StoredEvent],
) -> PaperOrder:
    metadata = metadata_event.payload
    _require_evidence_keys(
        metadata,
        {
            "eligible_open_utc",
            "fee_rate",
            "idempotency_key",
            "order_id",
            "order_kind",
            "parent_order_id",
            "reason",
            "requested_quantity",
            "side",
            "signal_at_utc",
            "slippage_rate",
        },
        "paper order metadata",
    )
    if metadata_event.event_id != (
        f"paper-order-meta:{sha256(order_id.encode('utf-8')).hexdigest()}"
    ):
        raise PaperReconciliationError("paper order metadata id is not deterministic")
    if _payload_text(metadata, "order_id") != order_id:
        raise PaperReconciliationError("paper order metadata changes order identity")
    key = _payload_text(metadata, "idempotency_key")
    side = _payload_text(metadata, "side")
    if side not in {"BUY", "SELL"}:
        raise PaperReconciliationError("paper order side is invalid")
    reason = _payload_text(metadata, "reason")
    if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", reason) is None:
        raise PaperReconciliationError("paper order reason is invalid")
    requested = _payload_number(metadata, "requested_quantity")
    if requested <= 0:
        raise PaperReconciliationError("paper order quantity is invalid")
    fee_rate = _payload_number(metadata, "fee_rate")
    slippage_rate = _payload_number(metadata, "slippage_rate")
    if fee_rate < 0 or slippage_rate < 0:
        raise PaperReconciliationError("paper order cost binding is invalid")
    parent_value = metadata["parent_order_id"]
    if parent_value is not None and (not isinstance(parent_value, str) or not parent_value):
        raise PaperReconciliationError("paper order parent is invalid")
    parent = parent_value if isinstance(parent_value, str) else None
    signal_text, signal_time = _evidence_timestamp(
        _payload_text(metadata, "signal_at_utc")
    )
    eligible_text, eligible_time = _evidence_timestamp(
        _payload_text(metadata, "eligible_open_utc")
    )
    order_kind = _payload_text(metadata, "order_kind")
    if order_kind not in {"MARKET", "STOP"}:
        raise PaperReconciliationError("paper order kind is invalid")
    try:
        _require_boundary(signal_time, "paper order signal")
        _require_boundary(eligible_time, "paper order eligible open")
    except ValueError as error:
        raise PaperReconciliationError(str(error)) from error
    if order_kind == "MARKET":
        if eligible_time != _next_boundary(signal_time):
            raise PaperReconciliationError("market order eligible open is not immediate")
        if creation.occurred_at_utc != signal_time:
            raise PaperReconciliationError("market order creation time is not signal time")
    else:
        if side != "SELL" or parent is None or not parent.startswith("paper-stop:"):
            raise PaperReconciliationError("stop order identity is invalid")
        if eligible_time < _next_boundary(signal_time):
            raise PaperReconciliationError("stop order fill is not after observation")
        if creation.occurred_at_utc != eligible_time:
            raise PaperReconciliationError("stop order creation is not trigger time")
    expected_key, expected_order_id = _order_identity(
        signal_text,
        side,
        reason,
        parent_order_id=parent,
    )
    if key != expected_key or order_id != expected_order_id:
        raise PaperReconciliationError("paper order identity is not deterministic")

    creation_payload = creation.payload
    _require_evidence_keys(
        creation_payload,
        {
            "filled_quantity",
            "idempotency_key",
            "order_id",
            "requested_quantity",
            "side",
            "status",
        },
        "paper order creation",
    )
    if not (
        _payload_text(creation_payload, "idempotency_key") == key
        and _payload_text(creation_payload, "order_id") == order_id
        and _payload_text(creation_payload, "side") == side
        and _payload_text(creation_payload, "status") == OrderStatus.CREATED.value
        and _exact(_payload_number(creation_payload, "requested_quantity"), requested)
        and _exact(_payload_number(creation_payload, "filled_quantity"), 0.0)
    ):
        raise PaperReconciliationError("paper order creation contradicts metadata")
    if metadata_event.occurred_at_utc != creation.occurred_at_utc:
        raise PaperReconciliationError("paper order metadata time is not creation time")

    if len(raw_fill_events) > 1:
        raise PaperReconciliationError("paper order has more than one fill")
    raw_fill = raw_fill_events[0] if raw_fill_events else None
    if raw_fill is not None:
        if raw_fill.occurred_at_utc <= signal_time:
            raise PaperReconciliationError("paper fill is not strictly after its signal")
        if raw_fill.occurred_at_utc != eligible_time:
            raise PaperReconciliationError("paper fill is outside its eligible open")
        accumulated = _payload_number(raw_fill.payload, "quantity")
        if _decimal(accumulated) > _decimal(requested):
            raise PaperReconciliationError("paper fill exceeds requested quantity")
    else:
        accumulated = 0.0

    status_values: list[OrderStatus] = []
    for event in status_events:
        payload = event.payload
        _require_evidence_keys(
            payload,
            {"idempotency_key", "order_id", "reason", "status"},
            "paper order status",
        )
        if _payload_text(payload, "order_id") != order_id:
            raise PaperReconciliationError("paper status changes order identity")
        try:
            status_values.append(OrderStatus(_payload_text(payload, "status")))
        except ValueError as error:
            raise PaperReconciliationError("paper order status is invalid") from error
    if status_values[:2] != [OrderStatus.SUBMITTED, OrderStatus.ACCEPTED]:
        raise PaperReconciliationError(
            "paper order lifecycle must begin CREATED, SUBMITTED, ACCEPTED"
        )
    if len(status_events) < 2:
        raise PaperReconciliationError("paper order lifecycle is incomplete")
    for event, suffix in zip(status_events[:2], ("submitted", "accepted"), strict=True):
        if event.occurred_at_utc != creation.occurred_at_utc:
            raise PaperReconciliationError("paper order acceptance time is not causal")
        _validate_status_identity(event, key, f"{key}:{suffix}", reason=None)
    if not (
        creation.sequence < status_events[0].sequence < status_events[1].sequence
        < metadata_event.sequence
    ):
        raise PaperReconciliationError("paper order evidence sequence is not causal")

    if raw_fill is None:
        if len(status_values) == 2:
            final_status = OrderStatus.ACCEPTED
        elif status_values == [
            OrderStatus.SUBMITTED,
            OrderStatus.ACCEPTED,
            OrderStatus.INSUFFICIENT_CASH,
        ]:
            terminal = status_events[2]
            _validate_status_identity(
                terminal,
                key,
                f"{key}:insufficient:{eligible_text}",
                reason="INSUFFICIENT_CASH",
            )
            final_status = OrderStatus.INSUFFICIENT_CASH
        else:
            raise PaperReconciliationError("paper no-fill lifecycle is invalid")
        if len(status_events) == 3:
            if (
                status_events[2].occurred_at_utc != eligible_time
                or metadata_event.sequence >= status_events[2].sequence
            ):
                raise PaperReconciliationError("paper terminal no-fill time is not causal")
    elif _exact(accumulated, requested):
        expected = [
            OrderStatus.SUBMITTED,
            OrderStatus.ACCEPTED,
            OrderStatus.COMPLETED,
        ]
        if status_values != expected:
            raise PaperReconciliationError("completed paper fill lacks exact lifecycle")
        terminal = status_events[2]
        _validate_status_identity(
            terminal,
            key,
            f"{key}:completed:{eligible_text}",
            reason=reason,
        )
        meta_fill = fill_metadata.get(raw_fill.event_id)
        if meta_fill is None or not (
            metadata_event.sequence < raw_fill.sequence < terminal.sequence < meta_fill.sequence
        ):
            raise PaperReconciliationError("completed paper fill sequence is not causal")
        if terminal.occurred_at_utc != eligible_time:
            raise PaperReconciliationError("completed paper fill status time is invalid")
        final_status = OrderStatus.COMPLETED
    else:
        expected = [
            OrderStatus.SUBMITTED,
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIAL,
            OrderStatus.CANCELED,
        ]
        if status_values != expected:
            raise PaperReconciliationError("partial paper fill lacks exact lifecycle")
        partial, canceled = status_events[2:]
        _validate_status_identity(
            partial,
            key,
            f"{key}:partial:{eligible_text}",
            reason=reason,
        )
        _validate_status_identity(
            canceled,
            key,
            f"{key}:cancel-remainder:{eligible_text}",
            reason="PARTIAL_REMAINDER",
        )
        meta_fill = fill_metadata.get(raw_fill.event_id)
        if meta_fill is None or not (
            metadata_event.sequence < raw_fill.sequence < partial.sequence
            < meta_fill.sequence < canceled.sequence
        ):
            raise PaperReconciliationError("partial paper fill sequence is not causal")
        if partial.occurred_at_utc != eligible_time or canceled.occurred_at_utc != eligible_time:
            raise PaperReconciliationError("partial paper fill status time is invalid")
        final_status = OrderStatus.CANCELED

    remainder = _decimal(requested) - _decimal(accumulated)
    return PaperOrder(
        order_id=order_id,
        idempotency_key=key,
        side=side,
        reason=reason,
        signal_at_utc=signal_time,
        requested_quantity=requested,
        filled_quantity=accumulated,
        remainder_quantity=float(remainder),
        status=final_status,
        order_kind=order_kind,
        parent_order_id=parent,
        eligible_open_utc=eligible_time,
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
    )


def _paper_fill_from_evidence(
    raw: StoredEvent,
    metadata_event: StoredEvent,
    order: PaperOrder,
) -> PaperFill:
    metadata = metadata_event.payload
    _require_evidence_keys(
        metadata,
        {
            "fee",
            "fee_rate",
            "fill_id",
            "fill_price",
            "order_id",
            "quantity",
            "reason",
            "reference_price",
            "side",
            "slippage",
            "slippage_rate",
        },
        "paper fill metadata",
    )
    if metadata_event.event_id != (
        f"paper-fill-meta:{sha256(raw.event_id.encode('utf-8')).hexdigest()}"
    ):
        raise PaperReconciliationError("paper fill metadata id is not deterministic")
    if metadata_event.occurred_at_utc != raw.occurred_at_utc:
        raise PaperReconciliationError("paper fill metadata time is inconsistent")
    raw_payload = raw.payload
    quantity = _payload_number(raw_payload, "quantity")
    fill_price = _payload_number(raw_payload, "price")
    fee = _payload_number(raw_payload, "fee")
    side = _payload_text(raw_payload, "side")
    order_id = _payload_text(raw_payload, "order_id")
    expected_fill_id = (
        "paper-fill:"
        f"{sha256('|'.join((order_id, _canonical_datetime(raw.occurred_at_utc), repr(quantity))).encode('utf-8')).hexdigest()}"
    )
    if raw.event_id != expected_fill_id:
        raise PaperReconciliationError("paper fill id is not deterministic")
    if not (
        _payload_text(metadata, "fill_id") == raw.event_id
        and _payload_text(metadata, "order_id") == order_id == order.order_id
        and _payload_text(metadata, "side") == side == order.side
        and _payload_text(metadata, "reason") == order.reason
        and _exact(_payload_number(metadata, "quantity"), quantity)
        and _exact(_payload_number(metadata, "fill_price"), fill_price)
        and _exact(_payload_number(metadata, "fee"), fee)
        and _exact(_payload_number(metadata, "fee_rate"), order.fee_rate)
        and _exact(_payload_number(metadata, "slippage_rate"), order.slippage_rate)
    ):
        raise PaperReconciliationError("paper fill metadata contradicts order or ledger")
    reference = _payload_number(metadata, "reference_price")
    expected_price = _computed_fill_price(reference, order.slippage_rate, side)
    expected_fee = _computed_fee(quantity, expected_price, order.fee_rate)
    expected_slippage = _computed_slippage(quantity, expected_price, reference)
    if not (
        _exact(fill_price, expected_price)
        and _exact(fee, expected_fee)
        and _exact(_payload_number(metadata, "slippage"), expected_slippage)
    ):
        raise PaperReconciliationError("paper fill costs are inconsistent")
    return PaperFill(
        fill_id=raw.event_id,
        order_id=order_id,
        side=side,
        reason=order.reason,
        quantity=quantity,
        reference_price=reference,
        fill_price=fill_price,
        fee=fee,
        slippage=expected_slippage,
        fill_time=raw.occurred_at_utc,
    )


def _validate_status_identity(
    event: StoredEvent,
    order_key: str,
    expected_key: str,
    *,
    reason: str | None,
) -> None:
    payload = event.payload
    actual_key = _payload_text(payload, "idempotency_key")
    if actual_key != expected_key:
        raise PaperReconciliationError("paper status idempotency key is invalid")
    expected_event_id = f"order-status:{sha256(actual_key.encode('utf-8')).hexdigest()}"
    if event.event_id != expected_event_id or not actual_key.startswith(f"{order_key}:"):
        raise PaperReconciliationError("paper status event id is invalid")
    if payload["reason"] != reason:
        raise PaperReconciliationError("paper status reason is invalid")


def _require_evidence_keys(
    payload: Mapping[str, object],
    expected: set[str],
    label: str,
) -> None:
    if set(payload) != expected:
        raise PaperReconciliationError(f"{label} fields are invalid")


def _evidence_timestamp(value: str) -> tuple[str, datetime]:
    try:
        return _timestamp(value)
    except ValueError as error:
        raise PaperReconciliationError("paper evidence timestamp is invalid") from error


def _completed_trades(fills: tuple[PaperFill, ...]) -> tuple[PaperTrade, ...]:
    entries: list[PaperFill] = []
    exits: list[PaperFill] = []
    position = Decimal("0")
    trades: list[PaperTrade] = []
    for fill in fills:
        quantity = Decimal(str(fill.quantity))
        if fill.side == "BUY":
            if position > 0:
                raise PaperReconciliationError("paper fills contain pyramiding")
            entries.append(fill)
            position += quantity
            continue
        if position <= 0 or quantity > position:
            raise PaperReconciliationError("paper fills contain an unmatched sell")
        exits.append(fill)
        position -= quantity
        if position != 0:
            continue
        entry_quantity = sum(Decimal(str(item.quantity)) for item in entries)
        exit_quantity = sum(Decimal(str(item.quantity)) for item in exits)
        if entry_quantity != exit_quantity:
            raise PaperReconciliationError("round-trip fill quantities do not reconcile")
        entry_value = sum(
            Decimal(str(item.quantity)) * Decimal(str(item.fill_price)) for item in entries
        )
        exit_value = sum(
            Decimal(str(item.quantity)) * Decimal(str(item.fill_price)) for item in exits
        )
        fees = sum(Decimal(str(item.fee)) for item in (*entries, *exits))
        gross = exit_value - entry_value
        trades.append(
            PaperTrade(
                entry_time=entries[0].fill_time,
                exit_time=exits[-1].fill_time,
                quantity=float(entry_quantity),
                entry_price=float(entry_value / entry_quantity),
                exit_price=float(exit_value / exit_quantity),
                gross_pnl=float(gross),
                net_pnl=float(gross - fees),
                fees=float(fees),
                exit_reason=exits[-1].reason,
            )
        )
        entries.clear()
        exits.clear()
        position = Decimal("0")
    return tuple(trades)


def _stop_from_event(event: StoredEvent) -> PaperStop:
    payload = event.payload
    _require_evidence_keys(
        payload,
        {
            "action",
            "active_after_utc",
            "fee_rate",
            "reason",
            "slippage_rate",
            "source_id",
            "stop_id",
            "stop_price",
        },
        "paper stop metadata",
    )
    if _payload_text(payload, "action") != "SET":
        raise PaperReconciliationError("stop SET evidence is invalid")
    reason = _payload_text(payload, "reason")
    if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", reason) is None:
        raise PaperReconciliationError("paper stop reason is invalid")
    source_id = _payload_text(payload, "source_id")
    observed = event.occurred_at_utc
    try:
        _require_boundary(observed, "stop observation")
    except ValueError as error:
        raise PaperReconciliationError(str(error)) from error
    _, active_after = _evidence_timestamp(_payload_text(payload, "active_after_utc"))
    if active_after != _next_boundary(observed):
        raise PaperReconciliationError("stop active time is not the next four-hour boundary")
    identity_material = "|".join(
        (_MARKET, source_id, _canonical_datetime(observed), reason)
    )
    expected_stop_id = (
        f"paper-stop:{sha256(identity_material.encode('utf-8')).hexdigest()}"
    )
    stop_id = _payload_text(payload, "stop_id")
    expected_event_id = f"paper-stop-set:{sha256(stop_id.encode('utf-8')).hexdigest()}"
    if stop_id != expected_stop_id or event.event_id != expected_event_id:
        raise PaperReconciliationError("paper stop identity is not deterministic")
    stop_price = _payload_number(payload, "stop_price")
    fee_rate = _payload_number(payload, "fee_rate")
    slippage_rate = _payload_number(payload, "slippage_rate")
    if stop_price <= 0:
        raise PaperReconciliationError("paper stop price is invalid")
    if fee_rate < 0 or slippage_rate < 0:
        raise PaperReconciliationError("paper stop cost binding is invalid")
    return PaperStop(
        stop_id=stop_id,
        stop_price=stop_price,
        reason=reason,
        observed_at_utc=observed,
        active_after_utc=active_after,
        source_id=source_id,
        fee_rate=fee_rate,
        slippage_rate=slippage_rate,
    )


def _validate_stop_terminal(event: StoredEvent, stop: PaperStop) -> None:
    payload = event.payload
    _require_evidence_keys(
        payload,
        {"action", "identity", "stop_id"},
        "paper stop terminal metadata",
    )
    action = _payload_text(payload, "action")
    identity = _payload_text(payload, "identity")
    if _payload_text(payload, "stop_id") != stop.stop_id:
        raise PaperReconciliationError("paper stop terminal changes stop identity")
    expected_id = (
        f"paper-stop-{action.lower()}:"
        f"{sha256((stop.stop_id + '|' + identity).encode('utf-8')).hexdigest()}"
    )
    if event.event_id != expected_id:
        raise PaperReconciliationError("paper stop terminal identity is not deterministic")
    if event.occurred_at_utc < stop.observed_at_utc:
        raise PaperReconciliationError("paper stop terminal time is not causal")
    if action == "TRIGGERED" and event.occurred_at_utc < stop.active_after_utc:
        raise PaperReconciliationError("paper stop triggered before it became active")


def _order_identity(
    signal_at: str,
    side: str,
    reason: str,
    *,
    parent_order_id: str | None,
) -> tuple[str, str]:
    parent = parent_order_id or "ROOT"
    key = f"{_MARKET}:{signal_at}:{side}:{reason}:{parent}"
    return key, f"paper-order:{sha256(key.encode('utf-8')).hexdigest()}"


def _assert_same_order(
    order: PaperOrder,
    *,
    key: str,
    side: str,
    reason: str,
    signal_at: datetime,
    quantity: float,
    parent_order_id: str | None,
) -> None:
    if not (
        order.idempotency_key == key
        and order.side == side
        and order.reason == reason
        and order.signal_at_utc == signal_at
        and _exact(order.requested_quantity, quantity)
        and order.parent_order_id == parent_order_id
    ):
        raise IdempotencyConflictError("deterministic order identity has conflicting payload")


def _timestamp(value: str | datetime) -> tuple[str, datetime]:
    if isinstance(value, str):
        if _UTC_Z.fullmatch(value) is None:
            raise ValueError("timestamp must be canonical aware UTC with Z")
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as error:
            raise ValueError("timestamp must be canonical aware UTC with Z") from error
        if _canonical_datetime(parsed) != value:
            raise ValueError("timestamp must be canonical aware UTC with Z")
        return value, parsed
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be canonical aware UTC with Z")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("timestamp must be canonical aware UTC with Z")
    canonical = _canonical_datetime(value)
    return canonical, value.astimezone(timezone.utc)


def _canonical_datetime(value: datetime) -> str:
    text = value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    if text.endswith(".000000+00:00"):
        text = text.replace(".000000+00:00", "Z")
    else:
        text = text.replace("+00:00", "Z")
    return text


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty")
    return value


def _reason(value: object) -> str:
    reason = _text(value, "reason")
    if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", reason) is None:
        raise ValueError("reason must be an uppercase code")
    return reason


def _side(value: object) -> str:
    if value not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    return str(value)


def _positive(value: object, label: str) -> float:
    number = _number(value, label)
    if number <= 0.0:
        raise ValueError(f"{label} must be finite and positive")
    return number


def _nonnegative(value: object, label: str) -> float:
    number = _number(value, label)
    if number < 0.0:
        raise ValueError(f"{label} must be finite and non-negative")
    return number


def _number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} must be finite") from error
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _nonnegative_rate(value: object) -> float:
    try:
        return _nonnegative(value, "fee and slippage rates")
    except ValueError as error:
        raise ValueError("fee and slippage rates must be finite and non-negative") from error


def _payload_text(payload: Mapping[str, object], key: str) -> str:
    try:
        value = payload[key]
    except KeyError as error:
        raise PaperReconciliationError(f"paper evidence lacks {key}") from error
    if not isinstance(value, str) or not value:
        raise PaperReconciliationError(f"paper evidence has invalid {key}")
    return value


def _payload_number(payload: Mapping[str, object], key: str) -> float:
    try:
        value = payload[key]
    except KeyError as error:
        raise PaperReconciliationError(f"paper evidence lacks {key}") from error
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise PaperReconciliationError(f"paper evidence has invalid {key}")
    return float(value)


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _exact(left: object, right: object) -> bool:
    return _decimal(left) == _decimal(right)


def _computed_fill_price(reference: float, slippage_rate: float, side: str) -> float:
    reference_decimal = _decimal(reference)
    rate = _decimal(slippage_rate)
    multiplier = Decimal("1") + rate if side == "BUY" else Decimal("1") - rate
    return float(reference_decimal * multiplier)


def _computed_fee(quantity: float, fill_price: float, fee_rate: float) -> float:
    return float(_decimal(quantity) * _decimal(fill_price) * _decimal(fee_rate))


def _computed_slippage(quantity: float, fill_price: float, reference: float) -> float:
    return float(abs(_decimal(fill_price) - _decimal(reference)) * _decimal(quantity))


def _require_boundary(value: datetime, label: str) -> None:
    if (
        value.tzinfo is None
        or value.utcoffset() != timedelta(0)
        or value.minute != 0
        or value.second != 0
        or value.microsecond != 0
        or value.hour % 4 != 0
    ):
        raise ValueError(f"{label} must be an exact four-hour UTC boundary")


def _next_boundary(value: datetime) -> datetime:
    _require_boundary(value, "timestamp")
    return value + _FOUR_HOURS


def _open_entry_identity(ledger: _Ledger) -> str:
    position = Decimal("0")
    source: str | None = None
    for fill in ledger.fills:
        quantity = _decimal(fill.quantity)
        if fill.side == "BUY":
            if position != 0:
                raise PaperReconciliationError("paper fills contain pyramiding")
            position = quantity
            source = fill.order_id
        else:
            if quantity > position:
                raise PaperReconciliationError("paper fills contain an unmatched sell")
            position -= quantity
            if position == 0:
                source = None
    if position <= 0 or source is None:
        raise ValueError("stop requires a filled paper entry")
    return source


def _paper_source_at_sequence(
    before_sequence: int,
    evidence: tuple[StoredEvent, ...],
    orders: Mapping[str, PaperOrder],
) -> str | None:
    position = Decimal("0")
    source: str | None = None
    for event in evidence:
        if event.sequence >= before_sequence:
            break
        if event.event_type != "FILL":
            continue
        order_id = _payload_text(event.payload, "order_id")
        if order_id not in orders:
            continue
        quantity = _decimal(_payload_number(event.payload, "quantity"))
        side = _payload_text(event.payload, "side")
        if side == "BUY":
            if position != 0:
                raise PaperReconciliationError("paper fills contain pyramiding")
            position = quantity
            source = order_id
        else:
            if quantity > position:
                raise PaperReconciliationError("paper fills contain an unmatched sell")
            position -= quantity
            if position == 0:
                source = None
    return source


def _inventory_before_sequence(
    before_sequence: int,
    evidence: tuple[StoredEvent, ...],
) -> Decimal:
    inventory = Decimal("0")
    for event in evidence:
        if event.sequence >= before_sequence:
            break
        if event.event_type != "FILL":
            continue
        quantity = _decimal(_payload_number(event.payload, "quantity"))
        side = _payload_text(event.payload, "side")
        if side == "BUY":
            inventory += quantity
        elif side == "SELL":
            inventory -= quantity
        else:
            raise PaperReconciliationError("fill side is invalid")
        if inventory < 0:
            raise PaperReconciliationError("fill history has negative inventory")
    return inventory
