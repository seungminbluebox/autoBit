from dataclasses import FrozenInstanceError

import numpy as np
import pandas as pd
import pytest

from autobit.strategy.donchian_trend import (
    PositionSnapshot,
    evaluate_close_exit,
    evaluate_entry,
    next_stop,
)


def _entry_row(**overrides: object) -> pd.Series:
    values: dict[str, object] = {
        "close": 101.0,
        "ema_200": 90.0,
        "entry_high": 100.0,
        "previous_close": 99.0,
        "previous_entry_high": 100.0,
        "atr_14": 2.0,
        "entry_data_valid": True,
        "warmup_complete": True,
    }
    values.update(overrides)
    return pd.Series(values)


def test_entry_requires_flat_warm_valid_breakout_and_crossing() -> None:
    """Removing any required entry guard must prevent a buy signal."""
    assert evaluate_entry(_entry_row(), is_flat=True)
    assert not evaluate_entry(_entry_row(), is_flat=False)
    assert not evaluate_entry(_entry_row(warmup_complete=False), is_flat=True)
    assert not evaluate_entry(_entry_row(entry_data_valid=False), is_flat=True)
    assert not evaluate_entry(_entry_row(previous_close=101.0), is_flat=True)
    assert not evaluate_entry(_entry_row(close=90.0), is_flat=True)


@pytest.mark.parametrize("value", [np.nan, np.inf, -np.inf])
def test_entry_fails_closed_for_nonfinite_required_inputs(value: float) -> None:
    """A nonfinite price, indicator, or ATR cannot produce an entry decision."""
    for column in ("close", "ema_200", "entry_high", "previous_close", "previous_entry_high", "atr_14"):
        assert not evaluate_entry(_entry_row(**{column: value}), is_flat=True)


def test_close_exit_is_strict_and_fails_closed() -> None:
    """Only a finite close strictly below the prior exit channel triggers the exit."""
    assert evaluate_close_exit(pd.Series({"close": 99.0, "exit_low": 100.0}))
    assert not evaluate_close_exit(pd.Series({"close": 100.0, "exit_low": 100.0}))
    assert not evaluate_close_exit(pd.Series({"close": np.nan, "exit_low": 100.0}))
    assert not evaluate_close_exit(pd.Series({"close": 99.0, "exit_low": np.inf}))


def test_stop_activates_at_two_r_and_never_moves_down() -> None:
    """Trailing begins at exactly +2R and preserves the highest previously set stop."""
    position = PositionSnapshot(entry_price=100.0, initial_stop=90.0, current_stop=95.0, high_water=110.0)

    assert next_stop(position, pd.Series({"high": 119.0, "atr_14": 2.0})) == 95.0
    assert next_stop(position, pd.Series({"high": 120.0, "atr_14": 2.0})) == 114.0
    advanced = PositionSnapshot(entry_price=100.0, initial_stop=90.0, current_stop=116.0, high_water=120.0)
    assert next_stop(advanced, pd.Series({"high": 121.0, "atr_14": 2.0})) == 116.0


def test_stop_fails_closed_for_invalid_snapshot_or_atr() -> None:
    """A nonpositive R, bad high, or invalid ATR keeps the prior protective stop."""
    position = PositionSnapshot(entry_price=100.0, initial_stop=90.0, current_stop=95.0, high_water=120.0)

    assert next_stop(position, pd.Series({"high": np.nan, "atr_14": 2.0})) == 95.0
    assert next_stop(position, pd.Series({"high": 121.0, "atr_14": 0.0})) == 95.0
    assert next_stop(
        PositionSnapshot(entry_price=100.0, initial_stop=100.0, current_stop=95.0, high_water=120.0),
        pd.Series({"high": 121.0, "atr_14": 2.0}),
    ) == 95.0


def test_position_snapshot_is_immutable() -> None:
    """Stop calculations receive an immutable position snapshot."""
    position = PositionSnapshot(entry_price=100.0, initial_stop=90.0, current_stop=95.0, high_water=110.0)

    with pytest.raises(FrozenInstanceError):
        position.current_stop = 96.0
