"""Boundary contracts for calendar-based walk-forward folds."""

import pandas as pd
import pytest

from autobit.validation.models import WalkForwardConfig
from autobit.validation.splits import build_rolling_folds


def test_rolling_folds_use_fixed_calendar_windows_and_a_five_day_embargo() -> None:
    """Replacing calendar offsets with fixed day counts must break these boundaries."""
    index = pd.date_range(
        "2019-09-01", "2026-09-01", freq="4h", inclusive="left", tz="UTC"
    )

    folds = build_rolling_folds(index, WalkForwardConfig())

    assert len(folds) == 19
    first = folds[0]
    last = folds[-1]
    assert first.fold_id == "fold-000"
    assert first.train_start == pd.Timestamp("2019-09-01T00:00:00Z")
    assert first.train_end == pd.Timestamp("2021-09-01T00:00:00Z")
    assert first.test_start == pd.Timestamp("2021-09-06T00:00:00Z")
    assert first.test_end == pd.Timestamp("2021-12-06T00:00:00Z")
    assert last.fold_id == "fold-018"
    assert last.train_start == pd.Timestamp("2024-03-01T00:00:00Z")
    assert last.test_end == pd.Timestamp("2026-06-06T00:00:00Z")
    for fold in folds:
        assert fold.train_start < fold.train_end < fold.test_start < fold.test_end
        assert fold.test_start - fold.train_end == pd.Timedelta(days=5)
        assert fold.train_index.max() < fold.train_end
        assert fold.test_index.min() >= fold.test_start
        assert fold.test_index.max() < fold.test_end
        assert fold.train_index.intersection(fold.test_index).empty
    for previous, current in zip(folds, folds[1:]):
        assert current.test_start == previous.test_end
    all_oos_timestamps = pd.DatetimeIndex(
        [timestamp for fold in folds for timestamp in fold.test_index]
    )
    assert all_oos_timestamps.is_unique


def test_leap_day_schedule_keeps_every_oos_window_contiguous() -> None:
    """Advancing a leap-clipped train start makes the second OOS window drift."""
    index = pd.date_range(
        "2020-02-29", "2022-12-06", freq="4h", inclusive="left", tz="UTC"
    )

    folds = build_rolling_folds(index, WalkForwardConfig())

    assert [fold.fold_id for fold in folds[:3]] == ["fold-000", "fold-001", "fold-002"]
    assert [fold.test_start for fold in folds[:3]] == [
        pd.Timestamp("2022-03-05T00:00:00Z"),
        pd.Timestamp("2022-06-05T00:00:00Z"),
        pd.Timestamp("2022-09-05T00:00:00Z"),
    ]
    assert [fold.train_start for fold in folds[:3]] == [
        pd.Timestamp("2020-02-29T00:00:00Z"),
        pd.Timestamp("2020-05-31T00:00:00Z"),
        pd.Timestamp("2020-08-31T00:00:00Z"),
    ]
    assert all(current.test_start == previous.test_end for previous, current in zip(folds, folds[1:]))


def test_january_month_end_schedule_does_not_drift_at_may_or_august() -> None:
    """Independently shifting a clipped January start moves the August OOS boundary."""
    index = pd.date_range(
        "2020-01-31", "2022-12-06", freq="4h", inclusive="left", tz="UTC"
    )

    folds = build_rolling_folds(index, WalkForwardConfig())

    assert [fold.test_start for fold in folds[:3]] == [
        pd.Timestamp("2022-02-05T00:00:00Z"),
        pd.Timestamp("2022-05-05T00:00:00Z"),
        pd.Timestamp("2022-08-05T00:00:00Z"),
    ]
    assert all(current.test_start == previous.test_end for previous, current in zip(folds, folds[1:]))


def test_sparse_or_off_grid_canonical_data_is_rejected() -> None:
    """Treating missing or off-grid candles as coverage makes OOS incomplete."""
    sparse = pd.DatetimeIndex(["2020-01-01T00:00:00Z", "2020-01-01T08:00:00Z"])
    off_grid = pd.DatetimeIndex(["2020-01-01T00:00:00Z", "2020-01-01T06:00:00Z"])

    with pytest.raises(ValueError, match="4-hour"):
        build_rolling_folds(sparse, WalkForwardConfig())
    with pytest.raises(ValueError, match="4-hour"):
        build_rolling_folds(off_grid, WalkForwardConfig())


def test_coverage_end_includes_the_last_complete_half_open_fold() -> None:
    """Using the final timestamp as coverage loses a valid final four-hour bar."""
    config = WalkForwardConfig(train_years=1, embargo_days=1, test_months=1, step_months=1)
    index = pd.date_range(
        "2020-01-01T00:00:00Z",
        "2021-02-02T00:00:00Z",
        freq="4h",
        inclusive="left",
    )

    folds = build_rolling_folds(index, config)

    assert len(folds) == 1
    assert folds[0].test_end == pd.Timestamp("2021-02-02T00:00:00Z")
    assert folds[0].test_index.max() == pd.Timestamp("2021-02-01T20:00:00Z")


def test_incomplete_final_four_hour_coverage_is_not_emitted() -> None:
    """A final missing candle must prevent the otherwise matching OOS window."""
    config = WalkForwardConfig(train_years=1, embargo_days=1, test_months=1, step_months=1)
    index = pd.date_range(
        "2020-01-01T00:00:00Z", "2021-02-01T16:00:00Z", freq="4h"
    )

    assert build_rolling_folds(index, config) == []


def test_fold_indexes_are_distinct_copies_and_never_mutate_the_caller() -> None:
    """Returning slices backed by the caller's index breaks fold and caller isolation."""
    index = pd.date_range(
        "2019-09-01", "2026-09-01", freq="4h", inclusive="left", tz="UTC"
    )
    original = index.copy(deep=True)

    folds = build_rolling_folds(index, WalkForwardConfig())

    assert index.equals(original)
    assert all(fold.train_index is not index for fold in folds)
    assert all(fold.test_index is not index for fold in folds)
    assert len({id(fold.train_index) for fold in folds}) == len(folds)
    assert len({id(fold.test_index) for fold in folds}) == len(folds)


def test_aware_non_utc_index_is_normalized_without_changing_the_caller() -> None:
    """Rejecting valid aware zones or mutating their timezone loses timestamp meaning."""
    config = WalkForwardConfig(train_years=1, embargo_days=1, test_months=1, step_months=1)
    index = pd.date_range(
        "2020-01-01T09:00:00+09:00",
        "2021-02-02T09:00:00+09:00",
        freq="4h",
        inclusive="left",
    )
    original = index.copy(deep=True)

    folds = build_rolling_folds(index, config)

    assert index.equals(original)
    assert len(folds) == 1
    assert folds[0].train_start == pd.Timestamp("2020-01-01T00:00:00Z")
    assert folds[0].test_start == pd.Timestamp("2021-01-02T00:00:00Z")


@pytest.mark.parametrize(
    "index",
    [
        pd.Index(["2020-01-01T00:00:00Z"]),
        pd.DatetimeIndex(["2020-01-01T00:00:00"]),
        pd.DatetimeIndex(["2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"]),
        pd.DatetimeIndex(["2020-01-02T00:00:00Z", "2020-01-01T00:00:00Z"]),
        pd.DatetimeIndex([pd.NaT], tz="UTC"),
        pd.DatetimeIndex([]),
    ],
)
def test_invalid_indexes_fail_closed(index: pd.Index) -> None:
    """Relaxing index validation can silently produce look-ahead folds."""
    with pytest.raises(ValueError, match="index"):
        build_rolling_folds(index, WalkForwardConfig())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("train_years", 0),
        ("embargo_days", -1),
        ("test_months", 1.5),
        ("step_months", True),
    ],
)
def test_config_requires_positive_non_boolean_integers(field: str, value: object) -> None:
    """Zero, fractional, and boolean durations must not alter calendar split semantics."""
    values: dict[str, object] = {
        "train_years": 2,
        "embargo_days": 5,
        "test_months": 3,
        "step_months": 3,
    }
    values[field] = value

    with pytest.raises(ValueError, match=field):
        WalkForwardConfig(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("step_months", [1, 4])
def test_config_requires_the_oos_step_to_equal_the_oos_window(step_months: int) -> None:
    """Any unequal step either overlaps or leaves a gap between OOS windows."""
    with pytest.raises(ValueError, match="step_months"):
        WalkForwardConfig(test_months=3, step_months=step_months)


def test_empty_and_insufficient_valid_indexes_have_no_folds() -> None:
    """A fold needs a complete train, embargo, and OOS calendar span."""
    config = WalkForwardConfig(train_years=1, embargo_days=1, test_months=1, step_months=1)
    empty = pd.DatetimeIndex([], tz="UTC")
    insufficient = pd.DatetimeIndex(
        pd.date_range("2020-01-01T00:00:00Z", "2021-02-01T16:00:00Z", freq="4h")
    )

    assert build_rolling_folds(empty, config) == []
    assert build_rolling_folds(insufficient, config) == []
