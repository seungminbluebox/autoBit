#루프실행 + strategy.py + upbit_api.py + trade.py
# main.py
import time
from config import UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY, TICKER, BUY_AMOUNT_KRW, INTERVAL_SEC
from upbit_api import print_asset_status ,create_upbit, get_market_data, get_balance_info
from trade import execute_buy, execute_sell
from market_mode import get_market_context
from strategy_loader import load_strategy
from telegram_alert import send_telegram_message
from datetime import datetime


# def update_avg_buy_price(prev_qty, prev_avg, new_qty, new_price):
#     total_qty = prev_qty + new_qty
#     if total_qty == 0:
#         return 0
#     total_cost = (prev_qty * prev_avg) + (new_qty * new_price)
#     return total_cost / total_qty

# 1. 업비트 객체 생성
upbit = create_upbit(UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY)

# 2. 프로그램 시작 시 보유량, 평단 불러오기
send_telegram_message("📡 자동매매 봇 시작됨 (main.py 실행)")
loop_count = 0
prev_mode = None
while True:
    try:
        btc_qty, avg_price = get_balance_info(upbit)
        # 3. 시세 및 지표 데이터 수집
        data = get_market_data(TICKER)
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_price = data["current_price"]
        print('='* 50)
        print(f"[{current_time}] [시세🪙] 현재가: {current_price:,.0f} KRW")
        # 시장 판단 + 설명 출력
        context = get_market_context()
        market_mode = context["mode"]
        explanation = context["explanation"]
        should_buy, should_sell = load_strategy(mode=market_mode)
        print(f"[🧠 시장 판단] 현재 시장 모드: {market_mode.upper()}")
        if market_mode != prev_mode:
            send_telegram_message(
                f"📈 시장 전환 감지!\n→ 이전: {prev_mode} → 현재: {market_mode}\n🕒"
            )
        if prev_mode != market_mode and market_mode == "defensive" and btc_qty > 0:
            print(f"[⚠️ 전략 변경] 상승/횡보장에서 하락장(DEFENSIVE) 진입 → 보유 포지션 전량 청산")
            profit_ratio_on_sell = (current_price - avg_price) / avg_price * 100 if avg_price > 0 else 0
            qty_sold = execute_sell(upbit, TICKER, btc_qty, 1.0)  # 전량
            if qty_sold > 0:
                btc_qty = 0.0
                avg_price = 0.0
                print(f"[💣 청산 완료] DEFENSIVE 진입 시점 전량 정리 (실현 손익: {profit_ratio_on_sell:.2f}%)")
                # ✅ 텔레그램 메시지에 수익률 추가
                send_telegram_message(
                    f"⚠️ DEFENSIVE 전략 진입\n"
                    f"보유 포지션 전량 청산 완료\n"
                    f"수량: {qty_sold:.6f} BTC\n"
                    f"실현 손익: {profit_ratio_on_sell:.2f}%"
                )
        print_asset_status(upbit)  # ← 루프 시작 시 현황

        # 1. 매수/매도 판단 지표 계산
        ma5 = data["ma5"]
        ema9 = data["ema9"]
        ema21 = data["ema21"]
        # 2. 매수/매도 판단
        buy_flag = should_buy(data)
        if market_mode == "bull":
            buy_log = (
                f"[🔴매수 판단] EMA9 > EMA21 ({ema9:,.0f} > {ema21:,.0f}) AND RSI > 55 ({rsi:.1f}) AND RSI 상승 → ✅ 조건 만족"
                if buy_flag else
                f"[🔴매수 판단] EMA9 ≤ EMA21 또는 RSI ≤ 55 또는 RSI 하락 → ❌ 조건 불충족"
            )
        else:
            buy_log = (
                f"[🔴매수 판단] 현재 시장 모드가 {market_mode.upper()} → ❌ 매수 불가"
            )
        print(buy_log)

        # 3. 매수 실행
        if buy_flag:#True
            qty_bought = execute_buy(upbit, TICKER, BUY_AMOUNT_KRW)
            if qty_bought > 0:#
                print(f"[🔴매수 완료] 평단 갱신: {avg_price:,.0f} KRW, 보유: {btc_qty:.6f} BTC")
                send_telegram_message(
                    f"🔴 매수 체결\n수량: {qty_bought:.6f} BTC\n단가: {current_price:,.0f} KRW\n총 보유량: {btc_qty:.6f} BTC\n평단: {avg_price:,.0f} KRW"
                )
        # 1. 매도 판단 지표 계산
        if avg_price > 0:
            profit_ratio = (current_price - avg_price) / avg_price * 100
        else:
            profit_ratio = 0
        rsi = data["rsi"]
        ema9 = data["ema9"]
        ema21 = data["ema21"]
        prev_rsi = data["prev_rsi"]
        # 2. 매도 판단
        sell_flag, sell_ratio = should_sell(data, btc_qty, avg_price)
        if avg_price > 0:
            profit_ratio = (current_price - avg_price) / avg_price * 100
        else:
            profit_ratio = 0
        if market_mode == "bull":
            sell_log = (
                f"[🔵매도 판단] 수익률 {profit_ratio:.2f}% ≥ 5% → ✅ 조건 만족"
                if profit_ratio >= 5 else
                f"[🔵매도 판단] 수익률 {profit_ratio:.2f}% ≥ 3% AND RSI 하락 ({rsi:.1f} < {prev_rsi:.1f}) → ✅ 조건 만족"
                if profit_ratio >= 3 and rsi < prev_rsi else
                f"[🔵매도 판단] 조건 불충족 (수익률 {profit_ratio:.2f}%, RSI {rsi:.1f} vs {prev_rsi:.1f}) → ❌"
            )
        else:
            sell_log = (
                f"[🔵매도 판단] 현재 시장 모드가 {market_mode.upper()} → ❌ 매도 불가"
            )
        print(sell_log)

        # 3. 매도 실행
        if sell_flag :#True
            qty_sold = execute_sell(upbit, TICKER, btc_qty, sell_ratio)
            if qty_sold > 0:
                btc_qty -= qty_sold
                print(f"[🔵매도 완료] 수익 실현: {qty_sold:.6f} BTC (≒ {int(current_price * qty_sold):,} KRW)")
                send_telegram_message(
                    f"🔵 매도 체결\n수량: {qty_sold:.6f} BTC\n단가: {current_price:,.0f} KRW\n잔여 보유량: {btc_qty:.6f} BTC\n수익률: {profit_ratio:.2f}%"
                )
        # 6. 다음 루프까지 대기
        print_asset_status(upbit)  # ← 루프 시작 시 현황
        for remaining in range(INTERVAL_SEC, 0, -1):
            print(f"\r[⏰ 대기 중] 다음 실행까지 {remaining}초...", end="")
            time.sleep(1)
        print("\r" + " " * 50, end="\r")  # Clear the countdown line
        print('='* 50)
        prev_mode = market_mode
        # send_telegram_message(
        #     f"💹 현 수익률 리포트\n현재가: {current_price:,.0f} KRW\n평단: {avg_price:,.0f} KRW\n보유량: {btc_qty:.6f} BTC\n수익률: {profit_ratio:.2f}%"
        # )
        loop_count += 1
        if loop_count % (86400 // INTERVAL_SEC) == 0:  # 86400 seconds = 24 hours
            send_telegram_message(f"✅ 루프 정상 작동 중 ({loop_count}회 경과)")

    except Exception as e:
        print(f"[오류 발생] {e}")
        time.sleep(INTERVAL_SEC)
