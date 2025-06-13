#루프실행 + strategy.py + upbit_api.py + trade.py
# main.py

import time
from config import UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY, TICKER, BUY_AMOUNT_KRW, INTERVAL_SEC
from upbit_api import print_asset_status ,create_upbit, get_market_data, get_balance_info
from trade import execute_buy, execute_sell
from market_mode import get_market_context
from strategy_loader import load_strategy


def update_avg_buy_price(prev_qty, prev_avg, new_qty, new_price):
    total_qty = prev_qty + new_qty
    if total_qty == 0:
        return 0
    total_cost = (prev_qty * prev_avg) + (new_qty * new_price)
    return total_cost / total_qty

# 1. 업비트 객체 생성
upbit = create_upbit(UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY)

# 2. 프로그램 시작 시 보유량, 평단 불러오기
btc_qty, avg_price = get_balance_info(upbit)
print(f"[초기화] 보유 BTC: {btc_qty}, 평단: {avg_price:,.0f} KRW")
prev_mode = None
while True:
    # 3. 시세 및 지표 데이터 수집
    data = get_market_data(TICKER)
    current_price = data["current_price"]
    print(f"[시세🪙] 현재가: {current_price:,.0f} KRW")
    try:
        print('='* 50)
        market_mode = get_market_context()
        should_buy, should_sell = load_strategy(mode=market_mode)
        print(f"🧠 시장 분석 결과: {market_mode.upper()} 전략 적용 중")
        # 루프 내부에서 시장 모드 판단 이후 추가
        if prev_mode != market_mode and market_mode == "defensive" and btc_qty > 0:
            print(f"[⚠️ 전략 변경] 상승/횡보장에서 하락장(DEFENSIVE) 진입 → 보유 포지션 전량 청산")
            qty_sold = execute_sell(upbit, TICKER, btc_qty, 1.0)  # 전량
            if qty_sold > 0:
                btc_qty = 0.0
                avg_price = 0.0
                print(f"[💣 청산 완료] DEFENSIVE 진입 시점 전량 정리")
        print_asset_status(upbit)  # ← 루프 시작 시 현황

        # 1. 매수/매도 판단 지표 계산
        ma5 = data["ma5"]
        ema9 = data["ema9"]
        ema21 = data["ema21"]
        # 2. 매수/매도 판단
        buy_flag = should_buy(data)
        buy_log = (
            f"[🔴매수 판단] EMA9 > EMA21 ({ema9:,.0f} > {ema21:,.0f}) AND RSI > 50 → ✅ 조건 만족"
            if buy_flag else
            f"[🔴매수 판단] EMA9 ≤ EMA21 ({ema9:,.0f} ≤ {ema21:,.0f}) OR RSI ≤ 50 → ❌ 조건 불충족"
        )
        print(buy_log)

        # 3. 매수 실행
        if buy_flag:#
            qty_bought = execute_buy(upbit, TICKER, BUY_AMOUNT_KRW)
            if qty_bought > 0:#
                avg_price = update_avg_buy_price(btc_qty, avg_price, qty_bought, current_price)
                btc_qty += qty_bought
                print(f"[🔴매수 완료] 평단 갱신: {avg_price:,.0f} KRW, 보유: {btc_qty:.6f} BTC")

        # 1. 매도 판단 지표 계산
        if avg_price > 0:
            profit_ratio = (current_price - avg_price) / avg_price * 100
        else:
            profit_ratio = 0
        rsi = data["rsi"]
        ema9 = data["ema9"]
        ema21 = data["ema21"]
        # 2. 매도 판단
        sell_flag, sell_ratio = should_sell(data, btc_qty, avg_price)
        sell_log = (
            f"[🔵매도 판단] EMA9 < EMA21 ({ema9:,.0f} > {ema21:,.0f}) OR RSI ≥ 70 → ✅ 조건 만족"
            if sell_flag else
            f"[🔵매도 판단] EMA9 ≥ EMA21 ({ema9:,.0f} ≥ {ema21:,.0f}) OR RSI < 70 → ❌ 조건 불충족"
        )
        print(sell_log)

        # 3. 매도 실행
        if sell_flag:#True
            qty_sold = execute_sell(upbit, TICKER, btc_qty, sell_ratio)
            if qty_sold > 0:
                btc_qty -= qty_sold
                print(f"[🔵매도 완료] 수익 실현: {qty_sold:.6f} BTC (≒ {int(current_price * qty_sold):,} KRW)")

        # 6. 다음 루프까지 대기
        print_asset_status(upbit)  # ← 루프 시작 시 현황
        print('='* 50)
        time.sleep(INTERVAL_SEC)
        prev_mode = market_mode


    except Exception as e:
        print(f"[오류 발생] {e}")
        time.sleep(INTERVAL_SEC)
