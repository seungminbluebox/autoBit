import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
import shutil

import pandas as pd
import pytest

from autobit.config import DataConfig
from autobit.data.collector import collect_evidence_range
from autobit.data import storage
from autobit.data.quality import canonicalize_ohlcv
from autobit.data.upbit_public import PUBLIC_CANDLE_URL, PublicDataUnavailable
from autobit.cli import main


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

    def __init__(
        self,
        outcomes: list[list[dict[str, object]] | BaseException],
        *,
        config: DataConfig = DataConfig(page_size=2),
    ) -> None:
        self._outcomes = iter(outcomes)
        self.collection_config = config
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


def _replace_checkpoint_state(root: Path, state: dict[str, object]) -> None:
    """Write a hash-linked state fixture without changing its raw page evidence."""
    contents = storage._canonical_json_bytes(state)
    snapshot_hash = hashlib.sha256(contents).hexdigest()
    (root / f"collection-{snapshot_hash}.json").write_bytes(contents)
    checkpoint = {
        **state,
        "collection_snapshot": f"collection-{snapshot_hash}.json",
        "collection_snapshot_sha256": snapshot_hash,
    }
    (root / "checkpoint.json").write_bytes(storage._canonical_json_bytes(checkpoint))


def _checkpoint_state(root: Path) -> dict[str, object]:
    checkpoint = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))
    return {
        key: value
        for key, value in checkpoint.items()
        if key not in {"collection_snapshot", "collection_snapshot_sha256"}
    }


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
        "config",
        "config_sha256",
        "pages",
        "next_to_utc",
        "previous_oldest_utc",
        "complete",
        "collection_snapshot",
        "collection_snapshot_sha256",
    }
    assert checkpoint["config"] == {
        "market": "KRW-BTC",
        "candle_unit_minutes": 240,
        "page_size": 2,
        "years": 7,
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


def test_identical_cross_page_overlap_is_counted_and_uses_later_collected_provenance(
    tmp_path: Path,
) -> None:
    overlap = _candle("2026-01-01T04:00:00Z", 104)
    result = _collect(
        tmp_path,
        _ScriptedPublicClient(
            [
                [_candle("2026-01-01T08:00:00Z", 108), overlap],
                [dict(overlap), _candle("2026-01-01T00:00:00Z", 100), _candle("2025-12-31T20:00:00Z", 96)],
            ]
        ),
    )

    assert result.frame["candle_date_time_utc"].is_unique
    assert result.frame.attrs["duplicates"] == 1
    assert result.frame.attrs["duplicate_policy"] == "latest_collected"
    assert result.frame.attrs["duplicate_provenance"] == (
        ("2026-01-01T04:00:00Z", "page:0:row:1", "page:1:row:0"),
    )
    assert len(result.evidence.pages) == 2

    raw = result.frame.rename(
        columns={
            "opening_price": "open",
            "high_price": "high",
            "low_price": "low",
            "trade_price": "close",
            "candle_acc_trade_volume": "volume",
        }
    ).set_index("candle_date_time_utc")
    quality = canonicalize_ohlcv(
        raw,
        datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    assert quality.report.duplicates == 1
    assert quality.frame.attrs["duplicate_provenance"] == (
        ("2026-01-01T04:00:00Z", "page:0:row:1", "page:1:row:0"),
    )


def test_conflicting_cross_page_overlap_is_rejected_after_both_pages_are_durable(
    tmp_path: Path,
) -> None:
    first = _candle("2026-01-01T04:00:00Z", 104)
    conflicting = _candle("2026-01-01T04:00:00Z", 105)

    with pytest.raises(
        ValueError,
        match=r"conflicting duplicate OHLCV timestamp.*page:0:row:1.*page:1:row:0",
    ):
        _collect(
            tmp_path,
            _ScriptedPublicClient(
                [
                    [_candle("2026-01-01T08:00:00Z", 108), first],
                    [conflicting, _candle("2026-01-01T00:00:00Z", 100), _candle("2025-12-31T20:00:00Z", 96)],
                ]
            ),
        )

    assert len(tuple(tmp_path.glob("page-*.json"))) == 2


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
    assert not (tmp_path / "collection-manifest.json").exists()
    assert rerun.evidence.collection_snapshot_path is not None
    snapshot = json.loads(rerun.evidence.collection_snapshot_path.read_text(encoding="utf-8"))
    assert [page["sha256"] for page in snapshot["pages"]] == [
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
    mismatched_client = _ScriptedPublicClient([], config=DataConfig(page_size=3))

    with pytest.raises(ValueError, match="checkpoint"):
        collect_evidence_range(
            mismatched_client,
            start_utc="2026-01-01T00:00:00Z",
            end_utc="2026-01-01T12:00:00Z",
            evidence_root=mismatch_root,
        )

    assert mismatched_client.calls == []
    checkpoint_path = mismatch_root / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["collection_snapshot_sha256"] = "0" * 64
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    checkpoint_client = _ScriptedPublicClient([])

    with pytest.raises(ValueError, match="snapshot"):
        _collect(mismatch_root, checkpoint_client)

    assert checkpoint_client.calls == []


def test_evidence_collection_uses_client_config_or_rejects_an_explicit_mismatch(
    tmp_path: Path,
) -> None:
    """The evidence identity cannot describe a different request than the client sends."""
    evidence_root = tmp_path / "evidence"
    mismatched_client = _ScriptedPublicClient([], config=DataConfig(page_size=2))

    with pytest.raises(ValueError, match="client configuration"):
        collect_evidence_range(
            mismatched_client,
            start_utc="2026-01-01T00:00:00Z",
            end_utc="2026-01-01T12:00:00Z",
            evidence_root=evidence_root,
            config=DataConfig(page_size=3),
        )

    assert mismatched_client.calls == []
    assert not evidence_root.exists()

    configured_client = _ScriptedPublicClient(
        [[_candle("2026-01-01T04:00:00Z"), _candle("2025-12-31T20:00:00Z")]],
        config=DataConfig(page_size=2),
    )
    result = collect_evidence_range(
        configured_client,
        start_utc="2026-01-01T00:00:00Z",
        end_utc="2026-01-01T12:00:00Z",
        evidence_root=evidence_root,
    )

    assert result.evidence.page_size == 2
    assert result.evidence.candle_unit_minutes == 240


@pytest.mark.parametrize(
    ("failure_stage", "expected_resume_calls"),
    [
        ("page", ["2026-01-01T12:00:00Z", "2026-01-01T04:00:00Z"]),
        ("snapshot", ["2026-01-01T12:00:00Z", "2026-01-01T04:00:00Z"]),
        ("checkpoint", ["2026-01-01T04:00:00Z"]),
    ],
)
def test_persistence_failure_leaves_a_recoverable_collection_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    expected_resume_calls: list[str],
) -> None:
    """A crash at any persistence boundary leaves only harmless orphan evidence."""
    first_page = [_candle("2026-01-01T08:00:00Z", 108), _candle("2026-01-01T04:00:00Z", 104)]
    second_page = [
        _candle("2026-01-01T04:00:00Z", 104),
        _candle("2026-01-01T00:00:00Z", 100),
        _candle("2025-12-31T20:00:00Z", 96),
    ]
    original_write = storage._atomic_write

    def fail_at_stage(destination: Path, contents: bytes) -> None:
        is_page = destination.name.startswith("page-")
        is_snapshot = destination.name.startswith("collection-") and destination.name != "collection-manifest.json"
        is_checkpoint = destination.name == "checkpoint.json"
        if (failure_stage == "page" and is_page) or (
            failure_stage == "snapshot" and is_snapshot
        ) or (failure_stage == "checkpoint" and is_checkpoint):
            raise OSError(f"injected {failure_stage} write failure")
        original_write(destination, contents)

    with monkeypatch.context() as injected:
        injected.setattr(storage, "_atomic_write", fail_at_stage)
        with pytest.raises(OSError, match=failure_stage):
            _collect(tmp_path, _ScriptedPublicClient([first_page]))

    recovery_client = _ScriptedPublicClient(
        [second_page] if failure_stage == "checkpoint" else [first_page, second_page]
    )
    recovered = _collect(tmp_path, recovery_client)

    assert recovery_client.calls == expected_resume_calls
    assert recovered.frame["candle_date_time_utc"].tolist() == [
        "2026-01-01T00:00:00Z",
        "2026-01-01T04:00:00Z",
        "2026-01-01T08:00:00Z",
    ]


@pytest.mark.parametrize(
    ("failure_stage", "expected_resume_calls"),
    [
        ("page", ["2026-01-01T12:00:00Z", "2026-01-01T04:00:00Z"]),
        ("snapshot", ["2026-01-01T04:00:00Z"]),
        ("checkpoint", ["2026-01-01T04:00:00Z"]),
    ],
)
def test_post_persistence_failure_keeps_prior_or_recovered_state_usable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    expected_resume_calls: list[str],
) -> None:
    """An exception after fsync/replace leaves an old or fully valid new state."""
    first_page = [_candle("2026-01-01T08:00:00Z", 108), _candle("2026-01-01T04:00:00Z", 104)]
    second_page = [
        _candle("2026-01-01T04:00:00Z", 104),
        _candle("2026-01-01T00:00:00Z", 100),
        _candle("2025-12-31T20:00:00Z", 96),
    ]
    original_write = storage._atomic_write

    def fail_after_stage(destination: Path, contents: bytes) -> None:
        original_write(destination, contents)
        if (
            (failure_stage == "page" and destination.name.startswith("page-"))
            or (failure_stage == "snapshot" and destination.name.startswith("collection-"))
            or (failure_stage == "checkpoint" and destination.name == "checkpoint.json")
        ):
            raise OSError(f"injected post-{failure_stage} write failure")

    with monkeypatch.context() as injected:
        injected.setattr(storage, "_atomic_write", fail_after_stage)
        with pytest.raises(OSError, match=f"post-{failure_stage}"):
            _collect(tmp_path, _ScriptedPublicClient([first_page]))

    recovery_client = _ScriptedPublicClient(
        [first_page, second_page] if failure_stage == "page" else [second_page]
    )
    recovered = _collect(tmp_path, recovery_client)

    assert recovery_client.calls == expected_resume_calls
    assert recovered.evidence.complete


def test_resume_uses_checkpoint_snapshot_without_a_mutable_manifest_alias(
    tmp_path: Path,
) -> None:
    """The checkpoint and its immutable snapshot are sufficient resume authority."""
    _collect(
        tmp_path,
        _ScriptedPublicClient([[_candle("2026-01-01T04:00:00Z"), _candle("2025-12-31T20:00:00Z")]]),
    )
    assert not (tmp_path / "collection-manifest.json").exists()
    rerun_client = _ScriptedPublicClient([])

    rerun = _collect(tmp_path, rerun_client)

    assert rerun.evidence.complete
    assert rerun_client.calls == []


def test_no_checkpoint_recovers_the_tip_of_one_unambiguous_snapshot_chain(
    tmp_path: Path,
) -> None:
    """A durable snapshot after a checkpoint-write crash avoids refetching its page."""
    _collect(
        tmp_path,
        _ScriptedPublicClient(
            [
                [_candle("2026-01-01T08:00:00Z", 108), _candle("2026-01-01T04:00:00Z", 104)],
                [_candle("2026-01-01T04:00:00Z", 104), _candle("2026-01-01T00:00:00Z", 100), _candle("2025-12-31T20:00:00Z", 96)],
            ]
        ),
    )
    (tmp_path / "checkpoint.json").unlink()
    assert not (tmp_path / "collection-manifest.json").exists()
    recovery_client = _ScriptedPublicClient([])

    recovered = _collect(tmp_path, recovery_client)

    assert recovered.evidence.complete
    assert recovery_client.calls == []
    assert len(recovered.evidence.pages) == 2


def test_no_checkpoint_rejects_conflicting_snapshot_chains_but_checkpoint_ignores_orphans(
    tmp_path: Path,
) -> None:
    """Only an unambiguous snapshot tip may replace a missing checkpoint authority."""
    primary = tmp_path / "primary"
    alternate = tmp_path / "alternate"
    _collect(
        primary,
        _ScriptedPublicClient([[_candle("2026-01-01T04:00:00Z", 100), _candle("2025-12-31T20:00:00Z", 96)]]),
    )
    _collect(
        alternate,
        _ScriptedPublicClient([[_candle("2026-01-01T04:00:00Z", 200), _candle("2025-12-31T20:00:00Z", 196)]]),
    )
    for source in [*alternate.glob("page-*.json"), *alternate.glob("collection-*.json")]:
        if source.name != "collection-manifest.json":
            shutil.copy2(source, primary / source.name)

    checkpoint_client = _ScriptedPublicClient([])
    checked = _collect(primary, checkpoint_client)
    assert checked.evidence.complete
    assert checkpoint_client.calls == []

    (primary / "checkpoint.json").unlink()
    assert not (primary / "collection-manifest.json").exists()
    ambiguous_client = _ScriptedPublicClient([])

    with pytest.raises(ValueError, match="ambiguous"):
        _collect(primary, ambiguous_client)

    assert ambiguous_client.calls == []


def test_no_checkpoint_ignores_a_nonsequential_orphan_snapshot(
    tmp_path: Path,
) -> None:
    """A malformed orphan does not break recovery from the valid snapshot chain."""
    _collect(
        tmp_path,
        _ScriptedPublicClient([[_candle("2026-01-01T04:00:00Z"), _candle("2025-12-31T20:00:00Z")]]),
    )
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    conflicting = {
        key: value
        for key, value in checkpoint.items()
        if key not in {"collection_snapshot", "collection_snapshot_sha256"}
    }
    conflicting["next_to_utc"] = "2026-01-01T12:00:00Z"
    conflicting["complete"] = False
    contents = storage._canonical_json_bytes(conflicting)
    digest = hashlib.sha256(contents).hexdigest()
    (tmp_path / f"collection-{digest}.json").write_bytes(contents)
    (tmp_path / "checkpoint.json").unlink()
    recovery_client = _ScriptedPublicClient([])

    recovered = _collect(tmp_path, recovery_client)

    assert recovery_client.calls == []
    assert recovered.evidence.complete


def test_checkpoint_with_a_nonsequential_request_boundary_fails_before_network(
    tmp_path: Path,
) -> None:
    """A hash-valid checkpoint must still bind every page to the prior boundary."""
    with pytest.raises(PublicDataUnavailable):
        _collect(
            tmp_path,
            _ScriptedPublicClient(
                [
                    [_candle("2026-01-01T08:00:00Z"), _candle("2026-01-01T04:00:00Z")],
                    PublicDataUnavailable("temporary outage"),
                ]
            ),
        )
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    state = {
        key: value
        for key, value in checkpoint.items()
        if key not in {"collection_snapshot", "collection_snapshot_sha256"}
    }
    state["next_to_utc"] = "2026-01-01T12:00:00Z"
    contents = storage._canonical_json_bytes(state)
    digest = hashlib.sha256(contents).hexdigest()
    (tmp_path / f"collection-{digest}.json").write_bytes(contents)
    checkpoint = {
        **state,
        "collection_snapshot": f"collection-{digest}.json",
        "collection_snapshot_sha256": digest,
    }
    (tmp_path / "checkpoint.json").write_bytes(storage._canonical_json_bytes(checkpoint))
    recovery_client = _ScriptedPublicClient([])

    with pytest.raises(ValueError, match="pagination"):
        _collect(tmp_path, recovery_client)

    assert recovery_client.calls == []


def test_repeated_page_does_not_advance_checkpoint_and_restart_uses_last_boundary(
    tmp_path: Path,
) -> None:
    """A no-progress response is rejected before it can become recovery authority."""
    first_page = [_candle("2026-01-01T08:00:00Z", 108), _candle("2026-01-01T04:00:00Z", 104)]

    with pytest.raises(ValueError, match="move backward"):
        _collect(tmp_path, _ScriptedPublicClient([first_page, first_page]))

    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert len(checkpoint["pages"]) == 1
    assert checkpoint["next_to_utc"] == "2026-01-01T04:00:00Z"
    resumed_client = _ScriptedPublicClient(
        [
            [
                _candle("2026-01-01T04:00:00Z", 104),
                _candle("2026-01-01T00:00:00Z", 100),
                _candle("2025-12-31T20:00:00Z", 96),
            ]
        ]
    )

    resumed = _collect(tmp_path, resumed_client)

    assert resumed_client.calls == ["2026-01-01T04:00:00Z"]
    assert resumed.evidence.complete
    assert len(resumed.evidence.pages) == 2


def test_completed_checkpoint_without_a_terminal_response_is_rejected_before_network(
    tmp_path: Path,
) -> None:
    """A completed range is meaningful only when an API terminal response exists."""
    _collect(tmp_path, _ScriptedPublicClient([[]]))
    state = _checkpoint_state(tmp_path)
    state["pages"] = []
    state["next_to_utc"] = "2026-01-01T12:00:00Z"
    state["previous_oldest_utc"] = None
    state["complete"] = True
    _replace_checkpoint_state(tmp_path, state)
    client = _ScriptedPublicClient([])

    with pytest.raises(ValueError, match="terminal response"):
        _collect(tmp_path, client)

    assert client.calls == []


def test_incomplete_checkpoint_that_already_crossed_start_is_rejected_before_network(
    tmp_path: Path,
) -> None:
    """A terminal non-empty page cannot be relabeled as resumable evidence."""
    _collect(
        tmp_path,
        _ScriptedPublicClient(
            [[_candle("2026-01-01T04:00:00Z"), _candle("2025-12-31T20:00:00Z")]]
        ),
    )
    state = _checkpoint_state(tmp_path)
    state["complete"] = False
    _replace_checkpoint_state(tmp_path, state)
    client = _ScriptedPublicClient([])

    with pytest.raises(ValueError, match="incomplete.*crossed"):
        _collect(tmp_path, client)

    assert client.calls == []


def test_nonempty_page_crossing_start_is_an_accepted_terminal_response(
    tmp_path: Path,
) -> None:
    """The first page whose oldest candle predates start completes the chain."""
    result = _collect(
        tmp_path,
        _ScriptedPublicClient(
            [[_candle("2026-01-01T04:00:00Z"), _candle("2025-12-31T20:00:00Z")]]
        ),
    )
    rerun_client = _ScriptedPublicClient([])

    rerun = _collect(tmp_path, rerun_client)

    assert result.evidence.complete
    assert result.evidence.pages[-1].oldest_timestamp_utc == "2025-12-31T20:00:00Z"
    assert rerun.evidence.complete
    assert rerun_client.calls == []


def test_final_empty_page_is_an_accepted_terminal_response(tmp_path: Path) -> None:
    """A final empty response also establishes a completed evidence chain."""
    result = _collect(
        tmp_path,
        _ScriptedPublicClient(
            [
                [_candle("2026-01-01T08:00:00Z"), _candle("2026-01-01T04:00:00Z")],
                [],
            ]
        ),
    )
    rerun_client = _ScriptedPublicClient([])

    rerun = _collect(tmp_path, rerun_client)

    assert result.evidence.complete
    assert result.evidence.pages[-1].row_count == 0
    assert result.evidence.pages[-1].oldest_timestamp_utc is None
    assert rerun.evidence.complete
    assert rerun_client.calls == []


def test_checkpointless_recovery_ignores_a_longer_nonprogress_snapshot_tip(
    tmp_path: Path,
) -> None:
    """Recovery must not select a hash-valid duplicate-page snapshot as its tip."""
    first_page = [_candle("2026-01-01T08:00:00Z"), _candle("2026-01-01T04:00:00Z")]
    with pytest.raises(PublicDataUnavailable):
        _collect(
            tmp_path,
            _ScriptedPublicClient([first_page, PublicDataUnavailable("temporary outage")]),
        )
    state = _checkpoint_state(tmp_path)
    pages = state["pages"]
    assert isinstance(pages, list)
    duplicate = dict(pages[0])
    duplicate["request_to_utc"] = "2026-01-01T04:00:00Z"
    state["pages"] = [*pages, duplicate]
    state["next_to_utc"] = "2026-01-01T04:00:00Z"
    state["previous_oldest_utc"] = "2026-01-01T04:00:00Z"
    _replace_checkpoint_state(tmp_path, state)
    (tmp_path / "checkpoint.json").unlink()

    recovered = storage.load_collection_evidence(
        tmp_path,
        source_url=PUBLIC_CANDLE_URL,
        start_utc="2026-01-01T00:00:00Z",
        end_utc="2026-01-01T12:00:00Z",
        config=DataConfig(page_size=2),
    )

    assert len(recovered.pages) == 1
    assert recovered.next_to_utc == "2026-01-01T04:00:00Z"


def test_data_quality_accepts_completed_evidence_chain_offline(tmp_path: Path) -> None:
    """Quality derives the raw frame from completed evidence without HTTP."""
    evidence_root = tmp_path / "evidence"
    _collect(
        evidence_root,
        _ScriptedPublicClient(
            [
                [
                    _candle("2026-01-01T08:00:00Z", 108),
                    _candle("2026-01-01T04:00:00Z", 104),
                ],
                [
                    _candle("2026-01-01T04:00:00Z", 104),
                    _candle("2026-01-01T00:00:00Z", 100),
                    _candle("2025-12-31T20:00:00Z", 96),
                ],
            ]
        ),
    )
    output = tmp_path / "quality"

    assert main(["data-quality", "--input", str(evidence_root), "--output", str(output)]) == 0

    processed = pd.read_csv(output / "processed.csv")
    assert processed["timestamp"].tolist() == [
        "2026-01-01 00:00:00+00:00",
        "2026-01-01 04:00:00+00:00",
        "2026-01-01 08:00:00+00:00",
    ]


def test_data_quality_rejects_incomplete_or_corrupted_evidence_before_writing(
    tmp_path: Path,
) -> None:
    """Evidence handoff validates completion and every immutable page first."""
    incomplete_root = tmp_path / "incomplete"
    first_page = [_candle("2026-01-01T08:00:00Z"), _candle("2026-01-01T04:00:00Z")]
    with pytest.raises(PublicDataUnavailable):
        _collect(
            incomplete_root,
            _ScriptedPublicClient([first_page, PublicDataUnavailable("temporary outage")]),
        )

    with pytest.raises(ValueError, match="incomplete collection evidence"):
        main(
            [
                "data-quality",
                "--input",
                str(incomplete_root),
                "--output",
                str(tmp_path / "incomplete-quality"),
            ]
        )
    assert not (tmp_path / "incomplete-quality").exists()

    corrupted_root = tmp_path / "corrupted"
    completed = _collect(
        corrupted_root,
        _ScriptedPublicClient([[_candle("2026-01-01T04:00:00Z"), _candle("2025-12-31T20:00:00Z")]]),
    )
    completed.evidence.pages[0].path.write_bytes(b"corrupt")

    with pytest.raises(ValueError, match="raw page evidence"):
        main(
            [
                "data-quality",
                "--input",
                str(corrupted_root),
                "--output",
                str(tmp_path / "corrupted-quality"),
            ]
        )
    assert not (tmp_path / "corrupted-quality").exists()
