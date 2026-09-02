from datetime import datetime, timezone
import warnings

import pandas as pd
import pytest

from autobit.data.quality import canonicalize_ohlcv


def make_frame(times: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0] * len(times),
            "high": [102.0] * len(times),
            "low": [99.0] * len(times),
            "close": [101.0] * len(times),
            "volume": [2.0] * len(times),
        },
        index=pd.to_datetime(times, utc=True),
    )


def test_single_gap_is_flat_filled_and_flagged() -> None:
    """A missing interior candle must be made flat so time-series tools retain cadence."""
    raw = make_frame(["2026-01-01T00:00:00Z", "2026-01-01T08:00:00Z"])

    result = canonicalize_ohlcv(raw, datetime(2026, 1, 2, tzinfo=timezone.utc))

    filled = result.frame.loc[pd.Timestamp("2026-01-01T04:00:00Z")]
    assert filled[["open", "high", "low", "close"]].tolist() == [101.0] * 4
    assert filled["volume"] == 0.0
    assert bool(filled["is_filled"])
    assert result.report.short_gap_bars == 1


def test_long_gap_is_not_filled() -> None:
    """Two omitted candles form a discontinuity, not fabricated price history."""
    raw = make_frame(["2026-01-01T00:00:00Z", "2026-01-01T12:00:00Z"])

    result = canonicalize_ohlcv(raw, datetime(2026, 1, 2, tzinfo=timezone.utc))

    assert pd.isna(result.frame.loc[pd.Timestamp("2026-01-01T04:00:00Z"), "close"])
    assert result.report.long_gap_regions == 1


def test_impossible_candle_is_quarantined_and_reported() -> None:
    """An OHLC bound violation must be unavailable for entry decisions."""
    raw = make_frame(["2026-01-01T00:00:00Z"])
    raw.loc[:, "high"] = 98.0

    result = canonicalize_ohlcv(raw, datetime(2026, 1, 2, tzinfo=timezone.utc))

    assert result.report.impossible_candles == 1
    assert pd.isna(result.frame.iloc[0]["close"])
    assert not bool(result.frame.iloc[0]["entry_data_valid"])


def test_conflicting_duplicate_timestamp_is_rejected() -> None:
    """Competing versions of a candle must not be silently chosen."""
    raw = make_frame(["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"])
    raw.iloc[1, raw.columns.get_loc("close")] = 103.0

    with pytest.raises(ValueError, match="conflicting duplicate"):
        canonicalize_ohlcv(raw, datetime(2026, 1, 2, tzinfo=timezone.utc))


def test_identical_duplicate_is_collapsed_and_reported_once() -> None:
    """Repeated copies of the same candle count as one duplicate removal."""
    raw = make_frame(["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"])

    result = canonicalize_ohlcv(raw, datetime(2026, 1, 2, tzinfo=timezone.utc))

    assert len(result.frame) == 1
    assert result.report.duplicates == 1


def test_unclosed_last_bar_is_removed_using_utc_now() -> None:
    """A bar that ends after now must never leak into a backtest decision."""
    raw = make_frame(["2026-01-01T00:00:00Z", "2026-01-01T04:00:00Z"])

    result = canonicalize_ohlcv(raw, datetime(2026, 1, 1, 6, tzinfo=timezone.utc))

    assert result.frame.index.tolist() == [pd.Timestamp("2026-01-01T00:00:00Z")]
    assert result.report.removed_partial_bars == 1


def test_prior_filled_bar_blocks_entries_for_the_rest_of_its_segment() -> None:
    """A fabricated candle in the 200-bar lookback invalidates later entry data."""
    raw = make_frame(
        [
            "2026-01-01T00:00:00Z",
            "2026-01-01T08:00:00Z",
            "2026-01-01T12:00:00Z",
        ]
    )

    result = canonicalize_ohlcv(raw, datetime(2026, 1, 2, tzinfo=timezone.utc))

    assert not bool(result.frame.loc[pd.Timestamp("2026-01-01T12:00:00Z"), "entry_data_valid"])


def test_long_gap_starts_a_clean_entry_segment() -> None:
    """Old bad data cannot invalidate rows after a long discontinuity."""
    raw = make_frame(["2026-01-01T00:00:00Z", "2026-01-01T12:00:00Z"])

    result = canonicalize_ohlcv(raw, datetime(2026, 1, 2, tzinfo=timezone.utc))

    assert result.frame.loc[pd.Timestamp("2026-01-01T00:00:00Z"), "segment_id"] == 0
    assert result.frame.loc[pd.Timestamp("2026-01-01T12:00:00Z"), "segment_id"] == 1
    assert bool(result.frame.loc[pd.Timestamp("2026-01-01T12:00:00Z"), "entry_data_valid"])


def test_single_gap_normalization_emits_no_pandas_future_warnings() -> None:
    """Canonicalization should remain compatible with pandas' future dtype rules."""
    raw = make_frame(["2026-01-01T00:00:00Z", "2026-01-01T08:00:00Z"])

    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        canonicalize_ohlcv(raw, datetime(2026, 1, 2, tzinfo=timezone.utc))
