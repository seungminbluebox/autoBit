"""Cost-aware normalized buy-and-hold benchmark."""

from dataclasses import dataclass
from datetime import datetime
import math

import pandas as pd

from autobit.config import CostConfig


@dataclass(frozen=True, slots=True)
class BuyAndHoldResult:
    initial_equity: float = 100.0
    entry_time: datetime | None = None
    exit_time: datetime | None = None
    entry_reference_price: float | None = None
    exit_reference_price: float | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    quantity: float = 0.0
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    total_fees: float = 0.0
    total_slippage: float = 0.0
    final_equity: float = 100.0
    total_return: float = 0.0


def run_buy_and_hold(
    frame: pd.DataFrame,
    costs: CostConfig = CostConfig(),
) -> BuyAndHoldResult:
    """Invest 100 at the first tradable next open and sell at the last close."""
    fee_rate = _cost_rate(costs.fee_rate)
    slippage_rate = _cost_rate(costs.slippage_rate)
    if not {"open", "close"}.issubset(frame.columns):
        raise ValueError("benchmark frame requires open and close columns")
    if len(frame) < 2:
        return BuyAndHoldResult()

    entry_position = next(
        (
            position
            for position in range(1, len(frame))
            if _tradable_price(frame.iloc[position]["open"])
        ),
        None,
    )
    if entry_position is None:
        return BuyAndHoldResult()
    exit_position = next(
        (
            position
            for position in range(len(frame) - 1, entry_position - 1, -1)
            if _tradable_price(frame.iloc[position]["close"])
        ),
        None,
    )
    if exit_position is None:
        return BuyAndHoldResult()

    entry_reference = float(frame.iloc[entry_position]["open"])
    exit_reference = float(frame.iloc[exit_position]["close"])
    entry_price = entry_reference * (1.0 + slippage_rate)
    exit_price = exit_reference * (1.0 - slippage_rate)
    if not math.isfinite(entry_price) or not math.isfinite(exit_price):
        raise ValueError("benchmark results must be finite")
    quantity = 100.0 / (entry_price * (1.0 + fee_rate))
    entry_notional = quantity * entry_price
    entry_fee = entry_notional * fee_rate
    cash = max(0.0, 100.0 - entry_notional - entry_fee)
    exit_notional = quantity * exit_price
    exit_fee = exit_notional * fee_rate
    final_equity = cash + exit_notional - exit_fee
    total_slippage = quantity * (
        (entry_price - entry_reference) + (exit_reference - exit_price)
    )
    if not all(
        math.isfinite(value)
        for value in (
            quantity,
            entry_fee,
            exit_fee,
            final_equity,
            total_slippage,
        )
    ):
        raise ValueError("benchmark results must be finite")

    return BuyAndHoldResult(
        entry_time=_utc_datetime(frame.index[entry_position]),
        exit_time=_utc_datetime(frame.index[exit_position]),
        entry_reference_price=entry_reference,
        exit_reference_price=exit_reference,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        total_fees=entry_fee + exit_fee,
        total_slippage=total_slippage,
        final_equity=final_equity,
        total_return=final_equity / 100.0 - 1.0,
    )


def _cost_rate(value: float) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("cost rates must be finite and in [0, 1)") from error
    if isinstance(value, bool) or not math.isfinite(converted) or not 0.0 <= converted < 1.0:
        raise ValueError("cost rates must be finite and in [0, 1)")
    return converted


def _tradable_price(value: object) -> bool:
    try:
        price = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(price) and price > 0.0


def _utc_datetime(value: object) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise ValueError("benchmark timestamps must be timezone-aware")
    return timestamp.tz_convert("UTC").to_pydatetime()
