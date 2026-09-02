"""Lookahead-safe trend indicator enrichment."""

import numpy as np
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

    ema_column = f"ema_{config.ema_period}"
    atr_column = f"atr_{config.atr_period}"
    for column in (
        ema_column,
        atr_column,
        "baseline_atr_pct",
        "entry_high",
        "exit_low",
        "previous_close",
        "previous_entry_high",
    ):
        result[column] = float("nan")
    result["warmup_complete"] = False

    for positions in result.groupby("segment_id", sort=False).indices.values():
        segment = result.iloc[positions]
        ema = _indicator_series(segment.ta.ema(length=config.ema_period), segment.index)
        atr = _indicator_series(segment.ta.atr(length=config.atr_period), segment.index)
        entry_high = segment["high"].shift(1).rolling(config.entry_period).max()

        result.iloc[positions, result.columns.get_loc(ema_column)] = ema.to_numpy()
        result.iloc[positions, result.columns.get_loc(atr_column)] = atr.to_numpy()
        result.iloc[positions, result.columns.get_loc("baseline_atr_pct")] = (
            (atr / segment["close"])
            .shift(1)
            .rolling(252, min_periods=1)
            .median()
            .to_numpy()
        )
        result.iloc[positions, result.columns.get_loc("entry_high")] = entry_high.to_numpy()
        result.iloc[positions, result.columns.get_loc("exit_low")] = (
            segment["low"].shift(1).rolling(config.exit_period).min().to_numpy()
        )
        result.iloc[positions, result.columns.get_loc("previous_close")] = (
            segment["close"].shift(1).to_numpy()
        )
        result.iloc[positions, result.columns.get_loc("previous_entry_high")] = (
            entry_high.shift(1).to_numpy()
        )
        normal_before = _normal_completed(segment).astype("int64").cumsum().shift(1, fill_value=0)
        result.iloc[positions, result.columns.get_loc("warmup_complete")] = (
            normal_before >= config.warmup_bars
        ).to_numpy()
    return result


def _indicator_series(value: object, index: pd.Index) -> pd.Series:
    """Normalize pandas-ta's short-input sentinel to an aligned NaN series."""
    if isinstance(value, pd.Series):
        return value.reindex(index)
    return pd.Series(float("nan"), index=index, dtype="float64")


def _normal_completed(segment: pd.DataFrame) -> pd.Series:
    """Identify rows eligible to advance the per-segment 600-bar warmup."""
    numeric = segment.reindex(columns=["open", "high", "low", "close", "volume"]).apply(
        pd.to_numeric, errors="coerce"
    )
    normal = pd.Series(np.isfinite(numeric.to_numpy()).all(axis=1), index=segment.index)
    for column in ("is_filled", "is_quarantined", "anomaly_spike", "anomaly_flat"):
        if column in segment:
            normal &= segment[column].eq(False)
    return normal
