"""Offline, public-data-only research command line interface."""

import argparse
from dataclasses import asdict, fields
from datetime import datetime, timezone
import hashlib
from itertools import combinations
import json
import math
from pathlib import Path
import sys

import httpx
import pandas as pd

from autobit.backtest.analyzers import PerformanceMetrics, calculate_metrics
from autobit.backtest.benchmark import BuyAndHoldResult, run_buy_and_hold
from autobit.backtest.engine import BacktestConfig, run_backtest
from autobit.config import CostConfig, DataConfig, ExchangeRulesConfig, StrategyConfig
from autobit.data.collector import collect_evidence_range, load_completed_evidence_frame
from autobit.data.quality import QualityReport, canonicalize_ohlcv
from autobit.data.storage import _atomic_write, _canonical_json_bytes
from autobit.data.upbit_public import UpbitPublicClient
from autobit.indicators.trend import compute_trend_indicators
from autobit.reporting.reports import SCHEMA_VERSION, write_report_bundle
from autobit.reporting.validation import (
    BenchmarkComparisonRow,
    CostScenarioRow,
    DiagnosticPathEvidence,
    FoldMetricRow,
    FoldTrainingSharpe,
    MetricSnapshot,
    OOSEquityRow,
    RegimeMetricRow,
    RunFailureEvidence,
    TrialMetricRow,
    ValidationDiagnosticEvidence,
    ValidationReportInput,
    write_validation_bundle,
)
from autobit.validation.models import (
    CostScenario,
    FoldWindow,
    StitchedOOSResult,
    WalkForwardConfig,
    WalkForwardResult,
    WalkForwardRun,
)
from autobit.validation.overfit import cpcv_splits
from autobit.validation.runner import run_walk_forward, validate_walk_forward_frame
from autobit.validation.splits import build_rolling_folds
from autobit.validation.trials import registered_cost_scenarios, registered_trials


_FOUR_HOURS = pd.Timedelta(hours=4)
_TRIAL_IDS = tuple(trial.trial_id for trial in registered_trials())
_COST_IDS = tuple(cost.cost_id for cost in registered_cost_scenarios())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autobit", description="KRW-BTC research tools")
    commands = parser.add_subparsers(dest="command", required=True)

    download = commands.add_parser("data-download", help="Download public candles")
    download.add_argument("--output", type=Path, required=True, help="Evidence directory")
    download.add_argument("--end-utc", type=_utc_end, required=True, help="UTC range end")
    download.add_argument("--years", type=_seven_years, default=7, help="History span (default: 7)")
    download.set_defaults(handler=_run_data_download)

    quality = commands.add_parser("data-quality", help="Validate public candles")
    quality.add_argument(
        "--input",
        type=Path,
        required=True,
        help="JSON, CSV, or completed evidence directory",
    )
    quality.add_argument("--output", type=Path, required=True, help="Result directory")
    quality.set_defaults(handler=_run_data_quality)

    simulation = commands.add_parser("backtest", help="Run a historical simulation")
    simulation.add_argument(
        "--input", type=Path, required=True, help="Canonical processed CSV source"
    )
    simulation.add_argument("--output", type=Path, required=True, help="Report directory")
    simulation.add_argument(
        "--slippage",
        type=_slippage,
        default=CostConfig().slippage_rate,
        help="Slippage rate in [0, 1)",
    )
    simulation.set_defaults(handler=_run_backtest)

    validation = commands.add_parser(
        "walk-forward", help="Run offline rolling validation"
    )
    validation.add_argument(
        "--input", type=Path, required=True, help="Canonical processed CSV source"
    )
    validation.add_argument(
        "--output", type=Path, required=True, help="Validation report directory"
    )
    validation.add_argument(
        "--end-utc",
        type=_walk_forward_end,
        default=None,
        help="Exclusive aware UTC 4-hour boundary",
    )
    validation.set_defaults(handler=_run_walk_forward)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    return int(arguments.handler(arguments))


def _run_data_download(arguments: argparse.Namespace) -> int:
    config = DataConfig(years=arguments.years)
    end = pd.Timestamp(arguments.end_utc)
    start = end - pd.DateOffset(years=arguments.years)
    with httpx.Client() as http_client:
        client = UpbitPublicClient(http_client, config)
        collection = collect_evidence_range(
            client,
            start_utc=_format_utc(start),
            end_utc=_format_utc(end),
            evidence_root=arguments.output,
            config=config,
        )

    frame = collection.frame
    evidence = collection.evidence
    if evidence.collection_snapshot_path is None or evidence.collection_snapshot_sha256 is None:
        raise ValueError("completed collection evidence is missing its snapshot")
    exchange_rules = ExchangeRulesConfig()
    rules_bytes = _canonical_json_bytes(asdict(exchange_rules))
    _atomic_write(arguments.output / "exchange-rules.json", rules_bytes)
    _atomic_write(
        arguments.output / "manifest.json",
        _canonical_json_bytes(
            {
                "schema_version": SCHEMA_VERSION,
                "market": config.market,
                "years": arguments.years,
                "start_utc": _format_utc(start),
                "end_utc": _format_utc(end),
                "source_url": evidence.source_url,
                "raw_pages": [
                    {
                        "path": page.path.name,
                        "sha256": page.sha256,
                        "request_to_utc": page.request_to_utc,
                        "oldest_timestamp_utc": page.oldest_timestamp_utc,
                        "row_count": page.row_count,
                    }
                    for page in evidence.pages
                ],
                "collection_snapshot": evidence.collection_snapshot_path.name,
                "collection_snapshot_sha256": evidence.collection_snapshot_sha256,
                "exchange_rules_sha256": hashlib.sha256(rules_bytes).hexdigest(),
                "config_sha256": evidence.config_sha256,
                "row_count": len(frame),
            }
        ),
    )
    return 0


def _run_data_quality(arguments: argparse.Namespace) -> int:
    raw = _read_ohlcv(arguments.input)
    result = canonicalize_ohlcv(raw, datetime.now(timezone.utc))
    arguments.output.mkdir(parents=True, exist_ok=True)
    processed = result.frame.to_csv(index=True, index_label="timestamp").encode("utf-8")
    _atomic_write(arguments.output / "processed.csv", processed)
    _atomic_write(
        arguments.output / "quality.json",
        _canonical_json_bytes(
            {
                "schema_version": SCHEMA_VERSION,
                "processed_sha256": hashlib.sha256(processed).hexdigest(),
                "quality": asdict(result.report),
            }
        ),
    )
    return 0


def _run_backtest(arguments: argparse.Namespace) -> int:
    config = BacktestConfig(
        costs=CostConfig(
            fee_rate=CostConfig().fee_rate,
            slippage_rate=arguments.slippage,
        )
    )
    processed = _read_processed_csv(arguments.input)
    quality = _load_quality_provenance(arguments.input, len(processed))
    frame = compute_trend_indicators(processed, config.strategy)
    result = run_backtest(frame, config)
    benchmark = run_buy_and_hold(frame, config.costs)
    metrics = calculate_metrics(
        equity_curve=result.equity_curve,
        trades=result.trades,
        orders=result.orders,
        periods_per_year=2190,
        total_fees=result.total_fees,
        total_slippage=result.total_slippage,
    )
    write_report_bundle(
        arguments.output,
        result=result,
        metrics=metrics,
        benchmark=benchmark,
        quality=quality,
        config=config,
        data_path=arguments.input,
        source_root=Path(__file__).resolve().parent,
    )
    return 0


def _run_walk_forward(arguments: argparse.Namespace) -> int:
    """Run the public-data-only validation pipeline with a process exit code."""
    try:
        frame = _read_walk_forward_csv(arguments.input)
        frame = _apply_walk_forward_end(frame, arguments.end_utc)
        folds = tuple(build_rolling_folds(frame.index, _walk_forward_config()))
        if len(folds) < 2:
            raise ValueError("walk-forward validation requires at least two complete folds")
        _preflight_validation_output(arguments.output)
        result = run_walk_forward(frame, folds)
        report = _validation_report(frame, folds, result)
        write_validation_bundle(arguments.output, report)
    except Exception as error:
        print(f"walk-forward failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    return 0


def _walk_forward_config() -> WalkForwardConfig:
    # Kept behind one seam so the CLI has a single canonical calendar policy.
    return WalkForwardConfig()


def _validation_report(
    frame: pd.DataFrame,
    folds: tuple[FoldWindow, ...],
    result: WalkForwardResult,
) -> ValidationReportInput:
    fold_ids = tuple(fold.fold_id for fold in folds)
    run_map = {
        (run.phase, run.fold_id, run.trial_id, run.cost_id): run
        for run in result.runs
    }
    stitched_map = {
        (item.trial_id, item.cost_id): item for item in result.stitched_oos
    }

    fold_rows: list[FoldMetricRow] = []
    fold_snapshots: dict[tuple[str, str, str], MetricSnapshot] = {}
    for fold in folds:
        for trial_id in _TRIAL_IDS:
            for cost_id in _COST_IDS:
                run = run_map[("OOS", fold.fold_id, trial_id, cost_id)]
                snapshot = (
                    _metric_snapshot(run.metrics, cost_id=cost_id)
                    if run.status == "COMPLETED" and run.metrics is not None
                    else None
                )
                if snapshot is not None:
                    fold_snapshots[(fold.fold_id, trial_id, cost_id)] = snapshot
                fold_rows.append(
                    FoldMetricRow(
                        fold_id=fold.fold_id,
                        trial_id=trial_id,
                        cost_id=cost_id,
                        status=run.status,
                        error=run.error,
                        metrics=snapshot,
                    )
                )

    trial_rows: list[TrialMetricRow] = []
    trial_snapshots: dict[tuple[str, str], MetricSnapshot] = {}
    for trial_id in _TRIAL_IDS:
        for cost_id in _COST_IDS:
            stitched = stitched_map[(trial_id, cost_id)]
            snapshot = None
            if stitched.status == "COMPLETE" and stitched.metrics is not None:
                fold_gross = tuple(
                    fold_snapshots[(fold.fold_id, trial_id, cost_id)].gross_return
                    for fold in folds
                )
                snapshot = _metric_snapshot(
                    stitched.metrics,
                    cost_id=cost_id,
                    gross_return=_compound(fold_gross),
                )
                trial_snapshots[(trial_id, cost_id)] = snapshot
            trial_rows.append(
                TrialMetricRow(
                    trial_id=trial_id,
                    cost_id=cost_id,
                    status=stitched.status,
                    error=None,
                    metrics=snapshot,
                )
            )

    failures = tuple(
        RunFailureEvidence(
            phase=run.phase,
            fold_id=run.fold_id,
            trial_id=run.trial_id,
            cost_id=run.cost_id,
            error=run.error or "unknown validation execution failure",
        )
        for run in result.runs
        if run.status == "FAILED"
    )
    costs = tuple(
        CostScenarioRow(
            cost_id=cost.cost_id,
            fee_rate=cost.fee_rate,
            slippage_rate=cost.slippage_rate,
            gross_return=trial_snapshots[("baseline", cost.cost_id)].gross_return,
            net_return=trial_snapshots[("baseline", cost.cost_id)].net_return,
            total_fees=trial_snapshots[("baseline", cost.cost_id)].total_fees,
            total_slippage=trial_snapshots[("baseline", cost.cost_id)].total_slippage,
        )
        for cost in registered_cost_scenarios()
    )
    baseline = stitched_map[("baseline", "baseline")]
    return ValidationReportInput(
        diagnostic_evidence=_diagnostic_evidence(folds, result, run_map, stitched_map),
        fold_ids=fold_ids,
        fold_metrics=tuple(fold_rows),
        trial_metrics=tuple(trial_rows),
        cost_scenarios=costs,
        regime_metrics=_regime_metrics(frame, result, baseline),
        benchmark_comparison=_benchmark_rows(frame, folds, trial_snapshots),
        oos_equity=tuple(
            OOSEquityRow(
                timestamp=point.timestamp,
                trial_id="baseline",
                cost_id="baseline",
                equity=point.equity,
            )
            for point in baseline.equity_curve
        ),
        run_failures=failures,
    )


def _metric_snapshot(
    metrics: PerformanceMetrics,
    *,
    cost_id: str,
    gross_return: float | None = None,
) -> MetricSnapshot:
    if gross_return is None:
        gross_return = metrics.total_return
        if cost_id != "zero":
            gross_return = (
                100.0 * (1.0 + metrics.total_return)
                + metrics.total_fees
                + metrics.total_slippage
            ) / 100.0 - 1.0
    return MetricSnapshot(
        gross_return=gross_return,
        net_return=metrics.total_return,
        annualized_return=metrics.annualized_return,
        sharpe=metrics.sharpe_ratio,
        sortino=metrics.sortino_ratio,
        calmar=metrics.calmar_ratio,
        max_drawdown=metrics.max_drawdown,
        max_drawdown_duration_bars=metrics.max_drawdown_duration_bars,
        profit_factor=metrics.profit_factor,
        expectancy=metrics.expectancy,
        win_rate=metrics.win_rate,
        average_win=metrics.average_win,
        average_loss=metrics.average_loss,
        average_win_loss_ratio=metrics.average_win_loss_ratio,
        trade_count=metrics.trade_count,
        mean_holding_bars=metrics.mean_holding_bars,
        median_holding_bars=metrics.median_holding_bars,
        exposure=metrics.exposure,
        turnover=metrics.turnover,
        total_fees=metrics.total_fees,
        total_slippage=metrics.total_slippage,
        cash_ratio=1.0 - metrics.exposure,
    )


def _diagnostic_evidence(
    folds: tuple[FoldWindow, ...],
    result: WalkForwardResult,
    run_map: dict[tuple[str, str, str, str], WalkForwardRun],
    stitched_map: dict[tuple[str, str], StitchedOOSResult],
) -> ValidationDiagnosticEvidence:
    train_sharpes: list[FoldTrainingSharpe] = []
    for fold in folds:
        run = run_map[("TRAIN", fold.fold_id, "baseline", "baseline")]
        if run.status != "COMPLETED" or run.metrics is None:
            raise ValueError("baseline/default TRAIN evidence must be complete")
        train_sharpes.append(
            FoldTrainingSharpe(fold_id=fold.fold_id, sharpe=run.metrics.sharpe_ratio)
        )

    baseline_failures = tuple(
        run for run in result.runs
        if run.cost_id == "baseline" and run.status == "FAILED"
    )
    if baseline_failures:
        return ValidationDiagnosticEvidence(
            status="INCOMPLETE",
            error=None,
            trial_ids=_TRIAL_IDS,
            paths=(),
            train_fold_sharpes=tuple(train_sharpes),
        )

    trial_returns: list[tuple[float, ...]] = []
    timestamps: tuple[datetime, ...] | None = None
    for trial_id in _TRIAL_IDS:
        stitched = stitched_map[(trial_id, "baseline")]
        if stitched.status != "COMPLETE":
            raise ValueError("complete diagnostics require every baseline-cost trial")
        current_times = tuple(point.timestamp for point in stitched.returns)
        if timestamps is None:
            timestamps = current_times
        elif current_times != timestamps:
            raise ValueError("diagnostic OOS returns must share exact timestamps")
        trial_returns.append(tuple(point.value for point in stitched.returns))
    observation_count = len(timestamps or ())
    if observation_count < 10:
        raise ValueError("diagnostics require at least ten aligned OOS returns")

    splits = cpcv_splits(
        n_observations=observation_count,
        n_groups=10,
        n_test_groups=2,
        embargo=30,
    )
    paths = tuple(
        DiagnosticPathEvidence(
            path_id=f"path-{index:03d}",
            test_group_ids=test_groups,
            in_sample_scores=tuple(
                _nonannualized_sharpe(tuple(values[position] for position in train))
                for values in trial_returns
            ),
            out_of_sample_scores=tuple(
                _nonannualized_sharpe(tuple(values[position] for position in test))
                for values in trial_returns
            ),
        )
        for index, ((train, test), test_groups) in enumerate(
            zip(splits, combinations(range(10), 2), strict=True)
        )
    )
    return ValidationDiagnosticEvidence(
        status="COMPLETE",
        error=None,
        trial_ids=_TRIAL_IDS,
        paths=paths,
        train_fold_sharpes=tuple(train_sharpes),
    )


def _nonannualized_sharpe(returns: tuple[float, ...]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = math.fsum(returns) / len(returns)
    variance = math.fsum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return mean / math.sqrt(variance) if variance > 0.0 else 0.0


def _regime_metrics(
    frame: pd.DataFrame,
    result: WalkForwardResult,
    baseline: StitchedOOSResult,
) -> tuple[RegimeMetricRow, ...]:
    enriched = compute_trend_indicators(frame, StrategyConfig())
    ema = enriched["ema_200"]
    slope = ema.diff()
    regimes = pd.Series("sideways", index=enriched.index, dtype="object")
    regimes.loc[(enriched["close"] > ema) & (slope > 0.0)] = "rising"
    regimes.loc[(enriched["close"] < ema) & (slope < 0.0)] = "falling"

    returns_by_regime: dict[str, list[float]] = {
        "rising": [], "falling": [], "sideways": []
    }
    for point in baseline.returns:
        regime = str(regimes.loc[pd.Timestamp(point.timestamp)])
        returns_by_regime[regime].append(point.value)

    trades_by_regime = {"rising": 0, "falling": 0, "sideways": 0}
    for run in result.runs:
        if (
            run.phase == "OOS"
            and run.trial_id == "baseline"
            and run.cost_id == "baseline"
            and run.status == "COMPLETED"
            and run.result is not None
        ):
            for trade in run.result.trades:
                regime = str(regimes.loc[pd.Timestamp(trade.exit_time)])
                trades_by_regime[regime] += 1

    rows: list[RegimeMetricRow] = []
    for regime_id in ("rising", "falling", "sideways"):
        regime_returns = tuple(returns_by_regime[regime_id])
        curve = _equity_from_returns(regime_returns)
        rows.append(
            RegimeMetricRow(
                regime_id=regime_id,
                net_return=curve[-1] / curve[0] - 1.0,
                sharpe=_annualized_sharpe(regime_returns),
                max_drawdown=_maximum_drawdown(curve),
                trade_count=trades_by_regime[regime_id],
            )
        )
    return tuple(rows)


def _benchmark_rows(
    frame: pd.DataFrame,
    folds: tuple[FoldWindow, ...],
    trial_snapshots: dict[tuple[str, str], MetricSnapshot],
) -> tuple[BenchmarkComparisonRow, ...]:
    oos = frame.loc[
        (frame.index >= folds[0].test_start) & (frame.index < folds[-1].test_end)
    ].copy(deep=True)
    rows: list[BenchmarkComparisonRow] = []
    for cost in registered_cost_scenarios():
        benchmark_return, benchmark_drawdown = _segmented_benchmark(oos, cost)
        strategy = trial_snapshots[("baseline", cost.cost_id)]
        rows.append(
            BenchmarkComparisonRow(
                cost_id=cost.cost_id,
                strategy_net_return=strategy.net_return,
                buy_and_hold_net_return=benchmark_return,
                strategy_max_drawdown=strategy.max_drawdown,
                buy_and_hold_max_drawdown=benchmark_drawdown,
            )
        )
    return tuple(rows)


def _segmented_benchmark(
    frame: pd.DataFrame, cost: CostScenario
) -> tuple[float, float]:
    available = frame.loc[:, ("open", "high", "low", "close", "volume")].notna().all(axis=1)
    run_ids = (~available).cumsum()
    capital = 100.0
    combined: list[float] = []
    costs = CostConfig(fee_rate=cost.fee_rate, slippage_rate=cost.slippage_rate)
    for _, segment in frame.loc[available].groupby(run_ids.loc[available], sort=False):
        benchmark = run_buy_and_hold(segment, costs)
        normalized = _benchmark_equity(segment, benchmark, cost)
        scaled = tuple(capital * value / 100.0 for value in normalized)
        combined.extend(scaled)
        capital = scaled[-1]
    if not combined:
        return 0.0, 0.0
    return capital / 100.0 - 1.0, _maximum_drawdown(tuple(combined))


def _benchmark_equity(
    frame: pd.DataFrame, benchmark: BuyAndHoldResult, cost: CostScenario
) -> tuple[float, ...]:
    if benchmark.entry_time is None or benchmark.exit_time is None:
        return (100.0,) * max(1, len(frame))
    cash = max(
        0.0,
        benchmark.initial_equity
        - benchmark.quantity * float(benchmark.entry_price)
        - benchmark.entry_fee,
    )
    values: list[float] = []
    for timestamp, close in frame["close"].items():
        moment = pd.Timestamp(timestamp).to_pydatetime()
        if moment < benchmark.entry_time:
            values.append(benchmark.initial_equity)
        elif moment >= benchmark.exit_time:
            values.append(benchmark.final_equity)
        else:
            liquidation_price = float(close) * (1.0 - cost.slippage_rate)
            liquidation_notional = benchmark.quantity * liquidation_price
            values.append(cash + liquidation_notional * (1.0 - cost.fee_rate))
    return tuple(values)


def _equity_from_returns(returns: tuple[float, ...]) -> tuple[float, ...]:
    values = [100.0]
    for value in returns:
        values.append(values[-1] * (1.0 + value))
    return tuple(values)


def _annualized_sharpe(returns: tuple[float, ...]) -> float:
    if not returns:
        return 0.0
    mean = math.fsum(returns) / len(returns)
    variance = math.fsum((value - mean) ** 2 for value in returns) / len(returns)
    return mean / math.sqrt(variance) * math.sqrt(2190) if variance > 0.0 else 0.0


def _maximum_drawdown(values: tuple[float, ...]) -> float:
    if not values:
        return 0.0
    peak = values[0]
    maximum = 0.0
    for value in values[1:]:
        if value >= peak:
            peak = value
        elif peak > 0.0:
            maximum = max(maximum, (peak - value) / peak)
    return maximum


def _compound(returns: tuple[float, ...]) -> float:
    value = 1.0
    for item in returns:
        value *= 1.0 + item
    return value - 1.0


def _read_walk_forward_csv(path: Path) -> pd.DataFrame:
    if not isinstance(path, Path) or path.suffix.lower() != ".csv" or not path.is_file():
        raise ValueError("walk-forward input must be an existing processed CSV file")
    frame = pd.read_csv(path)
    if "timestamp" not in frame:
        raise ValueError("processed CSV must contain a timestamp column")
    raw_timestamps = frame.pop("timestamp")
    try:
        parsed = pd.to_datetime(raw_timestamps, errors="raise")
    except (TypeError, ValueError) as error:
        raise ValueError("processed timestamps must be aware UTC values") from error
    index = pd.DatetimeIndex(parsed)
    if index.tz is None or str(index.tz) != "UTC":
        raise ValueError("processed timestamps must use canonical UTC")
    frame.index = index
    return _validated_walk_forward_frame(frame)


def _validated_walk_forward_frame(frame: pd.DataFrame) -> pd.DataFrame:
    return validate_walk_forward_frame(frame)


def _apply_walk_forward_end(
    frame: pd.DataFrame, end_utc: str | None
) -> pd.DataFrame:
    if end_utc is None:
        return frame
    end = pd.Timestamp(end_utc)
    coverage_end = frame.index[-1] + _FOUR_HOURS
    if end > coverage_end:
        raise ValueError("end-utc exceeds completed input coverage")
    filtered = frame.loc[frame.index < end].copy(deep=True)
    if filtered.empty:
        raise ValueError("end-utc excludes all input candles")
    return filtered


def _preflight_validation_output(output_dir: Path) -> None:
    """Create only missing parents after rejecting link and collision hazards."""
    if not isinstance(output_dir, Path):
        raise ValueError("walk-forward output must be a Path")
    output = output_dir.absolute()
    if output.exists() or _is_link_or_junction(output):
        raise ValueError("validation report output already exists or is a link/junction")
    _validate_real_directory_chain(output.parent)
    output.parent.mkdir(parents=True, exist_ok=True)
    _validate_real_directory_chain(output.parent)


def _validate_real_directory_chain(path: Path) -> None:
    current = path
    while True:
        if _is_link_or_junction(current):
            raise ValueError("validation report parent must not contain a link or junction")
        if current.exists() and not current.is_dir():
            raise ValueError("validation report parent components must be directories")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    checker = getattr(path, "is_junction", None)
    return bool(checker is not None and checker())


def _read_ohlcv(path: Path) -> pd.DataFrame:
    if path.is_dir():
        frame = load_completed_evidence_frame(path)
    elif path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError("JSON input must be a list of candle objects")
        frame = pd.DataFrame(payload)
    elif path.suffix.lower() == ".csv":
        frame = pd.read_csv(path)
    else:
        raise ValueError("input must be JSON or CSV")

    rename = {
        "candle_date_time_utc": "timestamp",
        "opening_price": "open",
        "high_price": "high",
        "low_price": "low",
        "trade_price": "close",
        "candle_acc_trade_volume": "volume",
    }
    frame = frame.rename(columns=rename)
    if "timestamp" not in frame:
        raise ValueError("input must contain a timestamp column")
    frame.index = pd.to_datetime(frame.pop("timestamp"), utc=True, errors="raise")
    return frame


def _read_processed_csv(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if "timestamp" not in frame:
        raise ValueError("processed CSV must contain a timestamp column")
    frame.index = pd.to_datetime(frame.pop("timestamp"), utc=True, errors="raise")
    return frame


def _load_quality_provenance(processed_path: Path, row_count: int) -> QualityReport:
    sidecar = processed_path.with_name("quality.json")
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(
            "quality provenance sidecar is missing or malformed"
        ) from error
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("quality provenance schema_version is invalid")
    expected_hash = payload.get("processed_sha256")
    actual_hash = hashlib.sha256(processed_path.read_bytes()).hexdigest()
    if not isinstance(expected_hash, str) or expected_hash != actual_hash:
        raise ValueError("quality provenance does not match processed data bytes")
    quality = payload.get("quality")
    expected_fields = tuple(field.name for field in fields(QualityReport))
    if not isinstance(quality, dict) or set(quality) != set(expected_fields):
        raise ValueError("quality provenance fields are invalid")
    values: dict[str, int] = {}
    for name in expected_fields:
        value = quality[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("quality provenance counts must be nonnegative integers")
        values[name] = value
    if values["total_bars"] != row_count:
        raise ValueError("quality provenance total_bars does not match processed data")
    return QualityReport(**values)


def _utc_end(value: str) -> str:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("end must be an aware UTC timestamp") from error
    if timestamp.tzinfo is None or timestamp.utcoffset().total_seconds() != 0.0:
        raise argparse.ArgumentTypeError("end must be an aware UTC timestamp")
    return _format_utc(timestamp)


def _walk_forward_end(value: str) -> str:
    formatted = _utc_end(value)
    timestamp = pd.Timestamp(formatted)
    if timestamp.value % _FOUR_HOURS.value != 0:
        raise argparse.ArgumentTypeError("end must align to a UTC 4-hour boundary")
    return formatted


def _seven_years(value: str) -> int:
    try:
        years = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("years must be 7") from error
    if years != 7:
        raise argparse.ArgumentTypeError("years must be 7")
    return years


def _slippage(value: str) -> float:
    try:
        rate = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("slippage must be finite and in [0, 1)") from error
    if not math.isfinite(rate) or not 0.0 <= rate < 1.0:
        raise argparse.ArgumentTypeError("slippage must be finite and in [0, 1)")
    return rate


def _format_utc(value: object) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return timestamp.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
