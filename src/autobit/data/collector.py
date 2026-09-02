"""Backward pagination and durable evidence for public Upbit candle payloads."""

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from autobit.config import DataConfig
from autobit.data.storage import (
    CollectionEvidence,
    load_collection_evidence,
    load_raw_pages,
    persist_raw_page,
)
from autobit.data.upbit_public import UpbitPublicClient


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """A normalized, deduplicated view derived from validated raw page evidence."""

    frame: pd.DataFrame
    evidence: CollectionEvidence


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
        if config is None:
            raise ValueError("config is required when evidence_root is supplied")
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
    config: DataConfig,
) -> CollectionResult:
    """Resume or complete a range, preserving each exact page before progress."""
    start, end = _validated_range(start_utc, end_utc)
    start_text = _format_utc(start)
    end_text = _format_utc(end)
    evidence = load_collection_evidence(
        evidence_root,
        source_url=client.source_url,
        start_utc=start_text,
        end_utc=end_text,
        config=config,
    )
    if evidence.complete:
        return CollectionResult(
            _frame_from_pages(load_raw_pages(evidence), start, end), evidence
        )

    while True:
        request_to_utc = evidence.next_to_utc
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

        timestamps = [_parse_utc(_timestamp_from(row)) for row in page]
        oldest = min(timestamps)
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
        if len(evidence.pages) > 1:
            prior_oldest = evidence.pages[-2].oldest_timestamp_utc
            if prior_oldest is not None and oldest >= _parse_utc(prior_oldest):
                raise ValueError("public candle page did not move backward")


def _collect_legacy_range(
    client: UpbitPublicClient, start_utc: str, end_utc: str
) -> pd.DataFrame:
    """Keep the original in-memory behavior for explicitly legacy callers."""
    start, end = _validated_range(start_utc, end_utc)
    records: dict[str, dict[str, object]] = {}
    to_utc = _format_utc(end)
    previous_oldest: pd.Timestamp | None = None

    while True:
        page = client.fetch_page(to_utc)
        if not page:
            break

        timestamps = [_parse_utc(_timestamp_from(row)) for row in page]
        oldest = min(timestamps)
        for row, timestamp in zip(page, timestamps, strict=True):
            if start <= timestamp <= end:
                records.setdefault(_timestamp_from(row), row)

        if oldest < start:
            break
        if previous_oldest is not None and oldest >= previous_oldest:
            break

        previous_oldest = oldest
        to_utc = _format_utc(oldest)

    rows = [records[key] for key in sorted(records, key=_parse_utc)]
    return pd.DataFrame(rows)


def _frame_from_pages(
    pages: tuple[list[dict[str, object]], ...], start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame:
    """Derive the normalized view separately from exact ordered page evidence."""
    records: dict[str, dict[str, object]] = {}
    for page in pages:
        for row in page:
            timestamp_text = _timestamp_from(row)
            timestamp = _parse_utc(timestamp_text)
            if start <= timestamp <= end:
                records.setdefault(timestamp_text, row)
    rows = [records[key] for key in sorted(records, key=_parse_utc)]
    return pd.DataFrame(rows)


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


def _format_utc(timestamp: pd.Timestamp) -> str:
    return timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
