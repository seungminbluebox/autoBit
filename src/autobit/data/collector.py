"""Backward pagination and durable evidence for public Upbit candle payloads."""

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from autobit.config import DataConfig
from autobit.data.storage import (
    CollectionEvidence,
    load_completed_collection_evidence,
    load_collection_evidence,
    load_raw_pages,
    persist_raw_page,
)
from autobit.data.upbit_public import UpbitPublicClient, parse_candle_utc


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """A normalized, deduplicated view derived from validated raw page evidence."""

    frame: pd.DataFrame
    evidence: CollectionEvidence


def load_completed_evidence_frame(evidence_root: Path) -> pd.DataFrame:
    """Derive the canonical raw view from a completed, validated evidence chain."""
    evidence = load_completed_collection_evidence(evidence_root)
    start, end = _validated_range(evidence.start_utc, evidence.end_utc)
    return _frame_from_pages(load_raw_pages(evidence), start, end)


def collect_range(
    client: UpbitPublicClient,
    start_utc: str,
    end_utc: str,
    *,
    evidence_root: Path | None = None,
    config: DataConfig | None = None,
) -> pd.DataFrame:
    """Collect a deduplicated, ascending raw candle frame for the UTC range.

    Legacy callers may omit ``evidence_root``. Production callers must use
    :func:`collect_evidence_range` so raw pages are durable before progress
    advances.
    """
    if evidence_root is not None:
        return collect_evidence_range(
            client,
            start_utc,
            end_utc,
            evidence_root=evidence_root,
            config=config,
        ).frame
    return _collect_legacy_range(client, start_utc, end_utc)


def collect_evidence_range(
    client: UpbitPublicClient,
    start_utc: str,
    end_utc: str,
    *,
    evidence_root: Path,
    config: DataConfig | None = None,
) -> CollectionResult:
    """Resume or complete a range, preserving each exact page before progress."""
    client_config = client.collection_config
    if config is not None and config != client_config:
        raise ValueError("explicit configuration does not match client configuration")
    start, end = _validated_range(start_utc, end_utc)
    start_text = _format_utc(start)
    end_text = _format_utc(end)
    evidence = load_collection_evidence(
        evidence_root,
        source_url=client.source_url,
        start_utc=start_text,
        end_utc=end_text,
        config=client_config,
    )
    if evidence.complete:
        return CollectionResult(
            _frame_from_pages(load_raw_pages(evidence), start, end), evidence
        )

    while True:
        request_to_utc = evidence.next_to_utc
        request_boundary = _parse_utc(request_to_utc)
        page = client.fetch_page(request_to_utc)
        if not page:
            evidence = persist_raw_page(
                evidence,
                page,
                request_to_utc=request_to_utc,
                oldest_timestamp_utc=None,
                next_to_utc=request_to_utc,
                complete=True,
            )
            return CollectionResult(
                _frame_from_pages(load_raw_pages(evidence), start, end), evidence
            )

        timestamps = [_parse_candle_utc(_timestamp_from(row)) for row in page]
        oldest = min(timestamps)
        if oldest >= request_boundary:
            raise ValueError("public candle page did not move backward")
        oldest_text = _format_utc(oldest)
        complete = oldest < start
        evidence = persist_raw_page(
            evidence,
            page,
            request_to_utc=request_to_utc,
            oldest_timestamp_utc=oldest_text,
            next_to_utc=oldest_text,
            complete=complete,
        )
        if complete:
            return CollectionResult(
                _frame_from_pages(load_raw_pages(evidence), start, end), evidence
            )


def _collect_legacy_range(
    client: UpbitPublicClient, start_utc: str, end_utc: str
) -> pd.DataFrame:
    """Keep the original in-memory behavior for explicitly legacy callers."""
    start, end = _validated_range(start_utc, end_utc)
    pages: list[list[dict[str, object]]] = []
    to_utc = _format_utc(end)
    previous_oldest: pd.Timestamp | None = None

    while True:
        page = client.fetch_page(to_utc)
        if not page:
            break

        timestamps = [_parse_candle_utc(_timestamp_from(row)) for row in page]
        oldest = min(timestamps)
        pages.append(page)

        if oldest < start:
            break
        if previous_oldest is not None and oldest >= previous_oldest:
            break

        previous_oldest = oldest
        to_utc = _format_utc(oldest)

    return _frame_from_pages(tuple(pages), start, end)


def _frame_from_pages(
    pages: tuple[list[dict[str, object]], ...], start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame:
    """Derive the normalized view separately from exact ordered page evidence."""
    records: dict[pd.Timestamp, tuple[dict[str, object], str]] = {}
    duplicate_provenance: list[tuple[str, str, str]] = []
    for page_index, page in enumerate(pages):
        for row_index, row in enumerate(page):
            timestamp_text = _timestamp_from(row)
            timestamp = _parse_candle_utc(timestamp_text)
            if start <= timestamp <= end:
                provenance = f"page:{page_index}:row:{row_index}"
                existing = records.get(timestamp)
                if existing is not None:
                    existing_row, existing_provenance = existing
                    if existing_row != row:
                        raise ValueError(
                            "conflicting duplicate OHLCV timestamp "
                            f"{_format_utc(timestamp)} between "
                            f"{existing_provenance} and {provenance}"
                        )
                    duplicate_provenance.append(
                        (
                            _format_utc(timestamp),
                            existing_provenance,
                            provenance,
                        )
                    )
                # The later collected identical observation is the deterministic
                # normalized representative; exact ordered pages remain evidence.
                records[timestamp] = (row, provenance)
    rows = [records[timestamp][0] for timestamp in sorted(records)]
    frame = pd.DataFrame(rows)
    frame.attrs["duplicates"] = len(duplicate_provenance)
    frame.attrs["conflicting_duplicates"] = 0
    frame.attrs["duplicate_policy"] = "latest_collected"
    frame.attrs["duplicate_provenance"] = tuple(duplicate_provenance)
    return frame


def _validated_range(start_utc: str, end_utc: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = _parse_utc(start_utc)
    end = _parse_utc(end_utc)
    if start > end:
        raise ValueError("start_utc must not be after end_utc")
    return start, end


def _timestamp_from(row: dict[str, object]) -> str:
    value = row.get("candle_date_time_utc")
    if not isinstance(value, str):
        raise ValueError("public candle is missing candle_date_time_utc")
    return value


def _parse_utc(value: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware UTC values")
    return timestamp.tz_convert("UTC")


def _parse_candle_utc(value: str) -> pd.Timestamp:
    return pd.Timestamp(parse_candle_utc(value))


def _format_utc(timestamp: pd.Timestamp) -> str:
    return timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
