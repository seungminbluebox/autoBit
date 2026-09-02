"""Stable, strict, content-addressed backtest report bundles."""

import csv
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import io
import json
import math
import os
from pathlib import Path
from typing import Final
from uuid import uuid4

from autobit.backtest.analyzers import PerformanceMetrics
from autobit.backtest.benchmark import BuyAndHoldResult
from autobit.backtest.engine import BacktestConfig, BacktestResult, EquityPoint, OrderRecord, TradeRecord
from autobit.data.quality import QualityReport


SCHEMA_VERSION: Final = "1.0"
TRADE_COLUMNS: Final = tuple(field.name for field in fields(TradeRecord))
ORDER_COLUMNS: Final = tuple(field.name for field in fields(OrderRecord))
EQUITY_COLUMNS: Final = tuple(field.name for field in fields(EquityPoint))
REPORT_FILENAMES: Final = (
    "summary.json",
    "trades.csv",
    "orders.csv",
    "equity.csv",
    "quality.json",
    "manifest.json",
)


@dataclass(frozen=True, slots=True)
class ReportBundle:
    output_dir: Path
    summary_path: Path
    trades_path: Path
    orders_path: Path
    equity_path: Path
    quality_path: Path
    manifest_path: Path
    config_sha256: str
    data_sha256: str
    source_code_sha256: str


def write_report_bundle(
    output_dir: Path,
    *,
    result: BacktestResult,
    metrics: PerformanceMetrics,
    quality: QualityReport,
    config: BacktestConfig,
    data_path: Path,
    benchmark: BuyAndHoldResult | None = None,
    source_root: Path = Path("src/autobit"),
) -> ReportBundle:
    """Write exactly six deterministic report files using atomic replacement."""
    output, data_file = _safe_report_paths(output_dir, data_path)
    code_root = Path(source_root)
    config_sha256 = hashlib.sha256(_canonical_json_bytes(config)).hexdigest()
    data_sha256 = hashlib.sha256(data_file.read_bytes()).hexdigest()
    source_code_sha256 = _source_code_hash(code_root)

    summary_bytes = _json_bytes(
        {
            "schema_version": SCHEMA_VERSION,
            "final_equity": result.final_equity,
            "metrics": metrics,
            "benchmark": benchmark,
        }
    )
    trades_bytes = _csv_bytes(TRADE_COLUMNS, result.trades)
    orders_bytes = _csv_bytes(ORDER_COLUMNS, result.orders)
    equity_bytes = _csv_bytes(EQUITY_COLUMNS, result.equity_curve)
    quality_bytes = _json_bytes(
        {"schema_version": SCHEMA_VERSION, "quality": quality}
    )
    manifest_bytes = _json_bytes(
        {
            "schema_version": SCHEMA_VERSION,
            "config_sha256": config_sha256,
            "data_sha256": data_sha256,
            "source_code_sha256": source_code_sha256,
            "files": list(REPORT_FILENAMES[:-1]),
        }
    )

    output.mkdir(parents=True, exist_ok=True)
    contents = (
        summary_bytes,
        trades_bytes,
        orders_bytes,
        equity_bytes,
        quality_bytes,
        manifest_bytes,
    )
    for filename, payload in zip(REPORT_FILENAMES, contents, strict=True):
        _atomic_write(output / filename, payload)

    return ReportBundle(
        output_dir=output,
        summary_path=output / REPORT_FILENAMES[0],
        trades_path=output / REPORT_FILENAMES[1],
        orders_path=output / REPORT_FILENAMES[2],
        equity_path=output / REPORT_FILENAMES[3],
        quality_path=output / REPORT_FILENAMES[4],
        manifest_path=output / REPORT_FILENAMES[5],
        config_sha256=config_sha256,
        data_sha256=data_sha256,
        source_code_sha256=source_code_sha256,
    )


def _safe_report_paths(output_dir: Path, data_path: Path) -> tuple[Path, Path]:
    output = Path(output_dir).resolve(strict=False)
    data_file = Path(data_path).resolve(strict=False)
    protected = {data_file, data_file.with_name("quality.json")}
    report_targets = {output, *(output / name for name in REPORT_FILENAMES)}
    if protected & report_targets:
        raise ValueError(
            "report output overlaps processed input or quality provenance"
        )
    return output, data_file


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            _json_value(value),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("report values must be finite")
        return value
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("report datetimes must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    raise TypeError(f"unsupported report value: {type(value).__name__}")


def _csv_bytes(columns: tuple[str, ...], records: tuple[object, ...]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="raise")
    writer.writeheader()
    for record in records:
        writer.writerow(
            {
                column: _csv_value(getattr(record, column))
                for column in columns
            }
        )
    return buffer.getvalue().encode("utf-8")


def _csv_value(value: object) -> object:
    converted = _json_value(value)
    if converted is None:
        return ""
    if isinstance(converted, (dict, list)):
        return json.dumps(
            converted,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    return converted


def _source_code_hash(source_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(source_root.rglob("*.py"), key=lambda item: item.relative_to(source_root).as_posix()):
        relative = path.relative_to(source_root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _atomic_write(destination: Path, contents: bytes) -> None:
    temporary = destination.with_name(f"{destination.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
