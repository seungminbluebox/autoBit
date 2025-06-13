# 업비트 api를 사용하여 실시간 매매 및 지표 계산을 위한 모듈
from pyupbit import Upbit, get_ohlcv
import numpy as np

# 실전 매매용 객체 생성
def create_upbit(access_key, secret_key):
    return Upbit(access_key, secret_key)


# 현재가 등 지표 데이터 구성
def get_market_data(ticker="KRW-BTC"):
    df = get_ohlcv(ticker, interval="minute5", count=100)

    close_prices = df["close"].tolist()
    current_price = close_prices[-1]
    ma5 = np.mean(close_prices[-5:])
    rsi = calculate_rsi(close_prices)
    prev_rsi = calculate_rsi(close_prices[:-1])  # 바로 이전 시점 RSI 계산

    ema9 = df["close"].ewm(span=9).mean().iloc[-1]
    ema21 = df["close"].ewm(span=21).mean().iloc[-1]
    volume_ma10 = df["volume"].rolling(window=10).mean().iloc[-1]
    volume_now = df["volume"].iloc[-1]
    stddev = np.std(close_prices[-20:])
    ma20 = np.mean(close_prices[-20:])
    lower_band = ma20 - 2 * stddev

    return {
        "current_price": current_price,
        "ma5": ma5,
        "rsi": rsi,
        "ema9": ema9,
        "ema21": ema21,  # 🔁 꼭 반환
        "volume": volume_now,
        "volume_ma10": volume_ma10,
        "prev_rsi": prev_rsi,  # ✅ 추가
        "lower_band": lower_band  # ✅ 추가


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
    current_price = get_market_data()["current_price"]
    btc_value = btc_qty * current_price
    total_asset = krw + btc_value
    print(f"[📊 자산 현황] KRW: {krw:,.0f} | BTC: {btc_qty:.6f} | 평단: {avg_price:,.0f} KRW | 총 자산: {total_asset:,.0f} KRW")
