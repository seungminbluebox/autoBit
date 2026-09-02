# KRW-BTC Walk-Forward and Reporting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic rolling Walk-forward validation, the nine pre-registered robustness trials, DSR/PBO overfit diagnostics, cost stress comparisons, and an auditable PASS/REVIEW/REJECT report.

**Architecture:** The validation layer calls the already-tested core backtest as a black box. Split generation, trial generation, overfit statistics, and policy classification remain separate pure modules so each can be tested without executing a seven-year simulation. The CLI orchestrates these modules and writes immutable report bundles.

**Tech Stack:** Python 3.12.13, pandas 2.2+, NumPy 2+, SciPy 1.15+, pytest 9+, core `autobit` package from Plan 1

**Spec:** `docs/superpowers/specs/2026-09-02-krw-btc-trend-system-design.md`

## Global Constraints

- Complete `docs/superpowers/plans/2026-09-02-krw-btc-core-backtest.md` first.
- Random K-fold and shuffled train/test splits are forbidden.
- Rolling train is 2 calendar years, embargo is 5 calendar days, OOS test is 3 calendar months, and step is 3 calendar months.
- Every OOS fold starts with equity 100, `FLAT`, and no pending orders.
- The baseline is fixed at EMA 200, entry 50, exit 20, ATR 14, stop 2.5 ATR.
- Exactly eight one-factor sensitivity trials plus the baseline are recorded; failed trials are retained.
- OOS results may never feed back into parameter selection.
- Cost scenarios are zero, baseline 0.05% fee plus 0.05% slippage, and stress slippage 0.10% and 0.20% per side.
- Every task follows red-green-refactor TDD and ends with a focused commit.

---

## File Map Locked by This Plan

```text
src/autobit/validation/models.py         Fold, trial, and validation result values
src/autobit/validation/splits.py         Rolling calendar split generation
src/autobit/validation/trials.py         Nine pre-registered parameter combinations
src/autobit/validation/runner.py         Fold/scenario orchestration and OOS stitching
src/autobit/validation/overfit.py        DSR, CPCV paths, and PBO
src/autobit/validation/policy.py         PASS/REVIEW/REJECT classification
src/autobit/reporting/validation.py      Fold, regime, cost, and benchmark reports
src/autobit/cli.py                       Adds `walk-forward`
tests/unit/test_walk_forward_splits.py
tests/unit/test_validation_trials.py
tests/unit/test_overfit.py
tests/unit/test_validation_policy.py
tests/integration/test_walk_forward_runner.py
tests/integration/test_validation_report.py
tests/regression/test_validation_golden.py
```

### Task 1: Calendar Rolling Splits with Embargo and Fold Isolation

**Files:**
- Create: `src/autobit/validation/models.py`
- Create: `src/autobit/validation/splits.py`
- Create: `tests/unit/test_walk_forward_splits.py`

**Interfaces:**
- Consumes: UTC `DatetimeIndex`, `WalkForwardConfig`
- Produces: `list[FoldWindow]` via `build_rolling_folds(index, config)`

- [ ] **Step 1: Write failing split-boundary tests**

```python
import pandas as pd

from autobit.validation.models import WalkForwardConfig
from autobit.validation.splits import build_rolling_folds


def test_rolling_folds_are_ordered_non_overlapping_and_embargoed() -> None:
    index = pd.date_range("2019-09-01", "2026-09-01", freq="4h", inclusive="left", tz="UTC")
    folds = build_rolling_folds(index, WalkForwardConfig())
    assert len(folds) >= 18
    first = folds[0]
    assert first.train_start == pd.Timestamp("2019-09-01T00:00:00Z")
    assert first.train_end == pd.Timestamp("2021-09-01T00:00:00Z")
    assert first.test_start == pd.Timestamp("2021-09-06T00:00:00Z")
    assert first.test_end == pd.Timestamp("2021-12-06T00:00:00Z")
    for fold in folds:
        assert fold.train_start < fold.train_end < fold.test_start < fold.test_end
        assert fold.test_start - fold.train_end == pd.Timedelta(days=5)
        assert not fold.train_index.intersection(fold.test_index).size


def test_each_fold_has_new_oos_objects() -> None:
    index = pd.date_range("2019-09-01", "2026-09-01", freq="4h", inclusive="left", tz="UTC")
    folds = build_rolling_folds(index, WalkForwardConfig())
    assert len({id(fold.test_index) for fold in folds}) == len(folds)
```

- [ ] **Step 2: Run the tests and verify missing modules fail**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_walk_forward_splits.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement calendar-based split models**

Use immutable values:

```python
@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    train_years: int = 2
    embargo_days: int = 5
    test_months: int = 3
    step_months: int = 3


@dataclass(frozen=True, slots=True)
class FoldWindow:
    fold_id: str
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_index: pd.DatetimeIndex
    test_index: pd.DatetimeIndex
```

Generate boundaries with `pd.DateOffset(years=2)` and `pd.DateOffset(months=3)`, not approximate day counts. Train and test use half-open intervals. Stop when a complete test window cannot fit. Include embargo observations only as historical indicator context; exclude them from train performance and OOS returns.

- [ ] **Step 4: Run focused split tests**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_walk_forward_splits.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit split generation**

```powershell
git add src/autobit/validation/models.py src/autobit/validation/splits.py tests/unit/test_walk_forward_splits.py
git commit -m "feat: add embargoed rolling validation folds"
```

### Task 2: Pre-Registered Trials and Cost Scenario Runner

**Files:**
- Create: `src/autobit/validation/trials.py`
- Create: `src/autobit/validation/runner.py`
- Create: `tests/unit/test_validation_trials.py`
- Create: `tests/integration/test_walk_forward_runner.py`

**Interfaces:**
- Consumes: processed DataFrame, folds, core `run_backtest`
- Produces: `registered_trials() -> Sequence[TrialConfig]`, `run_walk_forward(frame: pd.DataFrame, folds: Sequence[FoldWindow], backtest_fn: BacktestFn) -> WalkForwardResult`

- [ ] **Step 1: Write failing trial registry and isolation tests**

```python
from autobit.validation.trials import registered_trials


def test_trial_registry_contains_baseline_plus_eight_one_factor_changes() -> None:
    trials = registered_trials()
    assert len(trials) == 9
    baseline = trials[0]
    assert baseline.trial_id == "baseline"
    assert (baseline.ema_period, baseline.entry_period, baseline.exit_period, baseline.stop_atr_mult) == (200, 50, 20, 2.5)
    for trial in trials[1:]:
        changed = sum((trial.ema_period != 200, trial.entry_period != 50,
                       trial.exit_period != 20, trial.stop_atr_mult != 2.5))
        assert changed == 1
    assert {trial.trial_id for trial in trials} == {
        "baseline", "ema_150", "ema_250", "entry_40", "entry_60",
        "exit_15", "exit_25", "stop_2_0", "stop_3_0",
    }
```

The integration test injects a fake backtest function that records its starting equity, starting state, trial ID, fold ID, and cost ID. Assert every OOS call starts with equity 100, state `FLAT`, no orders, and that all `folds * 9 trials * 4 cost scenarios` calls are present exactly once.

- [ ] **Step 2: Run tests and verify failure**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_validation_trials.py tests/integration/test_walk_forward_runner.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement exact trials, scenarios, and OOS stitching**

Create these trial values in fixed order:

```python
TRIALS = (
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
```

Cost IDs are `zero`, `baseline`, `stress_10bps`, and `stress_20bps`; fees remain 0.05% except in `zero`, and slippage is 0%, 0.05%, 0.10%, and 0.20%. Force-close train positions at `train_end`, discard train broker state, and instantiate a new OOS broker. Stitch only chronological OOS equity returns for aggregate metrics.

- [ ] **Step 4: Run trial and runner tests**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_validation_trials.py tests/integration/test_walk_forward_runner.py -v`

Expected: all tests PASS and no test return is used to mutate any trial config.

- [ ] **Step 5: Commit the validation runner**

```powershell
git add src/autobit/validation/trials.py src/autobit/validation/runner.py tests/unit/test_validation_trials.py tests/integration/test_walk_forward_runner.py
git commit -m "feat: run pre-registered walk-forward trials"
```

### Task 3: Deflated Sharpe Ratio, CPCV, and PBO

**Files:**
- Create: `src/autobit/validation/overfit.py`
- Create: `tests/unit/test_overfit.py`

**Interfaces:**
- Consumes: non-annualized return series and trial score matrices
- Produces: `deflated_sharpe_probability`, `cpcv_splits`, `probability_of_backtest_overfitting`

- [ ] **Step 1: Write failing statistical-invariant tests**

```python
import numpy as np

from autobit.validation.overfit import cpcv_splits, deflated_sharpe_probability, probability_of_backtest_overfitting


def test_cpcv_ten_choose_two_produces_forty_five_paths() -> None:
    splits = cpcv_splits(n_observations=1000, n_groups=10, n_test_groups=2, embargo=30)
    assert len(splits) == 45
    for train, test in splits:
        assert not set(train).intersection(test)


def test_dsr_penalizes_more_trials() -> None:
    returns = np.array([0.01, -0.002, 0.008, -0.001] * 250, dtype=float)
    one_trial = deflated_sharpe_probability(returns, num_trials=1)
    nine_trials = deflated_sharpe_probability(returns, num_trials=9)
    assert 0.0 <= nine_trials <= one_trial <= 1.0


def test_pbo_is_high_when_in_sample_winners_reverse_out_of_sample() -> None:
    is_scores = np.array([[3.0, 2.0, 1.0], [1.0, 3.0, 2.0], [2.0, 1.0, 3.0]])
    oos_scores = np.array([[1.0, 2.0, 3.0], [3.0, 1.0, 2.0], [2.0, 3.0, 1.0]])
    assert probability_of_backtest_overfitting(is_scores, oos_scores) == 1.0
```

- [ ] **Step 2: Run tests and verify failure**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_overfit.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement statistics with explicit numerical guards**

Use `scipy.stats.norm`, sample skewness and Pearson kurtosis, non-annualized Sharpe, and the expected maximum Sharpe correction for all nine trials. When `num_trials == 1`, set the null expected maximum Sharpe to zero instead of evaluating an infinite normal quantile. Return `0.0` for fewer than three returns or zero variance. Clamp probability to `[0, 1]`.

For PBO, rank higher scores as better, find each split's IS-best trial, compute its OOS relative rank, apply `log(w / (1-w))` with ranks clipped away from 0 and 1, and return the fraction with positive logit. Reject mismatched or non-finite score matrices with `ValueError`.

- [ ] **Step 4: Run overfit tests plus randomized property checks**

Add a seeded test using `np.random.default_rng(20260902)` to assert DSR and PBO remain finite and within `[0, 1]` for 100 random samples.

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_overfit.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit overfit diagnostics**

```powershell
git add src/autobit/validation/overfit.py tests/unit/test_overfit.py
git commit -m "feat: add DSR and CPCV PBO diagnostics"
```

### Task 4: Validation Policy and Regime/Fold Reports

**Files:**
- Create: `src/autobit/validation/policy.py`
- Create: `src/autobit/reporting/validation.py`
- Create: `tests/unit/test_validation_policy.py`
- Create: `tests/integration/test_validation_report.py`

**Interfaces:**
- Consumes: aggregate OOS metrics, DSR, PBO, fold contributions, stress survival
- Produces: `ValidationDecision(status, reasons)`, JSON/CSV/Markdown validation bundle

- [ ] **Step 1: Write failing policy-boundary tests**

```python
from dataclasses import replace

from autobit.validation.policy import ValidationInputs, classify_validation


def passing_inputs() -> ValidationInputs:
    return ValidationInputs(oos_net_return=0.30, sharpe=1.1, profit_factor=1.6,
                            max_drawdown=0.14, trade_count=110, dsr=0.96, pbo=0.29,
                            positive_expectancy_fold_ratio=0.65, max_fold_profit_share=0.49,
                            train_test_sharpe_ratio=1.9, stress_survived=True)


def test_all_thresholds_must_pass() -> None:
    assert classify_validation(passing_inputs()).status == "PASS"


def test_trade_count_never_gets_relaxed() -> None:
    values = passing_inputs()
    decision = classify_validation(replace(values, trade_count=99))
    assert decision.status == "INSUFFICIENT_STATISTICS"


def test_hard_failures_reject() -> None:
    values = passing_inputs()
    decision = classify_validation(replace(values, max_drawdown=0.151))
    assert decision.status == "REJECT"
    assert "max_drawdown" in decision.reasons
```

- [ ] **Step 2: Run policy tests and verify failure**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_validation_policy.py -v`

Expected: collection FAIL.

- [ ] **Step 3: Implement precedence and complete reports**

Policy precedence is `REJECT`, then `INSUFFICIENT_STATISTICS`, then `PASS`, otherwise `REVIEW`. Encode the exact thresholds from the spec. Return every failing threshold as a stable reason code instead of stopping at the first one.

Write these artifacts:

```text
validation-summary.json
fold-metrics.csv
trial-metrics.csv
cost-scenarios.csv
regime-metrics.csv
benchmark-comparison.csv
oos-equity.csv
validation-report.md
manifest.json
```

The Markdown report must lead with the decision, list every reason, separate each fold from stitched OOS, show gross versus net costs, and disclose all nine trials including failures.

- [ ] **Step 4: Run report tests**

The integration test writes a synthetic result into a temporary directory and asserts every artifact exists, `manifest.json` hashes match file contents, and `validation-report.md` contains the decision, DSR, PBO, OOS trade count, maximum fold contribution, and buy-and-hold comparison.

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/unit/test_validation_policy.py tests/integration/test_validation_report.py -v`

Expected: all tests PASS.

- [ ] **Step 5: Commit policy and reporting**

```powershell
git add src/autobit/validation/policy.py src/autobit/reporting/validation.py tests/unit/test_validation_policy.py tests/integration/test_validation_report.py
git commit -m "feat: classify and report OOS validation"
```

### Task 5: Walk-Forward CLI and End-to-End Golden Validation

**Files:**
- Modify: `src/autobit/cli.py`
- Create: `tests/integration/test_cli_walk_forward.py`
- Create: `tests/regression/test_validation_golden.py`
- Create: `tests/fixtures/validation_golden.csv`
- Create: `tests/fixtures/validation_golden_expected.json`

**Interfaces:**
- Consumes: processed seven-year-compatible OHLCV file and output directory
- Produces: `autobit walk-forward --input PATH --output DIR`

- [ ] **Step 1: Write the failing CLI contract test**

```python
from pathlib import Path

from autobit.cli import main


def test_walk_forward_cli_writes_auditable_bundle(tmp_path: Path) -> None:
    code = main(["walk-forward", "--input", "tests/fixtures/validation_golden.csv",
                 "--output", str(tmp_path)])
    assert code == 0
    assert (tmp_path / "validation-summary.json").exists()
    assert (tmp_path / "validation-report.md").exists()
    assert (tmp_path / "manifest.json").exists()
```

- [ ] **Step 2: Run the CLI test and verify the subcommand is absent**

Run: `& '.\.venv\Scripts\python.exe' -m pytest tests/integration/test_cli_walk_forward.py -v`

Expected: FAIL because `walk-forward` is not a recognized subcommand.

- [ ] **Step 3: Wire the CLI to the validated runner**

Add only `--input`, `--output`, and optional `--end-utc`. Load canonical processed data, derive calendar folds, run all nine trials and four cost scenarios, calculate DSR/PBO, classify the baseline default-cost OOS result, and write the report bundle. Return exit code 0 for a completed report regardless of PASS/REVIEW/REJECT; return nonzero only for execution or data-quality failure.

- [ ] **Step 4: Run golden validation and the complete suite**

The golden fixture spans enough synthetic calendar time to form at least two folds. Store the expected fold IDs, trial IDs, scenario IDs, stitched OOS row count, DSR rounded to 8 decimals, PBO rounded to 8 decimals, and final decision. Assert exact equality across repeated runs.

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/unit tests/integration tests/regression tests/safety -v
& '.\.venv\Scripts\python.exe' -m autobit.cli walk-forward --input tests/fixtures/validation_golden.csv --output reports/validation-smoke
```

Expected: all tests PASS and a complete validation bundle is generated.

- [ ] **Step 5: Commit the Walk-forward deliverable**

```powershell
git add src/autobit/cli.py tests/integration/test_cli_walk_forward.py tests/regression/test_validation_golden.py tests/fixtures
git commit -m "feat: deliver reproducible walk-forward validation"
```

## Plan 2 Completion Gate

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest --cov=autobit.validation --cov=autobit.reporting.validation --cov-report=term-missing -v
git status --short
```

Required result: all tests pass, exactly nine trials and four cost scenarios are disclosed, folds never overlap, the decision policy matches every threshold, and repeated golden runs are byte-stable except documented creation timestamps. Do not start Plan 3 until this gate passes.
