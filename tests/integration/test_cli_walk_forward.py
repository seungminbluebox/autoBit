from pathlib import Path

import pytest

from autobit.cli import (
    _apply_walk_forward_end,
    _read_walk_forward_csv,
    build_parser,
    main,
)


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
