# 매매 전략을 정의하는 모듈
def should_buy(data):
    return data["current_price"] > data["ma5"]


def should_sell(data, btc_balance, avg_buy_price):
    if btc_balance <= 0:
        return False, 0.0

    profit_ratio = (data["current_price"] - avg_buy_price) / avg_buy_price
    if profit_ratio > 0.02: #and data["rsi"] > 70:
        return True, 0.5  # 50% 매도
    return False, 0.0
