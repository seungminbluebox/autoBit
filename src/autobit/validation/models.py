"""Immutable values used by walk-forward validation."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import pandas as pd

from autobit.backtest.analyzers import PerformanceMetrics
from autobit.backtest.engine import BacktestResult, EquityPoint


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


@dataclass(frozen=True, slots=True)
class TrialConfig:
    """One pre-registered strategy variant; only the registry creates these."""

    trial_id: str
    ema_period: int
    entry_period: int
    exit_period: int
    atr_period: int
    stop_atr_mult: float


@dataclass(frozen=True, slots=True)
class CostScenario:
    """One fixed, per-side execution-cost scenario."""

    cost_id: str
    fee_rate: float
    slippage_rate: float


@dataclass(frozen=True, slots=True)
class WalkForwardRun:
    """One retained train or OOS cell, including deterministic failure evidence."""

    phase: Literal["TRAIN", "OOS"]
    fold_id: str
    trial_id: str
    cost_id: str
    status: Literal["COMPLETED", "FAILED"]
    result: BacktestResult | None
    metrics: PerformanceMetrics | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReturnPoint:
    """One chronological OOS return derived within a fresh-equity fold."""

    timestamp: datetime
    value: float


@dataclass(frozen=True, slots=True)
class StitchedOOSResult:
    """Chronologically compounded OOS evidence for one trial/cost combination."""

    trial_id: str
    cost_id: str
    status: Literal["COMPLETE", "INCOMPLETE"]
    returns: tuple[ReturnPoint, ...]
    equity_curve: tuple[EquityPoint, ...]
    metrics: PerformanceMetrics | None


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    """The complete fixed run matrix and every trial/cost stitched OOS series."""

    runs: tuple[WalkForwardRun, ...]
    stitched_oos: tuple[StitchedOOSResult, ...]
