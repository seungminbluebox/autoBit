import pandas as pd
import matplotlib.pyplot as plt
import requests

# 🔌 현재 KRW-BTC 가격을 Upbit에서 실시간으로 조회
def get_current_price(ticker="KRW-BTC"):
    url = f"https://api.upbit.com/v1/ticker?markets={ticker}"
    try:
        res = requests.get(url)
        return float(res.json()[0]['trade_price'])
    except Exception as e:
        print("가격 조회 실패:", e)
        return None

# CSV 경로
LOG_PATH = "log.csv"

# 1. CSV 불러오기
df = pd.read_csv(
    LOG_PATH,
    names=["datetime", "ticker", "side", "price", "amount", "total_krw"],
    parse_dates=["datetime"]
)

# 2. 데이터 타입 변환
df["price"] = df["price"].astype(float)
df["amount"] = df["amount"].astype(float)
df["total_krw"] = df["total_krw"].astype(float)

# 3. 매수/매도 분리
buy_df = df[df["side"] == "buy"].copy()
sell_df = df[df["side"] == "sell"].copy()

# 4. 누적 매수/매도량, 원화
buy_df["cum_btc"] = buy_df["amount"].cumsum()
buy_df["cum_krw"] = buy_df["total_krw"].cumsum()
sell_df["cum_btc"] = sell_df["amount"].cumsum()
sell_df["cum_krw"] = sell_df["total_krw"].cumsum()

# 5. 누적 손익 계산 (매도 - 매수 누적 금액)
total_buy = buy_df["total_krw"].sum()
total_sell = sell_df["total_krw"].sum()
total_profit = total_sell - total_buy
profit_rate = (total_profit / total_buy) * 100 if total_buy > 0 else 0

# 6. 출력
print("📊 거래 요약")
print(f"총 매수 횟수: {len(buy_df)}")
print(f"총 매도 횟수: {len(sell_df)}")
print(f"총 매수 금액: {total_buy:,.0f}원")
print(f"총 매도 금액: {total_sell:,.0f}원")
print(f"누적 손익: {total_profit:,.0f}원")
print(f"수익률: {profit_rate:.2f}%")

# 7. 평가 손익 계산
total_bought_btc = buy_df["amount"].sum()
total_sold_btc = sell_df["amount"].sum()
remaining_btc = total_bought_btc - total_sold_btc

current_price = get_current_price()
if current_price is not None:
    evaluated_krw = remaining_btc * current_price
    total_profit_eval = total_sell + evaluated_krw - total_buy
    profit_rate_eval = (total_profit_eval / total_buy) * 100 if total_buy > 0 else 0

    print("\n🧮 평가 손익 반영")
    print(f"현재 보유 BTC: {remaining_btc:.8f}개")
    print(f"현재 시세: {current_price:,.0f}원")
    print(f"잔여 BTC 평가 금액: {evaluated_krw:,.0f}원")
    print(f"총 손익 (평가 포함): {total_profit_eval:,.0f}원")
    print(f"수익률 (평가 포함): {profit_rate_eval:.2f}%")

    # 8. 수수료 반영
    FEE_RATE = 0.0005  # 0.05%
    buy_fee = total_buy * FEE_RATE
    sell_fee = total_sell * FEE_RATE
    total_fee = buy_fee + sell_fee

    profit_excl_fee = total_profit_eval - total_fee
    rate_excl_fee = (profit_excl_fee / total_buy) * 100 if total_buy > 0 else 0

    print("\n💸 수수료 반영")
    print(f"총 수수료: {total_fee:,.0f}원 (매수 {buy_fee:,.0f} + 매도 {sell_fee:,.0f})")
    print(f"총 손익 (수수료 제외): {profit_excl_fee:,.0f}원")
    print(f"수익률 (수수료 제외): {rate_excl_fee:.2f}%")