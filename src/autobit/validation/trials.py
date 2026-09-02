"""Frozen pre-registered strategy and execution-cost trial registries."""

from collections.abc import Sequence

from autobit.validation.models import CostScenario, TrialConfig


_TRIALS: tuple[TrialConfig, ...] = (
    TrialConfig("baseline", 200, 50, 20, 14, 2.5),
    TrialConfig("ema_150", 150, 50, 20, 14, 2.5),
    TrialConfig("ema_250", 250, 50, 20, 14, 2.5),
    TrialConfig("entry_40", 200, 40, 20, 14, 2.5),
    TrialConfig("entry_60", 200, 60, 20, 14, 2.5),
    TrialConfig("exit_15", 200, 50, 15, 14, 2.5),
    TrialConfig("exit_25", 200, 50, 25, 14, 2.5),
    TrialConfig("stop_2_0", 200, 50, 20, 14, 2.0),
    TrialConfig("stop_3_0", 200, 50, 20, 14, 3.0),
)

_COST_SCENARIOS: tuple[CostScenario, ...] = (
    CostScenario("zero", 0.0, 0.0),
    CostScenario("baseline", 0.0005, 0.0005),
    CostScenario("stress_10bps", 0.0005, 0.001),
    CostScenario("stress_20bps", 0.0005, 0.002),
)


def registered_trials() -> Sequence[TrialConfig]:
    """Return the fixed baseline-plus-eight, one-factor trial sequence."""
    return _TRIALS


def registered_cost_scenarios() -> Sequence[CostScenario]:
    """Return the fixed zero, baseline, and two execution-stress scenarios."""
    return _COST_SCENARIOS
