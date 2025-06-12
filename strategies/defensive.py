### 📁 strategies/defensive.py (하락장 회피 전략 예시)

def should_buy(data):
    return False  # 하락장에서는 진입 없음

def should_sell(data, btc_balance, avg_price):
    if btc_balance <= 0:
        return False, 0.0
    return True, 1.0  # 무조건 청산