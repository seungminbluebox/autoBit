"""Calendar-accurate, embargoed rolling walk-forward folds."""

import pandas as pd

from autobit.validation.models import FoldWindow, WalkForwardConfig


_CANDLE_FREQUENCY = pd.Timedelta(hours=4)


def build_rolling_folds(
    index: pd.DatetimeIndex, config: WalkForwardConfig
) -> list[FoldWindow]:
    """Build complete fixed-length folds without changing the caller's index.

    The intervals are half-open. Every train window is derived backward from
    its embargo-adjusted OOS boundary, and each adjacent fold reuses a single
    calendar-quarter boundary from the first OOS anchor. The embargo is
    intentionally absent from both returned performance indexes; callers may
    use its observations only as pre-OOS indicator context.
    """
    timestamps = _validated_utc_copy(index)
    if not isinstance(config, WalkForwardConfig):
        raise ValueError("config must be a WalkForwardConfig")
    if timestamps.empty:
        return []

    if len(timestamps) == 1:
        return []

    coverage_end = timestamps[-1] + _CANDLE_FREQUENCY
    first_train_end = _first_complete_train_end(timestamps[0], config)
    first_test_start = first_train_end + pd.DateOffset(days=config.embargo_days)
    folds: list[FoldWindow] = []
    fold_number = 0
    test_start = first_test_start

    while True:
        train_end = test_start - pd.DateOffset(days=config.embargo_days)
        train_start = train_end - pd.DateOffset(years=config.train_years)
        test_end = _quarter_boundary(first_test_start, fold_number + 1, config)
        if test_end > coverage_end:
            break

        train_index = _owned_interval_copy(timestamps, train_start, train_end)
        test_index = _owned_interval_copy(timestamps, test_start, test_end)
        if train_index.empty or test_index.empty:
            break
        folds.append(
            FoldWindow(
                fold_id=f"fold-{fold_number:03d}",
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                train_index=train_index,
                test_index=test_index,
            )
        )
        test_start = test_end
        fold_number += 1

    return folds


def _first_complete_train_end(
    coverage_start: pd.Timestamp, config: WalkForwardConfig
) -> pd.Timestamp:
    """Find the first grid boundary with a full backward calendar lookback."""
    train_end = coverage_start + pd.DateOffset(years=config.train_years)
    while train_end - pd.DateOffset(years=config.train_years) < coverage_start:
        train_end += _CANDLE_FREQUENCY
    return train_end


def _quarter_boundary(
    first_test_start: pd.Timestamp, ordinal: int, config: WalkForwardConfig
) -> pd.Timestamp:
    """Return one anchored OOS boundary without accumulating month-end clipping."""
    return first_test_start + pd.DateOffset(months=ordinal * config.step_months)


def _validated_utc_copy(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Return an independent UTC copy after rejecting ambiguous time ordering."""
    if not isinstance(index, pd.DatetimeIndex):
        raise ValueError("index must be a pandas DatetimeIndex")
    if index.tz is None:
        raise ValueError("index timestamps must be timezone-aware")
    if index.empty:
        return pd.DatetimeIndex([], tz="UTC")
    if index.hasnans:
        raise ValueError("index must not contain NaT")
    if not index.is_unique:
        raise ValueError("index timestamps must be unique")
    if not index.is_monotonic_increasing:
        raise ValueError("index timestamps must be sorted ascending")
    try:
        timestamps = index.tz_convert("UTC").as_unit("ns").copy(deep=True)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError("index timestamps must be UTC-normalizable") from error
    _validate_canonical_cadence(timestamps)
    return timestamps


def _validate_canonical_cadence(timestamps: pd.DatetimeIndex) -> None:
    """Require Plan 1's complete UTC four-hour candle sequence."""
    if (timestamps.asi8 % _CANDLE_FREQUENCY.value != 0).any():
        raise ValueError("index timestamps must align to UTC 4-hour boundaries")
    if len(timestamps) > 1 and not (
        timestamps[1:].asi8 - timestamps[:-1].asi8 == _CANDLE_FREQUENCY.value
    ).all():
        raise ValueError("index timestamps must have exact contiguous 4-hour spacing")


def _owned_interval_copy(
    timestamps: pd.DatetimeIndex, start: pd.Timestamp, end: pd.Timestamp
) -> pd.DatetimeIndex:
    """Copy a half-open interval so no fold shares an index object or buffer."""
    selected = timestamps[(timestamps >= start) & (timestamps < end)]
    return pd.DatetimeIndex(selected.asi8.copy(), tz="UTC")
