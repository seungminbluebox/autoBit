"""Backward pagination for raw public Upbit candle payloads."""

import pandas as pd

from autobit.data.upbit_public import UpbitPublicClient


def collect_range(client: UpbitPublicClient, start_utc: str, end_utc: str) -> pd.DataFrame:
    """Collect a deduplicated, ascending raw candle frame for the UTC range."""
    start = _parse_utc(start_utc)
    end = _parse_utc(end_utc)
    if start > end:
        raise ValueError("start_utc must not be after end_utc")

    records: dict[str, dict[str, object]] = {}
    to_utc = end_utc
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

    rows = [records[key] for key in sorted(records, key=lambda timestamp: _parse_utc(timestamp))]
    return pd.DataFrame(rows)


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
