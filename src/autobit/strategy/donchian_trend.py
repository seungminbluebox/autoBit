"""Pure entry, exit, and stop decisions for the approved Donchian strategy."""

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

from autobit.config import StrategyConfig


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """The immutable position values required for a trailing-stop decision."""

    entry_price: float
    initial_stop: float
    current_stop: float
    high_water: float


def evaluate_entry(
    row: pd.Series,
    *,
    is_flat: bool,
    config: StrategyConfig = StrategyConfig(),
) -> bool:
    """Return whether a valid, warmed-up breakout may open a new position."""
    if is_flat is not True:
        return False
    if "warmup_complete" in row and not _is_true_boolean(row["warmup_complete"]):
        return False
    if not _is_true_boolean(row.get("entry_data_valid", True)):
        return False

    required = (
        row.get("close"),
        row.get(f"ema_{config.ema_period}"),
        row.get("entry_high"),
        row.get("previous_close"),
        row.get("previous_entry_high"),
        row.get(f"atr_{config.atr_period}"),
    )
    if not all(_is_finite(value) for value in required):
        return False

    close, ema, entry_high, previous_close, previous_entry_high, _atr = (float(value) for value in required)
    return close > entry_high and close > ema and previous_close <= previous_entry_high


def evaluate_close_exit(row: pd.Series) -> bool:
    """Return whether a finite close has strictly crossed below the exit channel."""
    close = row.get("close")
    exit_low = row.get("exit_low")
    if not _is_finite(close) or not _is_finite(exit_low):
        return False
    return float(close) < float(exit_low)


def next_stop(
    position: PositionSnapshot,
    row: pd.Series,
    config: StrategyConfig = StrategyConfig(),
) -> float:
    """Return a monotonic trailing stop, or retain the current stop when data is invalid."""
    current_stop = position.current_stop
    if not all(
        _is_finite(value)
        for value in (position.entry_price, position.initial_stop, current_stop, position.high_water)
    ):
        return current_stop

    high = row.get("high")
    if not _is_finite(high):
        return current_stop
    high_water = max(float(position.high_water), float(high))
    risk_unit = float(position.entry_price) - float(position.initial_stop)
    if risk_unit <= 0:
        return current_stop
    if high_water < float(position.entry_price) + config.profit_activation_r * risk_unit:
        return current_stop

    atr = row.get(f"atr_{config.atr_period}")
    if not _is_finite(atr) or float(atr) <= 0:
        return current_stop
    return max(
        float(current_stop),
        float(position.entry_price),
        high_water - config.trailing_atr_mult * float(atr),
    )


def _is_finite(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _is_true_boolean(value: object) -> bool:
    return isinstance(value, (bool, np.bool_)) and bool(value)
