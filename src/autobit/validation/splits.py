"""Calendar-accurate, embargoed rolling walk-forward folds."""

import pandas as pd

from autobit.validation.models import FoldWindow, WalkForwardConfig


def build_rolling_folds(
    index: pd.DatetimeIndex, config: WalkForwardConfig
) -> list[FoldWindow]:
    """Build complete fixed-length folds without changing the caller's index.

    The intervals are half-open.  The embargo is intentionally absent from both
    returned performance indexes; callers may use its observations only as
    pre-OOS indicator context.
    """
    timestamps = _validated_utc_copy(index)
    if not isinstance(config, WalkForwardConfig):
        raise ValueError("config must be a WalkForwardConfig")
    if timestamps.empty:
        return []

    available_end = timestamps[-1]
    train_start = timestamps[0]
    folds: list[FoldWindow] = []

    while True:
        train_end = train_start + pd.DateOffset(years=config.train_years)
        test_start = train_end + pd.DateOffset(days=config.embargo_days)
        test_end = test_start + pd.DateOffset(months=config.test_months)
        if test_end > available_end:
            break

        train_index = _owned_interval_copy(timestamps, train_start, train_end)
        test_index = _owned_interval_copy(timestamps, test_start, test_end)
        folds.append(
            FoldWindow(
                fold_id=f"fold-{len(folds):03d}",
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                train_index=train_index,
                test_index=test_index,
            )
        )
        train_start = train_start + pd.DateOffset(months=config.step_months)

    return folds


def _validated_utc_copy(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Return an independent UTC copy after rejecting ambiguous time ordering."""
    if not isinstance(index, pd.DatetimeIndex):
        raise ValueError("index must be a pandas DatetimeIndex")
    if index.empty:
        return pd.DatetimeIndex([], tz="UTC")
    if index.tz is None:
        raise ValueError("index timestamps must be timezone-aware")
    if index.hasnans:
        raise ValueError("index must not contain NaT")
    if not index.is_unique:
        raise ValueError("index timestamps must be unique")
    if not index.is_monotonic_increasing:
        raise ValueError("index timestamps must be sorted ascending")
    try:
        return index.tz_convert("UTC").as_unit("ns").copy(deep=True)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError("index timestamps must be UTC-normalizable") from error


def _owned_interval_copy(
    timestamps: pd.DatetimeIndex, start: pd.Timestamp, end: pd.Timestamp
) -> pd.DatetimeIndex:
    """Copy a half-open interval so no fold shares an index object or buffer."""
    selected = timestamps[(timestamps >= start) & (timestamps < end)]
    return pd.DatetimeIndex(selected.asi8.copy(), tz="UTC")
