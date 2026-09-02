"""Statistical invariants for overfit diagnostics."""

import math

import numpy as np
import pytest
from scipy.special import ndtri_exp
from scipy.stats import kurtosis, norm, skew

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


def test_dsr_matches_log_tail_oracle_without_huge_trial_saturation() -> None:
    """Converting 1/N to float must not flatten distinct enormous penalties."""
    returns = np.array([1.13, -0.87] * 50_000, dtype=float)
    trial_counts = (10**324, 10**400, 10**10_000)

    actual = [
        deflated_sharpe_probability(returns, num_trials=num_trials)
        for num_trials in trial_counts
    ]
    expected = [
        _log_tail_dsr_oracle(returns, num_trials=num_trials)
        for num_trials in trial_counts
    ]

    assert actual == pytest.approx(expected, abs=1e-12)
    assert actual[0] > actual[1] > actual[2]
    assert all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in actual)
    assert math.log(10**10_000) == pytest.approx(10_000 * math.log(10.0))


@pytest.mark.parametrize("num_trials", [1, 2, 9, 10**300])
def test_dsr_log_tail_change_preserves_existing_trial_oracles(num_trials: int) -> None:
    """Replacing tail evaluation must not change representable-trial DSR results."""
    returns = np.array([1.13, -0.87] * 50_000, dtype=float)

    assert deflated_sharpe_probability(
        returns, num_trials=num_trials
    ) == pytest.approx(
        _log_tail_dsr_oracle(returns, num_trials=num_trials), abs=1e-12
    )


def test_dsr_materializes_a_one_shot_generator_exactly_once() -> None:
    """The public iterable contract must not reject or re-consume generators."""
    consumed: list[float] = []

    def return_source():
        for value in (0.010, -0.009, 0.008, -0.007) * 250:
            consumed.append(value)
            yield value

    probability = deflated_sharpe_probability(return_source(), num_trials=9)

    assert probability == pytest.approx(0.6273299115522453, abs=1e-12)
    assert len(consumed) == 1000


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
        np.array([True, False, True]),
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


def test_pbo_materializes_outer_and_row_generators_exactly_once() -> None:
    """Nested one-shot score iterables must retain rows, columns, and rank order."""
    consumed_is: list[tuple[int, int]] = []
    consumed_oos: list[tuple[int, int]] = []
    in_sample_values = ((3.0, 2.0, 1.0), (1.0, 3.0, 2.0), (2.0, 1.0, 3.0))
    out_of_sample_values = ((1.0, 2.0, 3.0), (3.0, 1.0, 2.0), (2.0, 3.0, 1.0))

    def matrix_source(values, consumed):
        for row_index, row in enumerate(values):
            def row_source(row_values=row, index=row_index):
                for column_index, value in enumerate(row_values):
                    consumed.append((index, column_index))
                    yield value

            yield row_source()

    probability = probability_of_backtest_overfitting(
        matrix_source(in_sample_values, consumed_is),
        matrix_source(out_of_sample_values, consumed_oos),
    )

    assert probability == 1.0
    assert consumed_is == [(row, column) for row in range(3) for column in range(3)]
    assert consumed_oos == [(row, column) for row in range(3) for column in range(3)]


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


def _log_tail_dsr_oracle(returns: np.ndarray, *, num_trials: int) -> float:
    """Independent SciPy-moment/log-tail oracle for the public DSR result."""
    observation_count = returns.size
    observed_sharpe = float(np.mean(returns) / np.std(returns, ddof=1))
    sample_skew = float(skew(returns, bias=False))
    pearson_kurtosis = float(kurtosis(returns, fisher=False, bias=False))
    if num_trials == 1:
        expected_maximum = 0.0
    else:
        log_trial_count = math.log(num_trials)
        expected_standard_normal = (
            (1.0 - 0.5772156649015329) * -float(ndtri_exp(-log_trial_count))
            + 0.5772156649015329 * -float(ndtri_exp(-log_trial_count - 1.0))
        )
        expected_maximum = expected_standard_normal / math.sqrt(observation_count - 1)
    standard_error = math.sqrt(
        (
            1.0
            - sample_skew * observed_sharpe
            + (pearson_kurtosis - 1.0) / 4.0 * observed_sharpe**2
        )
        / (observation_count - 1)
    )
    return float(norm.cdf((observed_sharpe - expected_maximum) / standard_error))
