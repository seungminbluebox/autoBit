from __future__ import annotations

from pathlib import Path

import pytest

from autobit.config import CostConfig
from autobit.domain.models import OrderStatus, PositionState
from autobit.execution.paper_broker import PaperBroker, PaperReconciliationError
from autobit.persistence.sqlite_store import SQLiteStore


UTC_0 = "2026-01-01T00:00:00Z"
UTC_4 = "2026-01-01T04:00:00Z"
UTC_8 = "2026-01-01T08:00:00Z"
UTC_12 = "2026-01-01T12:00:00Z"


def _broker(path: Path, costs: CostConfig = CostConfig()) -> tuple[SQLiteStore, PaperBroker]:
    store = SQLiteStore(path)
    store.initialize(initial_equity=100.0)
    return store, PaperBroker(store, costs)


def _enter(
    broker: PaperBroker,
    *,
    quantity: float = 0.2,
    open_price: float = 100.0,
):
    order = broker.submit_entry(UTC_0, quantity=quantity)
    fill = broker.process_open(order.order_id, UTC_4, open_price=open_price)
    assert fill is not None
    return order, fill


@pytest.mark.parametrize(
    "costs",
    [
        CostConfig(fee_rate=-0.1),
        CostConfig(slippage_rate=-0.1),
        CostConfig(fee_rate=float("nan")),
        CostConfig(slippage_rate=float("inf")),
    ],
)
def test_constructor_rejects_invalid_costs(tmp_path: Path, costs: CostConfig) -> None:
    store = SQLiteStore(tmp_path / "paper.sqlite3")
    store.initialize()

    with pytest.raises(ValueError, match="fee and slippage rates"):
        PaperBroker(store, costs)


def test_market_buy_fills_next_open_once_with_exact_costs(tmp_path: Path) -> None:
    store, broker = _broker(
        tmp_path / "paper.sqlite3",
        CostConfig(fee_rate=0.0005, slippage_rate=0.0005),
    )
    order = broker.submit_entry(UTC_0, quantity=0.2)

    first = broker.process_open(order.order_id, UTC_4, open_price=100.0)
    second = broker.process_open(order.order_id, UTC_4, open_price=100.0)

    assert first is not None
    assert first.fill_price == pytest.approx(100.05)
    assert first.fee == pytest.approx(0.010005)
    assert first.slippage == pytest.approx(0.01)
    assert second is None
    state = store.replay_state()
    assert state.btc_quantity == pytest.approx(0.2)
    assert state.cash == pytest.approx(79.979995)
    order_view = broker.order(order.order_id)
    assert order_view.status is OrderStatus.COMPLETED
    assert order_view.filled_quantity == pytest.approx(0.2)
    assert [
        event.payload["status"]
        for event in state.event_evidence
        if event.event_type == "ORDER_STATUS"
        and event.payload["order_id"] == order.order_id
    ] == ["SUBMITTED", "ACCEPTED", "COMPLETED"]


def test_market_order_rejects_same_candle_and_unknown_order_without_mutation(
    tmp_path: Path,
) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    order = broker.submit_entry(UTC_4, quantity=0.2)
    before = store.replay_state()

    with pytest.raises(ValueError, match="strictly later"):
        broker.process_open(order.order_id, UTC_4, open_price=100.0)
    with pytest.raises(ValueError, match="unknown order"):
        broker.process_open("missing", UTC_8, open_price=100.0)

    assert store.replay_state() == before


def test_entry_with_insufficient_cash_terminates_without_inventory_mutation(
    tmp_path: Path,
) -> None:
    store, broker = _broker(
        tmp_path / "paper.sqlite3",
        CostConfig(fee_rate=0.01, slippage_rate=0.0),
    )
    order = broker.submit_entry(UTC_0, quantity=1.0)

    assert broker.process_open(order.order_id, UTC_4, open_price=100.0) is None

    state = store.replay_state()
    assert state.cash == 100.0
    assert state.btc_quantity == 0.0
    assert state.position_state is PositionState.FLAT
    assert broker.order(order.order_id).status is OrderStatus.INSUFFICIENT_CASH


def test_sell_cannot_exceed_owned_or_unreserved_btc(tmp_path: Path) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    with pytest.raises(ValueError, match="sell quantity exceeds position"):
        broker.submit_exit(UTC_0, quantity=0.1, owned_quantity=0.0, reason="EXIT_CHANNEL")

    _enter(broker, quantity=0.4)
    broker.submit_exit(UTC_8, quantity=0.3, owned_quantity=0.4, reason="CLOSE_EXIT")
    before = store.replay_state()
    with pytest.raises(ValueError, match="reserved position"):
        broker.submit_exit(UTC_8, quantity=0.2, owned_quantity=0.4, reason="RISK_EXIT")
    assert store.replay_state() == before


def test_second_active_exit_is_rejected_even_when_inventory_covers_both(
    tmp_path: Path,
) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.4)
    broker.submit_exit(UTC_8, quantity=0.2, owned_quantity=0.4, reason="CLOSE_EXIT")
    before = store.replay_state()

    with pytest.raises(ValueError, match="active exit"):
        broker.submit_exit(UTC_8, quantity=0.1, owned_quantity=0.4, reason="RISK_EXIT")

    assert store.replay_state() == before


def test_submit_and_process_are_idempotent_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    first_store, first = _broker(path, CostConfig(0.0, 0.0))
    created = first.submit_entry(UTC_0, quantity=0.2)
    again = first.submit_entry(UTC_0, quantity=0.2)
    assert again == created
    fill = first.process_open(created.order_id, UTC_4, open_price=100.0)
    sequence = first_store.replay_state().last_sequence
    first_store.close()

    second_store = SQLiteStore(path)
    second_store.initialize()
    second = PaperBroker(second_store, CostConfig(0.0, 0.0))
    assert second.submit_entry(UTC_0, quantity=0.2).order_id == created.order_id
    assert second.process_open(created.order_id, UTC_4, open_price=100.0) is None
    assert second_store.replay_state().last_sequence == sequence
    assert second.reconcile().fills == (fill,)


def test_entry_partial_fill_cancels_remainder_and_keeps_long(tmp_path: Path) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    order = broker.submit_entry(UTC_0, quantity=0.4)

    fill = broker.process_open(
        order.order_id,
        UTC_4,
        open_price=100.0,
        actual_quantity=0.15,
    )

    assert fill is not None and fill.quantity == pytest.approx(0.15)
    view = broker.order(order.order_id)
    assert view.status is OrderStatus.CANCELED
    assert view.filled_quantity == pytest.approx(0.15)
    assert view.remainder_quantity == pytest.approx(0.25)
    state = store.replay_state()
    assert state.position_state is PositionState.LONG
    assert state.pending_orders == ()


def test_sub_tolerance_entry_remainder_is_not_misclassified_as_a_full_fill(
    tmp_path: Path,
) -> None:
    _, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    order = broker.submit_entry(UTC_0, quantity=0.2)

    fill = broker.process_open(
        order.order_id,
        UTC_4,
        open_price=100.0,
        actual_quantity=0.19999999995,
    )

    assert fill is not None
    terminal = broker.order(order.order_id)
    assert terminal.status is OrderStatus.CANCELED
    assert terminal.remainder_quantity == pytest.approx(5e-11)


def test_unfilled_entry_is_canceled_without_a_fill_or_automatic_retry(tmp_path: Path) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    order = broker.submit_entry(UTC_0, quantity=0.4)

    assert broker.process_open(
        order.order_id,
        UTC_4,
        open_price=100.0,
        actual_quantity=0.0,
    ) is None

    assert broker.order(order.order_id).status is OrderStatus.CANCELED
    assert broker.reconcile().fills == ()
    assert store.replay_state().position_state is PositionState.FLAT


def test_exit_partial_fill_creates_one_exact_next_open_remainder(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store, broker = _broker(path, CostConfig(fee_rate=0.0, slippage_rate=0.001))
    _enter(broker, quantity=0.4)
    parent = broker.submit_exit(
        UTC_8,
        quantity=0.4,
        owned_quantity=0.4,
        reason="CLOSE_EXIT",
    )

    partial = broker.process_open(
        parent.order_id,
        UTC_12,
        open_price=101.0,
        actual_quantity=0.15,
    )
    assert partial is not None
    reconciliation = broker.reconcile()
    child, = reconciliation.active_orders
    assert broker.order(parent.order_id).status is OrderStatus.CANCELED
    assert child.parent_order_id == parent.order_id
    assert child.requested_quantity == pytest.approx(0.25)
    assert child.status is OrderStatus.ACCEPTED
    assert reconciliation.position_state is PositionState.EXIT_PENDING
    with pytest.raises(ValueError, match="strictly later"):
        broker.process_open(child.order_id, UTC_12, open_price=99.0)

    store.close()
    reopened_store = SQLiteStore(path)
    reopened_store.initialize()
    reopened = PaperBroker(reopened_store, CostConfig(0.0, 0.001))
    assert reopened.reconcile().active_orders == (child,)
    assert reopened.process_open(parent.order_id, UTC_12, open_price=101.0) is None
    assert len(reopened.reconcile().active_orders) == 1
    completed = reopened.process_open(child.order_id, "2026-01-01T16:00:00Z", open_price=99.0)
    assert completed is not None
    assert completed.quantity == pytest.approx(0.25)
    assert completed.fill_price == pytest.approx(98.901)
    result = reopened.reconcile()
    assert result.position_state is PositionState.FLAT
    assert result.active_orders == ()
    trade, = result.completed_trades
    assert trade.quantity == pytest.approx(0.4)
    assert trade.exit_price == pytest.approx((0.15 * 100.899 + 0.25 * 98.901) / 0.4)
    assert trade.exit_reason == "CLOSE_EXIT"


def test_persisted_stop_uses_worse_gap_and_is_not_retroactive(tmp_path: Path) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.001))
    _, entry = _enter(broker, quantity=0.2, open_price=100.0)
    broker.set_stop(UTC_4, 95.0, active_after=UTC_4, reason="TRAILING_STOP")

    assert broker.process_intrabar_stop(UTC_4, open_price=90.0, low_price=85.0) is None
    stop_fill = broker.process_intrabar_stop(UTC_8, open_price=90.0, low_price=85.0)

    assert stop_fill is not None
    assert stop_fill.reference_price == pytest.approx(90.0)
    assert stop_fill.fill_price == pytest.approx(89.91)
    result = broker.reconcile()
    assert result.position_state is PositionState.FLAT
    trade, = result.completed_trades
    assert trade.entry_price == entry.fill_price
    assert trade.exit_reason == "TRAILING_STOP"


def test_persisted_intrabar_stop_uses_stop_reference_and_survives_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    store, broker = _broker(path, CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    stop = broker.set_stop(UTC_4, 95.0, active_after=UTC_0, reason="HARD_STOP")
    store.close()

    reopened_store = SQLiteStore(path)
    reopened_store.initialize()
    reopened = PaperBroker(reopened_store, CostConfig(0.0, 0.0))
    assert reopened.active_stop() == stop
    fill = reopened.process_intrabar_stop(UTC_8, open_price=97.0, low_price=94.0)
    assert fill is not None
    assert fill.reference_price == 95.0
    assert fill.fill_price == 95.0
    assert reopened.active_stop() is None


@pytest.mark.parametrize("boundary", ["created", "submitted", "accepted", "fill"])
def test_lifecycle_exception_rolls_back_the_whole_broker_action(
    tmp_path: Path,
    boundary: str,
) -> None:
    path = tmp_path / f"{boundary}.sqlite3"
    store = SQLiteStore(path)
    store.initialize()

    def fail(at: str) -> None:
        if at == boundary:
            raise RuntimeError(f"fault at {at}")

    broker = PaperBroker(store, CostConfig(0.0, 0.0), fault_hook=fail)
    if boundary == "fill":
        order = PaperBroker(store, CostConfig(0.0, 0.0)).submit_entry(UTC_0, quantity=0.2)
        before = store.replay_state()
        with pytest.raises(RuntimeError, match="fault at fill"):
            broker.process_open(order.order_id, UTC_4, open_price=100.0)
    else:
        before = store.replay_state()
        with pytest.raises(RuntimeError, match=f"fault at {boundary}"):
            broker.submit_entry(UTC_0, quantity=0.2)

    assert store.replay_state() == before


def test_reconcile_fails_closed_when_a_fill_has_no_broker_evidence(tmp_path: Path) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    store.append_fill(
        "foreign",
        "BUY",
        0.1,
        10.0,
        0.0,
        UTC_4,
        fill_id="foreign-fill",
    )

    with pytest.raises(PaperReconciliationError, match="fill evidence"):
        broker.reconcile()


def test_reconcile_fails_closed_when_broker_lifecycle_misses_partial_status(
    tmp_path: Path,
) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    key = "KRW-BTC:2026-01-01T08:00:00Z:SELL:CLOSE_EXIT:ROOT"
    order_id = "manual-exit"
    store.record_order_once(
        key,
        "SELL",
        0.2,
        order_id=order_id,
        occurred_at=UTC_8,
        status=OrderStatus.ACCEPTED,
    )
    store.append_event(
        "manual-order-meta",
        "PAPER_ORDER",
        UTC_8,
        {
            "idempotency_key": key,
            "order_id": order_id,
            "order_kind": "MARKET",
            "parent_order_id": None,
            "reason": "CLOSE_EXIT",
            "requested_quantity": 0.2,
            "side": "SELL",
            "signal_at_utc": UTC_8,
        },
    )
    store.append_fill(
        order_id,
        "SELL",
        0.1,
        100.0,
        0.0,
        UTC_12,
        fill_id="manual-fill",
    )
    store.append_event(
        "manual-fill-meta",
        "PAPER_FILL",
        UTC_12,
        {
            "fee": 0.0,
            "fee_rate": 0.0,
            "fill_id": "manual-fill",
            "fill_price": 100.0,
            "order_id": order_id,
            "quantity": 0.1,
            "reason": "CLOSE_EXIT",
            "reference_price": 100.0,
            "side": "SELL",
            "slippage": 0.0,
            "slippage_rate": 0.0,
        },
    )

    with pytest.raises(PaperReconciliationError, match="order projection"):
        broker.reconcile()


def test_reconcile_exposes_exact_ledger_and_round_trip(tmp_path: Path) -> None:
    store, broker = _broker(
        tmp_path / "paper.sqlite3",
        CostConfig(fee_rate=0.01, slippage_rate=0.01),
    )
    _, buy = _enter(broker, quantity=0.2, open_price=100.0)
    exit_order = broker.submit_exit(
        UTC_8,
        quantity=0.2,
        owned_quantity=0.2,
        reason="CLOSE_EXIT",
    )
    sell = broker.process_open(exit_order.order_id, UTC_12, open_price=110.0)
    assert sell is not None

    result = broker.reconcile()

    assert result.cash == pytest.approx(
        100.0 - buy.quantity * buy.fill_price - buy.fee
        + sell.quantity * sell.fill_price - sell.fee
    )
    assert result.btc_quantity == 0.0
    assert result.total_fees == pytest.approx(buy.fee + sell.fee)
    assert result.total_slippage == pytest.approx(buy.slippage + sell.slippage)
    trade, = result.completed_trades
    assert trade.gross_pnl == pytest.approx(0.2 * (sell.fill_price - buy.fill_price))
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - buy.fee - sell.fee)
