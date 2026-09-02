from autobit.domain.models import OrderStatus, PositionState


class InvalidTransition(ValueError):
    """Raised when an order status cannot update the current position state."""


_TRANSITIONS: dict[tuple[PositionState, OrderStatus], PositionState] = {
    (PositionState.FLAT, OrderStatus.CREATED): PositionState.ENTRY_PENDING,
    (PositionState.FLAT, OrderStatus.SUBMITTED): PositionState.ENTRY_PENDING,
    (PositionState.ENTRY_PENDING, OrderStatus.CREATED): PositionState.ENTRY_PENDING,
    (PositionState.ENTRY_PENDING, OrderStatus.SUBMITTED): PositionState.ENTRY_PENDING,
    (PositionState.ENTRY_PENDING, OrderStatus.ACCEPTED): PositionState.ENTRY_PENDING,
    (PositionState.ENTRY_PENDING, OrderStatus.PARTIAL): PositionState.LONG,
    (PositionState.ENTRY_PENDING, OrderStatus.COMPLETED): PositionState.LONG,
    (PositionState.ENTRY_PENDING, OrderStatus.CANCELED): PositionState.FLAT,
    (PositionState.ENTRY_PENDING, OrderStatus.EXPIRED): PositionState.FLAT,
    (PositionState.ENTRY_PENDING, OrderStatus.INSUFFICIENT_CASH): PositionState.FLAT,
    (PositionState.ENTRY_PENDING, OrderStatus.REJECTED): PositionState.FLAT,
    (PositionState.LONG, OrderStatus.CREATED): PositionState.EXIT_PENDING,
    (PositionState.LONG, OrderStatus.SUBMITTED): PositionState.EXIT_PENDING,
    (PositionState.EXIT_PENDING, OrderStatus.CREATED): PositionState.EXIT_PENDING,
    (PositionState.EXIT_PENDING, OrderStatus.SUBMITTED): PositionState.EXIT_PENDING,
    (PositionState.EXIT_PENDING, OrderStatus.ACCEPTED): PositionState.EXIT_PENDING,
    (PositionState.EXIT_PENDING, OrderStatus.PARTIAL): PositionState.EXIT_PENDING,
    (PositionState.EXIT_PENDING, OrderStatus.COMPLETED): PositionState.FLAT,
    (PositionState.EXIT_PENDING, OrderStatus.CANCELED): PositionState.LONG,
    (PositionState.EXIT_PENDING, OrderStatus.EXPIRED): PositionState.LONG,
    (PositionState.EXIT_PENDING, OrderStatus.INSUFFICIENT_CASH): PositionState.LONG,
    (PositionState.EXIT_PENDING, OrderStatus.REJECTED): PositionState.LONG,
}


def transition_state(current: PositionState, status: OrderStatus) -> PositionState:
    try:
        return _TRANSITIONS[(current, status)]
    except KeyError as error:
        raise InvalidTransition(f"cannot transition {current.value} with {status.value}") from error
