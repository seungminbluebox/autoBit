"""Lookahead-safe trend indicator enrichment."""

import pandas as pd
import pandas_ta  # noqa: F401  # Register the pandas-ta DataFrame accessor.

from autobit.config import StrategyConfig


def compute_trend_indicators(frame: pd.DataFrame, config: StrategyConfig) -> pd.DataFrame:
    """Return a copied frame enriched with stable trend and channel indicators."""
    result = frame.copy()
    if "segment_id" not in result:
        result["segment_id"] = 0
    if "entry_data_valid" not in result:
        result["entry_data_valid"] = True

    result[f"ema_{config.ema_period}"] = result.ta.ema(length=config.ema_period)
    result[f"atr_{config.atr_period}"] = result.ta.atr(length=config.atr_period)
    atr_percent = result[f"atr_{config.atr_period}"] / result["close"]
    result["baseline_atr_pct"] = atr_percent.groupby(
        result["segment_id"], sort=False
    ).transform(
        lambda values: values.shift(1).rolling(252, min_periods=1).median()
    )
    result["entry_high"] = result["high"].shift(1).rolling(config.entry_period).max()
    result["exit_low"] = result["low"].shift(1).rolling(config.exit_period).min()
    result["previous_close"] = result["close"].shift(1)
    result["previous_entry_high"] = result["entry_high"].shift(1)
    result["warmup_complete"] = result.groupby("segment_id", sort=False).cumcount() >= config.warmup_bars
    return result
