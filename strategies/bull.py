# strategies/bull.py (V6: 개선된 상승장 전략)

def should_buy(data):
    rsi = data["rsi"]
    prev_rsi = data.get("prev_rsi", 0)
    ema9 = data["ema9"]
    ema21 = data["ema21"]
    spread = (ema9 - ema21) / ema21

    # ✅ 완화된 진입 조건
    cond1 = rsi > 50 and rsi > prev_rsi
    cond2 = ema9 > ema21 and spread > 0.001  # 0.1% 이상 이격만으로 진입 허용
    return cond1 and cond2

def should_sell(data, btc_balance, avg_price):
    if btc_balance <= 0:
        return False, 0.0

    price = data["current_price"]
    rsi = data["rsi"]
    prev_rsi = data.get("prev_rsi", 0)
    ema9 = data["ema9"]
    ema21 = data["ema21"]
    profit_ratio = (price - avg_price) / avg_price

    # ✅ 유연한 분할 익절 구조
    sell_ratio = 0.0
    if profit_ratio >= 0.05:
        return True, 1.0
    if profit_ratio >= 0.03:
        sell_ratio += 0.5
    if rsi < prev_rsi:
        sell_ratio += 0.3
    if ema9 < ema21:
        sell_ratio += 0.2

    return sell_ratio > 0, min(sell_ratio, 1.0)
