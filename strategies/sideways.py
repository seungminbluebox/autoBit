### 📁 strategies/sideways.py (횡보장 전략 예시)

def should_buy(data):
    return data["rsi"] < 45 and data["rsi"] > data.get("prev_rsi", 0)

def should_sell(data, btc_balance, avg_price):
    if btc_balance <= 0:
        return False, 0.0
    profit_ratio = (data["current_price"] - avg_price) / avg_price
    return profit_ratio >= 0.01, 1.0 if profit_ratio >= 0.02 else 0.5
