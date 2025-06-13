### 🔻 하락장 전략 (유지)
def should_buy(data):
    return False  # 횡보장 매매 금지

def should_sell(data, btc_balance, avg_price):
    return False, 0.0