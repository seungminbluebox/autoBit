import numpy as np
import pandas as pd
from pandas.testing import assert_series_equal

from autobit.config import StrategyConfig
from autobit.indicators.trend import compute_trend_indicators


def test_trend_indicators_are_stable_when_future_rows_are_appended() -> None:
    """Adding future candles cannot rewrite prior indicators or channels."""
    index = pd.date_range("2025-01-01", periods=650, freq="4h", tz="UTC")
    close = 100.0 + np.arange(650, dtype=float) * 0.3
    frame = pd.DataFrame(
        {"open": close - 0.2, "high": close + 1.5, "low": close - 1.0, "close": close, "volume": 10.0},
        index=index,
    )

    whole = compute_trend_indicators(frame, StrategyConfig())
    prefix = compute_trend_indicators(frame.iloc[:620], StrategyConfig())

    for column in ("ema_200", "atr_14", "entry_high", "exit_low"):
        assert_series_equal(whole.loc[prefix.index, column].fillna(-1.0), prefix[column].fillna(-1.0))
