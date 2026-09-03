"""Deterministic, restart-safe execution adapter for normalized paper trading.

The broker has deliberately no exchange or network dependency.  SQLite events
are the sole authority: every public mutation is wrapped by the store's outer
transaction and every view is rebuilt from immutable evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
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
_TOLERANCE = Decimal("1e-10")


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
        existing = self._existing_order(order_id)
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

        with self._store.transaction():
            ledger = self._build_ledger()
            if ledger.snapshot.btc_quantity > 0.0 or ledger.active_orders:
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
        existing = self._existing_order(order_id)
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

        with self._store.transaction():
            ledger = self._build_ledger()
            actual_owned = ledger.snapshot.btc_quantity
            if not _close(normalized_owned, actual_owned):
                raise ValueError("owned quantity does not match paper ledger")
            if normalized_quantity > normalized_owned + 1e-12:
                raise ValueError("sell quantity exceeds position")
            if any(order.side == "SELL" for order in ledger.active_orders):
                raise ValueError("an active exit already holds reserved position")
            reserved = sum(
                order.remainder_quantity
                for order in ledger.active_orders
                if order.side == "SELL"
            )
            if normalized_quantity > actual_owned - reserved + 1e-12:
                raise ValueError("sell quantity exceeds unreserved position")
            order = self._submit_order_in_transaction(
                signal_text=signal_text,
                signal_time=signal_time,
                occurred_at=signal_text,
                side="SELL",
                quantity=normalized_quantity,
                reason=normalized_reason,
                parent_order_id=parent_order_id,
                key=key,
                order_id=order_id,
            )
            if ledger.active_stop is not None:
                self._deactivate_stop_in_transaction(
                    ledger.active_stop,
                    signal_text,
                    action="CANCELED",
                    identity=order_id,
                )
            return order

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
            normalized_actual = _nonnegative(actual_quantity, "actual quantity")
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
            return self._process_order_in_transaction(
                order,
                candle_text=candle_text,
                candle_time=candle_time,
                reference_price=reference,
                actual_quantity=normalized_actual,
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
        open_value = _positive(open_price, "open price")
        low_value = _positive(low_price, "low price")
        if low_value > open_value:
            raise ValueError("low price cannot exceed open price")

        with self._store.transaction():
            ledger = self._build_ledger()
            stop = ledger.active_stop
            if stop is None or candle_time <= stop.active_after_utc:
                return None
            if open_value <= stop.stop_price:
                reference = open_value
            elif low_value <= stop.stop_price:
                reference = stop.stop_price
            else:
                return None
            if ledger.snapshot.btc_quantity <= 0.0:
                raise PaperReconciliationError("active stop has no owned BTC")
            if ledger.active_orders:
                raise PaperReconciliationError("stop trigger conflicts with an active order")

            key, order_id = _order_identity(
                _canonical_datetime(stop.active_after_utc),
                "SELL",
                stop.reason,
                parent_order_id=stop.stop_id,
            )
            order = self._submit_order_in_transaction(
                signal_text=_canonical_datetime(stop.active_after_utc),
                signal_time=stop.active_after_utc,
                occurred_at=candle_text,
                side="SELL",
                quantity=ledger.snapshot.btc_quantity,
                reason=stop.reason,
                parent_order_id=stop.stop_id,
                key=key,
                order_id=order_id,
            )
            fill = self._process_order_in_transaction(
                order,
                candle_text=candle_text,
                candle_time=candle_time,
                reference_price=reference,
                actual_quantity=None,
            )
            self._deactivate_stop_in_transaction(
                stop,
                candle_text,
                action="TRIGGERED",
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
    ) -> PaperStop:
        """Persist a stop whose low-price trigger is effective after its source candle."""
        observed_text, observed_time = _timestamp(observed_at)
        if active_after is None:
            active_text, active_time = observed_text, observed_time
        else:
            active_text, active_time = _timestamp(active_after)
        if active_time > observed_time:
            raise ValueError("stop cannot become active after it was observed")
        price = _positive(stop_price, "stop price")
        normalized_reason = _reason(reason)
        identity_material = "|".join(
            (_MARKET, observed_text, active_text, repr(price), normalized_reason)
        )
        stop_id = f"paper-stop:{sha256(identity_material.encode('utf-8')).hexdigest()}"
        event_id = f"paper-stop-set:{sha256(stop_id.encode('utf-8')).hexdigest()}"
        candidate = PaperStop(stop_id, price, normalized_reason, observed_time, active_time)

        with self._store.transaction():
            ledger = self._build_ledger()
            matching = next(
                (event for event in ledger.snapshot.event_evidence if event.event_id == event_id),
                None,
            )
            if matching is not None:
                if _stop_from_event(matching) != candidate:
                    raise IdempotencyConflictError("stop identity has conflicting evidence")
                return candidate
            if ledger.snapshot.btc_quantity <= 0.0:
                raise ValueError("stop requires owned BTC")
            self._store.append_event(
                event_id,
                "PAPER_STOP",
                observed_text,
                {
                    "action": "SET",
                    "active_after_utc": active_text,
                    "reason": normalized_reason,
                    "stop_id": stop_id,
                    "stop_price": price,
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

    def _existing_order(self, order_id: str) -> PaperOrder | None:
        return self._build_ledger().orders.get(order_id)

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
    ) -> PaperOrder:
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
                "order_id": order_id,
                "order_kind": "MARKET",
                "parent_order_id": parent_order_id,
                "reason": reason,
                "requested_quantity": quantity,
                "side": side,
                "signal_at_utc": signal_text,
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
            parent_order_id=parent_order_id,
        )

    def _process_order_in_transaction(
        self,
        order: PaperOrder,
        *,
        candle_text: str,
        candle_time: datetime,
        reference_price: float,
        actual_quantity: float | None,
    ) -> PaperFill | None:
        remainder = order.remainder_quantity
        quantity = remainder if actual_quantity is None else actual_quantity
        quantity_decimal = Decimal(str(quantity))
        remainder_decimal = Decimal(str(remainder))
        if quantity_decimal > remainder_decimal:
            raise ValueError("actual quantity exceeds order remainder")
        if quantity == 0.0:
            if order.side == "BUY":
                self._store.transition_order_status(
                    order.order_id,
                    f"{order.idempotency_key}:no-fill:{candle_text}",
                    OrderStatus.CANCELED,
                    candle_text,
                    reason="NO_FILL",
                )
                return None
            return None
        multiplier = 1.0 + self._slippage_rate if order.side == "BUY" else 1.0 - self._slippage_rate
        fill_price = reference_price * multiplier
        if not math.isfinite(fill_price) or fill_price <= 0.0:
            raise ValueError("slippage produces a non-positive fill price")
        fee = quantity * fill_price * self._fee_rate
        current = self._store.replay_state()
        if order.side == "BUY" and quantity * fill_price + fee > current.cash + 1e-12:
            self._store.transition_order_status(
                order.order_id,
                f"{order.idempotency_key}:insufficient:{candle_text}",
                OrderStatus.INSUFFICIENT_CASH,
                candle_text,
                reason="INSUFFICIENT_CASH",
            )
            return None
        if order.side == "SELL" and quantity > current.btc_quantity + 1e-12:
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
        slippage = abs(fill_price - reference_price) * quantity
        self._store.append_event(
            f"paper-fill-meta:{sha256(fill_id.encode('utf-8')).hexdigest()}",
            "PAPER_FILL",
            candle_text,
            {
                "fee": fee,
                "fee_rate": self._fee_rate,
                "fill_id": fill_id,
                "fill_price": fill_price,
                "order_id": order.order_id,
                "quantity": quantity,
                "reason": order.reason,
                "reference_price": reference_price,
                "side": order.side,
                "slippage": slippage,
                "slippage_rate": self._slippage_rate,
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
                remaining_owned = self._store.replay_state().btc_quantity
                if remaining_owned <= 0.0:
                    raise PaperReconciliationError("partial sell left no remainder")
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
                "stop_id": stop.stop_id,
            },
        )

    def _fault(self, boundary: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(boundary)

    def _build_ledger(self) -> _Ledger:
        snapshot = self._store.replay_state()
        created: dict[str, dict[str, object]] = {}
        statuses: dict[str, OrderStatus] = {}
        filled: dict[str, Decimal] = {}
        order_metadata: dict[str, Mapping[str, object]] = {}
        raw_fills: dict[str, StoredEvent] = {}
        fill_metadata: dict[str, StoredEvent] = {}
        active_stop: PaperStop | None = None

        for event in snapshot.event_evidence:
            payload = event.payload
            if event.event_type == "ORDER_CREATED":
                order_id = _payload_text(payload, "order_id")
                created[order_id] = dict(payload)
                statuses[order_id] = OrderStatus(_payload_text(payload, "status"))
                filled[order_id] = Decimal("0")
            elif event.event_type == "ORDER_STATUS":
                order_id = _payload_text(payload, "order_id")
                if order_id not in created:
                    raise PaperReconciliationError("order status lacks creation evidence")
                statuses[order_id] = OrderStatus(_payload_text(payload, "status"))
            elif event.event_type == "FILL":
                raw_fills[event.event_id] = event
                order_id = _payload_text(payload, "order_id")
                if order_id in filled:
                    filled[order_id] += Decimal(str(_payload_number(payload, "quantity")))
            elif event.event_type == "PAPER_ORDER":
                order_id = _payload_text(payload, "order_id")
                if order_id in order_metadata:
                    raise PaperReconciliationError("duplicate paper order metadata")
                order_metadata[order_id] = payload
            elif event.event_type == "PAPER_FILL":
                fill_id = _payload_text(payload, "fill_id")
                if fill_id in fill_metadata:
                    raise PaperReconciliationError("duplicate paper fill evidence")
                fill_metadata[fill_id] = event
            elif event.event_type == "PAPER_STOP":
                action = _payload_text(payload, "action")
                if action == "SET":
                    active_stop = _stop_from_event(event)
                elif action in {"CANCELED", "TRIGGERED"}:
                    stop_id = _payload_text(payload, "stop_id")
                    if active_stop is None or active_stop.stop_id != stop_id:
                        raise PaperReconciliationError("stop terminal event has no active stop")
                    active_stop = None
                else:
                    raise PaperReconciliationError("unknown paper stop action")

        if set(created) != set(order_metadata):
            raise PaperReconciliationError("order evidence does not match broker metadata")
        if set(raw_fills) != set(fill_metadata):
            raise PaperReconciliationError("fill evidence does not match broker metadata")

        orders: dict[str, PaperOrder] = {}
        for order_id, creation in created.items():
            metadata = order_metadata[order_id]
            requested = _payload_number(creation, "requested_quantity")
            accumulated = float(filled[order_id])
            if accumulated > requested + 1e-12:
                raise PaperReconciliationError("filled quantity exceeds requested quantity")
            if (
                _payload_text(creation, "idempotency_key")
                != _payload_text(metadata, "idempotency_key")
                or _payload_text(creation, "side") != _payload_text(metadata, "side")
                or not _close(requested, _payload_number(metadata, "requested_quantity"))
            ):
                raise PaperReconciliationError("paper order metadata contradicts order evidence")
            _, signal_time = _timestamp(_payload_text(metadata, "signal_at_utc"))
            parent = metadata.get("parent_order_id")
            if parent is not None and not isinstance(parent, str):
                raise PaperReconciliationError("invalid parent order identity")
            orders[order_id] = PaperOrder(
                order_id=order_id,
                idempotency_key=_payload_text(metadata, "idempotency_key"),
                side=_payload_text(metadata, "side"),
                reason=_payload_text(metadata, "reason"),
                signal_at_utc=signal_time,
                requested_quantity=requested,
                filled_quantity=accumulated,
                remainder_quantity=max(0.0, requested - accumulated),
                status=statuses[order_id],
                order_kind=_payload_text(metadata, "order_kind"),
                parent_order_id=parent,
            )

        paper_fills: list[PaperFill] = []
        cash = Decimal("100")
        btc = Decimal("0")
        total_fees = Decimal("0")
        total_slippage = Decimal("0")
        for fill_id, raw in raw_fills.items():
            meta_event = fill_metadata[fill_id]
            raw_payload = raw.payload
            meta = meta_event.payload
            for key, meta_key in (
                ("order_id", "order_id"),
                ("side", "side"),
            ):
                if _payload_text(raw_payload, key) != _payload_text(meta, meta_key):
                    raise PaperReconciliationError("paper fill evidence contradicts ledger fill")
            for key, meta_key in (
                ("quantity", "quantity"),
                ("price", "fill_price"),
                ("fee", "fee"),
            ):
                if not _close(
                    _payload_number(raw_payload, key),
                    _payload_number(meta, meta_key),
                ):
                    raise PaperReconciliationError("paper fill evidence contradicts ledger fill")
            quantity = Decimal(str(_payload_number(meta, "quantity")))
            fill_price = Decimal(str(_payload_number(meta, "fill_price")))
            reference = Decimal(str(_payload_number(meta, "reference_price")))
            fee = Decimal(str(_payload_number(meta, "fee")))
            fee_rate = Decimal(str(_payload_number(meta, "fee_rate")))
            slip_rate = Decimal(str(_payload_number(meta, "slippage_rate")))
            stated_slippage = Decimal(str(_payload_number(meta, "slippage")))
            side = _payload_text(meta, "side")
            expected_fill = reference * (
                Decimal("1") + slip_rate if side == "BUY" else Decimal("1") - slip_rate
            )
            if abs(fill_price - expected_fill) > _TOLERANCE:
                raise PaperReconciliationError("fill price does not match slippage evidence")
            if abs(fee - quantity * fill_price * fee_rate) > _TOLERANCE:
                raise PaperReconciliationError("fill fee does not match rate evidence")
            slippage = abs(fill_price - reference) * quantity
            if abs(stated_slippage - slippage) > _TOLERANCE:
                raise PaperReconciliationError("fill slippage evidence is inconsistent")
            if side == "BUY":
                cash -= quantity * fill_price + fee
                btc += quantity
            elif side == "SELL":
                cash += quantity * fill_price - fee
                btc -= quantity
            else:
                raise PaperReconciliationError("invalid fill side")
            if cash < -_TOLERANCE or btc < -_TOLERANCE:
                raise PaperReconciliationError("paper ledger has negative balance")
            total_fees += fee
            total_slippage += slippage
            paper_fills.append(
                PaperFill(
                    fill_id=fill_id,
                    order_id=_payload_text(meta, "order_id"),
                    side=side,
                    reason=_payload_text(meta, "reason"),
                    quantity=float(quantity),
                    reference_price=float(reference),
                    fill_price=float(fill_price),
                    fee=float(fee),
                    slippage=float(slippage),
                    fill_time=raw.occurred_at_utc,
                )
            )

        if abs(cash - Decimal(str(snapshot.cash))) > _TOLERANCE:
            raise PaperReconciliationError("cash does not reconcile to immutable fills")
        if abs(btc - Decimal(str(snapshot.btc_quantity))) > _TOLERANCE:
            raise PaperReconciliationError("BTC does not reconcile to immutable fills")

        active_orders = tuple(order for order in orders.values() if order.status in _ACTIVE)
        projected_active = {order.order_id: order for order in snapshot.pending_orders}
        if set(projected_active) != {order.order_id for order in active_orders}:
            raise PaperReconciliationError("active order projection contradicts broker evidence")
        for order in active_orders:
            projected = projected_active[order.order_id]
            if not (
                projected.idempotency_key == order.idempotency_key
                and projected.side == order.side
                and projected.status is order.status
                and _close(projected.requested_quantity, order.requested_quantity)
                and _close(projected.filled_quantity, order.filled_quantity)
            ):
                raise PaperReconciliationError("active order projection contradicts broker evidence")
        if len(active_orders) > 1:
            raise PaperReconciliationError("more than one active paper order")
        reserved = sum(
            Decimal(str(order.remainder_quantity))
            for order in active_orders
            if order.side == "SELL"
        )
        if reserved > btc + _TOLERANCE:
            raise PaperReconciliationError("active exits reserve more BTC than owned")
        if any(order.side == "BUY" for order in active_orders) and btc > 0:
            raise PaperReconciliationError("active entry would pyramid an existing position")
        if active_stop is not None and btc <= 0:
            raise PaperReconciliationError("active stop has no owned BTC")

        trades = _completed_trades(tuple(paper_fills))
        expected_position = snapshot.position_state
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
        if expected_position is not PositionState.HALTED and expected_position is not derived_position:
            raise PaperReconciliationError("position state contradicts paper evidence")

        return _Ledger(
            snapshot=snapshot,
            orders=orders,
            active_orders=tuple(
                sorted(
                    active_orders,
                    key=lambda item: (item.signal_at_utc, item.order_id),
                )
            ),
            fills=tuple(paper_fills),
            trades=trades,
            active_stop=active_stop,
            total_fees=float(total_fees),
            total_slippage=float(total_slippage),
        )


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
        if position <= 0 or quantity > position + _TOLERANCE:
            raise PaperReconciliationError("paper fills contain an unmatched sell")
        exits.append(fill)
        position -= quantity
        if abs(position) > _TOLERANCE:
            continue
        entry_quantity = sum(Decimal(str(item.quantity)) for item in entries)
        exit_quantity = sum(Decimal(str(item.quantity)) for item in exits)
        if abs(entry_quantity - exit_quantity) > _TOLERANCE:
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
    if _payload_text(payload, "action") != "SET":
        raise PaperReconciliationError("stop SET evidence is invalid")
    _, active_after = _timestamp(_payload_text(payload, "active_after_utc"))
    return PaperStop(
        stop_id=_payload_text(payload, "stop_id"),
        stop_price=_payload_number(payload, "stop_price"),
        reason=_payload_text(payload, "reason"),
        observed_at_utc=event.occurred_at_utc,
        active_after_utc=active_after,
    )


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
        and _close(order.requested_quantity, quantity)
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


def _close(left: float, right: float) -> bool:
    return abs(Decimal(str(left)) - Decimal(str(right))) <= _TOLERANCE
