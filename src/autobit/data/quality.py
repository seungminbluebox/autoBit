"""Validation and canonicalization for completed KRW-BTC four-hour candles."""

from dataclasses import dataclass
from datetime import datetime
import math

import pandas as pd


_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
_FREQUENCY = pd.Timedelta(hours=4)
_ENTRY_LOOKBACK = 200
_DEFAULT_SPIKE_RANGE_RATIO = 0.50


@dataclass(frozen=True, slots=True)
class QualityReport:
    total_bars: int
    duplicates: int
    conflicting_duplicates: int
    short_gap_bars: int
    long_gap_regions: int
    impossible_candles: int
    nonpositive_prices: int
    negative_volume: int
    zero_volume: int
    spike_flags: int
    removed_partial_bars: int


@dataclass(frozen=True, slots=True)
class QualityResult:
    frame: pd.DataFrame
    report: QualityReport


def canonicalize_ohlcv(
    raw: pd.DataFrame,
    now_utc: datetime,
    *,
    spike_range_ratio: float = _DEFAULT_SPIKE_RANGE_RATIO,
) -> QualityResult:
    """Return a time-ordered, UTC, completed-candle frame and its quality report."""
    spike_range_ratio = _validate_spike_range_ratio(spike_range_ratio)
    frame = raw.copy()
    frame.columns = frame.columns.str.lower().str.strip()
    frame.index = _as_utc_index(frame.index)
    _validate_four_hour_boundaries(frame.index)
    _require_ohlcv_schema(frame)
    frame = frame.loc[:, _OHLCV_COLUMNS].sort_index()
    frame = _deduplicate_or_raise(frame)
    _coerce_float64(frame)
    frame = _quarantine_invalid_values(frame)
    frame = _drop_unclosed_last_bar(frame, now_utc, _FREQUENCY)
    frame = _reindex_and_fill_single_gaps(frame, _FREQUENCY)
    frame = _flag_spikes_and_flat_bars(frame, spike_range_ratio)
    report = _build_quality_report(frame)
    return QualityResult(frame=frame, report=report)


def _as_utc_index(index: pd.Index) -> pd.DatetimeIndex:
    timestamps = pd.to_datetime(index, utc=True, errors="raise")
    if timestamps.isna().any():
        raise ValueError("OHLCV index contains missing timestamps")
    return pd.DatetimeIndex(timestamps)


def _validate_four_hour_boundaries(timestamps: pd.DatetimeIndex) -> None:
    off_grid = timestamps[timestamps.asi8 % _FREQUENCY.value != 0]
    if len(off_grid):
        rendered = ", ".join(timestamp.isoformat() for timestamp in off_grid.unique())
        raise ValueError(f"OHLCV timestamps must align to a UTC 4-hour boundary: {rendered}")


def _validate_spike_range_ratio(value: float) -> float:
    try:
        ratio = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("spike_range_ratio must be finite and greater than zero") from error
    if not math.isfinite(ratio) or ratio <= 0:
        raise ValueError("spike_range_ratio must be finite and greater than zero")
    return ratio


def _require_ohlcv_schema(frame: pd.DataFrame) -> None:
    missing = sorted(set(_OHLCV_COLUMNS).difference(frame.columns))
    if missing:
        raise ValueError(f"OHLCV frame is missing required columns: {', '.join(missing)}")


def _deduplicate_or_raise(frame: pd.DataFrame) -> pd.DataFrame:
    duplicate_rows = int(frame.index.duplicated(keep=False).sum())
    if not duplicate_rows:
        frame.attrs["duplicates"] = 0
        frame.attrs["conflicting_duplicates"] = 0
        return frame

    for _, rows in frame.groupby(level=0, sort=False):
        if len(rows) > 1 and rows.nunique(dropna=False).max() > 1:
            raise ValueError("conflicting duplicate OHLCV timestamps")

    result = frame.loc[~frame.index.duplicated(keep="first")].copy()
    result.attrs["duplicates"] = len(frame) - len(result)
    result.attrs["conflicting_duplicates"] = 0
    return result


def _coerce_float64(frame: pd.DataFrame) -> None:
    for column in _OHLCV_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")


def _quarantine_invalid_values(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    ohlc = result.loc[:, ["open", "high", "low", "close"]]
    missing_values = ohlc.isna().any(axis=1) | result["volume"].isna()
    impossible = (
        (result["high"] < result[["open", "close", "low"]].max(axis=1))
        | (result["low"] > result[["open", "close", "high"]].min(axis=1))
        | (result["high"] < result["low"])
    )
    nonpositive = (ohlc <= 0).any(axis=1)
    negative_volume = result["volume"] < 0
    quarantined = missing_values | impossible | nonpositive | negative_volume

    result["is_quarantined"] = quarantined
    result["is_filled"] = False
    result["anomaly_spike"] = False
    result["anomaly_flat"] = False
    result["segment_id"] = 0
    result.loc[quarantined, _OHLCV_COLUMNS] = float("nan")
    result.attrs["impossible_candles"] = int(impossible.sum())
    result.attrs["nonpositive_prices"] = int(nonpositive.sum())
    result.attrs["negative_volume"] = int(negative_volume.sum())
    return result


def _drop_unclosed_last_bar(frame: pd.DataFrame, now_utc: datetime, frequency: pd.Timedelta) -> pd.DataFrame:
    now = pd.Timestamp(now_utc)
    if now.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    now = now.tz_convert("UTC")
    complete = frame.index + frequency <= now
    result = frame.loc[complete].copy()
    result.attrs = frame.attrs.copy()
    result.attrs["removed_partial_bars"] = int((~complete).sum())
    return result


def _reindex_and_fill_single_gaps(frame: pd.DataFrame, frequency: pd.Timedelta) -> pd.DataFrame:
    if frame.empty:
        frame.attrs["short_gap_bars"] = 0
        frame.attrs["long_gap_regions"] = 0
        return frame

    original = frame.copy()
    full_index = pd.date_range(original.index.min(), original.index.max(), freq=frequency, tz="UTC")
    result = original.reindex(full_index)
    inserted = ~result.index.isin(original.index)
    for column in ("is_filled", "is_quarantined", "anomaly_spike", "anomaly_flat"):
        result[column] = result[column].eq(True)

    short_gap_bars = 0
    long_gap_regions = 0
    segment_id = 0
    segments: list[int] = []
    position = 0
    while position < len(result):
        if not inserted[position]:
            segments.append(segment_id)
            position += 1
            continue

        start = position
        while position < len(result) and inserted[position]:
            position += 1
        gap_size = position - start
        if gap_size == 1 and start > 0:
            prior_close = result.iloc[start - 1]["close"]
            if pd.notna(prior_close):
                result.iloc[start, result.columns.get_indexer(_OHLCV_COLUMNS)] = [
                    prior_close,
                    prior_close,
                    prior_close,
                    prior_close,
                    0.0,
                ]
                result.iloc[start, result.columns.get_loc("is_filled")] = True
                short_gap_bars += 1
                segments.append(segment_id)
                continue

        long_gap_regions += 1
        segments.extend([segment_id] * gap_size)
        segment_id += 1

    result["segment_id"] = segments
    result.attrs = original.attrs.copy()
    result.attrs["short_gap_bars"] = short_gap_bars
    result.attrs["long_gap_regions"] = long_gap_regions
    return result


def _flag_spikes_and_flat_bars(frame: pd.DataFrame, spike_range_ratio: float) -> pd.DataFrame:
    result = frame.copy()
    tradable = result.loc[:, ["open", "high", "low", "close"]].notna().all(axis=1)
    price_range = result["high"] - result["low"]
    result["anomaly_spike"] = tradable & ((price_range / result["close"]) > spike_range_ratio)
    result["anomaly_flat"] = tradable & (price_range == 0) & ~result["is_filled"]
    result.attrs = frame.attrs.copy()
    return _set_entry_data_valid(result)


def _set_entry_data_valid(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    unverified = result["anomaly_spike"] | result["anomaly_flat"]
    contamination = result["is_filled"] | result["is_quarantined"] | unverified
    recent_contamination = (
        contamination.groupby(result["segment_id"], sort=False)
        .transform(lambda rows: rows.rolling(_ENTRY_LOOKBACK, min_periods=1).max())
        .astype(bool)
    )
    valid_candle = result.loc[:, _OHLCV_COLUMNS].notna().all(axis=1)
    result["entry_data_valid"] = valid_candle & ~recent_contamination
    return result


def _build_quality_report(frame: pd.DataFrame) -> QualityReport:
    return QualityReport(
        total_bars=len(frame),
        duplicates=int(frame.attrs.get("duplicates", 0)),
        conflicting_duplicates=int(frame.attrs.get("conflicting_duplicates", 0)),
        short_gap_bars=int(frame.attrs.get("short_gap_bars", 0)),
        long_gap_regions=int(frame.attrs.get("long_gap_regions", 0)),
        impossible_candles=int(frame.attrs.get("impossible_candles", 0)),
        nonpositive_prices=int(frame.attrs.get("nonpositive_prices", 0)),
        negative_volume=int(frame.attrs.get("negative_volume", 0)),
        zero_volume=int((frame["volume"] == 0).sum()),
        spike_flags=int(frame["anomaly_spike"].sum()),
        removed_partial_bars=int(frame.attrs.get("removed_partial_bars", 0)),
    )
