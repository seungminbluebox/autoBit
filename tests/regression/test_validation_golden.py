import csv
import hashlib
import json
from pathlib import Path

from autobit import cli
from autobit.reporting.validation import REPORT_FILENAMES


GOLDEN = Path("tests/fixtures/validation_golden.csv")
EXPECTED = Path("tests/fixtures/validation_golden_expected.json")


def test_real_walk_forward_golden_is_exact_and_byte_stable(
    tmp_path: Path,
) -> None:
    """Run two independent real 9x4 pipelines and compare every published byte."""
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    primary = tmp_path / "primary"
    repeated = tmp_path / "repeated"
    primary_code = cli.main(
        [
            "walk-forward",
            "--input",
            str(GOLDEN),
            "--output",
            str(primary),
        ]
    )

    repeated_code = cli.main(
        [
            "walk-forward",
            "--input",
            str(GOLDEN),
            "--output",
            str(repeated),
        ]
    )

    assert primary_code == 0
    assert repeated_code == 0
    assert tuple(path.name for path in sorted(primary.iterdir())) == tuple(
        sorted(REPORT_FILENAMES)
    )
    summary = json.loads(
        (primary / "validation-summary.json").read_text(encoding="utf-8")
    )
    assert summary["fold_ids"] == expected["fold_ids"]
    assert summary["trial_ids"] == expected["trial_ids"]
    assert summary["cost_ids"] == expected["cost_ids"]
    assert round(summary["diagnostics"]["dsr"], 8) == expected["dsr_8dp"]
    assert round(summary["diagnostics"]["pbo"], 8) == expected["pbo_8dp"]
    assert summary["decision"]["status"] == expected["decision"]
    assert summary["diagnostic_evidence"]["status"] == "COMPLETE"
    assert len(summary["diagnostic_evidence"]["paths"]) == 45
    assert summary["run_failures"] == []

    fold_rows = _csv_rows(primary / "fold-metrics.csv")
    trial_rows = _csv_rows(primary / "trial-metrics.csv")
    cost_rows = _csv_rows(primary / "cost-scenarios.csv")
    equity_rows = _csv_rows(primary / "oos-equity.csv")
    assert len(fold_rows) == 2 * 9 * 4
    assert len(trial_rows) == 9 * 4
    assert len(cost_rows) == 4
    assert len(equity_rows) == expected["stitched_oos_row_count"]
    assert {row["fold_id"] for row in fold_rows} == set(expected["fold_ids"])
    assert {row["trial_id"] for row in trial_rows} == set(expected["trial_ids"])
    assert {row["cost_id"] for row in cost_rows} == set(expected["cost_ids"])
    assert {row["status"] for row in fold_rows} == {"COMPLETED"}
    assert {row["status"] for row in trial_rows} == {"COMPLETE"}

    manifest = json.loads((primary / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"] == {
        name: hashlib.sha256((primary / name).read_bytes()).hexdigest()
        for name in REPORT_FILENAMES[:-1]
    }
    for name in REPORT_FILENAMES:
        assert (primary / name).read_bytes() == (repeated / name).read_bytes()


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))
