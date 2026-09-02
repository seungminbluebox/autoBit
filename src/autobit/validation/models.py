"""Immutable values used by walk-forward validation."""

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    """Calendar durations for fixed-length rolling validation windows."""

    train_years: int = 2
    embargo_days: int = 5
    test_months: int = 3
    step_months: int = 3

    def __post_init__(self) -> None:
        for field_name, value in (
            ("train_years", self.train_years),
            ("embargo_days", self.embargo_days),
            ("test_months", self.test_months),
            ("step_months", self.step_months),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.step_months != self.test_months:
            raise ValueError("step_months must equal test_months")


@dataclass(frozen=True, slots=True)
class FoldWindow:
    """One fixed historical window and its embargoed out-of-sample interval."""

    fold_id: str
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_index: pd.DatetimeIndex
    test_index: pd.DatetimeIndex
