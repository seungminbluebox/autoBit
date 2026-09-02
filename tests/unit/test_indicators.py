import numpy as np
import pandas as pd
from pandas.testing import assert_series_equal

from autobit.config import StrategyConfig
from autobit.indicators.trend import compute_trend_indicators
from autobit.strategy.donchian_trend import evaluate_entry


def _frame(periods: int = 650) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=periods, freq="4h", tz="UTC")
    close = np.linspace(100.0, 200.0, periods)
    return pd.DataFrame(
        {"open": close, "high": close + 1.0, "low": close - 1.0, "close": close, "volume": 10.0},
        index=index,
    )


def test_entry_channel_excludes_current_high() -> None:
    """A breakout may react to this candle, but its high cannot set its threshold."""
    frame = _frame()
    frame.loc[frame.index[-1], ["high", "close"]] = [500.0, 250.0]

    enriched = compute_trend_indicators(frame, StrategyConfig())

    assert enriched.loc[frame.index[-1], "entry_high"] < enriched.loc[frame.index[-1], "high"]
    assert evaluate_entry(enriched.iloc[-1], is_flat=True)


def test_indicator_enrichment_preserves_caller_and_defaults_missing_quality_columns() -> None:
    """A simple research frame gains defaults only in its returned enrichment."""
    frame = _frame()

    enriched = compute_trend_indicators(frame, StrategyConfig())

    assert "segment_id" not in frame.columns
    assert "entry_data_valid" not in frame.columns
    assert enriched["segment_id"].eq(0).all()
    assert enriched["entry_data_valid"].all()
    assert not bool(enriched.iloc[599]["warmup_complete"])
    assert bool(enriched.iloc[600]["warmup_complete"])


def test_nan_or_invalid_data_never_enters() -> None:
    """Missing numeric inputs and explicit invalid quality both block an entry."""
    row = pd.Series(
        {
            "close": 101.0,
            "ema_200": np.nan,
            "entry_high": 100.0,
            "previous_close": 99.0,
            "previous_entry_high": 100.0,
            "atr_14": 2.0,
            "entry_data_valid": True,
        }
    )

    assert not evaluate_entry(row, is_flat=True)
    row["ema_200"] = 90.0
    row["entry_data_valid"] = False
    assert not evaluate_entry(row, is_flat=True)


def test_atr_baseline_uses_prior_252_completed_bars_per_segment() -> None:
    frame = _frame(320)
    frame["segment_id"] = [0] * 280 + [1] * 40
    frame.loc[:, "high"] = frame["close"] + np.linspace(1.0, 4.0, len(frame))
    frame.loc[:, "low"] = frame["close"] - np.linspace(1.0, 2.5, len(frame))

    enriched = compute_trend_indicators(frame, StrategyConfig())
    atr_pct = enriched["atr_14"] / enriched["close"]
    first_valid_position = int(np.flatnonzero(atr_pct.iloc[:280].notna().to_numpy())[0])

    assert pd.isna(enriched.iloc[first_valid_position]["baseline_atr_pct"])
    assert enriched.iloc[first_valid_position + 1]["baseline_atr_pct"] == atr_pct.iloc[first_valid_position]
    target = first_valid_position + 253
    assert enriched.iloc[target]["baseline_atr_pct"] == np.median(
        atr_pct.iloc[target - 252 : target].to_numpy()
    )
    assert pd.isna(enriched.iloc[280]["atr_14"])
    assert pd.isna(enriched.iloc[280]["baseline_atr_pct"])


def test_each_segment_matches_its_standalone_indicator_calculation() -> None:
    """A prior long-gap segment cannot seed any later segment's trend state."""
    first = _frame(620)
    second = _frame(620)
    second.index = pd.date_range(
        first.index[-1] + pd.Timedelta(hours=4),
        periods=len(second),
        freq="4h",
        tz="UTC",
    )
    second.loc[:, ["open", "high", "low", "close"]] += 20_000.0
    combined = pd.concat([first, second])
    combined["segment_id"] = [0] * len(first) + [1] * len(second)

    enriched = compute_trend_indicators(combined, StrategyConfig())

    for segment in (first, second):
        standalone = compute_trend_indicators(segment, StrategyConfig())
        from_combined = enriched.loc[segment.index]
        for column in (
            "ema_200",
            "atr_14",
            "entry_high",
            "exit_low",
            "previous_close",
            "previous_entry_high",
            "baseline_atr_pct",
            "warmup_complete",
        ):
            assert_series_equal(from_combined[column], standalone[column])


def test_warmup_counts_only_prior_normal_completed_bars() -> None:
    """Filled, quarantined, anomalous, and non-finite rows cannot satisfy warmup."""
    frame = _frame(605)
    frame["is_filled"] = False
    frame["is_quarantined"] = False
    frame["anomaly_spike"] = False
    frame["anomaly_flat"] = False
    frame.iloc[10, frame.columns.get_loc("is_filled")] = True
    frame.iloc[20, frame.columns.get_loc("is_quarantined")] = True
    frame.iloc[30, frame.columns.get_loc("anomaly_spike")] = True
    frame.iloc[40, frame.columns.get_loc("close")] = np.nan

    enriched = compute_trend_indicators(frame, StrategyConfig())

    assert not bool(enriched.iloc[603]["warmup_complete"])
    assert bool(enriched.iloc[604]["warmup_complete"])
