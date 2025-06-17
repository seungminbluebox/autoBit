import numpy as np
import pandas as pd
from upbit_api import get_ohlcv

def calculate_ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def calculate_rsi(series, period=14):
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)

    ma_up = up.rolling(period).mean()
    ma_down = down.rolling(period).mean()

    rs = ma_up / (ma_down + 1e-9)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def _determine_market_context(df):
    if len(df) < 30:
        return "sideways"

    sub_df = df[-288:] if len(df) >= 288 else df

    open_price = sub_df["open"].iloc[0]
    close_price = sub_df["close"].iloc[-1]
    return_ratio = (close_price - open_price) / open_price

    ema9 = calculate_ema(sub_df["close"], 9)
    ema21 = calculate_ema(sub_df["close"], 21)
    rsi = calculate_rsi(sub_df["close"])

    # ✅ 여기에 추가
    spread = abs(ema9.iloc[-1] - ema21.iloc[-1]) / ema21.iloc[-1]
    if rsi.iloc[-1] < 45 or spread < 0.0015:
        return "sideways"
    
    ema_score = 1 if ema9.iloc[-1] > ema21.iloc[-1] else 0
    rsi_score_bull = 1 if rsi.iloc[-1] > 55 else 0
    rsi_score_def = 1 if rsi.iloc[-1] < 50 else 0  # 하락장 기준 완화됨
    ret_score_bull = 1 if return_ratio > 0.005 else 0
    ret_score_def = 1 if return_ratio < -0.005 else 0

    bull_score = ema_score + rsi_score_bull + ret_score_bull
    def_score = (1 - ema_score) + rsi_score_def + ret_score_def

    if bull_score >= 2 and return_ratio > 0.003:
        print(f"bull_score: {bull_score}, def_score: {def_score}, return_ratio: {return_ratio:.4f}")
        return "bull"
    elif def_score >= 2 and return_ratio < -0.003:
        print(f"bull_score: {bull_score}, def_score: {def_score}, return_ratio: {return_ratio:.4f}")
        return "defensive"
    else:
        print(f"bull_score: {bull_score}, def_score: {def_score}, return_ratio: {return_ratio:.4f}")
        return "sideways"

def get_market_context(ticker="KRW-BTC", interval="minute5", count=288):
    df = get_ohlcv(ticker, interval, count)
    return _determine_market_context(df)

def get_market_context_from_df(df):
    return _determine_market_context(df)
