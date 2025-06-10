# 업비트 api를 사용하여 실시간 매매 및 지표 계산을 위한 모듈
from pyupbit import Upbit, get_ohlcv
import numpy as np

# 실전 매매용 객체 생성
def create_upbit(access_key, secret_key):
    return Upbit(access_key, secret_key)


# 현재가 등 지표 데이터 구성
def get_market_data(ticker="KRW-BTC"):
    df = get_ohlcv(ticker, interval="minute1", count=100)

    close_prices = df["close"].tolist()
    current_price = close_prices[-1]
    ma5 = np.mean(close_prices[-5:])
    rsi = calculate_rsi(close_prices)

    return {
        "current_price": current_price,
        "ma5": ma5,
        "rsi": rsi
    }


# 보유량, 평단 불러오기
def get_balance_info(upbit, ticker="KRW-BTC"):
    balances = upbit.get_balances()
    for b in balances:
        if b["currency"] == "BTC":
            qty = float(b["balance"])
            avg = float(b["avg_buy_price"])
            return qty, avg
    return 0.0, 0.0


# RSI 계산 함수
def calculate_rsi(closes, period=14):
    deltas = np.diff(closes)
    ups = deltas.clip(min=0)
    downs = -deltas.clip(max=0)

    if len(ups) < period:
        return 0

    ma_up = np.mean(ups[-period:])
    ma_down = np.mean(downs[-period:])
    if ma_down == 0:
        return 100
    rs = ma_up / ma_down
    return round(100 - (100 / (1 + rs)), 2)


def print_asset_status(upbit):
    btc_qty, avg_price = get_balance_info(upbit)
    balances = upbit.get_balances()
    krw = next((float(b["balance"]) for b in balances if b["currency"] == "KRW"), 0.0)
    print(f"[📊 자산 현황] KRW: {krw:,.0f} | BTC: {btc_qty:.6f} | 평단: {avg_price:,.0f} KRW")
