"""Immutable facts and intents; no execution or persistence dependencies."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from autobit.risk.breakers import RiskDecision


@dataclass(frozen=True, slots=True)
class PositionContext:
    entry_price: float
    initial_stop: float
    current_stop: float
    high_water: float
    quantity: float
    held_bars: int


@dataclass(frozen=True, slots=True)
class DecisionInput:
    row: Mapping[str, object]
    cash: float
    equity: float
    position: PositionContext | None
    has_pending_order: bool
    risk: RiskDecision

    def __post_init__(self) -> None:
        object.__setattr__(self, "row", MappingProxyType(dict(self.row)))


@dataclass(frozen=True, slots=True)
class Decision:
    action: Literal["hold", "buy", "sell"]
    reason: str | None
    quantity: float
    next_stop: float | None
    risk: RiskDecision
