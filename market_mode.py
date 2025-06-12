import numpy as np
from upbit_api import get_ohlcv

# 내부 판단 로직
def _determine_market_context(df):
    if len(df) < 12:
        return "sideways"

    sub_df = df[-288:] if len(df) >= 288 else df

    open_price = sub_df["open"].iloc[0]
    close_price = sub_df["close"].iloc[-1]
    return_ratio = (close_price - open_price) / open_price

    # 최근 12봉 기준: 양봉/음봉 수
    recent_closes = sub_df["close"].iloc[-12:]
    recent_opens = sub_df["open"].iloc[-12:]
    positive_candles = (recent_closes > recent_opens).sum()
    negative_candles = (recent_closes < recent_opens).sum()

    if return_ratio > 0.007 and positive_candles >= 7:
        return "bull"
    elif return_ratio < -0.007 and negative_candles >= 7:
        return "defensive"
    else:
        return "sideways"

# 실전용
def get_market_context(ticker="KRW-BTC", interval="minute5", count=288):
    df = get_ohlcv(ticker, interval, count)
    return _determine_market_context(df)

# 백테스트용
def get_market_context_from_df(df):
    return _determine_market_context(df)
