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
    
    # [상승장 추가 완화] 거의 모든 상승 시도를 포착하도록 기준 조정
    rsi_score_bull = 1 if rsi.iloc[-1] > 49 else 0        # RSI가 50에 근접만 해도 점수 부여
    ret_score_bull = 1 if return_ratio > 0.001 else 0     # 0.1%만 올라도 추세 점수 부여

    # [하락장 추가 완화] 하락 신호를 더 민감하게 감지하도록 기준 조정
    rsi_score_def = 1 if rsi.iloc[-1] < 53 else 0         # RSI가 53 미만이면 하락 점수 부여
    ret_score_def = 1 if return_ratio < 0 else 0          # 수익률이 조금이라도 마이너스(-)이면 하락 점수 부여

    bull_score = ema_score + rsi_score_bull + ret_score_bull
    def_score = (1 - ema_score) + rsi_score_def + ret_score_def

    # [상승장 판단 유지]
    if bull_score >= 2 and return_ratio > 0:
        print(f"bull_score: {bull_score}, def_score: {def_score}, return_ratio: {return_ratio:.4f}")
        return "bull"
        
    # [하락장 추가 완화] 점수 조건만으로 판단하여 반응 속도 극대화
    elif def_score >= 2:
        print(f"bull_score: {bull_score}, def_score: {def_score}, return_ratio: {return_ratio:.4f}")
        return "defensive"
    else:
        print(f"bull_score: {bull_score}, def_score: {def_score}, return_ratio: {return_ratio:.4f}")
        return "sideways"

def get_market_context(ticker="KRW-BTC", interval="minute5", count=288):
    df = get_ohlcv(ticker, interval, count)
    mode = _determine_market_context(df)
    # 판단에 사용된 주요 지표 출력 (종가, EMA, RSI, 수익률 등)
    last_close = df["close"].iloc[-1]
    ema9 = calculate_ema(df["close"], 9).iloc[-1]
    ema21 = calculate_ema(df["close"], 21).iloc[-1]
    rsi = calculate_rsi(df["close"]).iloc[-1]
    return_ratio = (last_close - df["open"].iloc[0]) / df["open"].iloc[0]

    return {
        "mode": mode,
        "explanation": {
            "종가 수익률": f"{return_ratio:.4f}",
            "EMA9": round(ema9),
            "EMA21": round(ema21),
            "RSI": round(rsi, 1),
        }
    }

def get_market_context_from_df(df):
    return _determine_market_context(df)
