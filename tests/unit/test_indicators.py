import numpy as np
import pandas as pd

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
