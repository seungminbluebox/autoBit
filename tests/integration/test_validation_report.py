from dataclasses import replace
from datetime import datetime, timezone
import csv
import hashlib
import json
from pathlib import Path

import pytest

from autobit.reporting import validation as validation_reporting
from autobit.reporting.validation import (
    BenchmarkComparisonRow,
    CostScenarioRow,
    FoldMetricRow,
    MetricSnapshot,
    OOSEquityRow,
    RegimeMetricRow,
    TrialMetricRow,
    ValidationReportInput,
    write_validation_bundle,
)
from autobit.validation.policy import ValidationInputs, classify_validation


TRIAL_IDS = (
    "baseline", "ema_150", "ema_250", "entry_40", "entry_60",
    "exit_15", "exit_25", "stop_2_0", "stop_3_0",
)
COST_IDS = ("zero", "baseline", "stress_10bps", "stress_20bps")
EXPECTED_FILES = {
    "validation-summary.json", "fold-metrics.csv", "trial-metrics.csv",
    "cost-scenarios.csv", "regime-metrics.csv", "benchmark-comparison.csv",
    "oos-equity.csv", "validation-report.md", "manifest.json",
}


def _policy_inputs() -> ValidationInputs:
    return ValidationInputs(
        oos_net_return=0.30, sharpe=1.1, profit_factor=1.6,
        max_drawdown=0.14, trade_count=110, dsr=0.96, pbo=0.29,
        positive_expectancy_fold_ratio=0.65, max_fold_profit_share=0.49,
        train_test_sharpe_ratio=1.9, stress_survived=True,
    )


def _metrics(seed: int = 0) -> MetricSnapshot:
    delta = seed / 10_000
    return MetricSnapshot(
        gross_return=0.32 + delta, net_return=0.30 + delta,
        annualized_return=0.08, sharpe=1.1, sortino=1.3, calmar=0.6,
        max_drawdown=0.14, max_drawdown_duration_bars=20,
        profit_factor=1.6, expectancy=0.01, win_rate=0.55,
        average_win=0.03, average_loss=-0.02, average_win_loss_ratio=1.5,
        trade_count=110, mean_holding_bars=18.0, median_holding_bars=12.0,
        exposure=0.42, turnover=2.1, total_fees=0.01,
        total_slippage=0.01, cash_ratio=0.58,
    )


def _report(*, failed_error: str = "synthetic failure") -> ValidationReportInput:
    fold_ids = ("fold-000", "fold-001")
    fold_rows = []
    trial_rows = []
    for fold_index, fold_id in enumerate(fold_ids):
        for trial_index, trial_id in enumerate(TRIAL_IDS):
            for cost_index, cost_id in enumerate(COST_IDS):
                failed = fold_id == "fold-001" and trial_id == "ema_250" and cost_id == "stress_20bps"
                fold_rows.append(FoldMetricRow(
                    fold_id=fold_id, trial_id=trial_id, cost_id=cost_id,
                    status="FAILED" if failed else "COMPLETED",
                    error=failed_error if failed else None,
                    metrics=None if failed else _metrics(fold_index * 100 + trial_index * 10 + cost_index),
                ))
    for trial_index, trial_id in enumerate(TRIAL_IDS):
        for cost_index, cost_id in enumerate(COST_IDS):
            incomplete = trial_id == "ema_250" and cost_id == "stress_20bps"
            trial_rows.append(TrialMetricRow(
                trial_id=trial_id, cost_id=cost_id,
                status="INCOMPLETE" if incomplete else "COMPLETE",
                error=failed_error if incomplete else None,
                metrics=None if incomplete else _metrics(trial_index * 10 + cost_index),
            ))
    costs = tuple(
        CostScenarioRow(
            cost_id=cost_id,
            fee_rate=0.0 if cost_id == "zero" else 0.0005,
            slippage_rate=(0.0, 0.0005, 0.001, 0.002)[index],
            gross_return=0.32,
            net_return=0.32 - index * 0.01,
            total_fees=0.0 if index == 0 else 0.01,
            total_slippage=index * 0.01,
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
            cost_id=cost_id, strategy_net_return=0.30 - index * 0.01,
            buy_and_hold_net_return=0.18 - index * 0.01,
            strategy_max_drawdown=0.14, buy_and_hold_max_drawdown=0.31,
        )
        for index, cost_id in enumerate(COST_IDS)
    )
    equity = tuple(
        OOSEquityRow(
            timestamp=datetime(2025, 1, 1 + index, tzinfo=timezone.utc),
            trial_id="baseline", cost_id="baseline", equity=value,
        )
        for index, value in enumerate((100.0, 105.0, 103.0))
    )
    inputs = _policy_inputs()
    return ValidationReportInput(
        decision=classify_validation(inputs), policy_inputs=inputs,
        fold_ids=fold_ids, fold_metrics=tuple(fold_rows),
        trial_metrics=tuple(trial_rows), cost_scenarios=costs,
        regime_metrics=regimes, benchmark_comparison=benchmarks,
        oos_equity=equity,
    )


def test_bundle_is_exact_hashed_strict_deterministic_and_fully_disclosed(tmp_path: Path) -> None:
    first = write_validation_bundle(tmp_path / "first", _report())
    second = write_validation_bundle(tmp_path / "second", _report())

    assert {path.name for path in first.output_dir.iterdir()} == EXPECTED_FILES
    assert {path.name for path in first.paths} == EXPECTED_FILES
    for name in EXPECTED_FILES:
        assert (first.output_dir / name).read_bytes() == (second.output_dir / name).read_bytes()

    summary = json.loads(first.summary_path.read_text(encoding="utf-8"), parse_constant=lambda value: pytest.fail(value))
    assert summary["decision"] == {"status": "PASS", "reasons": []}
    assert summary["diagnostics"]["dsr"] == 0.96
    assert summary["diagnostics"]["pbo"] == 0.29
    assert summary["diagnostics"]["trade_count"] == 110
    assert summary["diagnostics"]["max_fold_profit_share"] == 0.49
    assert summary["diagnostics"]["train_test_sharpe_ratio"] == 1.9
    assert summary["diagnostics"]["stress_survived"] is True

    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1.0"
    assert set(manifest["files"]) == EXPECTED_FILES - {"manifest.json"}
    for name, digest in manifest["files"].items():
        assert digest == hashlib.sha256((first.output_dir / name).read_bytes()).hexdigest()

    trial_rows = list(csv.DictReader(first.trial_metrics_path.open(encoding="utf-8", newline="")))
    assert len(trial_rows) == 36
    assert [(row["trial_id"], row["cost_id"]) for row in trial_rows] == [
        (trial_id, cost_id) for trial_id in TRIAL_IDS for cost_id in COST_IDS
    ]
    assert any(row["status"] == "INCOMPLETE" and row["error"] for row in trial_rows)

    markdown = first.report_path.read_text(encoding="utf-8")
    assert markdown.startswith("# Decision: PASS\n")
    for required in (
        "OOS net return", "OOS Sharpe", "Profit factor", "Maximum drawdown",
        "DSR", "PBO", "OOS trade count", "Positive-expectancy fold ratio",
        "Maximum fold contribution",
        "IS/OOS Sharpe ratio", "20 bps stress survival", "Buy-and-hold",
        "Fold metrics", "Stitched OOS trial metrics", "Gross return", "Net return",
        "ema_250", "synthetic failure",
    ):
        assert required in markdown


def test_csv_uses_utf8_and_quotes_commas_and_newlines(tmp_path: Path) -> None:
    error = "실패,원인\n상세"
    bundle = write_validation_bundle(tmp_path / "report", _report(failed_error=error))
    rows = list(csv.DictReader(bundle.trial_metrics_path.open(encoding="utf-8", newline="")))
    assert next(row for row in rows if row["status"] == "INCOMPLETE")["error"] == error
    assert "실패" in bundle.report_path.read_text(encoding="utf-8")


def test_invalid_report_evidence_is_rejected_before_output_mutation(tmp_path: Path) -> None:
    valid = _report()
    malformed_metric = replace(valid.trial_metrics[0].metrics, sharpe=float("inf"))
    impossible_metric = replace(valid.trial_metrics[0].metrics, average_win=-0.01)
    malformed = (
        replace(valid, trial_metrics=valid.trial_metrics[:-1]),
        replace(valid, trial_metrics=valid.trial_metrics + (valid.trial_metrics[0],)),
        replace(valid, fold_metrics=valid.fold_metrics[:-1]),
        replace(
            valid,
            trial_metrics=(
                replace(valid.trial_metrics[0], metrics=malformed_metric),
                *valid.trial_metrics[1:],
            ),
        ),
        replace(
            valid,
            trial_metrics=(
                replace(valid.trial_metrics[0], metrics=impossible_metric),
                *valid.trial_metrics[1:],
            ),
        ),
        replace(valid, oos_equity=(replace(valid.oos_equity[0], equity=float("nan")),)),
        replace(valid, oos_equity=(replace(valid.oos_equity[0], timestamp=datetime(2025, 1, 1)),)),
        replace(valid, oos_equity=valid.oos_equity + (valid.oos_equity[0],)),
        replace(valid, oos_equity=tuple(reversed(valid.oos_equity))),
        replace(valid, decision=replace(valid.decision, status="REVIEW")),
    )
    for index, report in enumerate(malformed):
        output = tmp_path / f"bad-{index}"
        with pytest.raises(ValueError):
            write_validation_bundle(output, report)
        assert not output.exists()


def test_existing_output_collision_is_never_merged_or_deleted(tmp_path: Path) -> None:
    output = tmp_path / "report"
    output.mkdir()
    marker = output / "user-data.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="already exists"):
        write_validation_bundle(output, _report())
    assert marker.read_text(encoding="utf-8") == "keep"
    assert {path.name for path in output.iterdir()} == {"user-data.txt"}


def test_publish_failure_leaves_no_partial_output_or_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "report"

    def fail_publish(source: object, destination: object) -> None:
        raise OSError("injected publish failure")

    monkeypatch.setattr(validation_reporting.os, "replace", fail_publish)
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
