from dataclasses import replace
from datetime import datetime, timedelta, timezone
import csv
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest

from autobit.reporting import validation as validation_reporting
from autobit.reporting.validation import (
    BenchmarkComparisonRow, CostScenarioRow, FoldMetricRow, FoldTrainingSharpe,
    MetricSnapshot, OOSEquityRow, RegimeMetricRow, TrialMetricRow,
    ValidationDiagnosticEvidence, ValidationReportInput, write_validation_bundle,
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
    *, net_return: float, cost_id: str, sharpe: float = 1.1,
    max_drawdown: float = 0.10, profit_factor: float = 1.6,
    trade_count: int = 110, expectancy: float = 0.01,
) -> MetricSnapshot:
    zero = cost_id == "zero"
    return MetricSnapshot(
        gross_return=net_return if zero else net_return + 0.02,
        net_return=net_return, annualized_return=net_return / 3.0,
        sharpe=sharpe, sortino=1.3, calmar=0.6,
        max_drawdown=max_drawdown, max_drawdown_duration_bars=20,
        profit_factor=profit_factor, expectancy=expectancy, win_rate=0.55,
        average_win=0.03, average_loss=-0.02, average_win_loss_ratio=1.5,
        trade_count=trade_count, mean_holding_bars=18.0,
        median_holding_bars=12.0, exposure=0.42, turnover=2.1,
        total_fees=0.0 if zero else 0.01,
        total_slippage=0.0 if zero else (0.01 if cost_id == "baseline" else 0.02),
        cash_ratio=0.58,
    )


def _diagnostics() -> ValidationDiagnosticEvidence:
    scores = tuple(tuple(float(9 - column) for column in range(9)) for _ in range(45))
    return ValidationDiagnosticEvidence(
        pbo_in_sample_scores=scores,
        pbo_out_of_sample_scores=scores,
        train_fold_sharpes=tuple(
            FoldTrainingSharpe(fold_id=fold_id, sharpe=1.5) for fold_id in FOLD_IDS
        ),
    )


def _equity() -> tuple[OOSEquityRow, ...]:
    values = [100.0]
    for value in [0.002, 0.001, 0.003, 0.0015] * 30:
        values.append(values[-1] * (1.0 + value))
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return tuple(
        OOSEquityRow(
            timestamp=start + timedelta(hours=4 * index), trial_id="baseline",
            cost_id="baseline", equity=value,
        )
        for index, value in enumerate(values)
    )


def _report(*, failed_error: str = "synthetic failure") -> ValidationReportInput:
    fold_rows: list[FoldMetricRow] = []
    trial_rows: list[TrialMetricRow] = []
    for fold_id in FOLD_IDS:
        for trial_index, trial_id in enumerate(TRIAL_IDS):
            for cost_id in COST_IDS:
                target = TARGETS[cost_id] - trial_index * 0.005
                fold_return = (1.0 + target) ** (1.0 / len(FOLD_IDS)) - 1.0
                failed = fold_id == "fold-001" and trial_id == "ema_250" and cost_id == "stress_20bps"
                fold_rows.append(FoldMetricRow(
                    fold_id=fold_id, trial_id=trial_id, cost_id=cost_id,
                    status="FAILED" if failed else "COMPLETED",
                    error=failed_error if failed else None,
                    metrics=None if failed else _metrics(net_return=fold_return, cost_id=cost_id),
                ))
    for trial_index, trial_id in enumerate(TRIAL_IDS):
        for cost_id in COST_IDS:
            target = TARGETS[cost_id] - trial_index * 0.005
            incomplete = trial_id == "ema_250" and cost_id == "stress_20bps"
            max_drawdown = 0.0 if trial_id == "baseline" and cost_id == "baseline" else 0.10
            trial_rows.append(TrialMetricRow(
                trial_id=trial_id, cost_id=cost_id,
                status="INCOMPLETE" if incomplete else "COMPLETE",
                error=failed_error if incomplete else None,
                metrics=None if incomplete else _metrics(
                    net_return=target, cost_id=cost_id, max_drawdown=max_drawdown,
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
    )


def _replace_trial_metric(
    report: ValidationReportInput, trial_id: str, cost_id: str,
    replacement: TrialMetricRow,
) -> ValidationReportInput:
    return replace(report, trial_metrics=tuple(
        replacement if (row.trial_id, row.cost_id) == (trial_id, cost_id) else row
        for row in report.trial_metrics
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
        "status": "INCOMPLETE", "error": "synthetic failure",
    }]
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
        "ema_250", "synthetic failure",
    ):
        assert required in markdown


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


def test_decision_is_derived_from_disclosed_baseline_metrics(tmp_path: Path) -> None:
    report = _report()
    baseline = next(row for row in report.trial_metrics
                    if (row.trial_id, row.cost_id) == ("baseline", "baseline"))
    rejected = replace(
        baseline,
        metrics=replace(baseline.metrics, sharpe=-9.0, profit_factor=0.0, trade_count=0),
    )
    bundle = write_validation_bundle(
        tmp_path / "report",
        _replace_trial_metric(report, "baseline", "baseline", rejected),
    )
    assert bundle.validation_inputs.sharpe == -9.0
    assert bundle.validation_inputs.profit_factor == 0.0
    assert bundle.validation_inputs.trade_count == 0
    assert bundle.decision.status == "REJECT"
    assert {"sharpe", "profit_factor", "trade_count"} <= set(bundle.decision.reasons)


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


@pytest.mark.parametrize("kind", ("short_paths", "short_trials", "nonfinite", "missing_fold", "duplicate_fold"))
def test_raw_diagnostic_evidence_has_exact_finite_shapes_and_fold_coverage(tmp_path: Path, kind: str) -> None:
    report = _report()
    evidence = report.diagnostic_evidence
    if kind == "short_paths":
        evidence = replace(evidence, pbo_in_sample_scores=evidence.pbo_in_sample_scores[:-1])
    elif kind == "short_trials":
        evidence = replace(evidence, pbo_out_of_sample_scores=tuple(
            row[:-1] for row in evidence.pbo_out_of_sample_scores))
    elif kind == "nonfinite":
        first = (float("nan"), *evidence.pbo_in_sample_scores[0][1:])
        evidence = replace(evidence, pbo_in_sample_scores=(first, *evidence.pbo_in_sample_scores[1:]))
    elif kind == "missing_fold":
        evidence = replace(evidence, train_fold_sharpes=evidence.train_fold_sharpes[:-1])
    else:
        evidence = replace(evidence, train_fold_sharpes=(
            *evidence.train_fold_sharpes[:-1], evidence.train_fold_sharpes[0]))
    with pytest.raises(ValueError):
        write_validation_bundle(tmp_path / "report", replace(report, diagnostic_evidence=evidence))


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
    assert next(row for row in rows if row["status"] == "INCOMPLETE")["error"] == error
    assert error.splitlines()[0] in bundle.report_path.read_text(encoding="utf-8")


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
