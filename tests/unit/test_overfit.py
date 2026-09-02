"""Statistical invariants for overfit diagnostics."""

import math

import numpy as np
import pytest

from autobit.validation.overfit import (
    cpcv_splits,
    deflated_sharpe_probability,
    probability_of_backtest_overfitting,
)


def test_cpcv_ten_choose_two_produces_forty_five_purged_paths() -> None:
    """Dropping combinations or a side of the embargo must break path invariants."""
    splits = cpcv_splits(
        n_observations=1000, n_groups=10, n_test_groups=2, embargo=30
    )

    assert len(splits) == 45
    test_counts = np.zeros(1000, dtype=int)
    for train, test in splits:
        assert train.ndim == test.ndim == 1
        assert np.array_equal(train, np.unique(train))
        assert np.array_equal(test, np.unique(test))
        assert not np.intersect1d(train, test).size
        assert train.min() >= 0 and test.min() >= 0
        assert train.max() < 1000 and test.max() < 1000
        assert np.min(np.abs(train[:, None] - test[None, :])) > 30
        test_counts[test] += 1
    assert np.array_equal(test_counts, np.full(1000, 9, dtype=int))


def test_cpcv_uses_lexicographic_contiguous_groups_and_both_embargo_sides() -> None:
    """Shuffled groups or one-sided purging would change these hand-derived paths."""
    splits = cpcv_splits(
        n_observations=12, n_groups=4, n_test_groups=1, embargo=2
    )

    assert len(splits) == 4
    assert np.array_equal(splits[0][1], np.array([0, 1, 2]))
    assert np.array_equal(splits[1][1], np.array([3, 4, 5]))
    assert np.array_equal(splits[1][0], np.array([0, 8, 9, 10, 11]))
    assert np.array_equal(splits[-1][1], np.array([9, 10, 11]))


def test_cpcv_array_split_covers_non_divisible_observations() -> None:
    """Truncating a remainder observation would make the CPCV partition incomplete."""
    splits = cpcv_splits(
        n_observations=11, n_groups=4, n_test_groups=1, embargo=1
    )

    assert [test.tolist() for _, test in splits] == [
        [0, 1, 2],
        [3, 4, 5],
        [6, 7, 8],
        [9, 10],
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("n_observations", 0),
        ("n_observations", True),
        ("n_groups", 1),
        ("n_groups", 1001),
        ("n_groups", 2.5),
        ("n_test_groups", 0),
        ("n_test_groups", 10),
        ("n_test_groups", False),
        ("embargo", 0),
        ("embargo", -1),
        ("embargo", 30.5),
    ],
)
def test_cpcv_rejects_invalid_configuration(field: str, value: object) -> None:
    """Invalid counts must fail rather than emit misleading or empty paths."""
    values: dict[str, object] = {
        "n_observations": 1000,
        "n_groups": 10,
        "n_test_groups": 2,
        "embargo": 30,
    }
    values[field] = value

    with pytest.raises(ValueError, match=field):
        cpcv_splits(**values)  # type: ignore[arg-type]


def test_cpcv_rejects_an_embargo_that_eliminates_training_data() -> None:
    """A nominal split with no possible training set is not a validation path."""
    with pytest.raises(ValueError, match="embargo"):
        cpcv_splits(
            n_observations=4, n_groups=2, n_test_groups=1, embargo=4
        )


def test_cpcv_returns_caller_independent_arrays() -> None:
    """Mutating one returned path must not corrupt siblings or a later invocation."""
    splits = cpcv_splits(
        n_observations=100, n_groups=5, n_test_groups=1, embargo=2
    )
    untouched_second_train = splits[1][0].copy()
    splits[0][0][0] = -1
    splits[0][1][0] = -1

    fresh = cpcv_splits(
        n_observations=100, n_groups=5, n_test_groups=1, embargo=2
    )

    assert np.array_equal(splits[1][0], untouched_second_train)
    assert all(np.all(train >= 0) and np.all(test >= 0) for train, test in fresh)


def test_dsr_uses_sample_moments_and_a_per_period_expected_maximum() -> None:
    """Population moments or an unscaled normal quantile change this known result."""
    returns = np.array([0.010, -0.009, 0.008, -0.007] * 250, dtype=float)

    probability = deflated_sharpe_probability(returns, num_trials=9)

    assert probability == pytest.approx(0.6273299115522453, abs=1e-12)


def test_dsr_penalizes_more_trials_monotonically() -> None:
    """A multiple-testing correction must never reward additional trials."""
    returns = np.array([0.010, -0.009, 0.008, -0.007] * 250, dtype=float)

    probabilities = [
        deflated_sharpe_probability(returns, num_trials=num_trials)
        for num_trials in (1, 2, 9, 100)
    ]

    assert all(0.0 <= value <= 1.0 for value in probabilities)
    assert probabilities == sorted(probabilities, reverse=True)
    assert probabilities[0] == pytest.approx(0.967520159639809, abs=1e-12)


@pytest.mark.parametrize(
    "returns",
    [
        np.array([], dtype=float),
        np.array([0.01], dtype=float),
        np.array([0.01, -0.01], dtype=float),
        np.array([0.01, 0.01, 0.01], dtype=float),
    ],
)
def test_dsr_has_defined_zero_for_insufficient_or_zero_variance_returns(
    returns: np.ndarray,
) -> None:
    """Undefined sample evidence must not be reported as statistical confidence."""
    assert deflated_sharpe_probability(returns, num_trials=9) == 0.0


@pytest.mark.parametrize(
    "returns",
    [
        np.array([[0.01, -0.01], [0.02, -0.02]]),
        np.array([0.01, np.nan, -0.01]),
        np.array([0.01, np.inf, -0.01]),
        np.array([1.0 + 2.0j, 2.0 + 1.0j, 3.0 + 0.0j]),
        np.array(["0.01", "-0.01", "0.02"]),
    ],
)
def test_dsr_rejects_non_finite_non_real_or_non_vector_returns(
    returns: np.ndarray,
) -> None:
    """Silently coercing malformed evidence can manufacture a finite probability."""
    with pytest.raises(ValueError, match="returns"):
        deflated_sharpe_probability(returns, num_trials=9)


@pytest.mark.parametrize("num_trials", [0, -1, 1.5, True])
def test_dsr_rejects_invalid_trial_counts(num_trials: object) -> None:
    """The Bailey expected maximum is defined only for positive integer trials."""
    with pytest.raises(ValueError, match="num_trials"):
        deflated_sharpe_probability(
            np.array([0.01, -0.01, 0.02]), num_trials=num_trials  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "returns",
    [
        np.array([1.0e308, -1.0e308, 5.0e307, -5.0e307, 1.0e307]),
        np.array([5.0e-324, -5.0e-324, 5.0e-324, -5.0e-324, 5.0e-324]),
    ],
)
def test_dsr_is_finite_for_extreme_finite_returns(returns: np.ndarray) -> None:
    """Overflow or underflow must not leak NaN from valid finite evidence."""
    probability = deflated_sharpe_probability(returns, num_trials=10**300)

    assert math.isfinite(probability)
    assert 0.0 <= probability <= 1.0


def test_pbo_is_one_when_in_sample_winners_reverse_out_of_sample() -> None:
    """Reversing every selected winner must produce positive logit on every path."""
    in_sample = np.array(
        [[3.0, 2.0, 1.0], [1.0, 3.0, 2.0], [2.0, 1.0, 3.0]]
    )
    out_of_sample = np.array(
        [[1.0, 2.0, 3.0], [3.0, 1.0, 2.0], [2.0, 3.0, 1.0]]
    )

    assert probability_of_backtest_overfitting(in_sample, out_of_sample) == 1.0


def test_pbo_is_zero_when_in_sample_winners_remain_best_out_of_sample() -> None:
    """Preserved winners must have non-positive overfitting logits."""
    scores = np.array(
        [[3.0, 2.0, 1.0], [1.0, 3.0, 2.0], [2.0, 1.0, 3.0]]
    )

    assert probability_of_backtest_overfitting(scores, scores.copy()) == 0.0


def test_pbo_ties_use_lowest_is_index_and_average_oos_rank() -> None:
    """Changing either documented tie rule would alter these two outcomes."""
    tied_is = np.ones((2, 3), dtype=float)
    first_is_worst_oos = np.array([[1.0, 2.0, 3.0], [1.0, 3.0, 2.0]])
    tied_oos = np.ones((2, 3), dtype=float)

    assert probability_of_backtest_overfitting(tied_is, first_is_worst_oos) == 1.0
    assert probability_of_backtest_overfitting(tied_is, tied_oos) == 0.0


@pytest.mark.parametrize(
    ("in_sample", "out_of_sample"),
    [
        (np.empty((0, 3)), np.empty((0, 3))),
        (np.ones((2, 1)), np.ones((2, 1))),
        (np.ones(3), np.ones(3)),
        (np.ones((2, 3)), np.ones((3, 2))),
        (np.array([[1.0, np.nan], [2.0, 3.0]]), np.ones((2, 2))),
        (np.ones((2, 2)), np.array([[1.0, np.inf], [2.0, 3.0]])),
        (
            np.array([[1.0 + 1.0j, 2.0], [2.0, 1.0]]),
            np.ones((2, 2)),
        ),
    ],
)
def test_pbo_rejects_malformed_score_matrices(
    in_sample: np.ndarray, out_of_sample: np.ndarray
) -> None:
    """Malformed score evidence must fail before ranking can hide the defect."""
    with pytest.raises(ValueError, match="score"):
        probability_of_backtest_overfitting(in_sample, out_of_sample)


def test_pbo_does_not_mutate_input_matrices() -> None:
    """Ranking must not reorder or overwrite evidence owned by the caller."""
    in_sample = np.array([[3.0, 2.0, 1.0], [1.0, 3.0, 2.0]])
    out_of_sample = np.array([[1.0, 2.0, 3.0], [3.0, 1.0, 2.0]])
    original_is = in_sample.copy()
    original_oos = out_of_sample.copy()

    probability_of_backtest_overfitting(in_sample, out_of_sample)

    assert np.array_equal(in_sample, original_is)
    assert np.array_equal(out_of_sample, original_oos)


def test_seeded_random_dsr_and_pbo_properties_are_bounded_and_finite() -> None:
    """Ordinary random samples must never escape the probability domain."""
    random = np.random.default_rng(20260902)

    for _ in range(100):
        returns = random.normal(
            loc=random.uniform(-0.02, 0.02),
            scale=random.uniform(1.0e-6, 0.05),
            size=int(random.integers(3, 500)),
        )
        dsr = deflated_sharpe_probability(
            returns, num_trials=int(random.integers(1, 1000))
        )
        rows = int(random.integers(1, 20))
        columns = int(random.integers(2, 12))
        in_sample = random.normal(size=(rows, columns))
        out_of_sample = random.normal(size=(rows, columns))
        pbo = probability_of_backtest_overfitting(in_sample, out_of_sample)

        assert math.isfinite(dsr) and 0.0 <= dsr <= 1.0
        assert math.isfinite(pbo) and 0.0 <= pbo <= 1.0
