"""Deterministic, immutable walk-forward validation report bundles."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
import hashlib
import io
from itertools import combinations
import json
import math
from numbers import Integral, Real
import os
from pathlib import Path
import shutil
from typing import Final, Literal
from uuid import uuid4

from autobit.validation.policy import (
    ValidationDecision,
    ValidationInputs,
    classify_validation,
)
from autobit.validation.overfit import (
    deflated_sharpe_probability,
    probability_of_backtest_overfitting,
)
from autobit.validation.trials import registered_cost_scenarios, registered_trials


SCHEMA_VERSION: Final = "1.0"
TRIAL_IDS: Final = tuple(trial.trial_id for trial in registered_trials())
COST_IDS: Final = tuple(cost.cost_id for cost in registered_cost_scenarios())
REGIME_IDS: Final = ("rising", "falling", "sideways")
_RECONCILIATION_TOLERANCE: Final = 1e-7
_FINITE_RATIO_SENTINEL: Final = 1e12
_NON_RECONCILED_FIELDS: Final = (
    "all_stitched_trial_cost_cells.median_holding_bars",
    "all_stitched_trial_cost_cells.exposure",
    "all_stitched_trial_cost_cells.turnover",
    "all_stitched_trial_cost_cells.cash_ratio",
    "stitched_trial_cost_cells_except_baseline_default.annualized_return",
    "stitched_trial_cost_cells_except_baseline_default.sharpe",
    "stitched_trial_cost_cells_except_baseline_default.sortino",
    "stitched_trial_cost_cells_except_baseline_default.calmar",
    "stitched_trial_cost_cells_except_baseline_default.max_drawdown",
    "stitched_trial_cost_cells_except_baseline_default.max_drawdown_duration_bars",
)
REPORT_FILENAMES: Final = (
    "validation-summary.json",
    "fold-metrics.csv",
    "trial-metrics.csv",
    "cost-scenarios.csv",
    "regime-metrics.csv",
    "benchmark-comparison.csv",
    "oos-equity.csv",
    "validation-report.md",
    "manifest.json",
)


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    """The complete fixed performance metric surface for one report cell."""

    gross_return: float
    net_return: float
    annualized_return: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float
    max_drawdown_duration_bars: int
    profit_factor: float
    expectancy: float
    win_rate: float
    average_win: float
    average_loss: float
    average_win_loss_ratio: float
    trade_count: int
    mean_holding_bars: float
    median_holding_bars: float
    exposure: float
    turnover: float
    total_fees: float
    total_slippage: float
    cash_ratio: float


@dataclass(frozen=True, slots=True)
class FoldMetricRow:
    fold_id: str
    trial_id: str
    cost_id: str
    status: Literal["COMPLETED", "FAILED"]
    error: str | None
    metrics: MetricSnapshot | None


@dataclass(frozen=True, slots=True)
class TrialMetricRow:
    trial_id: str
    cost_id: str
    status: Literal["COMPLETE", "INCOMPLETE"]
    error: str | None
    metrics: MetricSnapshot | None


@dataclass(frozen=True, slots=True)
class CostScenarioRow:
    cost_id: str
    fee_rate: float
    slippage_rate: float
    gross_return: float
    net_return: float
    total_fees: float
    total_slippage: float


@dataclass(frozen=True, slots=True)
class RegimeMetricRow:
    regime_id: Literal["rising", "falling", "sideways"]
    net_return: float
    sharpe: float
    max_drawdown: float
    trade_count: int


@dataclass(frozen=True, slots=True)
class BenchmarkComparisonRow:
    cost_id: str
    strategy_net_return: float
    buy_and_hold_net_return: float
    strategy_max_drawdown: float
    buy_and_hold_max_drawdown: float


@dataclass(frozen=True, slots=True)
class OOSEquityRow:
    timestamp: datetime
    trial_id: str
    cost_id: str
    equity: float


@dataclass(frozen=True, slots=True)
class FoldTrainingSharpe:
    """One baseline/default TRAIN Sharpe tied to a disclosed fold."""

    fold_id: str
    sharpe: float


@dataclass(frozen=True, slots=True)
class DiagnosticPathEvidence:
    """One labeled CPCV path with scores aligned to canonical trial IDs."""

    path_id: str
    test_group_ids: tuple[int, int]
    in_sample_scores: tuple[float, ...]
    out_of_sample_scores: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class ValidationDiagnosticEvidence:
    """Raw, pre-registered evidence from which DSR, PBO, and IS/OOS are derived."""

    status: Literal["COMPLETE", "INCOMPLETE"]
    error: str | None
    trial_ids: tuple[str, ...]
    paths: tuple[DiagnosticPathEvidence, ...]
    train_fold_sharpes: tuple[FoldTrainingSharpe, ...]


@dataclass(frozen=True, slots=True)
class RunFailureEvidence:
    """One deterministic TRAIN or OOS cell failure retained for audit."""

    phase: Literal["TRAIN", "OOS"]
    fold_id: str
    trial_id: str
    cost_id: str
    error: str


@dataclass(frozen=True, slots=True)
class ValidationReportInput:
    """All explicit, typed evidence needed to publish one validation report.

    Stress survival is derived only from completed baseline/stress-20bps fold
    metrics and their reconciled compounded return, never stitched path risk.
    """

    diagnostic_evidence: ValidationDiagnosticEvidence
    fold_ids: tuple[str, ...]
    fold_metrics: tuple[FoldMetricRow, ...]
    trial_metrics: tuple[TrialMetricRow, ...]
    cost_scenarios: tuple[CostScenarioRow, ...]
    regime_metrics: tuple[RegimeMetricRow, ...]
    benchmark_comparison: tuple[BenchmarkComparisonRow, ...]
    oos_equity: tuple[OOSEquityRow, ...]
    run_failures: tuple[RunFailureEvidence, ...]


@dataclass(frozen=True, slots=True)
class ValidationReportBundle:
    output_dir: Path
    summary_path: Path
    fold_metrics_path: Path
    trial_metrics_path: Path
    cost_scenarios_path: Path
    regime_metrics_path: Path
    benchmark_comparison_path: Path
    oos_equity_path: Path
    report_path: Path
    manifest_path: Path
    validation_inputs: ValidationInputs
    decision: ValidationDecision

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(self.output_dir / name for name in REPORT_FILENAMES)


_METRIC_COLUMNS: Final = tuple(field.name for field in fields(MetricSnapshot))
_FOLD_COLUMNS: Final = ("fold_id", "trial_id", "cost_id", "status", "error", *_METRIC_COLUMNS)
_TRIAL_COLUMNS: Final = ("trial_id", "cost_id", "status", "error", *_METRIC_COLUMNS)
_COST_COLUMNS: Final = tuple(field.name for field in fields(CostScenarioRow))
_REGIME_COLUMNS: Final = tuple(field.name for field in fields(RegimeMetricRow))
_BENCHMARK_COLUMNS: Final = tuple(field.name for field in fields(BenchmarkComparisonRow))
_EQUITY_COLUMNS: Final = tuple(field.name for field in fields(OOSEquityRow))


@dataclass(frozen=True, slots=True)
class _TradeLedger:
    trade_count: int
    wins: int
    losses: int
    gross_wins: float
    gross_losses: float
    net_pnl: float


@dataclass(frozen=True, slots=True)
class _PathMetrics:
    total_return: float
    annualized_return: float
    sharpe: float
    sortino: float
    max_drawdown: float
    max_drawdown_duration_bars: int
    calmar: float


def write_validation_bundle(
    output_dir: Path, report: ValidationReportInput
) -> ValidationReportBundle:
    """Validate, precompute, and atomically publish exactly nine report files."""
    output = _safe_new_output_path(output_dir)
    normalized, validation_inputs, decision = _validate_report(report)
    contents = _report_contents(normalized, validation_inputs, decision)
    if tuple(contents) != REPORT_FILENAMES[:-1]:
        raise RuntimeError("validation report file registry mismatch")
    manifest = _json_bytes(
        {
            "schema_version": SCHEMA_VERSION,
            "files": {
                name: hashlib.sha256(payload).hexdigest()
                for name, payload in contents.items()
            },
        }
    )
    all_contents = {**contents, "manifest.json": manifest}

    stage = output.with_name(f".{output.name}.validation-{uuid4().hex}.tmp")
    stage_created = False
    try:
        stage.mkdir(mode=0o700)
        stage_created = True
        for name in REPORT_FILENAMES:
            destination = stage / name
            with destination.open("xb") as handle:
                handle.write(all_contents[name])
                handle.flush()
                os.fsync(handle.fileno())
        _verify_staged_bundle(stage, all_contents)
        os.replace(stage, output)
    finally:
        if stage_created and stage.exists():
            shutil.rmtree(stage)

    return ValidationReportBundle(
        output_dir=output,
        summary_path=output / REPORT_FILENAMES[0],
        fold_metrics_path=output / REPORT_FILENAMES[1],
        trial_metrics_path=output / REPORT_FILENAMES[2],
        cost_scenarios_path=output / REPORT_FILENAMES[3],
        regime_metrics_path=output / REPORT_FILENAMES[4],
        benchmark_comparison_path=output / REPORT_FILENAMES[5],
        oos_equity_path=output / REPORT_FILENAMES[6],
        report_path=output / REPORT_FILENAMES[7],
        manifest_path=output / REPORT_FILENAMES[8],
        validation_inputs=validation_inputs,
        decision=decision,
    )


def _validate_report(
    report: object,
) -> tuple[ValidationReportInput, ValidationInputs, ValidationDecision]:
    if not isinstance(report, ValidationReportInput):
        raise ValueError("report must be ValidationReportInput")
    if not isinstance(report.fold_ids, tuple) or not report.fold_ids:
        raise ValueError("fold_ids must be a nonempty tuple")
    expected_fold_ids = tuple(f"fold-{index:03d}" for index in range(len(report.fold_ids)))
    if report.fold_ids != expected_fold_ids:
        raise ValueError("fold_ids must be unique, contiguous, and canonical")
    for name in (
        "fold_metrics", "trial_metrics", "cost_scenarios", "regime_metrics",
        "benchmark_comparison", "oos_equity", "run_failures",
    ):
        _require_tuple(getattr(report, name), name)

    expected_fold_cells = {
        (fold_id, trial_id, cost_id)
        for fold_id in report.fold_ids
        for trial_id in TRIAL_IDS
        for cost_id in COST_IDS
    }
    fold_map: dict[tuple[str, str, str], FoldMetricRow] = {}
    for row in report.fold_metrics:
        if not isinstance(row, FoldMetricRow):
            raise ValueError("fold_metrics must contain FoldMetricRow values")
        key = (row.fold_id, row.trial_id, row.cost_id)
        if key in fold_map:
            raise ValueError("fold_metrics contains duplicate matrix cells")
        _validate_cell(
            row.status, row.error, row.metrics,
            complete_status="COMPLETED", cost_id=row.cost_id,
        )
        fold_map[key] = row
    if set(fold_map) != expected_fold_cells:
        raise ValueError("fold_metrics must disclose every fold/trial/cost cell")

    expected_trial_cells = {
        (trial_id, cost_id) for trial_id in TRIAL_IDS for cost_id in COST_IDS
    }
    trial_map: dict[tuple[str, str], TrialMetricRow] = {}
    for row in report.trial_metrics:
        if not isinstance(row, TrialMetricRow):
            raise ValueError("trial_metrics must contain TrialMetricRow values")
        key = (row.trial_id, row.cost_id)
        if key in trial_map:
            raise ValueError("trial_metrics contains duplicate matrix cells")
        _validate_cell(
            row.status, row.error, row.metrics,
            complete_status="COMPLETE", cost_id=row.cost_id,
            require_failure_error=False,
        )
        trial_map[key] = row
    if set(trial_map) != expected_trial_cells:
        raise ValueError("trial_metrics must disclose the exact 9 by 4 matrix")

    for trial_id, cost_id in sorted(expected_trial_cells):
        folds = [fold_map[(fold_id, trial_id, cost_id)] for fold_id in report.fold_ids]
        stitched = trial_map[(trial_id, cost_id)]
        if any(row.status == "FAILED" for row in folds) and stitched.status != "INCOMPLETE":
            raise ValueError("a FAILED fold requires an INCOMPLETE stitched cell")
        if stitched.status == "COMPLETE":
            if any(row.metrics is None for row in folds):
                raise ValueError("a COMPLETE stitched cell requires complete fold evidence")
            _reconcile_stitched_metrics(
                tuple(row.metrics for row in folds if row.metrics is not None),
                stitched.metrics,
            )

    failures = _validate_run_failures(
        report.run_failures, fold_map, trial_map, report.fold_ids
    )

    baseline_metrics: dict[str, MetricSnapshot] = {}
    for cost_id in COST_IDS:
        stitched = trial_map[("baseline", cost_id)]
        if stitched.status != "COMPLETE" or stitched.metrics is None:
            raise ValueError("all baseline cost scenarios must have COMPLETE stitched evidence")
        baseline_metrics[cost_id] = stitched.metrics

    _validate_cost_scenarios(report.cost_scenarios, baseline_metrics)
    _validate_regimes(report.regime_metrics)
    _validate_benchmarks(report.benchmark_comparison, baseline_metrics)

    baseline_default_folds = tuple(
        fold_map[(fold_id, "baseline", "baseline")] for fold_id in report.fold_ids
    )
    if any(row.status != "COMPLETED" or row.metrics is None for row in baseline_default_folds):
        raise ValueError("baseline/default OOS fold evidence must be complete")
    equity_returns, path_metrics = _validate_equity(
        report.oos_equity, baseline_metrics["baseline"]
    )
    evidence = _validate_diagnostics(
        report.diagnostic_evidence, report.fold_ids, trial_map, failures
    )

    oos_sharpes = tuple(row.metrics.sharpe for row in baseline_default_folds if row.metrics)
    if evidence.status == "INCOMPLETE":
        train_test_ratio: float | None = None
        pbo: float | None = None
    else:
        train_sharpes = tuple(item.sharpe for item in evidence.train_fold_sharpes)
        train_mean = math.fsum(train_sharpes) / len(train_sharpes)
        oos_mean = math.fsum(oos_sharpes) / len(oos_sharpes)
        if train_mean <= 0.0:
            train_test_ratio = 0.0
        elif oos_mean <= 0.0:
            train_test_ratio = _FINITE_RATIO_SENTINEL
        else:
            train_test_ratio = min(_FINITE_RATIO_SENTINEL, train_mean / oos_mean)
        pbo = probability_of_backtest_overfitting(
            tuple(path.in_sample_scores for path in evidence.paths),
            tuple(path.out_of_sample_scores for path in evidence.paths),
        )

    fold_returns = tuple(
        row.metrics.net_return for row in baseline_default_folds if row.metrics
    )
    positive_expectancy_ratio = (
        sum(
            _trade_ledger(row.metrics).net_pnl > 0.0
            for row in baseline_default_folds if row.metrics
        )
        / len(baseline_default_folds)
    )
    max_fold_profit_share = _max_positive_fold_contribution_share(fold_returns)
    stress_folds = tuple(
        fold_map[(fold_id, "baseline", "stress_20bps")].metrics
        for fold_id in report.fold_ids
    )
    if any(metrics is None for metrics in stress_folds):
        raise ValueError("baseline/stress_20bps fold evidence must be complete")
    completed_stress_folds = tuple(
        metrics for metrics in stress_folds if metrics is not None
    )
    stress_compounded_return = _compound_returns(tuple(
        metrics.net_return for metrics in completed_stress_folds
    ))
    stress_survived = (
        stress_compounded_return > -1.0
        and all(
            metrics.net_return > -1.0 and metrics.max_drawdown < 1.0
            for metrics in completed_stress_folds
        )
    )

    inputs = ValidationInputs(
        oos_net_return=path_metrics.total_return,
        sharpe=path_metrics.sharpe,
        profit_factor=baseline_metrics["baseline"].profit_factor,
        max_drawdown=path_metrics.max_drawdown,
        trade_count=baseline_metrics["baseline"].trade_count,
        dsr=deflated_sharpe_probability(equity_returns, num_trials=len(TRIAL_IDS)),
        pbo=pbo,
        positive_expectancy_fold_ratio=positive_expectancy_ratio,
        max_fold_profit_share=max_fold_profit_share,
        train_test_sharpe_ratio=train_test_ratio,
        stress_survived=stress_survived,
    )
    normalized_folds = tuple(
        replace(row, metrics=_canonical_metrics(row.metrics))
        if row.status == "COMPLETED" and row.metrics is not None else row
        for row in report.fold_metrics
    )
    normalized_trials = tuple(
        replace(row, metrics=_canonical_metrics(row.metrics))
        if row.status == "COMPLETE" and row.metrics is not None else replace(
            row, error=_canonical_failure_error(tuple(
                failure for failure in failures
                if (failure.trial_id, failure.cost_id) == (row.trial_id, row.cost_id)
            ))
        )
        for row in report.trial_metrics
    )
    normalized_report = replace(
        report,
        diagnostic_evidence=evidence,
        fold_metrics=normalized_folds,
        trial_metrics=normalized_trials,
        run_failures=failures,
    )
    return normalized_report, inputs, classify_validation(inputs)


def _validate_cell(
    status: object,
    error: object,
    metrics: object,
    *,
    complete_status: str,
    cost_id: str,
    require_failure_error: bool = True,
) -> None:
    failed_status = "FAILED" if complete_status == "COMPLETED" else "INCOMPLETE"
    if status not in {complete_status, failed_status}:
        raise ValueError("report cell status is invalid")
    if status == complete_status:
        if error is not None or not isinstance(metrics, MetricSnapshot):
            raise ValueError("completed report cells require metrics and no error")
        _validate_metrics(metrics, cost_id=cost_id)
    else:
        valid_error = (
            isinstance(error, str) and bool(error)
            if require_failure_error
            else error is None or isinstance(error, str)
        )
        if not valid_error or metrics is not None:
            raise ValueError("failed report cells require an error and no metrics")


def _validate_metrics(metrics: MetricSnapshot, *, cost_id: str) -> None:
    for name in (
        "gross_return", "net_return", "annualized_return",
    ):
        value = _finite_real(getattr(metrics, name), f"metrics.{name}")
        if value < -1.0:
            raise ValueError(f"metrics.{name} must be at least -1")
    for name in ("sharpe", "sortino", "calmar", "expectancy"):
        _finite_real(getattr(metrics, name), f"metrics.{name}")
    for name in (
        "profit_factor", "average_win", "average_win_loss_ratio", "mean_holding_bars",
        "median_holding_bars", "turnover", "total_fees", "total_slippage",
    ):
        _nonnegative_real(getattr(metrics, name), f"metrics.{name}")
    average_loss = _finite_real(metrics.average_loss, "metrics.average_loss")
    if average_loss > 0.0:
        raise ValueError("metrics.average_loss must be nonpositive")
    for name in ("max_drawdown", "win_rate", "exposure", "cash_ratio"):
        _ratio(getattr(metrics, name), f"metrics.{name}")
    _nonnegative_integer(metrics.max_drawdown_duration_bars, "metrics.max_drawdown_duration_bars")
    _nonnegative_integer(metrics.trade_count, "metrics.trade_count")
    _trade_ledger(metrics)
    if metrics.net_return > metrics.gross_return + _RECONCILIATION_TOLERANCE:
        raise ValueError("metrics net return cannot exceed gross return")
    if cost_id == "zero" and (
        not _close(metrics.total_fees, 0.0)
        or not _close(metrics.total_slippage, 0.0)
        or not _close(metrics.gross_return, metrics.net_return)
    ):
        raise ValueError("zero cost metrics require zero costs and gross equal to net")


def _trade_ledger(metrics: MetricSnapshot) -> _TradeLedger:
    count = int(metrics.trade_count)
    if count == 0:
        for name in (
            "expectancy", "win_rate", "average_win", "average_loss",
            "profit_factor", "average_win_loss_ratio", "mean_holding_bars",
        ):
            if getattr(metrics, name) != 0.0:
                raise ValueError(f"zero-trade metrics require {name}=0")
        return _TradeLedger(0, 0, 0, 0.0, 0.0, 0.0)

    wins_value = metrics.win_rate * count
    wins = round(wins_value)
    if wins < 0 or wins > count or not _close(wins_value, wins):
        raise ValueError("trade win count implied by win_rate must be integer-like")
    if wins == 0:
        if metrics.average_win != 0.0:
            raise ValueError("zero wins require average_win=0")
    elif metrics.average_win <= 0.0:
        raise ValueError("positive wins require positive average_win")
    gross_wins = wins * metrics.average_win
    disclosed_net_pnl = metrics.expectancy * count

    if metrics.average_loss == 0.0:
        losses = 0
        gross_losses = 0.0
    elif gross_wins > 0.0:
        if metrics.profit_factor <= 0.0:
            raise ValueError("trades with wins and losses require positive profit_factor")
        disclosed_gross_losses = gross_wins / metrics.profit_factor
        losses_value = disclosed_gross_losses / abs(metrics.average_loss)
        losses = round(losses_value)
        if losses <= 0 or wins + losses > count or not _close(losses_value, losses):
            raise ValueError("trade loss count implied by profit_factor must be integer-like")
        gross_losses = losses * abs(metrics.average_loss)
        if not _close(disclosed_gross_losses, gross_losses):
            raise ValueError("profit_factor is inconsistent with average_loss and loss count")
    else:
        if metrics.profit_factor != 0.0:
            raise ValueError("zero wins require profit_factor=0")
        disclosed_gross_losses = -disclosed_net_pnl
        losses_value = disclosed_gross_losses / abs(metrics.average_loss)
        losses = round(losses_value)
        if losses <= 0 or wins + losses > count or not _close(losses_value, losses):
            raise ValueError("trade loss count implied by expectancy must be integer-like")
        gross_losses = losses * abs(metrics.average_loss)
    expected_net_pnl = gross_wins - gross_losses
    if not _close(expected_net_pnl, disclosed_net_pnl):
        raise ValueError("expectancy is inconsistent with win and loss evidence")
    expected_profit_factor = gross_wins / gross_losses if gross_losses > 0.0 else 0.0
    expected_ratio = (
        metrics.average_win / abs(metrics.average_loss)
        if metrics.average_loss < 0.0
        else 0.0
    )
    profit_factor_matches = (
        metrics.profit_factor == 0.0
        if expected_profit_factor == 0.0
        else _close(metrics.profit_factor, expected_profit_factor)
    )
    ratio_matches = (
        metrics.average_win_loss_ratio == 0.0
        if expected_ratio == 0.0
        else _close(metrics.average_win_loss_ratio, expected_ratio)
    )
    if not profit_factor_matches:
        raise ValueError("profit_factor is inconsistent with trade algebra")
    if not ratio_matches:
        raise ValueError("average_win_loss_ratio is inconsistent with trade algebra")
    return _TradeLedger(
        trade_count=count,
        wins=wins,
        losses=losses,
        gross_wins=gross_wins,
        gross_losses=gross_losses,
        net_pnl=expected_net_pnl,
    )


def _canonical_metrics(metrics: MetricSnapshot) -> MetricSnapshot:
    ledger = _trade_ledger(metrics)
    canonical_expectancy = (
        ledger.net_pnl / ledger.trade_count if ledger.trade_count else 0.0
    )
    return replace(metrics, expectancy=canonical_expectancy)


def _reconcile_stitched_metrics(
    folds: tuple[MetricSnapshot, ...], stitched: MetricSnapshot
) -> None:
    if not _close(_compound_returns(tuple(row.gross_return for row in folds)), stitched.gross_return):
        raise ValueError("fold gross_return must compound to stitched gross_return")
    if not _close(_compound_returns(tuple(row.net_return for row in folds)), stitched.net_return):
        raise ValueError("fold net_return must compound to stitched net_return")
    for name in ("total_fees", "total_slippage"):
        if not _close(math.fsum(getattr(row, name) for row in folds), getattr(stitched, name)):
            raise ValueError(f"fold {name} must sum to stitched {name}")

    ledgers = tuple(_trade_ledger(row) for row in folds)
    trade_count = sum(item.trade_count for item in ledgers)
    wins = sum(item.wins for item in ledgers)
    losses = sum(item.losses for item in ledgers)
    gross_wins = math.fsum(item.gross_wins for item in ledgers)
    gross_losses = math.fsum(item.gross_losses for item in ledgers)
    net_pnl = math.fsum(item.net_pnl for item in ledgers)
    expected: dict[str, float | int] = {
        "trade_count": trade_count,
        "expectancy": net_pnl / trade_count if trade_count else 0.0,
        "win_rate": wins / trade_count if trade_count else 0.0,
        "average_win": gross_wins / wins if wins else 0.0,
        "average_loss": -gross_losses / losses if losses else 0.0,
        "profit_factor": gross_wins / gross_losses if gross_losses else 0.0,
        "average_win_loss_ratio": (
            (gross_wins / wins) / (gross_losses / losses)
            if wins and losses and gross_losses
            else 0.0
        ),
        "mean_holding_bars": (
            math.fsum(row.mean_holding_bars * row.trade_count for row in folds)
            / trade_count
            if trade_count else 0.0
        ),
    }
    for name, value in expected.items():
        if not _close(getattr(stitched, name), value):
            raise ValueError(f"stitched {name} must be reconstructed from fold trades")


def _validate_cost_scenarios(
    rows: tuple[CostScenarioRow, ...],
    baseline_metrics: dict[str, MetricSnapshot],
) -> dict[str, CostScenarioRow]:
    cost_map: dict[str, CostScenarioRow] = {}
    registered_costs = {cost.cost_id: cost for cost in registered_cost_scenarios()}
    for row in rows:
        if not isinstance(row, CostScenarioRow) or row.cost_id in cost_map:
            raise ValueError("cost_scenarios must contain unique typed rows")
        expected = registered_costs.get(row.cost_id)
        if expected is None or not _close(row.fee_rate, expected.fee_rate) or not _close(
            row.slippage_rate, expected.slippage_rate
        ):
            raise ValueError("cost_scenarios must use the frozen execution costs")
        for name in ("gross_return", "net_return"):
            _finite_real(getattr(row, name), f"cost_scenarios.{name}")
        for name in ("fee_rate", "slippage_rate", "total_fees", "total_slippage"):
            _nonnegative_real(getattr(row, name), f"cost_scenarios.{name}")
        if row.net_return > row.gross_return + _RECONCILIATION_TOLERANCE:
            raise ValueError("cost scenario net return cannot exceed gross return")
        metrics = baseline_metrics[row.cost_id]
        for name in ("gross_return", "net_return", "total_fees", "total_slippage"):
            if not _close(getattr(row, name), getattr(metrics, name)):
                raise ValueError("cost scenario must match same-cost baseline stitched metrics")
        if row.cost_id == "zero" and (
            not _close(row.total_fees, 0.0)
            or not _close(row.total_slippage, 0.0)
            or not _close(row.gross_return, row.net_return)
        ):
            raise ValueError("zero cost scenario requires zero costs and gross equal to net")
        cost_map[row.cost_id] = row
    if set(cost_map) != set(COST_IDS):
        raise ValueError("cost_scenarios must disclose all four canonical costs")
    return cost_map


def _validate_regimes(rows: tuple[RegimeMetricRow, ...]) -> None:
    regime_map: dict[str, RegimeMetricRow] = {}
    for row in rows:
        if not isinstance(row, RegimeMetricRow) or row.regime_id in regime_map:
            raise ValueError("regime_metrics must contain unique typed rows")
        _finite_real(row.net_return, "regime_metrics.net_return")
        _finite_real(row.sharpe, "regime_metrics.sharpe")
        _ratio(row.max_drawdown, "regime_metrics.max_drawdown")
        _nonnegative_integer(row.trade_count, "regime_metrics.trade_count")
        regime_map[row.regime_id] = row
    if set(regime_map) != set(REGIME_IDS):
        raise ValueError("regime_metrics must disclose rising, falling, and sideways")


def _validate_benchmarks(
    rows: tuple[BenchmarkComparisonRow, ...],
    baseline_metrics: dict[str, MetricSnapshot],
) -> None:
    benchmark_map: dict[str, BenchmarkComparisonRow] = {}
    for row in rows:
        if not isinstance(row, BenchmarkComparisonRow) or row.cost_id in benchmark_map:
            raise ValueError("benchmark_comparison must contain unique typed rows")
        for name in ("strategy_net_return", "buy_and_hold_net_return"):
            _finite_real(getattr(row, name), f"benchmark_comparison.{name}")
        for name in ("strategy_max_drawdown", "buy_and_hold_max_drawdown"):
            _ratio(getattr(row, name), f"benchmark_comparison.{name}")
        metrics = baseline_metrics.get(row.cost_id)
        if metrics is None or not _close(row.strategy_net_return, metrics.net_return) or not _close(
            row.strategy_max_drawdown, metrics.max_drawdown
        ):
            raise ValueError("benchmark strategy values must match baseline stitched metrics")
        benchmark_map[row.cost_id] = row
    if set(benchmark_map) != set(COST_IDS):
        raise ValueError("benchmark_comparison must disclose all four canonical costs")


def _validate_equity(
    rows: tuple[OOSEquityRow, ...], baseline: MetricSnapshot
) -> tuple[tuple[float, ...], _PathMetrics]:
    if not rows:
        raise ValueError("oos_equity must not be empty")
    values: list[float] = []
    previous_timestamp: datetime | None = None
    zero_seen = False
    for row in rows:
        if not isinstance(row, OOSEquityRow):
            raise ValueError("oos_equity must contain OOSEquityRow values")
        if (row.trial_id, row.cost_id) != ("baseline", "baseline"):
            raise ValueError("oos_equity must contain exactly baseline/default stitched equity")
        timestamp = _utc(row.timestamp)
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            raise ValueError("oos_equity timestamps must be strictly increasing")
        value = _finite_real(row.equity, "oos_equity.equity")
        if value < 0.0:
            raise ValueError("oos_equity.equity must be nonnegative")
        if zero_seen and value > 0.0:
            raise ValueError("oos_equity cannot recover after reaching zero")
        zero_seen = zero_seen or value == 0.0
        values.append(value)
        previous_timestamp = timestamp
    if not _close(values[0], 100.0):
        raise ValueError("oos_equity must start at 100")
    returns = tuple(
        0.0 if previous == 0.0 else current / previous - 1.0
        for previous, current in zip(values, values[1:])
    )
    path = _calculate_path_metrics(values, returns)
    comparisons: tuple[tuple[str, float | int, float | int], ...] = (
        ("net_return", path.total_return, baseline.net_return),
        ("annualized_return", path.annualized_return, baseline.annualized_return),
        ("sharpe", path.sharpe, baseline.sharpe),
        ("sortino", path.sortino, baseline.sortino),
        ("max_drawdown", path.max_drawdown, baseline.max_drawdown),
        (
            "max_drawdown_duration_bars",
            path.max_drawdown_duration_bars,
            baseline.max_drawdown_duration_bars,
        ),
        ("calmar", path.calmar, baseline.calmar),
    )
    for name, derived, disclosed in comparisons:
        if not _close(derived, disclosed):
            raise ValueError(f"baseline stitched {name} must match OOS equity")
    return returns, path


def _calculate_path_metrics(
    equity: list[float], returns: tuple[float, ...]
) -> _PathMetrics:
    total_return = equity[-1] / equity[0] - 1.0
    if not returns or total_return == 0.0:
        annualized = 0.0
    elif total_return <= -1.0:
        annualized = -1.0
    else:
        annualized = (1.0 + total_return) ** (2190 / len(returns)) - 1.0
    mean = math.fsum(returns) / len(returns) if returns else 0.0
    variance = (
        math.fsum((value - mean) ** 2 for value in returns) / len(returns)
        if returns else 0.0
    )
    deviation = math.sqrt(max(0.0, variance))
    sharpe = mean / deviation * math.sqrt(2190) if deviation > 0.0 else 0.0
    downside = (
        math.sqrt(math.fsum(min(value, 0.0) ** 2 for value in returns) / len(returns))
        if returns else 0.0
    )
    sortino = mean / downside * math.sqrt(2190) if downside > 0.0 else 0.0
    peak = equity[0]
    maximum_drawdown = 0.0
    duration = 0
    maximum_duration = 0
    for value in equity[1:]:
        if value >= peak:
            peak = value
            duration = 0
        else:
            duration += 1
            maximum_duration = max(maximum_duration, duration)
            maximum_drawdown = max(maximum_drawdown, (peak - value) / peak)
    calmar = annualized / maximum_drawdown if maximum_drawdown > 0.0 else 0.0
    return _PathMetrics(
        total_return=total_return,
        annualized_return=annualized,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=maximum_drawdown,
        max_drawdown_duration_bars=maximum_duration,
        calmar=calmar,
    )


def _validate_run_failures(
    failures: tuple[RunFailureEvidence, ...],
    fold_map: dict[tuple[str, str, str], FoldMetricRow],
    trial_map: dict[tuple[str, str], TrialMetricRow],
    fold_ids: tuple[str, ...],
) -> tuple[RunFailureEvidence, ...]:
    seen: dict[tuple[str, str, str, str], RunFailureEvidence] = {}
    for failure in failures:
        if not isinstance(failure, RunFailureEvidence):
            raise ValueError("run_failures must contain RunFailureEvidence values")
        if failure.phase not in {"TRAIN", "OOS"}:
            raise ValueError("failure phase must be TRAIN or OOS")
        if failure.fold_id not in fold_ids or failure.trial_id not in TRIAL_IDS or failure.cost_id not in COST_IDS:
            raise ValueError("failure must map to a canonical run cell")
        if not isinstance(failure.error, str) or not failure.error:
            raise ValueError("failure error must be nonempty")
        key = (failure.phase, failure.fold_id, failure.trial_id, failure.cost_id)
        if key in seen:
            raise ValueError("run failures must be unique")
        seen[key] = failure

    expected_oos = {
        (row.fold_id, row.trial_id, row.cost_id): row.error
        for row in fold_map.values()
        if row.status == "FAILED"
    }
    actual_oos = {
        (item.fold_id, item.trial_id, item.cost_id): item.error
        for item in failures
        if item.phase == "OOS"
    }
    if actual_oos != expected_oos:
        raise ValueError("OOS failure evidence must match FAILED fold rows exactly")

    for (trial_id, cost_id), stitched in trial_map.items():
        related = tuple(
            item for item in failures
            if (item.trial_id, item.cost_id) == (trial_id, cost_id)
        )
        if stitched.status == "COMPLETE" and related:
            raise ValueError("COMPLETE stitched cells cannot have a run failure")
        if stitched.status == "INCOMPLETE" and not related:
            raise ValueError("INCOMPLETE stitched cells require structured failure evidence")
    return tuple(sorted(
        failures,
        key=lambda item: (
            _fold_ordinal(item.fold_id), _trial_ordinal(item.trial_id),
            _cost_ordinal(item.cost_id), 0 if item.phase == "TRAIN" else 1,
        ),
    ))


def _canonical_failure_error(
    failures: tuple[RunFailureEvidence, ...],
) -> str:
    """Render one stable description from structured failure evidence."""
    if not failures:
        raise ValueError("canonical failure error requires structured evidence")
    ordered = sorted(
        failures,
        key=lambda item: (
            _fold_ordinal(item.fold_id), _trial_ordinal(item.trial_id),
            _cost_ordinal(item.cost_id), 0 if item.phase == "TRAIN" else 1,
        ),
    )
    return "; ".join(
        f"[{item.phase} {item.fold_id} {item.trial_id}/{item.cost_id}] {item.error}"
        for item in ordered
    )


def _validate_diagnostics(
    evidence: object,
    fold_ids: tuple[str, ...],
    trial_map: dict[tuple[str, str], TrialMetricRow],
    failures: tuple[RunFailureEvidence, ...],
) -> ValidationDiagnosticEvidence:
    if not isinstance(evidence, ValidationDiagnosticEvidence):
        raise ValueError("diagnostic_evidence must be ValidationDiagnosticEvidence")
    if evidence.trial_ids != TRIAL_IDS:
        raise ValueError("diagnostic trial_ids must exactly match canonical trial order")
    if not isinstance(evidence.train_fold_sharpes, tuple):
        raise ValueError("train_fold_sharpes must be an immutable tuple")
    seen: dict[str, FoldTrainingSharpe] = {}
    for item in evidence.train_fold_sharpes:
        if not isinstance(item, FoldTrainingSharpe) or item.fold_id in seen:
            raise ValueError("train_fold_sharpes must contain unique typed rows")
        _finite_real(item.sharpe, "train_fold_sharpes.sharpe")
        seen[item.fold_id] = item
    if tuple(item.fold_id for item in evidence.train_fold_sharpes) != fold_ids:
        raise ValueError("train_fold_sharpes must exactly cover canonical fold order")

    baseline_cost_incomplete = any(
        trial_map[(trial_id, "baseline")].status == "INCOMPLETE"
        for trial_id in TRIAL_IDS
    ) or any(failure.cost_id == "baseline" for failure in failures)
    expected_status = "INCOMPLETE" if baseline_cost_incomplete else "COMPLETE"
    if evidence.status != expected_status:
        raise ValueError("diagnostic status must follow baseline-cost trial completeness")
    if evidence.status == "INCOMPLETE":
        if evidence.error is not None and not isinstance(evidence.error, str):
            raise ValueError("incomplete diagnostic error must be text or None")
        if evidence.paths != ():
            raise ValueError("incomplete diagnostics cannot contain fabricated PBO paths")
        relevant_failures = tuple(
            failure for failure in failures if failure.cost_id == "baseline"
        )
        return replace(
            evidence, error=_canonical_failure_error(relevant_failures)
        )
    if evidence.error is not None:
        raise ValueError("complete diagnostics cannot carry an error")
    if not isinstance(evidence.paths, tuple) or len(evidence.paths) != 45:
        raise ValueError("complete diagnostics require exactly 45 labeled paths")
    expected_groups = tuple(combinations(range(10), 2))
    for index, (path, test_groups) in enumerate(zip(evidence.paths, expected_groups, strict=True)):
        if not isinstance(path, DiagnosticPathEvidence):
            raise ValueError("diagnostic paths must be typed")
        if path.path_id != f"path-{index:03d}" or path.test_group_ids != test_groups:
            raise ValueError("diagnostic path labels must match canonical CPCV order")
        for name in ("in_sample_scores", "out_of_sample_scores"):
            scores = getattr(path, name)
            if not isinstance(scores, tuple) or len(scores) != len(TRIAL_IDS):
                raise ValueError("diagnostic path scores must align to all nine trials")
            for value in scores:
                _finite_real(value, f"diagnostic path {name}")
    return evidence


def _compound_returns(returns: tuple[float, ...]) -> float:
    result = 1.0
    for value in returns:
        result *= 1.0 + value
    return result - 1.0


def _max_positive_fold_contribution_share(returns: tuple[float, ...]) -> float:
    """Use chronological compounded positive PnL contributions; no profit means 1."""
    capital = 1.0
    positive: list[float] = []
    for value in returns:
        contribution = capital * value
        if contribution > 0.0:
            positive.append(contribution)
        capital *= 1.0 + value
    total = math.fsum(positive)
    return max(positive) / total if total > 0.0 else 1.0


def _close(left: Real, right: Real) -> bool:
    return math.isclose(
        _finite_real(left, "reconciliation value"),
        _finite_real(right, "reconciliation value"),
        rel_tol=_RECONCILIATION_TOLERANCE,
        abs_tol=_RECONCILIATION_TOLERANCE,
    )


def _stress_survival_evidence(report: ValidationReportInput) -> dict[str, object]:
    rows = sorted(
        (
            row for row in report.fold_metrics
            if (row.trial_id, row.cost_id) == ("baseline", "stress_20bps")
        ),
        key=lambda row: _fold_ordinal(row.fold_id),
    )
    metrics = tuple(row.metrics for row in rows if row.metrics is not None)
    compounded = _compound_returns(tuple(item.net_return for item in metrics))
    survived = (
        len(metrics) == len(rows)
        and compounded > -1.0
        and all(item.net_return > -1.0 and item.max_drawdown < 1.0 for item in metrics)
    )
    return {
        "source": "canonical completed baseline/stress_20bps folds",
        "conditions": [
            "each fold net_return > -1",
            "each fold max_drawdown < 1",
            "reconciled compounded net_return > -1",
        ],
        "folds": [
            {
                "fold_id": row.fold_id,
                "net_return": row.metrics.net_return,
                "max_drawdown": row.metrics.max_drawdown,
            }
            for row in rows if row.metrics is not None
        ],
        "compounded_net_return": compounded,
        "stitched_path_risk_used": False,
        "survived": survived,
    }


def _report_contents(
    report: ValidationReportInput,
    inputs: ValidationInputs,
    decision: ValidationDecision,
) -> dict[str, bytes]:
    fold_rows = sorted(report.fold_metrics, key=lambda row: (_fold_ordinal(row.fold_id), _trial_ordinal(row.trial_id), _cost_ordinal(row.cost_id)))
    trial_rows = sorted(report.trial_metrics, key=lambda row: (_trial_ordinal(row.trial_id), _cost_ordinal(row.cost_id)))
    cost_rows = sorted(report.cost_scenarios, key=lambda row: _cost_ordinal(row.cost_id))
    regime_rows = sorted(report.regime_metrics, key=lambda row: REGIME_IDS.index(row.regime_id))
    benchmark_rows = sorted(report.benchmark_comparison, key=lambda row: _cost_ordinal(row.cost_id))
    equity_rows = sorted(report.oos_equity, key=lambda row: (_trial_ordinal(row.trial_id), _cost_ordinal(row.cost_id), _utc(row.timestamp)))

    summary = {
        "schema_version": SCHEMA_VERSION,
        "decision": {
            "status": decision.status,
            "reasons": list(decision.reasons),
        },
        "diagnostics": asdict(inputs),
        "fold_ids": list(report.fold_ids),
        "trial_ids": list(TRIAL_IDS),
        "cost_ids": list(COST_IDS),
        "diagnostic_evidence": asdict(report.diagnostic_evidence),
        "run_failures": [
            asdict(item)
            for item in sorted(
                report.run_failures,
                key=lambda item: (
                    _fold_ordinal(item.fold_id), _trial_ordinal(item.trial_id),
                    _cost_ordinal(item.cost_id),
                    0 if item.phase == "TRAIN" else 1,
                ),
            )
        ],
        "non_reconciled_fields": list(_NON_RECONCILED_FIELDS),
        "stress_survival_evidence": _stress_survival_evidence(report),
        "fold_failures": [
            {
                "fold_id": row.fold_id,
                "trial_id": row.trial_id,
                "cost_id": row.cost_id,
                "status": row.status,
                "error": row.error,
            }
            for row in fold_rows if row.status == "FAILED"
        ],
        "incomplete_trial_cells": [
            {"trial_id": row.trial_id, "cost_id": row.cost_id, "status": row.status, "error": row.error}
            for row in trial_rows if row.status != "COMPLETE"
        ],
    }
    return {
        "validation-summary.json": _json_bytes(summary),
        "fold-metrics.csv": _csv_bytes(_FOLD_COLUMNS, (_metric_row(row) for row in fold_rows)),
        "trial-metrics.csv": _csv_bytes(_TRIAL_COLUMNS, (_metric_row(row) for row in trial_rows)),
        "cost-scenarios.csv": _csv_bytes(_COST_COLUMNS, (asdict(row) for row in cost_rows)),
        "regime-metrics.csv": _csv_bytes(_REGIME_COLUMNS, (asdict(row) for row in regime_rows)),
        "benchmark-comparison.csv": _csv_bytes(_BENCHMARK_COLUMNS, (asdict(row) for row in benchmark_rows)),
        "oos-equity.csv": _csv_bytes(_EQUITY_COLUMNS, (asdict(row) for row in equity_rows)),
        "validation-report.md": _markdown(
            inputs, decision, fold_rows, trial_rows, cost_rows,
            regime_rows, benchmark_rows, report.diagnostic_evidence,
        ).encode("utf-8"),
    }


def _metric_row(row: FoldMetricRow | TrialMetricRow) -> dict[str, object]:
    result = {
        field.name: getattr(row, field.name)
        for field in fields(row)
        if field.name != "metrics"
    }
    result.update(
        {name: getattr(row.metrics, name) if row.metrics is not None else None for name in _METRIC_COLUMNS}
    )
    return result


def _markdown(
    inputs: ValidationInputs,
    decision: ValidationDecision,
    fold_rows: list[FoldMetricRow],
    trial_rows: list[TrialMetricRow],
    cost_rows: list[CostScenarioRow],
    regime_rows: list[RegimeMetricRow],
    benchmark_rows: list[BenchmarkComparisonRow],
    diagnostic_evidence: ValidationDiagnosticEvidence,
) -> str:
    lines = [
        f"# Decision: {decision.status}",
        "",
        "## Reasons",
        "",
        *(f"- `{reason}`" for reason in decision.reasons),
    ]
    if not decision.reasons:
        lines.append("- None")
    lines.extend(
        [
            "", "## Diagnostics", "",
            f"- OOS net return: {_number(inputs.oos_net_return)}",
            f"- OOS Sharpe: {_number(inputs.sharpe)}",
            f"- Profit factor: {_number(inputs.profit_factor)}",
            f"- Maximum drawdown: {_number(inputs.max_drawdown)}",
            f"- DSR: {_number(inputs.dsr)}",
            f"- PBO: {_optional_number(inputs.pbo)}",
            f"- OOS trade count: {inputs.trade_count}",
            f"- Positive-expectancy fold ratio: {_number(inputs.positive_expectancy_fold_ratio)}",
            f"- Maximum fold contribution: {_number(inputs.max_fold_profit_share)}",
            f"- IS/OOS Sharpe ratio: {_optional_number(inputs.train_test_sharpe_ratio)}",
            f"- 20 bps stress survival: {str(inputs.stress_survived).lower()}",
            "- 20 bps stress source: canonical completed baseline/stress_20bps folds; "
            "stitched stress path-risk fields are not used",
            f"- Diagnostic evidence status: {diagnostic_evidence.status}",
            f"- Diagnostic evidence error: {_markdown_cell(diagnostic_evidence.error)}",
            "", "## Cost scenarios — Gross return versus Net return", "",
            "| Cost | Fee | Slippage | Gross return | Net return | Fees | Slippage cost |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    lines.extend(
        f"| {row.cost_id} | {_number(row.fee_rate)} | {_number(row.slippage_rate)} | {_number(row.gross_return)} | {_number(row.net_return)} | {_number(row.total_fees)} | {_number(row.total_slippage)} |"
        for row in cost_rows
    )
    lines.extend(["", "## Fold metrics", "", "| Fold | Trial | Cost | Status | Net return | Error |", "|---|---|---|---|---:|---|"])
    lines.extend(
        f"| {row.fold_id} | {row.trial_id} | {row.cost_id} | {row.status} | {_number(row.metrics.net_return) if row.metrics else ''} | {_markdown_cell(row.error)} |"
        for row in fold_rows
    )
    lines.extend(["", "## Stitched OOS trial metrics", "", "| Trial | Cost | Status | Gross return | Net return | Error |", "|---|---|---|---:|---:|---|"])
    lines.extend(
        f"| {row.trial_id} | {row.cost_id} | {row.status} | {_number(row.metrics.gross_return) if row.metrics else ''} | {_number(row.metrics.net_return) if row.metrics else ''} | {_markdown_cell(row.error)} |"
        for row in trial_rows
    )
    lines.extend(["", "## Regime metrics", "", "| Regime | Net return | Sharpe | Maximum drawdown | Trades |", "|---|---:|---:|---:|---:|"])
    lines.extend(
        f"| {row.regime_id} | {_number(row.net_return)} | {_number(row.sharpe)} | {_number(row.max_drawdown)} | {row.trade_count} |"
        for row in regime_rows
    )
    lines.extend(["", "## Buy-and-hold comparison", "", "| Cost | Strategy net return | Buy-and-hold net return | Strategy drawdown | Buy-and-hold drawdown |", "|---|---:|---:|---:|---:|"])
    lines.extend(
        f"| {row.cost_id} | {_number(row.strategy_net_return)} | {_number(row.buy_and_hold_net_return)} | {_number(row.strategy_max_drawdown)} | {_number(row.buy_and_hold_max_drawdown)} |"
        for row in benchmark_rows
    )
    lines.extend([
        "", "## Diagnostic evidence", "",
        "Raw labeled CPCV paths, TRAIN Sharpe rows, and structured failures are retained in validation-summary.json.",
        "", "## Non-reconciled fields", "",
        "The names below apply only to stitched cells. Canonical baseline/stress_20bps "
        "fold net return and maximum drawdown are reconciled for stress survival.",
        "",
        *(
            f"- `{name}` (disclosed but not used by validation policy)"
            for name in _NON_RECONCILED_FIELDS
        ),
        "",
        "The fold table is isolated from the stitched OOS table. Gross-to-net differences disclose fees and slippage.",
        "",
    ])
    return "\n".join(lines)


def _csv_bytes(columns: tuple[str, ...], rows: object) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="raise", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _csv_value(row[column]) for column in columns})
    return buffer.getvalue().encode("utf-8")


def _csv_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return _utc_iso(value)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError("CSV values must be finite")
        return repr(converted)
    return value


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            _json_value(value), ensure_ascii=False, allow_nan=False,
            indent=2, separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError("JSON values must be finite")
        return converted
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _safe_new_output_path(output_dir: object) -> Path:
    if not isinstance(output_dir, Path):
        raise ValueError("output_dir must be a Path")
    candidate = output_dir.absolute()
    if _is_link_or_junction(candidate):
        raise ValueError("report output must not be a link or junction")
    if candidate.exists():
        raise ValueError("report output already exists")
    parent = candidate.parent
    if (
        not parent.exists()
        or not parent.is_dir()
        or _is_link_or_junction(parent)
    ):
        raise ValueError("report output parent must be an existing real directory")
    return candidate


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    checker = getattr(path, "is_junction", None)
    return bool(checker is not None and checker())


def _verify_staged_bundle(stage: Path, contents: dict[str, bytes]) -> None:
    if {path.name for path in stage.iterdir()} != set(REPORT_FILENAMES):
        raise OSError("staged validation bundle has an unexpected file set")
    for name, payload in contents.items():
        if (stage / name).read_bytes() != payload:
            raise OSError(f"staged validation bundle verification failed for {name}")


def _require_tuple(value: object, name: str) -> None:
    if not isinstance(value, tuple):
        raise ValueError(f"{name} must be an immutable tuple")


def _finite_real(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite real number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be a finite real number")
    return converted


def _nonnegative_real(value: object, name: str) -> float:
    converted = _finite_real(value, name)
    if converted < 0.0:
        raise ValueError(f"{name} must be nonnegative")
    return converted


def _ratio(value: object, name: str) -> float:
    converted = _finite_real(value, name)
    if not 0.0 <= converted <= 1.0:
        raise ValueError(f"{name} must be in [0, 1]")
    return converted


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return int(value)


def _utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("report timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _fold_ordinal(fold_id: str) -> int:
    try:
        return int(fold_id.removeprefix("fold-"))
    except (AttributeError, ValueError) as error:
        raise ValueError("fold ID is not canonical") from error


def _trial_ordinal(trial_id: str) -> int:
    try:
        return TRIAL_IDS.index(trial_id)
    except ValueError as error:
        raise ValueError("unknown trial ID") from error


def _cost_ordinal(cost_id: str) -> int:
    try:
        return COST_IDS.index(cost_id)
    except ValueError as error:
        raise ValueError("unknown cost ID") from error


def _number(value: Real) -> str:
    return repr(float(value))


def _optional_number(value: Real | None) -> str:
    return "Unavailable" if value is None else _number(value)


def _markdown_cell(value: str | None) -> str:
    if value is None:
        return ""
    return value.replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")
