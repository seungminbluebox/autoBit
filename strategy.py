# strategy_v4_4.py

def should_buy(data):
    # RSI 상승 돌파 조건
    rsi_up = data["rsi"] - data.get("prev_rsi", 0) > 2
    ema_up = data["ema9"] > data["ema21"]
    spread = (data["ema9"] - data["ema21"]) / data["ema21"]
    spread_ok = spread > 0.003  # 0.3% 이상

    return rsi_up and ema_up and spread_ok


def should_sell(data, btc_balance, avg_price):
    if btc_balance <= 0:
        return False, 0.0

    price = data["current_price"]
    profit_ratio = (price - avg_price) / avg_price
    rsi = data["rsi"]
    prev_rsi = data.get("prev_rsi", 0)
    ema9 = data["ema9"]
    ema21 = data["ema21"]

    sell_ratio = 0.0

    # 조건 1: 충분한 수익 실현
    if profit_ratio >= 0.07:
        return True, 1.0
    if profit_ratio >= 0.04:
        sell_ratio += 0.5

    # 조건 2: RSI 꺾임
    if prev_rsi > 70 and rsi < prev_rsi:
        sell_ratio += 0.3

    # 조건 3: EMA 데드크로스
    if ema9 < ema21:
        sell_ratio += 0.5

    return sell_ratio > 0, min(sell_ratio, 1.0)
