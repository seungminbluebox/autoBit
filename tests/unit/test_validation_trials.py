from dataclasses import FrozenInstanceError

import pytest

from autobit.validation.trials import registered_cost_scenarios, registered_trials


def test_registered_trials_are_frozen_ordered_one_factor_robustness_set() -> None:
    """Changing an ID, order, or a second factor would invalidate the preregistration."""
    trials = registered_trials()

    assert [trial.trial_id for trial in trials] == [
        "baseline",
        "ema_150",
        "ema_250",
        "entry_40",
        "entry_60",
        "exit_15",
        "exit_25",
        "stop_2_0",
        "stop_3_0",
    ]
    baseline = trials[0]
    assert (
        baseline.ema_period,
        baseline.entry_period,
        baseline.exit_period,
        baseline.atr_period,
        baseline.stop_atr_mult,
    ) == (200, 50, 20, 14, 2.5)
    for trial in trials[1:]:
        assert sum(
            (
                trial.ema_period != 200,
                trial.entry_period != 50,
                trial.exit_period != 20,
                trial.atr_period != 14,
                trial.stop_atr_mult != 2.5,
            )
        ) == 1
    with pytest.raises(FrozenInstanceError):
        baseline.trial_id = "changed"  # type: ignore[misc]


def test_registered_cost_scenarios_are_exact_and_ordered() -> None:
    """Changing a stress fee or per-side slippage would alter the published comparison."""
    assert [
        (scenario.cost_id, scenario.fee_rate, scenario.slippage_rate)
        for scenario in registered_cost_scenarios()
    ] == [
        ("zero", 0.0, 0.0),
        ("baseline", 0.0005, 0.0005),
        ("stress_10bps", 0.0005, 0.001),
        ("stress_20bps", 0.0005, 0.002),
    ]
