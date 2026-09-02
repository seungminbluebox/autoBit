"""Pure statistical diagnostics for multiple-testing and selection overfit."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Set
from itertools import combinations
import math

import numpy as np
from scipy.special import ndtri_exp
from scipy.stats import norm


_EULER_MASCHERONI = 0.5772156649015329


def deflated_sharpe_probability(
    returns: Iterable[float] | np.ndarray, num_trials: int
) -> float:
    """Return Bailey-style DSR confidence for one non-annualized return series.

    The observed Sharpe ratio uses the sample standard deviation (``ddof=1``).
    Skewness is the adjusted Fisher-Pearson sample skew and kurtosis is Pearson
    kurtosis; for three observations, where unbiased kurtosis is undefined, the
    finite biased Pearson moment is used.  The expected maximum normal quantile
    is scaled by the null Sharpe standard error, ``1 / sqrt(T - 1)``, so it has
    the same per-observation units as the observed Sharpe and its standard error.

    Fewer than three observations and constant returns carry no usable evidence
    and therefore return ``0.0``.  Malformed or non-finite evidence is rejected.
    """
    _require_positive_integer(num_trials, "num_trials")
    values = _finite_real_array(returns, name="returns", dimensions=1)
    observation_count = values.size
    if observation_count < 3:
        return 0.0

    maximum_magnitude = float(np.max(np.abs(values)))
    if maximum_magnitude == 0.0:
        return 0.0
    scaled = values / maximum_magnitude
    mean_return = float(np.mean(scaled))
    deviations = scaled - mean_return
    second_central_moment = float(np.mean(deviations * deviations))
    if second_central_moment <= 0.0 or not math.isfinite(second_central_moment):
        return 0.0

    sample_standard_deviation = math.sqrt(
        second_central_moment * observation_count / (observation_count - 1)
    )
    if sample_standard_deviation == 0.0:
        return 0.0
    observed_sharpe = mean_return / sample_standard_deviation

    moment_scale = math.sqrt(second_central_moment)
    standardized = deviations / moment_scale
    biased_skew = float(np.mean(standardized**3))
    sample_skew = (
        math.sqrt(observation_count * (observation_count - 1))
        / (observation_count - 2)
        * biased_skew
    )
    biased_excess_kurtosis = float(np.mean(standardized**4)) - 3.0
    if observation_count > 3:
        sample_excess_kurtosis = (
            (observation_count - 1)
            / ((observation_count - 2) * (observation_count - 3))
            * ((observation_count + 1) * biased_excess_kurtosis + 6.0)
        )
        pearson_kurtosis = sample_excess_kurtosis + 3.0
    else:
        pearson_kurtosis = biased_excess_kurtosis + 3.0

    expected_maximum = _expected_maximum_sharpe(
        num_trials=num_trials, observation_count=observation_count
    )
    probability = _sharpe_probability(
        observed_sharpe=observed_sharpe,
        expected_maximum=expected_maximum,
        sample_skew=sample_skew,
        pearson_kurtosis=pearson_kurtosis,
        observation_count=observation_count,
    )
    return float(np.clip(probability, 0.0, 1.0))


def cpcv_splits(
    n_observations: int,
    n_groups: int,
    n_test_groups: int,
    embargo: int,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Build lexicographic CPCV train/test index pairs with two-sided purging.

    Observations are partitioned into contiguous groups using ``array_split``.
    Every chosen test group is retained in full.  Up to ``embargo`` adjacent
    observations on both sides of each chosen group are removed from training.
    """
    _require_positive_integer(n_observations, "n_observations")
    _require_positive_integer(n_groups, "n_groups")
    _require_positive_integer(n_test_groups, "n_test_groups")
    _require_positive_integer(embargo, "embargo")
    if n_groups < 2:
        raise ValueError("n_groups must be at least 2")
    if n_groups > n_observations:
        raise ValueError("n_groups cannot exceed n_observations")
    if n_test_groups >= n_groups:
        raise ValueError("n_test_groups must be smaller than n_groups")
    if embargo >= n_observations:
        raise ValueError("embargo must be smaller than n_observations")

    groups = tuple(
        group.copy()
        for group in np.array_split(np.arange(n_observations, dtype=np.int64), n_groups)
    )
    splits: list[tuple[np.ndarray, np.ndarray]] = []
    for test_group_ids in combinations(range(n_groups), n_test_groups):
        test = np.concatenate([groups[group_id] for group_id in test_group_ids]).copy()
        train_mask = np.ones(n_observations, dtype=bool)
        train_mask[test] = False
        for group_id in test_group_ids:
            group = groups[group_id]
            start = int(group[0])
            stop = int(group[-1]) + 1
            train_mask[max(0, start - embargo) : start] = False
            train_mask[stop : min(n_observations, stop + embargo)] = False
        train = np.flatnonzero(train_mask).astype(np.int64, copy=True)
        if train.size == 0:
            raise ValueError("embargo leaves an empty training set")
        splits.append((train, test))
    return splits


def probability_of_backtest_overfitting(
    in_sample_scores: Iterable[Iterable[float]] | np.ndarray,
    out_of_sample_scores: Iterable[Iterable[float]] | np.ndarray,
) -> float:
    """Return the fraction of paths whose selected winner has positive OOS logit.

    Higher scores are better.  Each row is a CPCV path and each column is a
    trial.  The lowest column index wins an in-sample tie.  The selected trial's
    out-of-sample rank uses average ranks for ties, mapped from best ``0`` to
    worst ``1`` before clipping and applying the logit.

    Some canonical PBO descriptions define an inverted rank/logit direction.
    This public interface deliberately follows this project's report contract:
    a **positive** logit means that the selected trial ranks worse than the OOS
    median, and the returned probability is the fraction of those positive
    logits.  Thus complete IS/OOS winner reversal returns ``1.0``.
    """
    in_sample = _finite_real_array(
        in_sample_scores, name="in-sample score matrix", dimensions=2
    )
    out_of_sample = _finite_real_array(
        out_of_sample_scores, name="out-of-sample score matrix", dimensions=2
    )
    if in_sample.shape != out_of_sample.shape:
        raise ValueError("score matrices must have identical shapes")
    path_count, trial_count = in_sample.shape
    if path_count < 1:
        raise ValueError("score matrices must contain at least one path")
    if trial_count < 2:
        raise ValueError("score matrices must contain at least two trials")

    selected_trials = np.argmax(in_sample, axis=1)
    positive_logits = 0
    rank_clip = np.finfo(np.float64).eps
    for path_index, selected_trial in enumerate(selected_trials):
        row = out_of_sample[path_index]
        selected_score = row[selected_trial]
        better_count = int(np.count_nonzero(row > selected_score))
        tied_others = int(np.count_nonzero(row == selected_score)) - 1
        average_zero_based_rank = better_count + tied_others / 2.0
        relative_rank = average_zero_based_rank / (trial_count - 1)
        clipped_rank = float(np.clip(relative_rank, rank_clip, 1.0 - rank_clip))
        logit = math.log(clipped_rank / (1.0 - clipped_rank))
        positive_logits += int(logit > 0.0)
    return positive_logits / path_count


def _require_positive_integer(value: object, name: str) -> None:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise ValueError(f"{name} must be a positive integer")
    if int(value) < 1:
        raise ValueError(f"{name} must be a positive integer")


def _finite_real_array(values: object, *, name: str, dimensions: int) -> np.ndarray:
    materialized = _materialize_nested_iterable(values, depth=dimensions)
    try:
        array = np.asarray(materialized)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a rectangular real numeric array") from error
    if array.ndim != dimensions:
        raise ValueError(f"{name} must be {dimensions}-dimensional")
    if not np.issubdtype(array.dtype, np.number) or np.iscomplexobj(array):
        raise ValueError(f"{name} must contain real numeric values")
    try:
        result = np.array(array, dtype=np.float64, copy=True)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(f"{name} must contain finite real numeric values") from error
    if not np.all(np.isfinite(result)):
        raise ValueError(f"{name} must contain only finite values")
    return result


def _materialize_nested_iterable(values: object, *, depth: int) -> object:
    if isinstance(values, (Mapping, Set)):
        raise ValueError(
            "statistical evidence requires ordered iterables, not mappings or sets"
        )
    if isinstance(values, np.ndarray):
        return values
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Iterable):
        return values
    materialized = list(values)
    if depth > 1:
        return [
            _materialize_nested_iterable(value, depth=depth - 1)
            for value in materialized
        ]
    return materialized


def _expected_maximum_sharpe(*, num_trials: int, observation_count: int) -> float:
    if num_trials == 1:
        return 0.0
    log_trial_count = math.log(num_trials)
    expected_maximum_standard_normal = (
        (1.0 - _EULER_MASCHERONI) * -float(ndtri_exp(-log_trial_count))
        + _EULER_MASCHERONI * -float(ndtri_exp(-log_trial_count - 1.0))
    )
    return expected_maximum_standard_normal / math.sqrt(observation_count - 1)


def _sharpe_probability(
    *,
    observed_sharpe: float,
    expected_maximum: float,
    sample_skew: float,
    pearson_kurtosis: float,
    observation_count: int,
) -> float:
    if not all(
        math.isfinite(value)
        for value in (observed_sharpe, expected_maximum, sample_skew, pearson_kurtosis)
    ):
        if math.isinf(observed_sharpe):
            return 1.0 if observed_sharpe > 0.0 else 0.0
        return 0.0

    scale = max(1.0, abs(observed_sharpe))
    scaled_sharpe = observed_sharpe / scale
    scaled_variance_numerator = (
        1.0 / (scale * scale)
        - sample_skew * scaled_sharpe / scale
        + (pearson_kurtosis - 1.0) / 4.0 * scaled_sharpe * scaled_sharpe
    )
    if scaled_variance_numerator < 0.0:
        scaled_variance_numerator = 0.0
    if scaled_variance_numerator == 0.0:
        if observed_sharpe > expected_maximum:
            return 1.0
        if observed_sharpe < expected_maximum:
            return 0.0
        return 0.5
    scaled_standard_error = math.sqrt(
        scaled_variance_numerator / (observation_count - 1)
    )
    z_score = (
        scaled_sharpe - expected_maximum / scale
    ) / scaled_standard_error
    if math.isnan(z_score):
        return 0.0
    return float(norm.cdf(z_score))
