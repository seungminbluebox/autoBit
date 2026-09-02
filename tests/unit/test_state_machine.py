from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from autobit.domain.models import OrderEvent, OrderStatus, PositionState
from autobit.domain.state_machine import InvalidTransition, transition_state


def test_only_allowed_position_transitions_succeed() -> None:
    assert transition_state(PositionState.FLAT, OrderStatus.SUBMITTED) is PositionState.ENTRY_PENDING
    assert transition_state(PositionState.ENTRY_PENDING, OrderStatus.COMPLETED) is PositionState.LONG
    assert transition_state(PositionState.LONG, OrderStatus.SUBMITTED) is PositionState.EXIT_PENDING
    assert transition_state(PositionState.EXIT_PENDING, OrderStatus.COMPLETED) is PositionState.FLAT
    with pytest.raises(InvalidTransition):
        transition_state(PositionState.FLAT, OrderStatus.COMPLETED)


def test_enum_values_match_the_approved_spec_names() -> None:
    assert [state.value for state in PositionState] == [
        "FLAT",
        "ENTRY_PENDING",
        "LONG",
        "EXIT_PENDING",
        "HALTED",
    ]
    assert [status.value for status in OrderStatus] == [
        "CREATED",
        "SUBMITTED",
        "ACCEPTED",
        "PARTIAL",
        "COMPLETED",
        "CANCELED",
        "EXPIRED",
        "INSUFFICIENT_CASH",
        "REJECTED",
    ]


def test_order_event_is_immutable_and_uses_approved_defaults() -> None:
    event = OrderEvent(
        order_id="entry-1",
        idempotency_key="KRW-BTC:2026-01-01T00:00:00Z:ENTRY",
        status=OrderStatus.CREATED,
        occurred_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        side="BUY",
        requested_quantity=0.1,
    )

    assert event.filled_quantity == 0.0
    assert event.fill_price is None
    assert event.fee == 0.0
    assert event.reason is None
    with pytest.raises(FrozenInstanceError):
        event.filled_quantity = 0.1


@pytest.mark.parametrize("status", [OrderStatus.CREATED, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED])
def test_entry_pending_remains_pending_before_a_fill(status: OrderStatus) -> None:
    assert transition_state(PositionState.ENTRY_PENDING, status) is PositionState.ENTRY_PENDING


@pytest.mark.parametrize("status", [OrderStatus.PARTIAL, OrderStatus.COMPLETED])
def test_entry_fill_transitions_to_long(status: OrderStatus) -> None:
    assert transition_state(PositionState.ENTRY_PENDING, status) is PositionState.LONG


@pytest.mark.parametrize(
    "status",
    [OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.INSUFFICIENT_CASH, OrderStatus.REJECTED],
)
def test_unfilled_entry_termination_returns_to_flat(status: OrderStatus) -> None:
    assert transition_state(PositionState.ENTRY_PENDING, status) is PositionState.FLAT


@pytest.mark.parametrize("status", [OrderStatus.CREATED, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PARTIAL])
def test_exit_pending_remains_pending_until_completed(status: OrderStatus) -> None:
    assert transition_state(PositionState.EXIT_PENDING, status) is PositionState.EXIT_PENDING


@pytest.mark.parametrize(
    "status",
    [OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.INSUFFICIENT_CASH, OrderStatus.REJECTED],
)
def test_terminated_exit_returns_to_long(status: OrderStatus) -> None:
    assert transition_state(PositionState.EXIT_PENDING, status) is PositionState.LONG


@pytest.mark.parametrize("status", list(OrderStatus))
def test_halted_has_no_order_status_transition(status: OrderStatus) -> None:
    with pytest.raises(InvalidTransition):
        transition_state(PositionState.HALTED, status)
