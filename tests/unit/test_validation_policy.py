from dataclasses import replace
import math

import pytest
import numpy as np

from autobit.validation.policy import ValidationInputs, classify_validation


def passing_inputs() -> ValidationInputs:
    return ValidationInputs(
        oos_net_return=0.30,
        sharpe=1.1,
        profit_factor=1.6,
        max_drawdown=0.14,
        trade_count=110,
        dsr=0.96,
        pbo=0.29,
        positive_expectancy_fold_ratio=0.65,
        max_fold_profit_share=0.49,
        train_test_sharpe_ratio=1.9,
        stress_survived=True,
    )


def test_every_pass_threshold_is_strictly_satisfied() -> None:
    assert classify_validation(passing_inputs()).status == "PASS"
    assert classify_validation(passing_inputs()).reasons == ()


@pytest.mark.parametrize(
    ("changes", "expected_status", "expected_reason"),
    (
        ({"oos_net_return": -0.0001}, "REJECT", "oos_net_return"),
        ({"sharpe": 0.0}, "REJECT", "sharpe"),
        ({"profit_factor": 1.0}, "REJECT", "profit_factor"),
        ({"max_drawdown": 0.1500001}, "REJECT", "max_drawdown"),
        ({"dsr": 0.4999999}, "REJECT", "dsr"),
        ({"pbo": 0.50}, "REJECT", "pbo"),
        ({"trade_count": 99}, "INSUFFICIENT_STATISTICS", "trade_count"),
        ({"sharpe": 1.0}, "REVIEW", "sharpe"),
        ({"profit_factor": 1.5}, "REVIEW", "profit_factor"),
        ({"dsr": 0.95}, "PASS", None),
        ({"pbo": 0.30}, "REVIEW", "pbo"),
        ({"positive_expectancy_fold_ratio": 0.5999999}, "REVIEW", "positive_expectancy_fold_ratio"),
        ({"max_fold_profit_share": 0.50}, "REVIEW", "max_fold_profit_share"),
        ({"train_test_sharpe_ratio": 2.0}, "REVIEW", "train_test_sharpe_ratio"),
        ({"stress_survived": False}, "REVIEW", "stress_survived"),
    ),
)
def test_exact_policy_boundaries(
    changes: dict[str, object], expected_status: str, expected_reason: str | None
) -> None:
    decision = classify_validation(replace(passing_inputs(), **changes))
    assert decision.status == expected_status
    if expected_reason is None:
        assert decision.reasons == ()
    else:
        assert expected_reason in decision.reasons


def test_reject_precedes_low_trade_count_and_returns_all_reasons_in_fixed_order() -> None:
    decision = classify_validation(
        replace(
            passing_inputs(),
            oos_net_return=-0.1,
            sharpe=-0.2,
            profit_factor=0.9,
            max_drawdown=0.20,
            trade_count=1,
            dsr=0.49,
            pbo=0.50,
            positive_expectancy_fold_ratio=0.1,
            max_fold_profit_share=0.8,
            train_test_sharpe_ratio=3.0,
            stress_survived=False,
        )
    )
    assert decision.status == "REJECT"
    assert decision.reasons == (
        "oos_net_return",
        "sharpe",
        "profit_factor",
        "max_drawdown",
        "trade_count",
        "dsr",
        "pbo",
        "positive_expectancy_fold_ratio",
        "max_fold_profit_share",
        "train_test_sharpe_ratio",
        "stress_survived",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("oos_net_return", math.nan),
        ("oos_net_return", -1.01),
        ("sharpe", math.inf),
        ("profit_factor", True),
        ("max_drawdown", -0.01),
        ("trade_count", 100.0),
        ("trade_count", True),
        ("dsr", 1.01),
        ("pbo", -0.01),
        ("positive_expectancy_fold_ratio", 1.01),
        ("max_fold_profit_share", -0.01),
        ("train_test_sharpe_ratio", -0.01),
        ("stress_survived", 1),
    ),
)
def test_inputs_reject_nonfinite_wrong_type_and_impossible_domains(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError, match=field):
        replace(passing_inputs(), **{field: value})


def test_inputs_and_decisions_are_immutable() -> None:
    values = passing_inputs()
    decision = classify_validation(values)
    with pytest.raises((AttributeError, TypeError)):
        values.sharpe = 9.0  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        decision.status = "REJECT"  # type: ignore[misc]


def test_numpy_real_and_integer_inputs_are_canonicalized_to_builtins() -> None:
    values = replace(
        passing_inputs(),
        oos_net_return=np.float32(0.30),
        sharpe=np.float64(1.1),
        trade_count=np.int64(110),
    )
    assert type(values.oos_net_return) is float
    assert type(values.sharpe) is float
    assert type(values.trade_count) is int


@pytest.mark.parametrize(
    ("changes", "reason"),
    (
        ({"pbo": None}, "pbo_unavailable"),
        (
            {"train_test_sharpe_ratio": None},
            "train_test_sharpe_ratio_unavailable",
        ),
    ),
)
def test_unavailable_diagnostics_force_review_with_stable_reason(
    changes: dict[str, object], reason: str
) -> None:
    decision = classify_validation(replace(passing_inputs(), **changes))
    assert decision.status == "REVIEW"
    assert reason in decision.reasons


def test_hard_reject_and_insufficient_statistics_keep_precedence_when_diagnostics_unavailable() -> None:
    rejected = classify_validation(
        replace(passing_inputs(), sharpe=0.0, pbo=None, train_test_sharpe_ratio=None)
    )
    insufficient = classify_validation(
        replace(passing_inputs(), trade_count=99, pbo=None, train_test_sharpe_ratio=None)
    )
    assert rejected.status == "REJECT"
    assert insufficient.status == "INSUFFICIENT_STATISTICS"
