from dataclasses import replace
from datetime import datetime, timedelta, timezone
import csv
import hashlib
from itertools import combinations
import json
import math
from pathlib import Path

import numpy as np
import pytest

from autobit.backtest.analyzers import calculate_metrics
from autobit.reporting import validation as validation_reporting
from autobit.reporting.validation import (
    BenchmarkComparisonRow, CostScenarioRow, DiagnosticPathEvidence,
    FoldMetricRow, FoldTrainingSharpe, MetricSnapshot, OOSEquityRow,
    RegimeMetricRow, RunFailureEvidence, TrialMetricRow,
    ValidationDiagnosticEvidence, ValidationReportInput,
    write_validation_bundle,
)
from autobit.validation.policy import ValidationInputs, classify_validation


TRIAL_IDS = (
    "baseline", "ema_150", "ema_250", "entry_40", "entry_60",
    "exit_15", "exit_25", "stop_2_0", "stop_3_0",
)
COST_IDS = ("zero", "baseline", "stress_10bps", "stress_20bps")
FOLD_IDS = ("fold-000", "fold-001", "fold-002")
EXPECTED_FILES = {
    "validation-summary.json", "fold-metrics.csv", "trial-metrics.csv",
    "cost-scenarios.csv", "regime-metrics.csv", "benchmark-comparison.csv",
    "oos-equity.csv", "validation-report.md", "manifest.json",
}
TARGETS = {
    "zero": 0.28,
    "baseline": math.prod(1.0 + value for value in ([0.002, 0.001, 0.003, 0.0015] * 30)) - 1.0,
    "stress_10bps": 0.22,
    "stress_20bps": 0.18,
}


def _metrics(
    *, net_return: float, cost_id: str, gross_return: float | None = None,
    sharpe: float = 1.1,
    max_drawdown: float = 0.10, profit_factor: float = 1.6,
    trade_count: int = 110, wins: int = 54, losses: int = 56,
    total_fees: float | None = None, total_slippage: float | None = None,
    annualized_return: float | None = None, sortino: float = 1.3,
    calmar: float = 0.6, max_drawdown_duration_bars: int = 20,
) -> MetricSnapshot:
    zero = cost_id == "zero"
    average_win = 0.04 if wins else 0.0
    average_loss = -0.02 if losses else 0.0
    gross_wins = wins * average_win
    gross_losses = abs(losses * average_loss)
    net_pnl = gross_wins - gross_losses
    calculated_profit_factor = gross_wins / gross_losses if gross_losses else 0.0
    if profit_factor != 1.6:
        calculated_profit_factor = profit_factor
    return MetricSnapshot(
        gross_return=(net_return if zero else net_return + 0.02)
        if gross_return is None else gross_return,
        net_return=net_return,
        annualized_return=net_return / 3.0 if annualized_return is None else annualized_return,
        sharpe=sharpe, sortino=sortino, calmar=calmar,
        max_drawdown=max_drawdown,
        max_drawdown_duration_bars=max_drawdown_duration_bars,
        profit_factor=calculated_profit_factor,
        expectancy=net_pnl / trade_count if trade_count else 0.0,
        win_rate=wins / trade_count if trade_count else 0.0,
        average_win=average_win, average_loss=average_loss,
        average_win_loss_ratio=average_win / abs(average_loss) if losses else 0.0,
        trade_count=trade_count, mean_holding_bars=18.0,
        median_holding_bars=12.0, exposure=0.42, turnover=2.1,
        total_fees=(0.0 if zero else 0.01) if total_fees is None else total_fees,
        total_slippage=(
            0.0 if zero else (0.01 if cost_id == "baseline" else 0.02)
        ) if total_slippage is None else total_slippage,
        cash_ratio=0.58,
    )


def _snapshot_from_task2(
    closed_pnls: tuple[float, ...], equity_curve: tuple[float, ...]
) -> MetricSnapshot:
    """Adapt the real Task 2 analyzer result to the Task 4 report boundary."""
    calculated = calculate_metrics(
        closed_pnls=closed_pnls,
        equity_curve=equity_curve,
        holding_bars=(1.0,) * len(closed_pnls),
    )
    return MetricSnapshot(
        gross_return=calculated.total_return,
        net_return=calculated.total_return,
        annualized_return=calculated.annualized_return,
        sharpe=calculated.sharpe_ratio,
        sortino=calculated.sortino_ratio,
        calmar=calculated.calmar_ratio,
        max_drawdown=calculated.max_drawdown,
        max_drawdown_duration_bars=calculated.max_drawdown_duration_bars,
        profit_factor=calculated.profit_factor,
        expectancy=calculated.expectancy,
        win_rate=calculated.win_rate,
        average_win=calculated.average_win,
        average_loss=calculated.average_loss,
        average_win_loss_ratio=calculated.average_win_loss_ratio,
        trade_count=calculated.trade_count,
        mean_holding_bars=calculated.mean_holding_bars,
        median_holding_bars=calculated.median_holding_bars,
        exposure=calculated.exposure,
        turnover=calculated.turnover,
        total_fees=calculated.total_fees,
        total_slippage=calculated.total_slippage,
        cash_ratio=1.0 - calculated.exposure,
    )


def _diagnostics() -> ValidationDiagnosticEvidence:
    scores = tuple(float(9 - column) for column in range(9))
    paths = tuple(
        DiagnosticPathEvidence(
            path_id=f"path-{index:03d}", test_group_ids=test_groups,
            in_sample_scores=scores, out_of_sample_scores=scores,
        )
        for index, test_groups in enumerate(combinations(range(10), 2))
    )
    return ValidationDiagnosticEvidence(
        status="COMPLETE", error=None, trial_ids=TRIAL_IDS, paths=paths,
        train_fold_sharpes=tuple(
            FoldTrainingSharpe(fold_id=fold_id, sharpe=1.5) for fold_id in FOLD_IDS
        ),
    )


def _equity_values() -> tuple[float, ...]:
    values = [100.0]
    for value in [0.002, 0.001, 0.003, 0.0015] * 30:
        values.append(values[-1] * (1.0 + value))
    return tuple(values)


def _equity() -> tuple[OOSEquityRow, ...]:
    values = _equity_values()
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return tuple(
        OOSEquityRow(
            timestamp=start + timedelta(hours=4 * index), trial_id="baseline",
            cost_id="baseline", equity=value,
        )
        for index, value in enumerate(values)
    )


def _path_statistics(values: tuple[float, ...]) -> dict[str, float | int]:
    returns = tuple(current / previous - 1.0 for previous, current in zip(values, values[1:]))
    total_return = values[-1] / values[0] - 1.0
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(returns)
    sharpe = mean / math.sqrt(variance) * math.sqrt(2190)
    downside = math.sqrt(sum(min(value, 0.0) ** 2 for value in returns) / len(returns))
    sortino = mean / downside * math.sqrt(2190) if downside else 0.0
    annualized = (1.0 + total_return) ** (2190 / len(returns)) - 1.0
    peak = values[0]
    max_drawdown = 0.0
    duration = 0
    max_duration = 0
    for value in values[1:]:
        if value >= peak:
            peak = value
            duration = 0
        else:
            duration += 1
            max_duration = max(max_duration, duration)
            max_drawdown = max(max_drawdown, (peak - value) / peak)
    return {
        "annualized_return": annualized,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "max_drawdown_duration_bars": max_duration,
        "calmar": annualized / max_drawdown if max_drawdown else 0.0,
    }


def _report(*, failed_error: str = "synthetic failure") -> ValidationReportInput:
    fold_rows: list[FoldMetricRow] = []
    trial_rows: list[TrialMetricRow] = []
    fold_counts = ((40, 20, 20), (35, 17, 18), (35, 17, 18))
    for fold_index, fold_id in enumerate(FOLD_IDS):
        for trial_index, trial_id in enumerate(TRIAL_IDS):
            for cost_id in COST_IDS:
                target = TARGETS[cost_id] - trial_index * 0.005
                fold_return = (1.0 + target) ** (1.0 / len(FOLD_IDS)) - 1.0
                gross_target = target if cost_id == "zero" else target + 0.02
                fold_gross = (1.0 + gross_target) ** (1.0 / len(FOLD_IDS)) - 1.0
                total_fees = 0.0 if cost_id == "zero" else 0.01
                total_slippage = 0.0 if cost_id == "zero" else (
                    0.01 if cost_id == "baseline" else 0.02
                )
                trades, wins, losses = fold_counts[fold_index]
                failed = fold_id == "fold-001" and trial_id == "ema_250" and cost_id == "stress_20bps"
                fold_rows.append(FoldMetricRow(
                    fold_id=fold_id, trial_id=trial_id, cost_id=cost_id,
                    status="FAILED" if failed else "COMPLETED",
                    error=failed_error if failed else None,
                    metrics=None if failed else _metrics(
                        net_return=fold_return, gross_return=fold_gross,
                        cost_id=cost_id, trade_count=trades, wins=wins,
                        losses=losses, total_fees=total_fees / len(FOLD_IDS),
                        total_slippage=total_slippage / len(FOLD_IDS),
                    ),
                ))
    for trial_index, trial_id in enumerate(TRIAL_IDS):
        for cost_id in COST_IDS:
            target = TARGETS[cost_id] - trial_index * 0.005
            incomplete = trial_id == "ema_250" and cost_id == "stress_20bps"
            path_fields = (
                _path_statistics(_equity_values())
                if trial_id == "baseline" and cost_id == "baseline"
                else {"max_drawdown": 0.10}
            )
            trial_rows.append(TrialMetricRow(
                trial_id=trial_id, cost_id=cost_id,
                status="INCOMPLETE" if incomplete else "COMPLETE",
                error=failed_error if incomplete else None,
                metrics=None if incomplete else _metrics(
                    net_return=target, cost_id=cost_id, **path_fields,
                ),
            ))
    baseline_by_cost = {
        row.cost_id: row.metrics for row in trial_rows
        if row.trial_id == "baseline" and row.metrics is not None
    }
    costs = tuple(
        CostScenarioRow(
            cost_id=cost_id, fee_rate=(0.0, 0.0005, 0.0005, 0.0005)[index],
            slippage_rate=(0.0, 0.0005, 0.001, 0.002)[index],
            gross_return=baseline_by_cost[cost_id].gross_return,
            net_return=baseline_by_cost[cost_id].net_return,
            total_fees=baseline_by_cost[cost_id].total_fees,
            total_slippage=baseline_by_cost[cost_id].total_slippage,
        )
        for index, cost_id in enumerate(COST_IDS)
    )
    regimes = tuple(
        RegimeMetricRow(regime_id=regime, net_return=value, sharpe=1.0,
                        max_drawdown=0.1, trade_count=30)
        for regime, value in (("rising", 0.2), ("falling", -0.05), ("sideways", 0.1))
    )
    benchmarks = tuple(
        BenchmarkComparisonRow(
            cost_id=cost_id,
            strategy_net_return=baseline_by_cost[cost_id].net_return,
            buy_and_hold_net_return=0.18 - index * 0.01,
            strategy_max_drawdown=baseline_by_cost[cost_id].max_drawdown,
            buy_and_hold_max_drawdown=0.31,
        )
        for index, cost_id in enumerate(COST_IDS)
    )
    return ValidationReportInput(
        diagnostic_evidence=_diagnostics(), fold_ids=FOLD_IDS,
        fold_metrics=tuple(fold_rows), trial_metrics=tuple(trial_rows),
        cost_scenarios=costs, regime_metrics=regimes,
        benchmark_comparison=benchmarks, oos_equity=_equity(),
        run_failures=(
            RunFailureEvidence(
                phase="OOS", fold_id="fold-001", trial_id="ema_250",
                cost_id="stress_20bps", error=failed_error,
            ),
        ),
    )


def _replace_trial_metric(
    report: ValidationReportInput, trial_id: str, cost_id: str,
    replacement: TrialMetricRow,
) -> ValidationReportInput:
    return replace(report, trial_metrics=tuple(
        replacement if (row.trial_id, row.cost_id) == (trial_id, cost_id) else row
        for row in report.trial_metrics
    ))


def _replace_fold_metric(
    report: ValidationReportInput, fold_id: str, trial_id: str, cost_id: str,
    replacement: FoldMetricRow,
) -> ValidationReportInput:
    return replace(report, fold_metrics=tuple(
        replacement
        if (row.fold_id, row.trial_id, row.cost_id) == (fold_id, trial_id, cost_id)
        else row
        for row in report.fold_metrics
    ))


def test_bundle_derives_policy_and_is_exact_hashed_deterministic_and_disclosed(tmp_path: Path) -> None:
    first = write_validation_bundle(tmp_path / "first", _report())
    second = write_validation_bundle(tmp_path / "second", _report())

    assert first.decision.status == "PASS"
    assert first.validation_inputs.oos_net_return == pytest.approx(TARGETS["baseline"])
    assert first.validation_inputs.dsr == 1.0
    assert first.validation_inputs.pbo == 0.0
    assert first.validation_inputs.trade_count == 110
    assert first.validation_inputs.positive_expectancy_fold_ratio == 1.0
    fold_return = (1.0 + TARGETS["baseline"]) ** (1.0 / 3.0) - 1.0
    expected_share = (1.0 + fold_return) ** 2 / (
        1.0 + (1.0 + fold_return) + (1.0 + fold_return) ** 2
    )
    assert first.validation_inputs.max_fold_profit_share == pytest.approx(expected_share)
    assert first.validation_inputs.max_fold_profit_share < 0.50
    assert first.validation_inputs.train_test_sharpe_ratio == pytest.approx(1.5 / 1.1)
    assert first.validation_inputs.stress_survived is True
    assert {path.name for path in first.output_dir.iterdir()} == EXPECTED_FILES
    assert {path.name for path in first.paths} == EXPECTED_FILES
    for name in EXPECTED_FILES:
        assert (first.output_dir / name).read_bytes() == (second.output_dir / name).read_bytes()

    summary = json.loads(first.summary_path.read_text(encoding="utf-8"),
                         parse_constant=lambda value: pytest.fail(value))
    assert summary["decision"] == {"status": "PASS", "reasons": []}
    assert summary["diagnostics"]["dsr"] == 1.0
    assert summary["fold_failures"] == [{
        "fold_id": "fold-001", "trial_id": "ema_250",
        "cost_id": "stress_20bps", "status": "FAILED",
        "error": "synthetic failure",
    }]
    assert summary["incomplete_trial_cells"] == [{
        "trial_id": "ema_250", "cost_id": "stress_20bps",
        "status": "INCOMPLETE",
        "error": "[OOS fold-001 ema_250/stress_20bps] synthetic failure",
    }]
    assert summary["run_failures"] == [{
        "phase": "OOS", "fold_id": "fold-001", "trial_id": "ema_250",
        "cost_id": "stress_20bps", "error": "synthetic failure",
    }]
    audit = summary["diagnostic_evidence"]
    assert audit["status"] == "COMPLETE"
    assert audit["error"] is None
    assert audit["trial_ids"] == list(TRIAL_IDS)
    assert len(audit["paths"]) == 45
    assert audit["paths"][0] == {
        "path_id": "path-000", "test_group_ids": [0, 1],
        "in_sample_scores": [float(9 - index) for index in range(9)],
        "out_of_sample_scores": [float(9 - index) for index in range(9)],
    }
    assert audit["paths"][-1]["test_group_ids"] == [8, 9]
    assert audit["train_fold_sharpes"] == [
        {"fold_id": fold_id, "sharpe": 1.5} for fold_id in FOLD_IDS
    ]
    assert summary["non_reconciled_fields"] == [
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
    ]
    assert summary["stress_survival_evidence"]["source"] == (
        "canonical completed baseline/stress_20bps folds"
    )
    assert summary["stress_survival_evidence"]["stitched_path_risk_used"] is False
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1.0"
    assert set(manifest["files"]) == EXPECTED_FILES - {"manifest.json"}
    for name, digest in manifest["files"].items():
        assert digest == hashlib.sha256((first.output_dir / name).read_bytes()).hexdigest()

    markdown = first.report_path.read_text(encoding="utf-8")
    assert markdown.startswith("# Decision: PASS\n")
    for required in (
        "OOS net return", "OOS Sharpe", "Profit factor", "Maximum drawdown",
        "DSR", "PBO", "OOS trade count", "Positive-expectancy fold ratio",
        "Maximum fold contribution", "IS/OOS Sharpe ratio",
        "20 bps stress survival", "Buy-and-hold", "Fold metrics",
        "Stitched OOS trial metrics", "Gross return", "Net return",
        "ema_250", "synthetic failure", "canonical completed baseline/stress_20bps",
        "stitched stress path-risk fields are not used",
    ):
        assert required in markdown


def test_stress_survival_uses_fold_evidence_not_stitched_stress_drawdown(
    tmp_path: Path,
) -> None:
    report = _report()
    stress_fold = next(
        row for row in report.fold_metrics
        if (row.fold_id, row.trial_id, row.cost_id)
        == ("fold-000", "baseline", "stress_20bps")
    )
    failed_survival = _replace_fold_metric(
        report, stress_fold.fold_id, stress_fold.trial_id, stress_fold.cost_id,
        replace(stress_fold, metrics=replace(stress_fold.metrics, max_drawdown=1.0)),
    )
    failed_bundle = write_validation_bundle(tmp_path / "fold-failed", failed_survival)
    assert failed_bundle.validation_inputs.stress_survived is False
    assert failed_bundle.decision.status == "REVIEW"
    assert "stress_survived" in failed_bundle.decision.reasons

    stitched = next(
        row for row in report.trial_metrics
        if (row.trial_id, row.cost_id) == ("baseline", "stress_20bps")
    )
    forged_stitched = replace(
        stitched, metrics=replace(stitched.metrics, max_drawdown=1.0)
    )
    benchmark = next(
        row for row in report.benchmark_comparison if row.cost_id == "stress_20bps"
    )
    report = _replace_trial_metric(
        report, stitched.trial_id, stitched.cost_id, forged_stitched
    )
    report = replace(
        report,
        benchmark_comparison=tuple(
            replace(benchmark, strategy_max_drawdown=1.0)
            if row.cost_id == "stress_20bps" else row
            for row in report.benchmark_comparison
        ),
    )
    forged_bundle = write_validation_bundle(tmp_path / "stitched-forged", report)
    assert forged_bundle.validation_inputs.stress_survived is True
    assert forged_bundle.decision.status == "PASS"


def test_caller_cannot_supply_or_override_policy_inputs_or_decision() -> None:
    report = _report()
    fake = ValidationInputs(
        oos_net_return=0.9, sharpe=9.0, profit_factor=9.0,
        max_drawdown=0.0, trade_count=999, dsr=1.0, pbo=0.0,
        positive_expectancy_fold_ratio=1.0, max_fold_profit_share=0.1,
        train_test_sharpe_ratio=0.1, stress_survived=True,
    )
    with pytest.raises(TypeError, match="policy_inputs"):
        replace(report, policy_inputs=fake)
    with pytest.raises(TypeError, match="decision"):
        replace(report, decision=classify_validation(fake))


def test_forged_bad_baseline_metrics_are_rejected_before_policy_classification(
    tmp_path: Path,
) -> None:
    report = _report()
    baseline = next(row for row in report.trial_metrics
                    if (row.trial_id, row.cost_id) == ("baseline", "baseline"))
    rejected = replace(
        baseline,
        metrics=replace(baseline.metrics, sharpe=-9.0, profit_factor=0.0, trade_count=0),
    )
    with pytest.raises(ValueError, match="zero-trade|equity"):
        write_validation_bundle(
            tmp_path / "report",
            _replace_trial_metric(report, "baseline", "baseline", rejected),
        )


def test_failed_fold_requires_incomplete_stitched_cell(tmp_path: Path) -> None:
    report = _report()
    incomplete = next(row for row in report.trial_metrics
                      if (row.trial_id, row.cost_id) == ("ema_250", "stress_20bps"))
    forged = replace(incomplete, status="COMPLETE", error=None,
                     metrics=_metrics(net_return=0.17, cost_id="stress_20bps"))
    with pytest.raises(ValueError, match="FAILED fold"):
        write_validation_bundle(
            tmp_path / "report",
            _replace_trial_metric(report, "ema_250", "stress_20bps", forged),
        )


@pytest.mark.parametrize("field", ("gross_return", "net_return", "total_fees", "total_slippage"))
def test_cost_rows_must_match_same_cost_baseline_stitched_metrics(tmp_path: Path, field: str) -> None:
    report = _report()
    row = report.cost_scenarios[1]
    forged = replace(row, **{field: getattr(row, field) + 0.01})
    with pytest.raises(ValueError, match="baseline stitched"):
        write_validation_bundle(
            tmp_path / "report",
            replace(report, cost_scenarios=(report.cost_scenarios[0], forged,
                                             *report.cost_scenarios[2:])),
        )


def test_benchmark_strategy_values_match_baseline_stitched_metrics(tmp_path: Path) -> None:
    report = _report()
    forged = replace(report.benchmark_comparison[1], strategy_max_drawdown=0.12)
    with pytest.raises(ValueError, match="baseline stitched"):
        write_validation_bundle(
            tmp_path / "report",
            replace(report, benchmark_comparison=(report.benchmark_comparison[0], forged,
                                                   *report.benchmark_comparison[2:])),
        )


@pytest.mark.parametrize("field", ("total_fees", "total_slippage", "gross_return"))
def test_zero_cost_complete_metrics_are_economically_consistent(tmp_path: Path, field: str) -> None:
    report = _report()
    zero = report.trial_metrics[0]
    value = 0.01 if field != "gross_return" else zero.metrics.net_return - 0.01
    forged = replace(zero, metrics=replace(zero.metrics, **{field: value}))
    with pytest.raises(ValueError, match="zero cost|gross"):
        write_validation_bundle(
            tmp_path / "report", _replace_trial_metric(report, "baseline", "zero", forged),
        )


def test_completed_fold_returns_must_compound_to_stitched_net_return(tmp_path: Path) -> None:
    report = _report()
    row = report.fold_metrics[0]
    forged = replace(
        row,
        metrics=replace(
            row.metrics,
            gross_return=row.metrics.gross_return + 0.02,
            net_return=row.metrics.net_return + 0.02,
        ),
    )
    with pytest.raises(ValueError, match="compound"):
        write_validation_bundle(
            tmp_path / "report", replace(report, fold_metrics=(forged, *report.fold_metrics[1:])),
        )


def test_zero_trade_folds_cannot_support_a_110_trade_stitched_ledger(tmp_path: Path) -> None:
    report = _report()
    rows = []
    for row in report.fold_metrics:
        if (row.trial_id, row.cost_id) == ("baseline", "baseline"):
            metrics = replace(
                row.metrics, trade_count=0, expectancy=0.0, win_rate=0.0,
                average_win=0.0, average_loss=0.0,
                profit_factor=0.0, average_win_loss_ratio=0.0,
                mean_holding_bars=0.0,
            )
            row = replace(row, metrics=metrics)
        rows.append(row)
    with pytest.raises(ValueError, match="trade_count"):
        write_validation_bundle(
            tmp_path / "report", replace(report, fold_metrics=tuple(rows))
        )


@pytest.mark.parametrize("field", ("total_fees", "total_slippage"))
def test_fold_cost_totals_must_sum_to_stitched_costs(tmp_path: Path, field: str) -> None:
    report = _report()
    row = next(
        item for item in report.fold_metrics
        if (item.fold_id, item.trial_id, item.cost_id)
        == ("fold-000", "baseline", "baseline")
    )
    forged = replace(row, metrics=replace(
        row.metrics, **{field: getattr(row.metrics, field) + 0.02}
    ))
    with pytest.raises(ValueError, match=field):
        write_validation_bundle(
            tmp_path / "report",
            _replace_fold_metric(report, row.fold_id, row.trial_id, row.cost_id, forged),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("profit_factor", 99.0),
        ("profit_factor", 0.0),
        ("profit_factor", -1.0),
        ("win_rate", 0.333),
        ("expectancy", 9.0),
        ("average_loss", 0.02),
        ("average_win_loss_ratio", 99.0),
    ),
)
def test_each_fold_trade_metric_algebra_is_self_consistent(
    tmp_path: Path, field: str, value: float
) -> None:
    report = _report()
    row = report.fold_metrics[0]
    forged = replace(row, metrics=replace(row.metrics, **{field: value}))
    with pytest.raises(ValueError, match="trade|win|loss|profit|expectancy"):
        write_validation_bundle(
            tmp_path / "report",
            _replace_fold_metric(report, row.fold_id, row.trial_id, row.cost_id, forged),
        )


def test_zero_implied_losses_require_zero_average_loss_and_ratio(tmp_path: Path) -> None:
    report = _report()
    rebuilt_folds = []
    wins_by_fold = {"fold-000": 20, "fold-001": 17, "fold-002": 17}
    for row in report.fold_metrics:
        if (row.trial_id, row.cost_id) == ("entry_40", "zero"):
            wins = wins_by_fold[row.fold_id]
            metrics = replace(
                row.metrics,
                expectancy=wins * 0.04 / row.metrics.trade_count,
                average_loss=-0.02,
                profit_factor=0.0,
                average_win_loss_ratio=2.0,
            )
            row = replace(row, metrics=metrics)
        rebuilt_folds.append(row)
    stitched = next(
        row for row in report.trial_metrics
        if (row.trial_id, row.cost_id) == ("entry_40", "zero")
    )
    stitched = replace(
        stitched,
        metrics=replace(
            stitched.metrics,
            expectancy=54 * 0.04 / 110,
            average_loss=0.0,
            profit_factor=0.0,
            average_win_loss_ratio=0.0,
        ),
    )
    forged = _replace_trial_metric(
        replace(report, fold_metrics=tuple(rebuilt_folds)),
        "entry_40", "zero", stitched,
    )
    with pytest.raises(ValueError, match="zero losses|average_loss|profit_factor"):
        write_validation_bundle(tmp_path / "report", forged)


@pytest.mark.parametrize(
    ("closed_pnls", "equity_curve"),
    (
        ((1.0, 2.0), (100.0, 103.0)),
        ((-1.0, -2.0), (100.0, 97.0)),
        ((0.0,), (100.0, 100.0)),
        ((1e-8,), (100.0, 100.00000001)),
        ((-1e-8,), (100.0, 99.99999999)),
        ((1.0, -1e-8), (100.0, 100.99999999)),
        ((1.0, -1e-10), (100.0, 100.9999999999)),
        ((1.0, -1e-12), (100.0, 100.999999999999)),
        ((1e8, -1e-4), (1e10, 10099999999.9999)),
    ),
)
def test_task2_trade_metrics_preserve_exact_sign_for_tiny_pnl(
    closed_pnls: tuple[float, ...], equity_curve: tuple[float, ...]
) -> None:
    snapshot = _snapshot_from_task2(closed_pnls, equity_curve)

    validation_reporting._validate_metrics(snapshot, cost_id="zero")
    if len(closed_pnls) == 2 and closed_pnls[0] > 0.0 and closed_pnls[1] < 0.0:
        expected_ratio = closed_pnls[0] / abs(closed_pnls[1])
        assert snapshot.profit_factor == pytest.approx(expected_ratio)
        assert snapshot.average_win_loss_ratio == pytest.approx(expected_ratio)


@pytest.mark.parametrize(
    ("closed_pnls", "equity_curve"),
    (
        ((1.0, -1e-12), (100.0, 100.999999999999)),
        ((1e8, -1e-4), (1e10, 10099999999.9999)),
    ),
)
def test_bundle_accepts_cancellation_prone_task2_trade_metrics(
    tmp_path: Path,
    closed_pnls: tuple[float, ...],
    equity_curve: tuple[float, ...],
) -> None:
    report = _report()
    snapshot = _snapshot_from_task2(closed_pnls, equity_curve)
    empty = _snapshot_from_task2((), (100.0, 100.0))
    fold_metrics = tuple(
        replace(
            row,
            metrics=snapshot if row.fold_id == "fold-000" else empty,
        )
        if (row.trial_id, row.cost_id) == ("entry_40", "zero")
        else row
        for row in report.fold_metrics
    )
    stitched = next(
        row for row in report.trial_metrics
        if (row.trial_id, row.cost_id) == ("entry_40", "zero")
    )
    report = _replace_trial_metric(
        replace(report, fold_metrics=fold_metrics),
        stitched.trial_id,
        stitched.cost_id,
        replace(stitched, metrics=snapshot),
    )

    bundle = write_validation_bundle(tmp_path / "report", report)
    assert bundle.trial_metrics_path.exists()


@pytest.mark.parametrize(
    "snapshot",
    (
        replace(
            _snapshot_from_task2((), (100.0, 100.0)), average_loss=-1e-12
        ),
        replace(
            _snapshot_from_task2((-1.0,), (100.0, 99.0)), average_win=1e-12
        ),
        replace(
            _snapshot_from_task2((1.0,), (100.0, 101.0)), profit_factor=1e-12
        ),
    ),
)
def test_zero_trade_win_and_loss_conventions_require_exact_zero(
    snapshot: MetricSnapshot,
) -> None:
    with pytest.raises(ValueError, match="zero-trade|zero wins|profit_factor"):
        validation_reporting._validate_metrics(snapshot, cost_id="zero")


@pytest.mark.parametrize(
    "field",
    (
        "trade_count", "expectancy", "win_rate", "average_win",
        "average_loss", "profit_factor", "average_win_loss_ratio",
        "mean_holding_bars",
    ),
)
def test_stitched_trade_statistics_are_reconstructed_from_folds(
    tmp_path: Path, field: str
) -> None:
    report = _report()
    stitched = next(
        row for row in report.trial_metrics
        if (row.trial_id, row.cost_id) == ("baseline", "baseline")
    )
    current = getattr(stitched.metrics, field)
    forged_value = current + (1 if field == "trade_count" else 0.01)
    forged = replace(stitched, metrics=replace(stitched.metrics, **{field: forged_value}))
    with pytest.raises(
        ValueError,
        match="trade|win|loss|profit|expectancy|holding",
    ):
        write_validation_bundle(
            tmp_path / "report",
            _replace_trial_metric(report, "baseline", "baseline", forged),
        )


@pytest.mark.parametrize(
    "field",
    (
        "annualized_return", "sharpe", "sortino", "max_drawdown_duration_bars",
        "calmar",
    ),
)
def test_baseline_path_statistics_are_recomputed_from_equity(
    tmp_path: Path, field: str
) -> None:
    report = _report()
    stitched = next(
        row for row in report.trial_metrics
        if (row.trial_id, row.cost_id) == ("baseline", "baseline")
    )
    current = getattr(stitched.metrics, field)
    forged_value = current + (1 if field == "max_drawdown_duration_bars" else 0.01)
    forged = replace(stitched, metrics=replace(stitched.metrics, **{field: forged_value}))
    with pytest.raises(ValueError, match=field):
        write_validation_bundle(
            tmp_path / "report",
            _replace_trial_metric(report, "baseline", "baseline", forged),
        )


@pytest.mark.parametrize("kind", ("wrong_key", "start_50", "final_mismatch", "drawdown_mismatch", "zero_recovery"))
def test_oos_equity_must_be_the_canonical_reconciled_baseline_series(tmp_path: Path, kind: str) -> None:
    report = _report()
    equity = list(report.oos_equity)
    if kind == "wrong_key":
        equity[0] = replace(equity[0], trial_id="ema_150")
    elif kind == "start_50":
        equity[0] = replace(equity[0], equity=np.float32(50.0))
    elif kind == "final_mismatch":
        equity[-1] = replace(equity[-1], equity=equity[-1].equity + 1.0)
    elif kind == "drawdown_mismatch":
        equity[len(equity) // 2] = replace(equity[len(equity) // 2], equity=50.0)
    else:
        equity[1] = replace(equity[1], equity=0.0)
        equity[2] = replace(equity[2], equity=1.0)
    with pytest.raises(ValueError):
        write_validation_bundle(tmp_path / "report", replace(report, oos_equity=tuple(equity)))


@pytest.mark.parametrize(
    "kind",
    (
        "short_paths", "trial_permutation", "bad_path_id", "bad_groups",
        "short_trials", "nonfinite", "missing_fold", "duplicate_fold",
    ),
)
def test_raw_diagnostic_evidence_has_exact_finite_shapes_and_fold_coverage(tmp_path: Path, kind: str) -> None:
    report = _report()
    evidence = report.diagnostic_evidence
    if kind == "short_paths":
        evidence = replace(evidence, paths=evidence.paths[:-1])
    elif kind == "trial_permutation":
        evidence = replace(evidence, trial_ids=tuple(reversed(evidence.trial_ids)))
    elif kind == "bad_path_id":
        evidence = replace(
            evidence,
            paths=(replace(evidence.paths[0], path_id="path-999"), *evidence.paths[1:]),
        )
    elif kind == "bad_groups":
        evidence = replace(
            evidence,
            paths=(replace(evidence.paths[0], test_group_ids=(0, 2)), *evidence.paths[1:]),
        )
    elif kind == "short_trials":
        evidence = replace(
            evidence,
            paths=(
                replace(evidence.paths[0], out_of_sample_scores=evidence.paths[0].out_of_sample_scores[:-1]),
                *evidence.paths[1:],
            ),
        )
    elif kind == "nonfinite":
        first = (float("nan"), *evidence.paths[0].in_sample_scores[1:])
        evidence = replace(
            evidence,
            paths=(replace(evidence.paths[0], in_sample_scores=first), *evidence.paths[1:]),
        )
    elif kind == "missing_fold":
        evidence = replace(evidence, train_fold_sharpes=evidence.train_fold_sharpes[:-1])
    else:
        evidence = replace(evidence, train_fold_sharpes=(
            *evidence.train_fold_sharpes[:-1], evidence.train_fold_sharpes[0]))
    with pytest.raises(ValueError):
        write_validation_bundle(tmp_path / "report", replace(report, diagnostic_evidence=evidence))


def _report_with_baseline_sensitivity_failure() -> ValidationReportInput:
    report = _report()
    error = "baseline-cost sensitivity failed"
    fold = next(
        row for row in report.fold_metrics
        if (row.fold_id, row.trial_id, row.cost_id)
        == ("fold-001", "ema_150", "baseline")
    )
    report = _replace_fold_metric(
        report, fold.fold_id, fold.trial_id, fold.cost_id,
        replace(fold, status="FAILED", error=error, metrics=None),
    )
    stitched = next(
        row for row in report.trial_metrics
        if (row.trial_id, row.cost_id) == ("ema_150", "baseline")
    )
    report = _replace_trial_metric(
        report, stitched.trial_id, stitched.cost_id,
        replace(stitched, status="INCOMPLETE", error=error, metrics=None),
    )
    return replace(
        report,
        run_failures=(
            *report.run_failures,
            RunFailureEvidence(
                phase="OOS", fold_id="fold-001", trial_id="ema_150",
                cost_id="baseline", error=error,
            ),
        ),
        diagnostic_evidence=replace(
            report.diagnostic_evidence,
            status="INCOMPLETE", error="PBO unavailable: baseline trial failed",
            paths=(),
        ),
    )


def test_sensitivity_failure_publishes_structured_incomplete_diagnostics_without_fake_pbo(
    tmp_path: Path,
) -> None:
    bundle = write_validation_bundle(
        tmp_path / "report", _report_with_baseline_sensitivity_failure()
    )
    assert bundle.validation_inputs.pbo is None
    assert bundle.validation_inputs.train_test_sharpe_ratio is None
    assert bundle.decision.status == "REVIEW"
    assert "pbo_unavailable" in bundle.decision.reasons
    assert "train_test_sharpe_ratio_unavailable" in bundle.decision.reasons
    summary = json.loads(bundle.summary_path.read_text(encoding="utf-8"))
    assert summary["diagnostics"]["pbo"] is None
    assert summary["diagnostic_evidence"]["status"] == "INCOMPLETE"
    assert summary["diagnostic_evidence"]["paths"] == []
    assert summary["diagnostic_evidence"]["error"] == (
        "[OOS fold-001 ema_150/baseline] baseline-cost sensitivity failed"
    )
    assert "PBO unavailable: baseline trial failed" not in bundle.report_path.read_text(
        encoding="utf-8"
    )
    assert len(summary["run_failures"]) == 2
    assert "Unavailable" in bundle.report_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "kind", ("missing_oos", "wrong_error", "unexplained_incomplete", "failure_with_complete")
)
def test_structured_failures_reconcile_with_fold_and_stitched_statuses(
    tmp_path: Path, kind: str
) -> None:
    report = _report()
    if kind == "missing_oos":
        report = replace(report, run_failures=())
    elif kind == "wrong_error":
        report = replace(
            report,
            run_failures=(replace(report.run_failures[0], error="different"),),
        )
    elif kind == "unexplained_incomplete":
        report = replace(
            report,
            run_failures=(),
            fold_metrics=tuple(
                replace(row, status="COMPLETED", error=None,
                        metrics=_metrics(
                            net_return=(1.0 + (TARGETS["stress_20bps"] - 0.01)) ** (1.0 / 3.0) - 1.0,
                            gross_return=(1.0 + (TARGETS["stress_20bps"] + 0.01)) ** (1.0 / 3.0) - 1.0,
                            cost_id="stress_20bps", trade_count=35, wins=17, losses=18,
                            total_fees=0.01 / 3.0, total_slippage=0.02 / 3.0,
                        ))
                if (row.fold_id, row.trial_id, row.cost_id)
                == ("fold-001", "ema_250", "stress_20bps") else row
                for row in report.fold_metrics
            ),
        )
    else:
        report = replace(
            report,
            run_failures=(
                *report.run_failures,
                RunFailureEvidence(
                    phase="TRAIN", fold_id="fold-000", trial_id="baseline",
                    cost_id="baseline", error="train failed",
                ),
            ),
        )
    with pytest.raises(ValueError, match="failure|FAILED|INCOMPLETE|COMPLETE"):
        write_validation_bundle(tmp_path / "report", report)


def test_diagnostic_status_must_follow_baseline_cost_trial_completeness(tmp_path: Path) -> None:
    incomplete = _report_with_baseline_sensitivity_failure()
    forged_complete = replace(
        incomplete,
        diagnostic_evidence=_diagnostics(),
    )
    with pytest.raises(ValueError, match="diagnostic"):
        write_validation_bundle(tmp_path / "incomplete", forged_complete)

    complete = _report()
    forged_incomplete = replace(
        complete,
        diagnostic_evidence=replace(
            complete.diagnostic_evidence,
            status="INCOMPLETE", error="forged", paths=(),
        ),
    )
    with pytest.raises(ValueError, match="diagnostic"):
        write_validation_bundle(tmp_path / "complete", forged_incomplete)


def test_numpy_scalars_serialize_as_strict_builtin_numbers(tmp_path: Path) -> None:
    report = _report()
    baseline = report.trial_metrics[0]
    numpy_metrics = replace(
        baseline.metrics, net_return=np.float32(baseline.metrics.net_return),
        gross_return=np.float32(baseline.metrics.gross_return),
        trade_count=np.int64(baseline.metrics.trade_count),
    )
    report = _replace_trial_metric(
        report, "baseline", "zero", replace(baseline, metrics=numpy_metrics))
    zero_cost = replace(
        report.cost_scenarios[0], gross_return=np.float32(numpy_metrics.gross_return),
        net_return=np.float32(numpy_metrics.net_return),
    )
    report = replace(report, cost_scenarios=(zero_cost, *report.cost_scenarios[1:]))
    bundle = write_validation_bundle(tmp_path / "report", report)
    json.loads(bundle.summary_path.read_text(encoding="utf-8"),
               parse_constant=lambda value: pytest.fail(value))


def test_csv_utf8_quoting_and_failure_lists_match_rows(tmp_path: Path) -> None:
    error = "실패,원인\n상세"
    bundle = write_validation_bundle(tmp_path / "report", _report(failed_error=error))
    rows = list(csv.DictReader(bundle.trial_metrics_path.open(encoding="utf-8", newline="")))
    assert next(row for row in rows if row["status"] == "INCOMPLETE")["error"] == (
        f"[OOS fold-001 ema_250/stress_20bps] {error}"
    )
    assert error.splitlines()[0] in bundle.report_path.read_text(encoding="utf-8")


def test_stitched_failure_error_is_canonicalized_from_sorted_structured_failures(
    tmp_path: Path,
) -> None:
    report = _report()
    stitched = next(
        row for row in report.trial_metrics
        if (row.trial_id, row.cost_id) == ("ema_250", "stress_20bps")
    )
    report = _replace_trial_metric(
        report, stitched.trial_id, stitched.cost_id,
        replace(stitched, error="contradictory stitched error"),
    )
    report = replace(
        report,
        run_failures=(
            *report.run_failures,
            RunFailureEvidence(
                phase="TRAIN", fold_id="fold-000", trial_id="ema_250",
                cost_id="stress_20bps", error="train failure",
            ),
        ),
    )
    expected = (
        "[TRAIN fold-000 ema_250/stress_20bps] train failure; "
        "[OOS fold-001 ema_250/stress_20bps] synthetic failure"
    )

    bundle = write_validation_bundle(tmp_path / "report", report)
    summary = json.loads(bundle.summary_path.read_text(encoding="utf-8"))
    trial_rows = list(csv.DictReader(
        bundle.trial_metrics_path.open(encoding="utf-8", newline="")
    ))
    incomplete = next(row for row in trial_rows if row["status"] == "INCOMPLETE")
    markdown = bundle.report_path.read_text(encoding="utf-8")

    assert summary["incomplete_trial_cells"][0]["error"] == expected
    assert incomplete["error"] == expected
    assert expected in markdown
    assert "contradictory stitched error" not in json.dumps(
        summary, ensure_ascii=False
    )
    assert "contradictory stitched error" not in bundle.trial_metrics_path.read_text(
        encoding="utf-8"
    )
    assert "contradictory stitched error" not in markdown


def test_existing_output_collision_is_never_merged_or_deleted(tmp_path: Path) -> None:
    output = tmp_path / "report"
    output.mkdir()
    marker = output / "user-data.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="already exists"):
        write_validation_bundle(output, _report())
    assert marker.read_text(encoding="utf-8") == "keep"


def test_output_and_immediate_parent_junctions_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "report"
    original = Path.is_junction
    monkeypatch.setattr(
        Path, "is_junction",
        lambda self: self in {output, output.parent} or original(self),
    )
    with pytest.raises(ValueError, match="junction|link"):
        write_validation_bundle(output, _report())
    assert not output.exists()


def test_publish_failure_leaves_no_partial_output_or_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "report"
    monkeypatch.setattr(
        validation_reporting.os, "replace",
        lambda source, destination: (_ for _ in ()).throw(OSError("injected publish failure")),
    )
    with pytest.raises(OSError, match="injected"):
        write_validation_bundle(output, _report())
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_preexisting_staging_collision_is_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "report"
    collision = tmp_path / ".report.validation-fixed.tmp"
    collision.mkdir()
    marker = collision / "user-data.txt"
    marker.write_text("keep", encoding="utf-8")

    class FixedUuid:
        hex = "fixed"

    monkeypatch.setattr(validation_reporting, "uuid4", lambda: FixedUuid())
    with pytest.raises(FileExistsError):
        write_validation_bundle(output, _report())
    assert marker.read_text(encoding="utf-8") == "keep"
