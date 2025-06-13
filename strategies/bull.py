### ✅ 상승장 전략 (완성)
def should_buy(data):
    return (
        data["ema9"] > data["ema21"] and
        data["rsi"] > 55 and
        data["rsi"] > data["prev_rsi"]
    )

def should_sell(data, btc_balance, avg_price):
    if btc_balance <= 0:
        return False, 0.0

    profit_ratio = (data["current_price"] - avg_price) / avg_price
    rsi = data["rsi"]
    prev_rsi = data["prev_rsi"]

    if profit_ratio >= 0.05:
        return True, 1.0
    if profit_ratio >= 0.03 and rsi < prev_rsi:
        return True, 1.0

    return False, 0.0