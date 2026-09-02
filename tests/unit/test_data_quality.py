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


def test_off_grid_timestamp_is_rejected_without_rounding_or_dropping() -> None:
    """An observed candle at 02:00 UTC is not a canonical four-hour candle."""
    raw = make_frame(["2026-01-01T02:00:00Z"])

    with pytest.raises(ValueError, match="4-hour boundary") as error:
        canonicalize_ohlcv(raw, datetime(2026, 1, 2, tzinfo=timezone.utc))

    assert "2026-01-01T02:00:00+00:00" in str(error.value)


@pytest.mark.parametrize(
    ("unit", "offset"),
    [
        ("s", pd.Timedelta(seconds=1)),
        ("ms", pd.Timedelta(milliseconds=1)),
        ("us", pd.Timedelta(microseconds=1)),
        ("ns", pd.Timedelta(nanoseconds=1)),
    ],
)
def test_four_hour_grid_validation_uses_nanoseconds_for_every_stored_unit(
    unit: str, offset: pd.Timedelta
) -> None:
    """Valid and invalid boundaries have the same result for s/ms/us/ns indexes."""
    boundary = pd.Timestamp("2026-01-01T00:00:00Z")
    exact = make_frame([boundary.isoformat()])
    exact.index = pd.DatetimeIndex([boundary]).as_unit(unit)

    result = canonicalize_ohlcv(exact, datetime(2026, 1, 2, tzinfo=timezone.utc))

    assert result.frame.index.dtype.unit == "ns"
    off_grid = make_frame([(boundary + offset).isoformat()])
    off_grid.index = pd.DatetimeIndex([boundary + offset]).as_unit(unit)
    with pytest.raises(ValueError, match="4-hour boundary"):
        canonicalize_ohlcv(off_grid, datetime(2026, 1, 2, tzinfo=timezone.utc))


def test_timestamp_conversion_outside_nanosecond_range_fails_closed() -> None:
    """A coarser-resolution timestamp must not be silently rounded into range."""
    raw = make_frame(["2026-01-01T00:00:00Z"])
    raw.index = pd.DatetimeIndex(["2500-01-01T00:00:00Z"], dtype="datetime64[us, UTC]")

    with pytest.raises(ValueError, match="nanosecond"):
        canonicalize_ohlcv(raw, datetime(2501, 1, 1, tzinfo=timezone.utc))


@pytest.mark.parametrize("nanoseconds", [1, 999])
def test_submicrosecond_offset_is_rejected_without_rounding(nanoseconds: int) -> None:
    """Integer-nanosecond grid validation must not rely on microseconds."""
    timestamp = pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(nanoseconds=nanoseconds)
    raw = make_frame([timestamp.isoformat()])

    with pytest.raises(ValueError, match="4-hour boundary"):
        canonicalize_ohlcv(raw, datetime(2026, 1, 2, tzinfo=timezone.utc))


def test_consistently_shifted_series_is_rejected_with_all_off_grid_timestamps() -> None:
    """A complete-looking 02:00/06:00 series must not establish a shifted cadence."""
    raw = make_frame(["2026-01-01T02:00:00Z", "2026-01-01T06:00:00Z"])

    with pytest.raises(ValueError, match="4-hour boundary") as error:
        canonicalize_ohlcv(raw, datetime(2026, 1, 2, tzinfo=timezone.utc))

    assert "2026-01-01T02:00:00+00:00" in str(error.value)
    assert "2026-01-01T06:00:00+00:00" in str(error.value)


def test_explicit_spike_threshold_controls_flags_count_and_entry_eligibility() -> None:
    """Research can make an 11.9% range a spike by supplying a 10% threshold."""
    raw = make_frame(["2026-01-01T00:00:00Z"])
    raw.loc[:, "high"] = 111.0

    result = canonicalize_ohlcv(
        raw,
        datetime(2026, 1, 2, tzinfo=timezone.utc),
        spike_range_ratio=0.10,
    )

    assert bool(result.frame.iloc[0]["anomaly_spike"])
    assert result.report.spike_flags == 1
    assert not bool(result.frame.iloc[0]["entry_data_valid"])


def test_flat_anomaly_is_counted_and_contaminates_the_following_row() -> None:
    """An unfilled zero-range candle is an anomaly and blocks the next entry row."""
    raw = make_frame(["2026-01-01T00:00:00Z", "2026-01-01T04:00:00Z"])
    raw.iloc[0, raw.columns.get_indexer(["open", "high", "low", "close"])] = 101.0

    result = canonicalize_ohlcv(
        raw,
        datetime(2026, 1, 2, tzinfo=timezone.utc),
        spike_range_ratio=0.10,
    )

    assert bool(result.frame.iloc[0]["anomaly_flat"])
    assert int(result.frame["anomaly_flat"].sum()) == 1
    assert not bool(result.frame.iloc[0]["entry_data_valid"])
    assert not bool(result.frame.iloc[1]["entry_data_valid"])


def test_anomaly_invalidates_current_and_next_199_rows_but_not_200th_following_row() -> None:
    """The inclusive 200-row window expires exactly after 199 clean followers."""
    times = pd.date_range("2026-01-01T00:00:00Z", periods=202, freq="4h")
    raw = make_frame([timestamp.isoformat() for timestamp in times])
    raw.iloc[0, raw.columns.get_loc("high")] = 111.0

    result = canonicalize_ohlcv(
        raw,
        datetime(2026, 2, 15, tzinfo=timezone.utc),
        spike_range_ratio=0.10,
    )

    assert not bool(result.frame.iloc[199]["entry_data_valid"])
    assert bool(result.frame.iloc[200]["entry_data_valid"])


@pytest.mark.parametrize("threshold", [0.0, -0.1, float("inf"), float("nan")])
def test_spike_threshold_must_be_positive_and_finite(threshold: float) -> None:
    """Invalid thresholds cannot silently alter research eligibility."""
    raw = make_frame(["2026-01-01T00:00:00Z"])

    with pytest.raises(ValueError, match="spike_range_ratio"):
        canonicalize_ohlcv(raw, datetime(2026, 1, 2, tzinfo=timezone.utc), spike_range_ratio=threshold)
