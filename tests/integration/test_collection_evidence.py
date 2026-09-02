import json
from pathlib import Path

import pandas as pd
import pytest

from autobit.config import DataConfig
from autobit.data.collector import collect_evidence_range
from autobit.data.upbit_public import PUBLIC_CANDLE_URL, PublicDataUnavailable


def _candle(timestamp: str, price: float = 100.0) -> dict[str, object]:
    return {
        "market": "KRW-BTC",
        "candle_date_time_utc": timestamp,
        "opening_price": price,
        "high_price": price + 1.0,
        "low_price": price - 1.0,
        "trade_price": price,
        "candle_acc_trade_volume": 1.0,
    }


class _ScriptedPublicClient:
    source_url = PUBLIC_CANDLE_URL

    def __init__(self, outcomes: list[list[dict[str, object]] | BaseException]) -> None:
        self._outcomes = iter(outcomes)
        self.calls: list[str] = []

    def fetch_page(self, to_utc: str) -> list[dict[str, object]]:
        self.calls.append(to_utc)
        outcome = next(self._outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _collect(
    root: Path,
    client: _ScriptedPublicClient,
    *,
    config: DataConfig = DataConfig(page_size=2),
):
    return collect_evidence_range(
        client,
        start_utc="2026-01-01T00:00:00Z",
        end_utc="2026-01-01T12:00:00Z",
        evidence_root=root,
        config=config,
    )


def test_interrupted_collection_resumes_from_saved_page_without_refetching_it(
    tmp_path: Path,
) -> None:
    """A later transient failure cannot discard a page already made durable."""
    first_page = [_candle("2026-01-01T08:00:00Z", 108), _candle("2026-01-01T04:00:00Z", 104)]
    first_client = _ScriptedPublicClient(
        [first_page, PublicDataUnavailable("temporary outage")]
    )

    with pytest.raises(PublicDataUnavailable, match="temporary outage"):
        _collect(tmp_path, first_client)

    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["next_to_utc"] == "2026-01-01T04:00:00Z"
    assert checkpoint["complete"] is False
    assert set(checkpoint) == {
        "source_url",
        "market",
        "candle_unit_minutes",
        "page_size",
        "start_utc",
        "end_utc",
        "config_sha256",
        "pages",
        "next_to_utc",
        "previous_oldest_utc",
        "complete",
        "collection_snapshot",
        "collection_snapshot_sha256",
    }
    page_path = tmp_path / checkpoint["pages"][0]["path"]
    assert json.loads(page_path.read_text(encoding="utf-8")) == first_page
    assert [row["candle_date_time_utc"] for row in json.loads(page_path.read_text(encoding="utf-8"))] == [
        "2026-01-01T08:00:00Z",
        "2026-01-01T04:00:00Z",
    ]

    resumed_client = _ScriptedPublicClient(
        [[_candle("2026-01-01T04:00:00Z", 104), _candle("2026-01-01T00:00:00Z", 100), _candle("2025-12-31T20:00:00Z", 96)]]
    )
    result = _collect(tmp_path, resumed_client)

    assert resumed_client.calls == ["2026-01-01T04:00:00Z"]
    assert result.frame["candle_date_time_utc"].tolist() == [
        "2026-01-01T00:00:00Z",
        "2026-01-01T04:00:00Z",
        "2026-01-01T08:00:00Z",
    ]
    assert result.evidence.complete
    assert len(result.evidence.pages) == 2


def test_completed_evidence_rerun_loads_pages_without_network_calls(tmp_path: Path) -> None:
    """A known-complete range is derived again from validated local evidence."""
    initial_client = _ScriptedPublicClient(
        [[_candle("2026-01-01T04:00:00Z"), _candle("2025-12-31T20:00:00Z")]]
    )
    initial = _collect(tmp_path, initial_client)
    rerun_client = _ScriptedPublicClient([])

    rerun = _collect(tmp_path, rerun_client)

    assert rerun_client.calls == []
    pd.testing.assert_frame_equal(rerun.frame, initial.frame)
    manifest = json.loads((tmp_path / "collection-manifest.json").read_text(encoding="utf-8"))
    assert [page["sha256"] for page in manifest["pages"]] == [
        page.sha256 for page in rerun.evidence.pages
    ]


def test_corrupted_or_mismatched_evidence_fails_closed_before_network(
    tmp_path: Path,
) -> None:
    """Resume trusts neither altered page bytes nor a checkpoint for another config."""
    complete_client = _ScriptedPublicClient(
        [[_candle("2026-01-01T04:00:00Z"), _candle("2025-12-31T20:00:00Z")]]
    )
    result = _collect(tmp_path, complete_client)
    (tmp_path / result.evidence.pages[0].path.name).write_bytes(b"corrupt")
    corrupted_client = _ScriptedPublicClient([])

    with pytest.raises(ValueError, match="evidence"):
        _collect(tmp_path, corrupted_client)

    assert corrupted_client.calls == []

    mismatch_root = tmp_path / "mismatch"
    _collect(
        mismatch_root,
        _ScriptedPublicClient([[_candle("2026-01-01T04:00:00Z"), _candle("2025-12-31T20:00:00Z")]]),
    )
    mismatched_client = _ScriptedPublicClient([])

    with pytest.raises(ValueError, match="checkpoint"):
        _collect(mismatch_root, mismatched_client, config=DataConfig(page_size=3))

    assert mismatched_client.calls == []
    checkpoint_path = mismatch_root / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["collection_snapshot_sha256"] = "0" * 64
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    checkpoint_client = _ScriptedPublicClient([])

    with pytest.raises(ValueError, match="snapshot"):
        _collect(mismatch_root, checkpoint_client)

    assert checkpoint_client.calls == []
