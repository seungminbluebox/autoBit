"""Offline, public-data-only research command line interface."""

import argparse
from dataclasses import asdict, fields
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import httpx
import pandas as pd

from autobit.backtest.analyzers import calculate_metrics
from autobit.backtest.benchmark import run_buy_and_hold
from autobit.backtest.engine import BacktestConfig, run_backtest
from autobit.config import CostConfig, DataConfig, ExchangeRulesConfig
from autobit.data.collector import collect_range
from autobit.data.quality import QualityReport, canonicalize_ohlcv
from autobit.data.storage import _atomic_write, _canonical_json_bytes, save_snapshot
from autobit.data.upbit_public import UpbitPublicClient
from autobit.indicators.trend import compute_trend_indicators
from autobit.reporting.reports import SCHEMA_VERSION, write_report_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autobit", description="KRW-BTC research tools")
    commands = parser.add_subparsers(dest="command", required=True)

    download = commands.add_parser("data-download", help="Download public candles")
    download.add_argument("--output", type=Path, required=True, help="Evidence directory")
    download.add_argument("--end-utc", type=_utc_end, required=True, help="UTC range end")
    download.add_argument("--years", type=_seven_years, default=7, help="History span (default: 7)")
    download.set_defaults(handler=_run_data_download)

    quality = commands.add_parser("data-quality", help="Validate public candles")
    quality.add_argument("--input", type=Path, required=True, help="JSON or CSV source")
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
        frame = collect_range(
            client,
            start_utc=_format_utc(start),
            end_utc=_format_utc(end),
        )

    payload = frame.to_dict(orient="records")
    snapshot = save_snapshot(arguments.output, payload, config=config)
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
                "source_url": snapshot.source_url,
                "raw_snapshot": snapshot.path.name,
                "raw_snapshot_sha256": snapshot.sha256,
                "exchange_rules_sha256": hashlib.sha256(rules_bytes).hexdigest(),
                "config_sha256": snapshot.config_hash,
                "row_count": snapshot.row_count,
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


def _read_ohlcv(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError("JSON input must be a list of candle objects")
        frame = pd.DataFrame(payload)
    elif suffix == ".csv":
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
