import json
from pathlib import Path

import pytest

from autobit.config import DataConfig
from autobit.data.storage import save_json_snapshot, save_snapshot


def test_snapshot_write_is_content_addressed_and_deterministic(tmp_path: Path) -> None:
    first = save_json_snapshot(tmp_path, [{"market": "KRW-BTC", "trade_price": 100.0}])
    second = save_json_snapshot(tmp_path, [{"trade_price": 100.0, "market": "KRW-BTC"}])

    assert first.path.exists()
    assert first.path == second.path
    assert first.sha256 == second.sha256
    assert first.sha256 in first.path.name
    assert json.loads(first.path.read_text(encoding="utf-8"))[0]["market"] == "KRW-BTC"
    assert not list(tmp_path.glob("*.tmp"))


def test_snapshot_checkpoint_follows_a_successfully_stored_payload(tmp_path: Path) -> None:
    manifest = save_snapshot(
        tmp_path,
        [
            {"candle_date_time_utc": "2026-01-01T04:00:00Z", "market": "KRW-BTC"},
            {"candle_date_time_utc": "2026-01-01T00:00:00Z", "market": "KRW-BTC"},
        ],
        config=DataConfig(page_size=2),
    )

    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint == {
        "oldest_timestamp_utc": "2026-01-01T00:00:00Z",
        "raw_snapshot_sha256": manifest.sha256,
    }
    assert manifest.row_count == 2
    assert manifest.source_url == "https://api.upbit.com/v1/candles/minutes/240"
    assert len(manifest.config_hash) == 64


def test_failed_atomic_replace_leaves_no_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_replace(_: Path, __: Path) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr("autobit.data.storage.os.replace", fail_replace)

    with pytest.raises(OSError, match="disk failure"):
        save_json_snapshot(tmp_path, [{"market": "KRW-BTC"}])

    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.json"))
