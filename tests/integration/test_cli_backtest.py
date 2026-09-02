from dataclasses import FrozenInstanceError, asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import httpx
import pandas as pd
import pytest

from autobit.backtest.analyzers import PerformanceMetrics
from autobit.backtest.benchmark import BuyAndHoldResult
from autobit.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    EquityPoint,
    OrderRecord,
    TradeRecord,
)
from autobit.config import CostConfig
from autobit.cli import build_parser, main
from autobit.data.quality import QualityReport
from autobit.domain.models import OrderStatus
from autobit.reporting.reports import ReportBundle, write_report_bundle


EXPECTED_FILES = {
    "summary.json",
    "trades.csv",
    "orders.csv",
    "equity.csv",
    "quality.json",
    "manifest.json",
}
FIXTURES = Path(__file__).parents[1] / "fixtures"


def _quality(*, total_bars: int = 2, impossible_candles: int = 0) -> QualityReport:
    return QualityReport(
        total_bars=total_bars,
        duplicates=0,
        conflicting_duplicates=0,
        short_gap_bars=0,
        long_gap_regions=0,
        impossible_candles=impossible_candles,
        nonpositive_prices=0,
        negative_volume=0,
        zero_volume=0,
        spike_flags=0,
        removed_partial_bars=0,
    )


def _write_quality_sidecar(
    path: Path, quality: QualityReport, processed_path: Path
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "processed_sha256": hashlib.sha256(
                    processed_path.read_bytes()
                ).hexdigest(),
                "quality": asdict(quality),
            },
            allow_nan=False,
        ),
        encoding="utf-8",
    )


def test_report_bundle_is_exact_stable_strict_and_content_addressed(tmp_path: Path) -> None:
    source_data = tmp_path / "input.csv"
    source_data.write_bytes(b"source bytes\n")
    source_root = tmp_path / "source"
    (source_root / "nested").mkdir(parents=True)
    (source_root / "z.py").write_bytes(b"z = 2\n")
    (source_root / "nested" / "a.py").write_bytes(b"a = 1\n")
    output = tmp_path / "report"
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = BacktestResult(
        equity_curve=(EquityPoint(timestamp, 100.0),),
        orders=(
            OrderRecord(
                order_id="1",
                status=OrderStatus.COMPLETED,
                side="BUY",
                requested_quantity=0.5,
                filled_quantity=0.5,
                remainder_quantity=0.0,
                occurred_at=timestamp,
                signal_time=timestamp,
                fill_time=timestamp,
                fill_price=100.0,
                reason=None,
            ),
        ),
        trades=(
            TradeRecord(
                entry_time=timestamp,
                exit_time=timestamp,
                quantity=0.5,
                entry_price=100.0,
                exit_price=110.0,
                gross_pnl=5.0,
                net_pnl=5.0,
                fees=0.0,
                exit_reason="CLOSE_EXIT",
            ),
        ),
        final_equity=105.0,
        total_fees=0.0,
        total_slippage=0.0,
    )
    config = BacktestConfig(costs=CostConfig(fee_rate=0.0, slippage_rate=0.0))

    bundle = write_report_bundle(
        output,
        result=result,
        metrics=PerformanceMetrics(total_return=0.05, trade_count=1),
        benchmark=BuyAndHoldResult(final_equity=101.0, total_return=0.01),
        quality=_quality(),
        config=config,
        data_path=source_data,
        source_root=source_root,
    )

    assert isinstance(bundle, ReportBundle)
    assert {path.name for path in output.iterdir()} == EXPECTED_FILES
    assert bundle.data_sha256 == "8482cb75bd3c9abc864397324f7ba6cdbdb4920fe384ef7ee34b6ab4996ae8b2"
    expected_config_hash = hashlib.sha256(
        json.dumps(
            asdict(config),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert bundle.config_sha256 == expected_config_hash
    code_digest = hashlib.sha256()
    for path in sorted(
        source_root.rglob("*.py"),
        key=lambda item: item.relative_to(source_root).as_posix(),
    ):
        code_digest.update(path.relative_to(source_root).as_posix().encode("utf-8"))
        code_digest.update(b"\0")
        code_digest.update(path.read_bytes())
        code_digest.update(b"\0")
    assert bundle.source_code_sha256 == code_digest.hexdigest()
    assert not list(output.glob("*.tmp"))
    with pytest.raises(FrozenInstanceError):
        bundle.data_sha256 = "changed"  # type: ignore[misc]

    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert list(summary) == ["schema_version", "final_equity", "metrics", "benchmark"]
    assert summary["schema_version"] == "1.0"
    assert summary["metrics"]["trade_count"] == 1
    assert summary["benchmark"]["entry_time"] is None
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert list(manifest) == [
        "schema_version",
        "config_sha256",
        "data_sha256",
        "source_code_sha256",
        "files",
    ]
    assert manifest["data_sha256"] == bundle.data_sha256
    assert manifest["files"] == [
        "summary.json",
        "trades.csv",
        "orders.csv",
        "equity.csv",
        "quality.json",
    ]

    assert (output / "trades.csv").read_text(encoding="utf-8").splitlines()[0] == (
        "entry_time,exit_time,quantity,entry_price,exit_price,gross_pnl,net_pnl,fees,exit_reason"
    )
    assert (output / "orders.csv").read_text(encoding="utf-8").splitlines()[1].split(",")[1] == "COMPLETED"
    assert (output / "equity.csv").read_text(encoding="utf-8").splitlines() == [
        "timestamp,equity",
        "2026-01-01T00:00:00Z,100.0",
    ]
    quality = json.loads((output / "quality.json").read_text(encoding="utf-8"))
    assert list(quality) == ["schema_version", "quality"]
    assert quality["quality"] == asdict(_quality())


def test_zero_row_csv_schemas_remain_stable(tmp_path: Path) -> None:
    source_data = tmp_path / "empty.csv"
    source_data.write_bytes(b"")
    source_root = tmp_path / "source"
    source_root.mkdir()
    output = tmp_path / "report"

    write_report_bundle(
        output,
        result=BacktestResult((), (), (), 100.0, 0.0, 0.0),
        metrics=PerformanceMetrics(),
        quality=_quality(),
        config=BacktestConfig(),
        data_path=source_data,
        source_root=source_root,
    )

    assert (output / "trades.csv").read_text(encoding="utf-8").count("\n") == 1
    assert (output / "orders.csv").read_text(encoding="utf-8").count("\n") == 1
    assert (output / "equity.csv").read_text(encoding="utf-8").count("\n") == 1


def test_nonfinite_report_value_is_rejected_before_any_file_is_written(tmp_path: Path) -> None:
    source_data = tmp_path / "input.csv"
    source_data.write_bytes(b"data")
    source_root = tmp_path / "source"
    source_root.mkdir()
    output = tmp_path / "report"

    with pytest.raises(ValueError):
        write_report_bundle(
            output,
            result=BacktestResult((), (), (), 100.0, 0.0, 0.0),
            metrics=PerformanceMetrics(total_return=float("nan")),
            quality=_quality(),
            config=BacktestConfig(),
            data_path=source_data,
            source_root=source_root,
        )

    assert not output.exists()


def test_data_download_uses_public_client_writes_evidence_and_closes_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=[
                    {
                        "market": "KRW-BTC",
                        "candle_date_time_utc": "2025-12-31T20:00:00Z",
                        "opening_price": 100.0,
                        "high_price": 101.0,
                        "low_price": 99.0,
                        "trade_price": 100.0,
                        "candle_acc_trade_volume": 2.0,
                    }
                ],
            )
        return httpx.Response(200, json=[])

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("autobit.cli.httpx.Client", lambda: http_client)
    output = tmp_path / "download"

    assert main(
        [
            "data-download",
            "--output",
            str(output),
            "--end-utc",
            "2026-01-01T00:00:00Z",
        ]
    ) == 0

    assert http_client.is_closed
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/v1/candles/minutes/240"),
        ("GET", "/v1/candles/minutes/240"),
    ]
    snapshots = list(output.glob("snapshot-*.json"))
    assert len(snapshots) == 1
    assert json.loads(snapshots[0].read_text(encoding="utf-8"))[0]["market"] == "KRW-BTC"
    assert {
        path.name for path in output.iterdir()
    } == {
        snapshots[0].name,
        "checkpoint.json",
        "exchange-rules.json",
        "manifest.json",
    }
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1.0"
    assert manifest["years"] == 7
    assert manifest["end_utc"] == "2026-01-01T00:00:00Z"
    assert len(manifest["raw_snapshot_sha256"]) == 64
    assert len(manifest["exchange_rules_sha256"]) == 64


def test_data_quality_reads_csv_and_writes_canonical_csv_plus_quality(tmp_path: Path) -> None:
    raw = tmp_path / "raw.csv"
    raw.write_text(
        "timestamp,open,high,low,close,volume\n"
        "2025-01-01T00:00:00Z,100,102,99,101,2\n"
        "2025-01-01T08:00:00Z,101,103,100,102,3\n",
        encoding="utf-8",
    )
    output = tmp_path / "quality"

    assert main(["data-quality", "--input", str(raw), "--output", str(output)]) == 0

    assert {path.name for path in output.iterdir()} == {"processed.csv", "quality.json"}
    processed = pd.read_csv(output / "processed.csv", parse_dates=["timestamp"])
    assert processed["timestamp"].tolist() == list(
        pd.date_range("2025-01-01", periods=3, freq="4h", tz="UTC")
    )
    assert processed.iloc[1][["open", "high", "low", "close"]].tolist() == [101.0] * 4
    quality = json.loads((output / "quality.json").read_text(encoding="utf-8"))
    assert quality["schema_version"] == "1.0"
    assert quality["quality"]["short_gap_bars"] == 1


def test_canonical_quality_output_is_directly_consumable_by_backtest(tmp_path: Path) -> None:
    timestamps = pd.date_range("2025-01-01", periods=614, freq="4h", tz="UTC")
    raw = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": 100.0,
            "high": 102.0,
            "low": 98.0,
            "close": 100.0,
            "volume": 2.0,
        }
    )
    source = tmp_path / "raw.csv"
    raw.to_csv(source, index=False)
    canonical = tmp_path / "canonical"
    report = tmp_path / "report"

    assert main(["data-quality", "--input", str(source), "--output", str(canonical)]) == 0
    assert main(
        [
            "backtest",
            "--input",
            str(canonical / "processed.csv"),
            "--output",
            str(report),
            "--slippage",
            "0",
        ]
    ) == 0

    summary = json.loads((report / "summary.json").read_text(encoding="utf-8"))
    assert summary["schema_version"] == "1.0"
    assert summary["metrics"]["trade_count"] == 0


def test_backtest_command_runs_one_enriched_scenario_and_writes_bundle(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    processed = canonical / "processed.csv"
    processed.write_bytes((FIXTURES / "entry_next_open.csv").read_bytes())
    _write_quality_sidecar(
        canonical / "quality.json", _quality(total_bars=614), processed
    )
    output = tmp_path / "report"

    assert main(
        [
            "backtest",
            "--input",
            str(processed),
            "--output",
            str(output),
            "--slippage",
            "0",
        ]
    ) == 0

    assert {path.name for path in output.iterdir()} == EXPECTED_FILES
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["schema_version"] == "1.0"
    assert summary["metrics"]["trade_count"] == 1
    assert summary["final_equity"] > 0.0
    assert len((output / "trades.csv").read_text(encoding="utf-8").splitlines()) == 2


def test_repeated_backtest_reports_are_byte_identical(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    processed = canonical / "processed.csv"
    processed.write_bytes((FIXTURES / "entry_next_open.csv").read_bytes())
    _write_quality_sidecar(
        canonical / "quality.json", _quality(total_bars=614), processed
    )
    first = tmp_path / "first"
    second = tmp_path / "second"

    for output in (first, second):
        assert main(
            [
                "backtest",
                "--input",
                str(processed),
                "--output",
                str(output),
                "--slippage",
                "0",
            ]
        ) == 0

    assert {
        name: (first / name).read_bytes() for name in EXPECTED_FILES
    } == {
        name: (second / name).read_bytes() for name in EXPECTED_FILES
    }


@pytest.mark.parametrize("use_dot_segment", [False, True])
def test_backtest_rejects_report_output_that_overlaps_canonical_provenance(
    tmp_path: Path, use_dot_segment: bool
) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    processed = canonical / "processed.csv"
    processed.write_bytes((FIXTURES / "entry_next_open.csv").read_bytes())
    sidecar = canonical / "quality.json"
    _write_quality_sidecar(sidecar, _quality(total_bars=614), processed)
    processed_before = processed.read_bytes()
    sidecar_before = sidecar.read_bytes()
    listing_before = tuple(sorted(path.name for path in canonical.iterdir()))
    output = canonical / "unused" / ".." if use_dot_segment else canonical

    with pytest.raises(ValueError, match="overlap"):
        main(
            [
                "backtest",
                "--input",
                str(processed),
                "--output",
                str(output),
                "--slippage",
                "0",
            ]
        )

    assert processed.read_bytes() == processed_before
    assert sidecar.read_bytes() == sidecar_before
    assert tuple(sorted(path.name for path in canonical.iterdir())) == listing_before


def test_impossible_candle_quality_provenance_reaches_report_exactly(tmp_path: Path) -> None:
    timestamps = pd.date_range("2025-01-01", periods=614, freq="4h", tz="UTC")
    raw = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": 100.0,
            "high": 102.0,
            "low": 98.0,
            "close": 100.0,
            "volume": 2.0,
        }
    )
    raw.loc[0, "high"] = 97.0
    source = tmp_path / "raw.csv"
    raw.to_csv(source, index=False)
    canonical = tmp_path / "canonical"
    report = tmp_path / "report"

    main(["data-quality", "--input", str(source), "--output", str(canonical)])
    main(
        [
            "backtest",
            "--input",
            str(canonical / "processed.csv"),
            "--output",
            str(report),
        ]
    )

    source_quality = json.loads(
        (canonical / "quality.json").read_text(encoding="utf-8")
    )
    report_quality = json.loads(
        (report / "quality.json").read_text(encoding="utf-8")
    )
    manifest = json.loads((report / "manifest.json").read_text(encoding="utf-8"))
    assert source_quality["quality"]["impossible_candles"] == 1
    assert report_quality["quality"] == source_quality["quality"]
    assert manifest["data_sha256"] == source_quality["processed_sha256"]


@pytest.mark.parametrize(
    "sidecar",
    [
        None,
        "{malformed",
        json.dumps(
            {
                "schema_version": "1.0",
                "quality": asdict(_quality(total_bars=999)),
            }
        ),
    ],
)
def test_backtest_fails_closed_for_missing_malformed_or_mismatched_quality(
    tmp_path: Path, sidecar: str | None
) -> None:
    processed = tmp_path / "processed.csv"
    processed.write_bytes((FIXTURES / "entry_next_open.csv").read_bytes())
    if sidecar is not None:
        (tmp_path / "quality.json").write_text(sidecar, encoding="utf-8")

    with pytest.raises(ValueError, match="quality provenance"):
        main(
            [
                "backtest",
                "--input",
                str(processed),
                "--output",
                str(tmp_path / "report"),
            ]
        )


def test_backtest_rejects_quality_sidecar_from_different_processed_bytes(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw.csv"
    pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2025-01-01", periods=614, freq="4h", tz="UTC"
            ),
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 101.0,
            "volume": 2.0,
        }
    ).to_csv(raw, index=False)
    canonical = tmp_path / "canonical"
    main(["data-quality", "--input", str(raw), "--output", str(canonical)])
    processed = canonical / "processed.csv"
    processed.write_bytes(processed.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="quality provenance"):
        main(
            [
                "backtest",
                "--input",
                str(processed),
                "--output",
                str(tmp_path / "report"),
            ]
        )


def test_terminal_open_position_has_nonzero_exposure_and_turnover_in_cli_report(
    tmp_path: Path,
) -> None:
    frame = pd.read_csv(
        FIXTURES / "entry_next_open.csv",
    ).iloc[:613]
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    processed = canonical / "processed.csv"
    frame.to_csv(processed, index=False)
    _write_quality_sidecar(
        canonical / "quality.json", _quality(total_bars=len(frame)), processed
    )
    output = tmp_path / "report"

    assert main(
        [
            "backtest",
            "--input",
            str(processed),
            "--output",
            str(output),
            "--slippage",
            "0",
        ]
    ) == 0

    summary = json.loads(
        (output / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["metrics"]["trade_count"] == 0
    assert summary["metrics"]["exposure"] > 0.0
    assert summary["metrics"]["turnover"] > 0.0


@pytest.mark.parametrize(
    "argv",
    [
        ["data-download", "--output", "out", "--end-utc", "2026-01-01", "--years", "7"],
        ["data-download", "--output", "out", "--end-utc", "2026-01-01T00:00:00Z", "--years", "6"],
        ["backtest", "--input", "in.csv", "--output", "out", "--slippage", "nan"],
        ["backtest", "--input", "in.csv", "--output", "out", "--slippage", "1"],
    ],
)
def test_cli_rejects_non_utc_non_seven_year_or_invalid_slippage(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)
