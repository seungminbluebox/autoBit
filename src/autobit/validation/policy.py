"""Pre-registered, immutable OOS validation policy."""

from dataclasses import dataclass
import math
from numbers import Integral, Real
from typing import Literal


ValidationStatus = Literal["PASS", "REVIEW", "REJECT", "INSUFFICIENT_STATISTICS"]


@dataclass(frozen=True, slots=True)
class ValidationInputs:
    """The complete evidence used by the fixed post-OOS decision policy."""

    oos_net_return: float
    sharpe: float
    profit_factor: float
    max_drawdown: float
    trade_count: int
    dsr: float
    pbo: float
    positive_expectancy_fold_ratio: float
    max_fold_profit_share: float
    train_test_sharpe_ratio: float
    stress_survived: bool

    def __post_init__(self) -> None:
        oos_net_return = _require_finite_real(self.oos_net_return, "oos_net_return")
        if oos_net_return < -1.0:
            raise ValueError("oos_net_return must be at least -1")
        _require_finite_real(self.sharpe, "sharpe")
        for name in ("profit_factor", "train_test_sharpe_ratio"):
            value = _require_finite_real(getattr(self, name), name)
            if value < 0.0:
                raise ValueError(f"{name} must be nonnegative")
        for name in (
            "max_drawdown",
            "dsr",
            "pbo",
            "positive_expectancy_fold_ratio",
            "max_fold_profit_share",
        ):
            value = _require_finite_real(getattr(self, name), name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if isinstance(self.trade_count, bool) or not isinstance(self.trade_count, Integral):
            raise ValueError("trade_count must be a nonnegative integer")
        if self.trade_count < 0:
            raise ValueError("trade_count must be a nonnegative integer")
        if type(self.stress_survived) is not bool:
            raise ValueError("stress_survived must be a bool")
        for name in (
            "oos_net_return",
            "sharpe",
            "profit_factor",
            "max_drawdown",
            "dsr",
            "pbo",
            "positive_expectancy_fold_ratio",
            "max_fold_profit_share",
            "train_test_sharpe_ratio",
        ):
            object.__setattr__(self, name, float(getattr(self, name)))
        object.__setattr__(self, "trade_count", int(self.trade_count))


@dataclass(frozen=True, slots=True)
class ValidationDecision:
    """One non-overridable status and every unmet fixed threshold."""

    status: ValidationStatus
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status not in {"PASS", "REVIEW", "REJECT", "INSUFFICIENT_STATISTICS"}:
            raise ValueError("status must be a known validation status")
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(reason, str) or not reason for reason in self.reasons
        ):
            raise ValueError("reasons must be a tuple of nonempty reason codes")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("reasons must be unique")
        if self.status == "PASS" and self.reasons:
            raise ValueError("PASS cannot carry failure reasons")


def classify_validation(values: ValidationInputs) -> ValidationDecision:
    """Apply the frozen policy with REJECT taking precedence over sample size."""
    if not isinstance(values, ValidationInputs):
        raise ValueError("values must be ValidationInputs")

    failures = (
        ("oos_net_return", values.oos_net_return < 0.0),
        ("sharpe", values.sharpe <= 1.0),
        ("profit_factor", values.profit_factor <= 1.5),
        ("max_drawdown", values.max_drawdown > 0.15),
        ("trade_count", values.trade_count < 100),
        ("dsr", values.dsr < 0.95),
        ("pbo", values.pbo >= 0.30),
        (
            "positive_expectancy_fold_ratio",
            values.positive_expectancy_fold_ratio < 0.60,
        ),
        ("max_fold_profit_share", values.max_fold_profit_share >= 0.50),
        ("train_test_sharpe_ratio", values.train_test_sharpe_ratio >= 2.0),
        ("stress_survived", not values.stress_survived),
    )
    reasons = tuple(name for name, failed in failures if failed)
    hard_reject = (
        values.oos_net_return < 0.0
        or values.sharpe <= 0.0
        or values.profit_factor <= 1.0
        or values.max_drawdown > 0.15
        or values.dsr < 0.50
        or values.pbo >= 0.50
    )
    if hard_reject:
        status: ValidationStatus = "REJECT"
    elif values.trade_count < 100:
        status = "INSUFFICIENT_STATISTICS"
    elif not reasons:
        status = "PASS"
    else:
        status = "REVIEW"
    return ValidationDecision(status=status, reasons=reasons)


def _require_finite_real(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite real number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be a finite real number")
    return converted
