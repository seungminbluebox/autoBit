"""Deterministic, immutable walk-forward validation report bundles."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
import hashlib
import io
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
from autobit.validation.trials import registered_cost_scenarios, registered_trials


SCHEMA_VERSION: Final = "1.0"
TRIAL_IDS: Final = tuple(trial.trial_id for trial in registered_trials())
COST_IDS: Final = tuple(cost.cost_id for cost in registered_cost_scenarios())
REGIME_IDS: Final = ("rising", "falling", "sideways")
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
class ValidationReportInput:
    """All explicit, typed evidence needed to publish one validation report."""

    decision: ValidationDecision
    policy_inputs: ValidationInputs
    fold_ids: tuple[str, ...]
    fold_metrics: tuple[FoldMetricRow, ...]
    trial_metrics: tuple[TrialMetricRow, ...]
    cost_scenarios: tuple[CostScenarioRow, ...]
    regime_metrics: tuple[RegimeMetricRow, ...]
    benchmark_comparison: tuple[BenchmarkComparisonRow, ...]
    oos_equity: tuple[OOSEquityRow, ...]


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


def write_validation_bundle(
    output_dir: Path, report: ValidationReportInput
) -> ValidationReportBundle:
    """Validate, precompute, and atomically publish exactly nine report files."""
    output = _safe_new_output_path(output_dir)
    normalized = _validate_report(report)
    contents = _report_contents(normalized)
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
    )


def _validate_report(report: object) -> ValidationReportInput:
    if not isinstance(report, ValidationReportInput):
        raise ValueError("report must be ValidationReportInput")
    if not isinstance(report.policy_inputs, ValidationInputs):
        raise ValueError("policy_inputs must be ValidationInputs")
    expected_decision = classify_validation(report.policy_inputs)
    if report.decision != expected_decision:
        raise ValueError("decision must exactly match the frozen OOS policy inputs")
    if not isinstance(report.fold_ids, tuple) or not report.fold_ids:
        raise ValueError("fold_ids must be a nonempty tuple")
    expected_fold_ids = tuple(f"fold-{index:03d}" for index in range(len(report.fold_ids)))
    if report.fold_ids != expected_fold_ids:
        raise ValueError("fold_ids must be unique, contiguous, and canonical")

    _require_tuple(report.fold_metrics, "fold_metrics")
    _require_tuple(report.trial_metrics, "trial_metrics")
    _require_tuple(report.cost_scenarios, "cost_scenarios")
    _require_tuple(report.regime_metrics, "regime_metrics")
    _require_tuple(report.benchmark_comparison, "benchmark_comparison")
    _require_tuple(report.oos_equity, "oos_equity")

    expected_fold_cells = tuple(
        (fold_id, trial_id, cost_id)
        for fold_id in report.fold_ids
        for trial_id in TRIAL_IDS
        for cost_id in COST_IDS
    )
    fold_map: dict[tuple[str, str, str], FoldMetricRow] = {}
    for row in report.fold_metrics:
        if not isinstance(row, FoldMetricRow):
            raise ValueError("fold_metrics must contain FoldMetricRow values")
        key = (row.fold_id, row.trial_id, row.cost_id)
        if key in fold_map:
            raise ValueError("fold_metrics contains duplicate matrix cells")
        _validate_cell(row.status, row.error, row.metrics, complete_status="COMPLETED")
        fold_map[key] = row
    if set(fold_map) != set(expected_fold_cells):
        raise ValueError("fold_metrics must disclose every fold/trial/cost cell")

    expected_trial_cells = tuple(
        (trial_id, cost_id) for trial_id in TRIAL_IDS for cost_id in COST_IDS
    )
    trial_map: dict[tuple[str, str], TrialMetricRow] = {}
    for row in report.trial_metrics:
        if not isinstance(row, TrialMetricRow):
            raise ValueError("trial_metrics must contain TrialMetricRow values")
        key = (row.trial_id, row.cost_id)
        if key in trial_map:
            raise ValueError("trial_metrics contains duplicate matrix cells")
        _validate_cell(row.status, row.error, row.metrics, complete_status="COMPLETE")
        trial_map[key] = row
    if set(trial_map) != set(expected_trial_cells):
        raise ValueError("trial_metrics must disclose the exact 9 by 4 matrix")

    cost_map: dict[str, CostScenarioRow] = {}
    registered_costs = {cost.cost_id: cost for cost in registered_cost_scenarios()}
    for row in report.cost_scenarios:
        if not isinstance(row, CostScenarioRow) or row.cost_id in cost_map:
            raise ValueError("cost_scenarios must contain unique typed rows")
        expected = registered_costs.get(row.cost_id)
        if expected is None or row.fee_rate != expected.fee_rate or row.slippage_rate != expected.slippage_rate:
            raise ValueError("cost_scenarios must use the frozen execution costs")
        for name in ("gross_return", "net_return"):
            _finite_real(getattr(row, name), f"cost_scenarios.{name}")
        for name in ("fee_rate", "slippage_rate", "total_fees", "total_slippage"):
            _nonnegative_real(getattr(row, name), f"cost_scenarios.{name}")
        cost_map[row.cost_id] = row
    if set(cost_map) != set(COST_IDS):
        raise ValueError("cost_scenarios must disclose all four canonical costs")

    regime_map: dict[str, RegimeMetricRow] = {}
    for row in report.regime_metrics:
        if not isinstance(row, RegimeMetricRow) or row.regime_id in regime_map:
            raise ValueError("regime_metrics must contain unique typed rows")
        _finite_real(row.net_return, "regime_metrics.net_return")
        _finite_real(row.sharpe, "regime_metrics.sharpe")
        _ratio(row.max_drawdown, "regime_metrics.max_drawdown")
        _nonnegative_integer(row.trade_count, "regime_metrics.trade_count")
        regime_map[row.regime_id] = row
    if set(regime_map) != set(REGIME_IDS):
        raise ValueError("regime_metrics must disclose rising, falling, and sideways")

    benchmark_map: dict[str, BenchmarkComparisonRow] = {}
    for row in report.benchmark_comparison:
        if not isinstance(row, BenchmarkComparisonRow) or row.cost_id in benchmark_map:
            raise ValueError("benchmark_comparison must contain unique typed rows")
        for name in ("strategy_net_return", "buy_and_hold_net_return"):
            _finite_real(getattr(row, name), f"benchmark_comparison.{name}")
        for name in ("strategy_max_drawdown", "buy_and_hold_max_drawdown"):
            _ratio(getattr(row, name), f"benchmark_comparison.{name}")
        benchmark_map[row.cost_id] = row
    if set(benchmark_map) != set(COST_IDS):
        raise ValueError("benchmark_comparison must disclose all four canonical costs")

    if not report.oos_equity:
        raise ValueError("oos_equity must not be empty")
    equity_keys: set[tuple[str, str, datetime]] = set()
    last_timestamp: dict[tuple[str, str], datetime] = {}
    for row in report.oos_equity:
        if not isinstance(row, OOSEquityRow):
            raise ValueError("oos_equity must contain OOSEquityRow values")
        if row.trial_id not in TRIAL_IDS or row.cost_id not in COST_IDS:
            raise ValueError("oos_equity contains unknown trial or cost IDs")
        timestamp = _utc(row.timestamp)
        key = (row.trial_id, row.cost_id, timestamp)
        if key in equity_keys:
            raise ValueError("oos_equity contains duplicate matrix cells")
        series_key = (row.trial_id, row.cost_id)
        if series_key in last_timestamp and timestamp <= last_timestamp[series_key]:
            raise ValueError("oos_equity timestamps must be strictly increasing")
        equity = _finite_real(row.equity, "oos_equity.equity")
        if equity < 0.0:
            raise ValueError("oos_equity.equity must be nonnegative")
        equity_keys.add(key)
        last_timestamp[series_key] = timestamp
    return report


def _validate_cell(
    status: object,
    error: object,
    metrics: object,
    *,
    complete_status: str,
) -> None:
    failed_status = "FAILED" if complete_status == "COMPLETED" else "INCOMPLETE"
    if status not in {complete_status, failed_status}:
        raise ValueError("report cell status is invalid")
    if status == complete_status:
        if error is not None or not isinstance(metrics, MetricSnapshot):
            raise ValueError("completed report cells require metrics and no error")
        _validate_metrics(metrics)
    else:
        if not isinstance(error, str) or not error or metrics is not None:
            raise ValueError("failed report cells require an error and no metrics")


def _validate_metrics(metrics: MetricSnapshot) -> None:
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


def _report_contents(report: ValidationReportInput) -> dict[str, bytes]:
    fold_rows = sorted(report.fold_metrics, key=lambda row: (_fold_ordinal(row.fold_id), _trial_ordinal(row.trial_id), _cost_ordinal(row.cost_id)))
    trial_rows = sorted(report.trial_metrics, key=lambda row: (_trial_ordinal(row.trial_id), _cost_ordinal(row.cost_id)))
    cost_rows = sorted(report.cost_scenarios, key=lambda row: _cost_ordinal(row.cost_id))
    regime_rows = sorted(report.regime_metrics, key=lambda row: REGIME_IDS.index(row.regime_id))
    benchmark_rows = sorted(report.benchmark_comparison, key=lambda row: _cost_ordinal(row.cost_id))
    equity_rows = sorted(report.oos_equity, key=lambda row: (_trial_ordinal(row.trial_id), _cost_ordinal(row.cost_id), _utc(row.timestamp)))

    summary = {
        "schema_version": SCHEMA_VERSION,
        "decision": {
            "status": report.decision.status,
            "reasons": list(report.decision.reasons),
        },
        "diagnostics": asdict(report.policy_inputs),
        "fold_ids": list(report.fold_ids),
        "trial_ids": list(TRIAL_IDS),
        "cost_ids": list(COST_IDS),
        "failed_cells": [
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
        "validation-report.md": _markdown(report, fold_rows, trial_rows, cost_rows, regime_rows, benchmark_rows).encode("utf-8"),
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
    report: ValidationReportInput,
    fold_rows: list[FoldMetricRow],
    trial_rows: list[TrialMetricRow],
    cost_rows: list[CostScenarioRow],
    regime_rows: list[RegimeMetricRow],
    benchmark_rows: list[BenchmarkComparisonRow],
) -> str:
    inputs = report.policy_inputs
    lines = [
        f"# Decision: {report.decision.status}",
        "",
        "## Reasons",
        "",
        *(f"- `{reason}`" for reason in report.decision.reasons),
    ]
    if not report.decision.reasons:
        lines.append("- None")
    lines.extend(
        [
            "", "## Diagnostics", "",
            f"- OOS net return: {_number(inputs.oos_net_return)}",
            f"- OOS Sharpe: {_number(inputs.sharpe)}",
            f"- Profit factor: {_number(inputs.profit_factor)}",
            f"- Maximum drawdown: {_number(inputs.max_drawdown)}",
            f"- DSR: {_number(inputs.dsr)}",
            f"- PBO: {_number(inputs.pbo)}",
            f"- OOS trade count: {inputs.trade_count}",
            f"- Positive-expectancy fold ratio: {_number(inputs.positive_expectancy_fold_ratio)}",
            f"- Maximum fold contribution: {_number(inputs.max_fold_profit_share)}",
            f"- IS/OOS Sharpe ratio: {_number(inputs.train_test_sharpe_ratio)}",
            f"- 20 bps stress survival: {str(inputs.stress_survived).lower()}",
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
    lines.extend(["", "The fold table is isolated from the stitched OOS table. Gross-to-net differences disclose fees and slippage; cash ratio and exposure are retained in the CSV evidence.", ""])
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
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("CSV values must be finite")
        return repr(value)
    return value


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, separators=(",", ": ")) + "\n").encode("utf-8")


def _safe_new_output_path(output_dir: object) -> Path:
    if not isinstance(output_dir, Path):
        raise ValueError("output_dir must be a Path")
    candidate = output_dir.absolute()
    if candidate.is_symlink():
        raise ValueError("report output must not be a symlink")
    if candidate.exists():
        raise ValueError("report output already exists")
    parent = candidate.parent
    if not parent.exists() or not parent.is_dir() or parent.is_symlink():
        raise ValueError("report output parent must be an existing real directory")
    return candidate


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


def _markdown_cell(value: str | None) -> str:
    if value is None:
        return ""
    return value.replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")
