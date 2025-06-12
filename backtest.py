# backtest.py
import pandas as pd  # ← 이거 추가
from collections import Counter
import pyupbit
import numpy as np
import matplotlib.pyplot as plt
from upbit_api import calculate_rsi
from market_mode import get_market_context_from_df
from strategy_loader import load_strategy

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

# current_file = "./csvLog/상승장9.3 241211-241217.csv"
# current_file = "./csvLog/하락장-11.3 250221-250227.csv"
# current_file = "./csvLog/횡보장 1.57 241002-241010.csv"
# current_file = "./csvLog/횡보장 4.41 250509-250517.csv"
current_file = "./csvLog/횡보장 -4.47 250208-250214.csv"

# 데이터 불러오기
df = pd.read_csv(current_file) 
print(f"현재 사용 중인 파일: {current_file.split('/')[-1]}")

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
mode_counter = Counter()

# 루프 시작
for i in range(20, len(df)):
    market_mode = get_market_context_from_df(df[:i])  # 최근 시세 기반
    mode_counter[market_mode] += 1  # ✅ 등장 횟수 누적
    should_buy, should_sell = load_strategy(market_mode)
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
        "profit_ratio": profit_ratio,
        "ema9": row["ema9"],
        "ema21": row["ema21"],
        "volume": row["volume"],
        "volume_ma10": row["volume_ma10"],
        "prev_rsi": df.iloc[i-1]["rsi"],
    }
    low_5 = df.iloc[i-5:i]["low"].tolist()
    data["low_5"] = low_5
    high_5 = df.iloc[i-5:i]["high"].tolist()
    data["high_5"] = high_5
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
print("📊 시장 모드 등장 횟수:")
for mode, count in mode_counter.items():
    print(f" - {mode}: {count}회")
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
