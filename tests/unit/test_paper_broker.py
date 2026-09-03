from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from random import Random
from threading import Barrier

import pytest

from autobit.config import CostConfig
from autobit.domain.models import OrderStatus, PositionState
from autobit.execution.paper_broker import PaperBroker, PaperReconciliationError
from autobit.persistence.sqlite_store import IdempotencyConflictError, SQLiteStore


UTC_0 = "2026-01-01T00:00:00Z"
UTC_4 = "2026-01-01T04:00:00Z"
UTC_8 = "2026-01-01T08:00:00Z"
UTC_12 = "2026-01-01T12:00:00Z"
UTC_16 = "2026-01-01T16:00:00Z"
UTC_20 = "2026-01-01T20:00:00Z"


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


def test_reconcile_uses_the_store_canonical_float_projection_without_tolerance(
    tmp_path: Path,
) -> None:
    generator = Random(7439)
    for index in range(32):
        price = generator.uniform(5.0, 150.0)
        quantity = generator.uniform(0.000001, min(0.5, 90.0 / price))
        store, broker = _broker(
            tmp_path / f"ordinary-{index}.sqlite3",
            CostConfig(0.0, 0.0),
        )
        order = broker.submit_entry(UTC_0, quantity=quantity)
        fill = broker.process_open(order.order_id, UTC_4, open_price=price)

        assert fill is not None
        result = broker.reconcile()
        assert result.cash == store.replay_state().cash
        assert result.btc_quantity == store.replay_state().btc_quantity
        store.close()


def test_submitted_order_binds_costs_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store, broker = _broker(path, CostConfig(fee_rate=0.01, slippage_rate=0.02))
    order = broker.submit_entry(UTC_0, quantity=0.2)
    store.close()

    reopened_store = SQLiteStore(path)
    reopened_store.initialize()
    reopened = PaperBroker(reopened_store, CostConfig(fee_rate=0.0, slippage_rate=0.0))
    fill = reopened.process_open(order.order_id, UTC_4, open_price=100.0)

    assert fill is not None
    assert fill.fill_price == 102.0
    assert fill.fee == pytest.approx(0.204)


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


def test_sub_picounit_cash_shortfall_is_insufficient_without_a_fill(tmp_path: Path) -> None:
    store, broker = _broker(
        tmp_path / "paper.sqlite3",
        CostConfig(fee_rate=5e-15, slippage_rate=0.0),
    )
    order = broker.submit_entry(UTC_0, quantity=1.0)

    assert broker.process_open(order.order_id, UTC_4, open_price=100.0) is None

    assert broker.order(order.order_id).status is OrderStatus.INSUFFICIENT_CASH
    assert broker.reconcile().fills == ()
    assert store.replay_state().cash == 100.0


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


def test_manual_remainder_parent_is_rejected_before_mutation(tmp_path: Path) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    before = store.replay_state()

    with pytest.raises(ValueError, match="broker-managed"):
        broker.submit_exit(
            UTC_8,
            quantity=0.2,
            owned_quantity=0.2,
            reason="CLOSE_EXIT",
            parent_order_id="paper-order:forged",
        )

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


def test_near_quantity_retry_is_an_idempotency_conflict(tmp_path: Path) -> None:
    _, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    broker.submit_entry(UTC_0, quantity=0.2)

    with pytest.raises(Exception, match="conflicting|different"):
        broker.submit_entry(UTC_0, quantity=0.20000000005)


def test_concurrent_identical_submit_creates_one_lifecycle_and_both_succeed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    bootstrap = SQLiteStore(path)
    bootstrap.initialize()
    bootstrap.close()
    stores = [SQLiteStore(path), SQLiteStore(path)]
    for store in stores:
        store.initialize()
    brokers = [PaperBroker(store, CostConfig(0.0, 0.0)) for store in stores]
    barrier = Barrier(2)

    def submit(index: int):
        barrier.wait(timeout=5.0)
        return brokers[index].submit_entry(UTC_0, quantity=0.2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(submit, range(2)))

    assert results[0] == results[1]
    evidence = stores[0].replay_state().event_evidence
    assert sum(event.event_type == "ORDER_CREATED" for event in evidence) == 1
    assert sum(event.event_type == "PAPER_ORDER" for event in evidence) == 1
    for store in stores:
        store.close()


def test_concurrent_same_identity_with_different_quantity_has_one_conflict(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    bootstrap = SQLiteStore(path)
    bootstrap.initialize()
    bootstrap.close()
    stores = [SQLiteStore(path), SQLiteStore(path)]
    for store in stores:
        store.initialize()
    brokers = [PaperBroker(store, CostConfig(0.0, 0.0)) for store in stores]
    barrier = Barrier(2)

    def submit(index: int):
        barrier.wait(timeout=5.0)
        try:
            return brokers[index].submit_entry(
                UTC_0,
                quantity=(0.2, 0.20000000005)[index],
            )
        except IdempotencyConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(submit, range(2)))

    assert sum(not isinstance(result, Exception) for result in results) == 1
    conflict, = (
        result for result in results if isinstance(result, IdempotencyConflictError)
    )
    assert "conflicting" in str(conflict)
    evidence = stores[0].replay_state().event_evidence
    assert sum(event.event_type == "ORDER_CREATED" for event in evidence) == 1
    assert sum(event.event_type == "PAPER_ORDER" for event in evidence) == 1
    for store in stores:
        store.close()


def test_concurrent_identical_process_open_charges_one_fill(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    bootstrap, broker = _broker(path, CostConfig(0.0, 0.0))
    order = broker.submit_entry(UTC_0, quantity=0.2)
    bootstrap.close()
    stores = [SQLiteStore(path), SQLiteStore(path)]
    for store in stores:
        store.initialize()
    brokers = [PaperBroker(store, CostConfig(0.0, 0.0)) for store in stores]
    barrier = Barrier(2)

    def process(index: int):
        barrier.wait(timeout=5.0)
        return brokers[index].process_open(order.order_id, UTC_4, open_price=100.0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(process, range(2)))

    assert sum(result is not None for result in results) == 1
    assert stores[0].replay_state().btc_quantity == 0.2
    assert sum(event.event_type == "FILL" for event in stores[0].replay_state().event_evidence) == 1
    for store in stores:
        store.close()


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


def test_zero_injected_entry_is_rejected_without_mutation(tmp_path: Path) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    order = broker.submit_entry(UTC_0, quantity=0.4)
    before = store.replay_state()

    with pytest.raises(ValueError, match="positive"):
        broker.process_open(
            order.order_id,
            UTC_4,
            open_price=100.0,
            actual_quantity=0.0,
        )

    assert store.replay_state() == before
    assert broker.order(order.order_id).status is OrderStatus.ACCEPTED
    assert broker.reconcile().fills == ()
    assert store.replay_state().position_state is PositionState.ENTRY_PENDING


def test_zero_fill_root_exit_keeps_the_existing_protective_stop(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store, broker = _broker(path, CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    stop = broker.set_stop(UTC_4, 95.0, reason="HARD_STOP")
    order = broker.submit_exit(
        UTC_8,
        quantity=0.2,
        owned_quantity=0.2,
        reason="CLOSE_EXIT",
    )
    before = broker.reconcile()

    with pytest.raises(ValueError, match="positive"):
        broker.process_open(
            order.order_id,
            UTC_12,
            open_price=100.0,
            actual_quantity=0.0,
        )

    result = broker.reconcile()
    assert result == before
    assert result.btc_quantity == 0.2
    assert result.position_state is PositionState.EXIT_PENDING
    assert result.active_orders == (order,)
    assert result.active_stop == stop
    store.close()

    reopened_store = SQLiteStore(path)
    reopened_store.initialize()
    reopened = PaperBroker(reopened_store, CostConfig(0.0, 0.0))
    assert reopened.reconcile() == result


def test_market_exit_cancels_protective_stops_only_after_inventory_is_flat(
    tmp_path: Path,
) -> None:
    _, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    stop = broker.set_stop(UTC_4, 95.0, reason="HARD_STOP")
    parent = broker.submit_exit(
        UTC_8,
        quantity=0.2,
        owned_quantity=0.2,
        reason="CLOSE_EXIT",
    )
    assert broker.active_stop() == stop

    broker.process_open(parent.order_id, UTC_12, open_price=100.0, actual_quantity=0.1)
    child, = broker.reconcile().active_orders
    assert broker.active_stop() == stop

    broker.process_open(child.order_id, UTC_16, open_price=100.0)
    result = broker.reconcile()
    assert result.btc_quantity == 0.0
    assert result.active_orders == ()
    assert result.active_stop is None


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


def test_remainder_child_inherits_parent_cost_binding_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store, broker = _broker(path, CostConfig(fee_rate=0.01, slippage_rate=0.02))
    _enter(broker, quantity=0.2)
    parent = broker.submit_exit(
        UTC_8,
        quantity=0.2,
        owned_quantity=0.2,
        reason="CLOSE_EXIT",
    )
    store.close()

    reopened_store = SQLiteStore(path)
    reopened_store.initialize()
    reopened = PaperBroker(reopened_store, CostConfig(0.0, 0.0))
    reopened.process_open(
        parent.order_id,
        UTC_12,
        open_price=100.0,
        actual_quantity=0.1,
    )
    child, = reopened.reconcile().active_orders

    assert child.fee_rate == 0.01
    assert child.slippage_rate == 0.02
    fill = reopened.process_open(child.order_id, UTC_16, open_price=100.0)
    assert fill is not None
    assert fill.fill_price == 98.0
    assert fill.fee == pytest.approx(0.098)


def test_tiny_exit_remainder_stays_pending_without_a_completed_round_trip(
    tmp_path: Path,
) -> None:
    _, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    parent = broker.submit_exit(
        UTC_8,
        quantity=0.2,
        owned_quantity=0.2,
        reason="CLOSE_EXIT",
    )

    broker.process_open(
        parent.order_id,
        UTC_12,
        open_price=100.0,
        actual_quantity=0.19999999995,
    )

    result = broker.reconcile()
    child, = result.active_orders
    assert child.requested_quantity == pytest.approx(5e-11)
    assert result.btc_quantity == pytest.approx(5e-11)
    assert result.position_state is PositionState.EXIT_PENDING
    assert result.completed_trades == ()


def test_exit_remainder_child_rejects_late_open_without_mutation(tmp_path: Path) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    parent = broker.submit_exit(
        UTC_8,
        quantity=0.2,
        owned_quantity=0.2,
        reason="CLOSE_EXIT",
    )
    broker.process_open(parent.order_id, UTC_12, open_price=100.0, actual_quantity=0.1)
    child, = broker.reconcile().active_orders
    before = store.replay_state()

    with pytest.raises(ValueError, match="eligible|immediately following"):
        broker.process_open(child.order_id, UTC_20, open_price=100.0)

    assert store.replay_state() == before


def test_exit_remainder_child_cannot_partially_fill_or_create_grandchild(
    tmp_path: Path,
) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    parent = broker.submit_exit(
        UTC_8,
        quantity=0.2,
        owned_quantity=0.2,
        reason="CLOSE_EXIT",
    )
    broker.process_open(parent.order_id, UTC_12, open_price=100.0, actual_quantity=0.1)
    child, = broker.reconcile().active_orders
    before = store.replay_state()

    with pytest.raises(ValueError, match="remainder child must fill completely"):
        broker.process_open(child.order_id, UTC_16, open_price=100.0, actual_quantity=0.05)

    assert store.replay_state() == before
    assert broker.reconcile().active_orders == (child,)


def test_exit_remainder_child_rejects_zero_fill_across_reopen_without_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    store, broker = _broker(path, CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    parent = broker.submit_exit(
        UTC_8,
        quantity=0.2,
        owned_quantity=0.2,
        reason="CLOSE_EXIT",
    )
    broker.process_open(parent.order_id, UTC_12, open_price=100.0, actual_quantity=0.1)
    child, = broker.reconcile().active_orders
    store.close()

    reopened_store = SQLiteStore(path)
    reopened_store.initialize()
    reopened = PaperBroker(reopened_store, CostConfig(0.0, 0.0))
    before = reopened_store.replay_state()
    with pytest.raises(ValueError, match="positive"):
        reopened.process_open(
            child.order_id,
            UTC_16,
            open_price=100.0,
            actual_quantity=0.0,
        )

    assert reopened_store.replay_state() == before
    assert reopened.reconcile().active_orders == (child,)


def test_persisted_stop_uses_worse_gap_and_is_not_retroactive(tmp_path: Path) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.001))
    _, entry = _enter(broker, quantity=0.2, open_price=100.0)
    broker.set_stop(UTC_4, 95.0, reason="TRAILING_STOP")

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
    stop = broker.set_stop(UTC_4, 95.0, reason="HARD_STOP")
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


def test_future_stop_version_does_not_shadow_the_prior_active_stop(tmp_path: Path) -> None:
    _, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    broker.set_stop(UTC_4, 95.0, reason="HARD_STOP")
    broker.set_stop(UTC_8, 97.0, reason="TRAILING_STOP")

    fill = broker.process_intrabar_stop(UTC_8, open_price=100.0, low_price=94.0)

    assert fill is not None
    assert fill.reference_price == 95.0
    assert fill.fill_price == 95.0
    assert broker.active_stop() is None


def test_stop_binds_trigger_costs_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store, broker = _broker(path, CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    broker.set_stop(UTC_4, 95.0, reason="HARD_STOP")
    store.close()

    reopened_store = SQLiteStore(path)
    reopened_store.initialize()
    reopened = PaperBroker(
        reopened_store,
        CostConfig(fee_rate=0.1, slippage_rate=0.2),
    )
    fill = reopened.process_intrabar_stop(UTC_8, open_price=100.0, low_price=94.0)

    assert fill is not None
    assert fill.reference_price == 95.0
    assert fill.fill_price == 95.0
    assert fill.fee == 0.0


def test_stop_is_next_boundary_only_and_exact_boundary_can_trigger(tmp_path: Path) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    stop = broker.set_stop(UTC_4, 95.0, reason="HARD_STOP")
    before = store.replay_state()

    assert stop.active_after_utc.isoformat() == "2026-01-01T08:00:00+00:00"
    assert broker.process_intrabar_stop(UTC_4, open_price=100.0, low_price=90.0) is None
    assert store.replay_state() == before
    fill = broker.process_intrabar_stop(UTC_8, open_price=100.0, low_price=90.0)
    assert fill is not None and fill.fill_price == 95.0


def test_same_stop_source_with_changed_price_conflicts_and_stop_cannot_loosen(
    tmp_path: Path,
) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    original = broker.set_stop(UTC_4, 95.0, reason="HARD_STOP")
    sequence = store.replay_state().last_sequence
    assert broker.set_stop(UTC_4, 95.0, reason="HARD_STOP") == original
    assert store.replay_state().last_sequence == sequence
    with pytest.raises(Exception, match="conflict"):
        broker.set_stop(UTC_4, 96.0, reason="HARD_STOP")

    raised = broker.set_stop(UTC_8, 97.0, reason="TRAILING_STOP")
    assert raised.stop_price == 97.0
    before_lower = store.replay_state()
    with pytest.raises(ValueError, match="cannot loosen"):
        broker.set_stop(UTC_12, 96.0, reason="TRAILING_STOP")
    assert store.replay_state() == before_lower


def test_backdated_or_non_next_boundary_stop_activation_is_rejected(tmp_path: Path) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    before = store.replay_state()

    with pytest.raises(ValueError, match="next four-hour boundary"):
        broker.set_stop(UTC_4, 95.0, active_after=UTC_0, reason="HARD_STOP")
    with pytest.raises(ValueError, match="next four-hour boundary"):
        broker.set_stop(UTC_4, 95.0, active_after=UTC_12, reason="HARD_STOP")

    assert store.replay_state() == before


def test_stop_rejects_a_noncurrent_source_before_mutation(tmp_path: Path) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    before = store.replay_state()

    with pytest.raises(ValueError, match="currently open"):
        broker.set_stop(
            UTC_4,
            95.0,
            source_id="paper-order:forged",
            reason="HARD_STOP",
        )

    assert store.replay_state() == before


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


def test_reconcile_preserves_a_generic_fill_without_claiming_broker_evidence(
    tmp_path: Path,
) -> None:
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

    result = broker.reconcile()

    assert result.cash == 99.0
    assert result.btc_quantity == 0.1
    assert result.position_state is PositionState.LONG
    assert result.fills == ()
    assert result.completed_trades == ()


def test_reconcile_fails_closed_when_broker_lifecycle_misses_partial_status(
    tmp_path: Path,
) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    order = broker.submit_exit(
        UTC_8,
        quantity=0.2,
        owned_quantity=0.2,
        reason="CLOSE_EXIT",
    )
    fill_material = "|".join((order.order_id, UTC_12, repr(0.1)))
    fill_id = f"paper-fill:{sha256(fill_material.encode('utf-8')).hexdigest()}"
    store.append_fill(
        order.order_id,
        "SELL",
        0.1,
        100.0,
        0.0,
        UTC_12,
        fill_id=fill_id,
    )
    store.append_event(
        f"paper-fill-meta:{sha256(fill_id.encode('utf-8')).hexdigest()}",
        "PAPER_FILL",
        UTC_12,
        {
            "fee": 0.0,
            "fee_rate": 0.0,
            "fill_id": fill_id,
            "fill_price": 100.0,
            "order_id": order.order_id,
            "quantity": 0.1,
            "reason": "CLOSE_EXIT",
            "reference_price": 100.0,
            "side": "SELL",
            "slippage": 0.0,
            "slippage_rate": 0.0,
        },
    )

    with pytest.raises(PaperReconciliationError, match="lifecycle"):
        broker.reconcile()


def test_reconcile_rejects_forged_arbitrary_broker_order_id(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "paper.sqlite3")
    store.initialize()
    key = "KRW-BTC:2026-01-01T00:00:00Z:BUY:ENTRY:ROOT"
    store.record_order_once(
        key,
        "BUY",
        0.2,
        order_id="forged-id",
        occurred_at=UTC_0,
        status=OrderStatus.ACCEPTED,
    )
    store.append_event(
        f"paper-order-meta:{sha256('forged-id'.encode('utf-8')).hexdigest()}",
        "PAPER_ORDER",
        UTC_0,
        {
            "eligible_open_utc": UTC_4,
            "fee_rate": 0.0,
            "idempotency_key": key,
            "order_id": "forged-id",
            "order_kind": "MARKET",
            "parent_order_id": None,
            "reason": "ENTRY",
            "requested_quantity": 0.2,
            "side": "BUY",
            "signal_at_utc": UTC_0,
            "slippage_rate": 0.0,
        },
    )
    broker = PaperBroker(store, CostConfig(0.0, 0.0))

    with pytest.raises(PaperReconciliationError, match="deterministic|lifecycle"):
        broker.reconcile()


def test_reconcile_rejects_initial_accepted_even_with_deterministic_identity(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "paper.sqlite3")
    store.initialize()
    key = "KRW-BTC:2026-01-01T00:00:00Z:BUY:ENTRY:ROOT"
    order_id = f"paper-order:{sha256(key.encode('utf-8')).hexdigest()}"
    store.record_order_once(
        key,
        "BUY",
        0.2,
        order_id=order_id,
        occurred_at=UTC_0,
        status=OrderStatus.ACCEPTED,
    )
    store.append_event(
        f"paper-order-meta:{sha256(order_id.encode('utf-8')).hexdigest()}",
        "PAPER_ORDER",
        UTC_0,
        {
            "eligible_open_utc": UTC_4,
            "fee_rate": 0.0,
            "idempotency_key": key,
            "order_id": order_id,
            "order_kind": "MARKET",
            "parent_order_id": None,
            "reason": "ENTRY",
            "requested_quantity": 0.2,
            "side": "BUY",
            "signal_at_utc": UTC_0,
            "slippage_rate": 0.0,
        },
    )
    broker = PaperBroker(store, CostConfig(0.0, 0.0))

    with pytest.raises(PaperReconciliationError, match="creation|lifecycle"):
        broker.reconcile()


def test_reconcile_rejects_public_store_same_candle_broker_fill(tmp_path: Path) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    order = broker.submit_entry(UTC_0, quantity=0.2)
    fill_material = "|".join((order.order_id, UTC_0, repr(0.2)))
    fill_id = f"paper-fill:{sha256(fill_material.encode('utf-8')).hexdigest()}"
    store.append_fill(
        order.order_id,
        "BUY",
        0.2,
        100.0,
        0.0,
        UTC_0,
        fill_id=fill_id,
    )
    store.transition_order_status(
        order.order_id,
        f"{order.idempotency_key}:completed:{UTC_0}",
        OrderStatus.COMPLETED,
        UTC_0,
        reason="ENTRY",
    )
    store.append_event(
        f"paper-fill-meta:{sha256(fill_id.encode('utf-8')).hexdigest()}",
        "PAPER_FILL",
        UTC_0,
        {
            "fee": 0.0,
            "fee_rate": 0.0,
            "fill_id": fill_id,
            "fill_price": 100.0,
            "order_id": order.order_id,
            "quantity": 0.2,
            "reason": "ENTRY",
            "reference_price": 100.0,
            "side": "BUY",
            "slippage": 0.0,
            "slippage_rate": 0.0,
        },
    )

    with pytest.raises(PaperReconciliationError, match="strictly after|eligible"):
        broker.reconcile()


def test_reconcile_rejects_stop_terminal_before_its_future_trigger_lifecycle(
    tmp_path: Path,
) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    stop = broker.set_stop(UTC_4, 95.0, reason="HARD_STOP")
    key = f"KRW-BTC:{UTC_4}:SELL:HARD_STOP:{stop.stop_id}"
    order_id = f"paper-order:{sha256(key.encode('utf-8')).hexdigest()}"
    terminal_id = (
        "paper-stop-triggered:"
        f"{sha256((stop.stop_id + '|' + order_id).encode('utf-8')).hexdigest()}"
    )
    store.append_event(
        terminal_id,
        "PAPER_STOP",
        UTC_8,
        {"action": "TRIGGERED", "identity": order_id, "stop_id": stop.stop_id},
    )
    store.record_order_once(
        key,
        "SELL",
        0.2,
        order_id=order_id,
        occurred_at=UTC_8,
        status=OrderStatus.CREATED,
    )
    store.transition_order_status(
        order_id,
        f"{key}:submitted",
        OrderStatus.SUBMITTED,
        UTC_8,
    )
    store.transition_order_status(
        order_id,
        f"{key}:accepted",
        OrderStatus.ACCEPTED,
        UTC_8,
    )
    store.append_event(
        f"paper-order-meta:{sha256(order_id.encode('utf-8')).hexdigest()}",
        "PAPER_ORDER",
        UTC_8,
        {
            "eligible_open_utc": UTC_8,
            "fee_rate": 0.0,
            "idempotency_key": key,
            "order_id": order_id,
            "order_kind": "STOP",
            "parent_order_id": stop.stop_id,
            "reason": "HARD_STOP",
            "requested_quantity": 0.2,
            "side": "SELL",
            "signal_at_utc": UTC_4,
            "slippage_rate": 0.0,
        },
    )
    fill_material = "|".join((order_id, UTC_8, repr(0.2)))
    fill_id = f"paper-fill:{sha256(fill_material.encode('utf-8')).hexdigest()}"
    store.append_fill(
        order_id,
        "SELL",
        0.2,
        95.0,
        0.0,
        UTC_8,
        fill_id=fill_id,
    )
    store.transition_order_status(
        order_id,
        f"{key}:completed:{UTC_8}",
        OrderStatus.COMPLETED,
        UTC_8,
        reason="HARD_STOP",
    )
    store.append_event(
        f"paper-fill-meta:{sha256(fill_id.encode('utf-8')).hexdigest()}",
        "PAPER_FILL",
        UTC_8,
        {
            "fee": 0.0,
            "fee_rate": 0.0,
            "fill_id": fill_id,
            "fill_price": 95.0,
            "order_id": order_id,
            "quantity": 0.2,
            "reason": "HARD_STOP",
            "reference_price": 95.0,
            "side": "SELL",
            "slippage": 0.0,
            "slippage_rate": 0.0,
        },
    )

    with pytest.raises(PaperReconciliationError, match="sequence|causal"):
        broker.reconcile()


def test_reconcile_rejects_partial_stop_trigger_that_leaves_unprotected_btc(
    tmp_path: Path,
) -> None:
    store, broker = _broker(tmp_path / "paper.sqlite3", CostConfig(0.0, 0.0))
    _enter(broker, quantity=0.2)
    stop = broker.set_stop(UTC_4, 95.0, reason="HARD_STOP")
    key = f"KRW-BTC:{UTC_4}:SELL:HARD_STOP:{stop.stop_id}"
    order_id = f"paper-order:{sha256(key.encode('utf-8')).hexdigest()}"
    store.record_order_once(
        key,
        "SELL",
        0.1,
        order_id=order_id,
        occurred_at=UTC_8,
        status=OrderStatus.CREATED,
    )
    store.transition_order_status(
        order_id,
        f"{key}:submitted",
        OrderStatus.SUBMITTED,
        UTC_8,
    )
    store.transition_order_status(
        order_id,
        f"{key}:accepted",
        OrderStatus.ACCEPTED,
        UTC_8,
    )
    store.append_event(
        f"paper-order-meta:{sha256(order_id.encode('utf-8')).hexdigest()}",
        "PAPER_ORDER",
        UTC_8,
        {
            "eligible_open_utc": UTC_8,
            "fee_rate": 0.0,
            "idempotency_key": key,
            "order_id": order_id,
            "order_kind": "STOP",
            "parent_order_id": stop.stop_id,
            "reason": "HARD_STOP",
            "requested_quantity": 0.1,
            "side": "SELL",
            "signal_at_utc": UTC_4,
            "slippage_rate": 0.0,
        },
    )
    fill_material = "|".join((order_id, UTC_8, repr(0.1)))
    fill_id = f"paper-fill:{sha256(fill_material.encode('utf-8')).hexdigest()}"
    store.append_fill(
        order_id,
        "SELL",
        0.1,
        95.0,
        0.0,
        UTC_8,
        fill_id=fill_id,
    )
    store.transition_order_status(
        order_id,
        f"{key}:completed:{UTC_8}",
        OrderStatus.COMPLETED,
        UTC_8,
        reason="HARD_STOP",
    )
    store.append_event(
        f"paper-fill-meta:{sha256(fill_id.encode('utf-8')).hexdigest()}",
        "PAPER_FILL",
        UTC_8,
        {
            "fee": 0.0,
            "fee_rate": 0.0,
            "fill_id": fill_id,
            "fill_price": 95.0,
            "order_id": order_id,
            "quantity": 0.1,
            "reason": "HARD_STOP",
            "reference_price": 95.0,
            "side": "SELL",
            "slippage": 0.0,
            "slippage_rate": 0.0,
        },
    )
    terminal_id = (
        "paper-stop-triggered:"
        f"{sha256((stop.stop_id + '|' + order_id).encode('utf-8')).hexdigest()}"
    )
    store.append_event(
        terminal_id,
        "PAPER_STOP",
        UTC_8,
        {"action": "TRIGGERED", "identity": order_id, "stop_id": stop.stop_id},
    )

    with pytest.raises(PaperReconciliationError, match="full|inventory|residual"):
        broker.reconcile()


def test_reconcile_preserves_generic_store_orders_without_treating_them_as_broker_orders(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "paper.sqlite3")
    store.initialize()
    store.record_order_once("generic", "BUY", 0.1, occurred_at=UTC_0)
    broker = PaperBroker(store, CostConfig(0.0, 0.0))

    result = broker.reconcile()

    assert result.active_orders == ()
    assert result.position_state is PositionState.ENTRY_PENDING


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
