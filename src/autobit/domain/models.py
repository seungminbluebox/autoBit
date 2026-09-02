from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class PositionState(str, Enum):
    FLAT = "FLAT"
    ENTRY_PENDING = "ENTRY_PENDING"
    LONG = "LONG"
    EXIT_PENDING = "EXIT_PENDING"
    HALTED = "HALTED"


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIAL = "PARTIAL"
    COMPLETED = "COMPLETED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class OrderEvent:
    order_id: str
    idempotency_key: str
    status: OrderStatus
    occurred_at_utc: datetime
    side: str
    requested_quantity: float
    filled_quantity: float = 0.0
    fill_price: float | None = None
    fee: float = 0.0
    reason: str | None = None
