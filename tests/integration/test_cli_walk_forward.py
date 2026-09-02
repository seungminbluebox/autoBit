from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from autobit import cli as cli_module
from autobit.cli import (
    _apply_walk_forward_end,
    _preflight_validation_output,
    _read_walk_forward_csv,
    _continuous_benchmark,
    build_parser,
    main,
)
from autobit.data.quality import canonicalize_ohlcv
from autobit.validation.models import CostScenario


GOLDEN = Path("tests/fixtures/validation_golden.csv")


def test_walk_forward_cli_has_only_the_public_validation_arguments(
    tmp_path: Path,
) -> None:
    arguments = build_parser().parse_args(
        [
            "walk-forward",
            "--input",
            str(GOLDEN),
            "--output",
            str(tmp_path / "report"),
        ]
    )

    assert arguments.command == "walk-forward"
    assert arguments.input == GOLDEN
    assert arguments.output == tmp_path / "report"
    assert arguments.end_utc is None


@pytest.mark.parametrize(
    "end_utc",
    ("2025-04-06T01:00:00Z", "2025-04-06T00:00:00+09:00", "2025-04-06"),
)
def test_walk_forward_end_must_be_an_aware_utc_four_hour_boundary(
    end_utc: str,
) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "walk-forward",
                "--input",
                str(GOLDEN),
                "--output",
                "report",
                "--end-utc",
                end_utc,
            ]
        )


def test_walk_forward_end_is_exclusive_and_never_extends_coverage() -> None:
    frame = _read_walk_forward_csv(GOLDEN)
    filtered = _apply_walk_forward_end(frame, "2025-04-06T00:00:00Z")

    assert filtered.index[-1].isoformat() == "2025-04-05T20:00:00+00:00"
    assert not (filtered.index >= "2025-04-06T00:00:00Z").any()
    with pytest.raises(ValueError, match="coverage"):
        _apply_walk_forward_end(frame, "2025-07-06T04:00:00Z")


def test_walk_forward_returns_nonzero_before_execution_when_folds_are_insufficient(
    tmp_path: Path,
) -> None:
    output = tmp_path / "report"

    code = main(
        [
            "walk-forward",
            "--input",
            str(GOLDEN),
            "--output",
            str(output),
            "--end-utc",
            "2025-04-06T00:00:00Z",
        ]
    )

    assert code != 0
    assert not output.exists()


def test_walk_forward_rejects_unsorted_processed_data_without_repair(
    tmp_path: Path,
) -> None:
    source = tmp_path / "unsorted.csv"
    source.write_text(
        "timestamp,open,high,low,close,volume\n"
        "2025-01-01T04:00:00Z,100,102,99,101,1\n"
        "2025-01-01T00:00:00Z,100,101,99,100,1\n",
        encoding="utf-8",
    )
    output = tmp_path / "report"

    assert main(
        ["walk-forward", "--input", str(source), "--output", str(output)]
    ) != 0
    assert not output.exists()


def test_cli_reads_actual_canonical_long_gap_and_quarantine_rows(
    tmp_path: Path,
) -> None:
    index = pd.date_range("2024-01-01", periods=24, freq="4h", tz="UTC")
    raw = pd.DataFrame(
        {
            "open": 100.0, "high": 102.0, "low": 99.0,
            "close": 101.0, "volume": 2.0,
        },
        index=index,
    ).drop(index=list(index[5:7]))
    raw.loc[index[12], "high"] = 98.0
    canonical = canonicalize_ohlcv(
        raw, (index[-1] + timedelta(hours=8)).to_pydatetime()
    ).frame
    source = tmp_path / "processed.csv"
    canonical.to_csv(source, index=True, index_label="timestamp")

    loaded = _read_walk_forward_csv(source)

    assert loaded.index.equals(canonical.index)
    assert loaded.loc[index[5:6], "close"].isna().all()
    assert bool(loaded.loc[index[12], "is_quarantined"])
    assert pd.isna(loaded.loc[index[12], "close"])


def test_walk_forward_benchmark_holds_through_missing_price_regions() -> None:
    index = pd.date_range("2025-01-01", periods=6, freq="4h", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": [100.0, 100.0, float("nan"), 1_000.0, 1_000.0, 900.0],
            "high": [101.0, 111.0, float("nan"), 1_001.0, 1_001.0, 901.0],
            "low": [99.0, 99.0, float("nan"), 899.0, 899.0, 899.0],
            "close": [100.0, 110.0, float("nan"), 1_000.0, 900.0, 900.0],
            "volume": [1.0, 1.0, float("nan"), 1.0, 1.0, 1.0],
        },
        index=index,
    )

    net_return, max_drawdown = _continuous_benchmark(
        frame, CostScenario("zero", 0.0, 0.0)
    )

    assert net_return == pytest.approx(8.0)
    assert max_drawdown == pytest.approx(0.10)


def test_walk_forward_benchmark_does_not_retrade_because_of_a_future_gap() -> None:
    index = pd.date_range("2025-01-01", periods=6, freq="4h", tz="UTC")
    complete = pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1.0,
        },
        index=index,
    )
    future_gap = complete.copy(deep=True)
    future_gap.loc[index[3], ["open", "high", "low", "close", "volume"]] = float("nan")
    cost = CostScenario("baseline", 0.001, 0.002)

    uninterrupted = _continuous_benchmark(complete, cost)
    gapped = _continuous_benchmark(future_gap, cost)

    assert gapped == pytest.approx(uninterrupted)


def test_walk_forward_has_no_private_execution_arguments() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "walk-forward",
                "--input",
                str(GOLDEN),
                "--output",
                "report",
                "--live",
            ]
        )


def test_walk_forward_preflight_creates_nested_smoke_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    output = Path("reports/validation-smoke")

    _preflight_validation_output(output)

    assert output.parent.is_dir()
    assert not output.exists()


def test_walk_forward_preflight_preserves_existing_output_and_runs_before_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "report"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    called = False

    def forbidden_runner(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("matrix must not start")

    monkeypatch.setattr(cli_module, "run_walk_forward", forbidden_runner)
    code = main(
        ["walk-forward", "--input", str(GOLDEN), "--output", str(output)]
    )

    assert code != 0
    assert not called
    assert marker.read_text(encoding="utf-8") == "keep"


def test_walk_forward_preflight_rejects_an_unsafe_parent_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    original = Path.is_junction
    monkeypatch.setattr(
        Path,
        "is_junction",
        lambda self: self == unsafe or original(self),
    )

    with pytest.raises(ValueError, match="link|junction"):
        _preflight_validation_output(unsafe / "nested" / "report")
    assert not (unsafe / "nested").exists()
