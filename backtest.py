# backtest.py
import pandas as pd  # ← 이거 추가

import pyupbit
import numpy as np
import matplotlib.pyplot as plt
from strategy import should_buy, should_sell
from upbit_api import calculate_rsi

# 설정값
TICKER = "KRW-BTC"
INTERVAL = "minute5"
COUNT = 200
BUY_AMOUNT = 100000
INITIAL_KRW = 1_000_000

# 가상 자산 상태
krw = INITIAL_KRW
btc = 0.0
avg_price = 0.0
portfolio_history = []

# 데이터 불러오기
# df = pd.read_csv("하락장250221-250227.csv") #-0.81 #-3 손절X 2.72 v2 -0.71
# df = pd.read_csv("횡보장250509-250517.csv") # -2.37 #-3 손절X -2.37 v2 -0.33
df= pd.read_csv("상승장241211-241217.csv") #2.72% #-3 손절X 2.72 v2 1.36

df["ma5"] = df["close"].rolling(window=5).mean()
df["ma20"] = df["close"].rolling(window=20).mean()
df["ema9"] = df["close"].ewm(span=9).mean()
df["ema21"] = df["close"].ewm(span=21).mean()
df["volume_ma10"] = df["volume"].rolling(window=10).mean()
df["timestamp"] = pd.to_datetime(df["timestamp"])
df.set_index("timestamp", inplace=True)

# RSI 계산
closes = df["close"].tolist()
df["rsi"] = [0]*len(df)
for i in range(14, len(df)):
    df.loc[df.index[i], "rsi"] = calculate_rsi(closes[:i+1])

# 루프 시작
for i in range(20, len(df)):
    row = df.iloc[i]
    price = row["close"]

    # 평가 수익률
    profit_ratio = (price - avg_price) / avg_price if avg_price > 0 else 0

    # 전략용 데이터 구성 (사전 계산된 지표만 담음)
    data = {
        "current_price": price,
        "ma5": row["ma5"],
        "ma20": row["ma20"],
        "rsi": row["rsi"],
        "volume": row["volume"],
        "profit_ratio": profit_ratio,
        "ema9": row["ema9"],
        "ema21": row["ema21"],
        "volume": row["volume"],
        "volume_ma10": row["volume_ma10"],
        "prev_rsi": df.iloc[i-1]["rsi"],

    }

    # 매수 판단
    if should_buy(data) and krw >= BUY_AMOUNT:
        qty = BUY_AMOUNT / price
        total_cost = btc * avg_price + qty * price
        btc += qty
        avg_price = total_cost / btc
        krw -= BUY_AMOUNT
        # print(f"[매수] {price:,.0f}원에 {qty:.6f} BTC")

    # 매도 판단
    sell_flag, sell_ratio = should_sell(data, btc, avg_price)
    if sell_flag and btc > 0:
        sell_qty = btc * sell_ratio
        krw += sell_qty * price
        btc -= sell_qty
        # print(f"[매도] {price:,.0f}원에 {sell_qty:.6f} BTC")

    # 자산 기록
    total_asset = krw + btc * price
    portfolio_history.append(total_asset)

# 결과 출력
final_asset = krw + btc * closes[-1]
profit = final_asset - INITIAL_KRW
print("\n✅ 백테스트 결과")
print(f"- 초기 자산: {INITIAL_KRW:,} KRW")
print(f"- 최종 자산: {int(final_asset):,} KRW")
print(f"- 총 수익: {int(profit):,} KRW ({profit / INITIAL_KRW * 100:.2f}%)")

# 그래프 출력
plt.plot(portfolio_history, label="Portfolio")
plt.title("back test")
plt.xlabel("time")
plt.ylabel("KRW")
plt.grid(True)
plt.legend()
plt.tight_layout()
plt.show()
