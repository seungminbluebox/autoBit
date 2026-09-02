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


def test_leap_year_and_month_end_boundaries_use_calendar_offsets() -> None:
    """A 365-day year or 90-day month approximation shifts this February fold."""
    index = pd.DatetimeIndex(
        [
            "2020-02-29T00:00:00Z",
            "2021-02-28T00:00:00Z",
            "2022-02-27T00:00:00Z",
            "2022-03-05T00:00:00Z",
            "2022-06-04T00:00:00Z",
            "2022-06-05T00:00:00Z",
        ]
    )

    folds = build_rolling_folds(index, WalkForwardConfig())

    assert len(folds) == 1
    fold = folds[0]
    assert fold.train_end == pd.Timestamp("2022-02-28T00:00:00Z")
    assert fold.test_start == pd.Timestamp("2022-03-05T00:00:00Z")
    assert fold.test_end == pd.Timestamp("2022-06-05T00:00:00Z")
    assert fold.train_index.tolist() == [
        pd.Timestamp("2020-02-29T00:00:00Z"),
        pd.Timestamp("2021-02-28T00:00:00Z"),
        pd.Timestamp("2022-02-27T00:00:00Z"),
    ]
    assert fold.test_index.tolist() == [
        pd.Timestamp("2022-03-05T00:00:00Z"),
        pd.Timestamp("2022-06-04T00:00:00Z"),
    ]


def test_irregular_sorted_index_respects_half_open_boundaries_exactly() -> None:
    """Changing either comparison to inclusive leaks a boundary observation."""
    config = WalkForwardConfig(train_years=1, embargo_days=1, test_months=1, step_months=1)
    index = pd.DatetimeIndex(
        [
            "2020-01-31T00:00:00Z",
            "2020-11-15T00:00:00Z",
            "2021-01-30T23:59:59Z",
            "2021-01-31T00:00:00Z",
            "2021-02-01T00:00:00Z",
            "2021-02-15T00:00:00Z",
            "2021-03-01T00:00:00Z",
            "2021-03-01T23:59:59Z",
            "2021-03-02T00:00:00Z",
        ]
    )

    folds = build_rolling_folds(index, config)

    assert len(folds) == 1
    fold = folds[0]
    assert fold.train_end == pd.Timestamp("2021-01-31T00:00:00Z")
    assert fold.test_start == pd.Timestamp("2021-02-01T00:00:00Z")
    assert fold.test_end == pd.Timestamp("2021-03-01T00:00:00Z")
    assert fold.train_index.tolist() == [
        pd.Timestamp("2020-01-31T00:00:00Z"),
        pd.Timestamp("2020-11-15T00:00:00Z"),
        pd.Timestamp("2021-01-30T23:59:59Z"),
    ]
    assert fold.test_index.tolist() == [
        pd.Timestamp("2021-02-01T00:00:00Z"),
        pd.Timestamp("2021-02-15T00:00:00Z"),
    ]


def test_incomplete_last_test_window_is_not_emitted() -> None:
    """Accepting a test end beyond the observed range creates partial OOS results."""
    config = WalkForwardConfig(train_years=1, embargo_days=1, test_months=1, step_months=1)
    index = pd.DatetimeIndex(
        [
            "2020-01-01T00:00:00Z",
            "2020-12-31T00:00:00Z",
            "2021-01-02T00:00:00Z",
            "2021-01-31T23:59:59Z",
        ]
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
    index = pd.DatetimeIndex(
        [
            "2020-01-01T00:00:00-05:00",
            "2020-12-31T23:00:00-05:00",
            "2021-01-02T00:00:00-05:00",
            "2021-02-01T00:00:00-05:00",
            "2021-02-02T00:00:00-05:00",
        ]
    )
    original = index.copy(deep=True)

    folds = build_rolling_folds(index, config)

    assert index.equals(original)
    assert len(folds) == 1
    assert folds[0].train_start == pd.Timestamp("2020-01-01T05:00:00Z")
    assert folds[0].test_start == pd.Timestamp("2021-01-02T05:00:00Z")


@pytest.mark.parametrize(
    "index",
    [
        pd.Index(["2020-01-01T00:00:00Z"]),
        pd.DatetimeIndex(["2020-01-01T00:00:00"]),
        pd.DatetimeIndex(["2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"]),
        pd.DatetimeIndex(["2020-01-02T00:00:00Z", "2020-01-01T00:00:00Z"]),
        pd.DatetimeIndex([pd.NaT], tz="UTC"),
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


def test_empty_and_insufficient_valid_indexes_have_no_folds() -> None:
    """A fold needs a complete train, embargo, and OOS calendar span."""
    config = WalkForwardConfig(train_years=1, embargo_days=1, test_months=1, step_months=1)
    empty = pd.DatetimeIndex([], tz="UTC")
    insufficient = pd.DatetimeIndex(
        ["2020-01-01T00:00:00Z", "2021-02-01T23:59:59Z"]
    )

    assert build_rolling_folds(empty, config) == []
    assert build_rolling_folds(insufficient, config) == []
