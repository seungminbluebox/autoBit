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
# 1. 업비트 객체 생성
upbit = create_upbit(UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY)
# 2. 프로그램 시작 시 보유량, 평단 불러오기
send_telegram_message("📡 자동매매 봇 시작됨 V1.1 (main.py 실행)")
loop_count = 0
prev_mode = None

while True:
    try:
        # 1. 현재 보유량, 평단, 현재가 불러오기
        btc_qty, avg_price = get_balance_info(upbit)
        data = get_market_data(TICKER)
        current_price = data["current_price"]
        # --- 현재 상태 출력 ---
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print('='* 50)
        print(f"[{current_time}] [시세🪙] 현재가: {current_price:,.0f} KRW")
        print_asset_status(upbit) # 루프 시작 시점의 '진짜' 자산 상태를 먼저 출력
        # --- 시장 모드 판단 ---
        context = get_market_context()
        market_mode = context["mode"]
        explanation = context["explanation"]
        should_buy, should_sell = load_strategy(mode=market_mode)
        print(f"[🧠 시장 판단] 현재 시장 모드: {market_mode.upper()}")
        # --- 시장 모드 변경 시 특별 대응 ---
        if market_mode != prev_mode:
            send_telegram_message(
                f"📈 시장 전환 감지!\n→ 이전: {prev_mode} → 현재: {market_mode}\n🕒"
            )
            # 'defensive' 모드로 바뀌었고, 보유 코인이 있다면 전량 매도
            if market_mode == "defensive" and btc_qty > 0:
                profit_ratio_on_sell = (current_price - avg_price) / avg_price * 100 if avg_price > 0 else 0
                qty_sold = execute_sell(upbit, TICKER, btc_qty, 1.0)  # 전량
                if qty_sold > 0:
                    print(f"[💣 청산 완료] DEFENSIVE 진입 시점 전량 정리 (실현 손익: {profit_ratio_on_sell:.2f}%)")
                    # ✅ 텔레그램 메시지에 수익률 추가
                    send_telegram_message(
                        f"⚠️ DEFENSIVE 전략 진입\n"
                        f"보유 포지션 전량 청산 완료\n"
                        f"수량: {qty_sold:.6f} BTC\n"
                        f"실현 손익: {profit_ratio_on_sell:.2f}%"
                    )


        # 2. 매수 판단
        buy_flag = should_buy(data)
        sell_flag, sell_ratio = should_sell(data, btc_qty, avg_price)
        profit_ratio = (current_price - avg_price) / avg_price * 100 if avg_price > 0 else 0
        
        if market_mode == "bull":
            buy_log = f"[🔴매수 판단] 조건 만족 → ✅ 매수" if buy_flag else f"[🔴매수 판단] 조건 불충족 → ❌ 대기"
            sell_log = f"[🔵매도 판단] 조건 만족 → ✅ 매도" if sell_flag else f"[🔵매도 판단] 조건 불충족 (수익률 {profit_ratio:.2f}%) → ❌ 대기"
        else:
            buy_log = f"[🔴매수 판단] 현재 시장 모드가 {market_mode.upper()} → ❌ 매수 불가"
            sell_log = f"[🔵매도 판단] 현재 시장 모드가 {market_mode.upper()} → ❌ 매도 불가"
        print(buy_log)
        print(sell_log)

        # [매수 실행]
        if buy_flag:
            qty_bought = execute_buy(upbit, TICKER, BUY_AMOUNT_KRW)
            if qty_bought > 0:
                # 매수 성공 후 btc_qty, avg_price를 직접 수정하는 코드가 '전부' 사라짐
                send_telegram_message(f"🔴 매수 체결\n수량: {qty_bought:.6f} BTC\n단가: {current_price:,.0f} KRW")
        # [매도 실행]
        if sell_flag:
            qty_sold = execute_sell(upbit, TICKER, btc_qty, sell_ratio)
            if qty_sold > 0:
                # 매도 성공 후 btc_qty를 직접 수정하는 코드가 '전부' 사라짐
                print(f"[🔵매도 완료] 수익 실현: {qty_sold:.6f} BTC (≒ {int(current_price * qty_sold):,} KRW)")
                send_telegram_message(f"🔵 매도 체결\n수량: {qty_sold:.6f} BTC\n단가: {current_price:,.0f} KRW\n수익률: {profit_ratio:.2f}%")

        # 3. 다음 루프까지 대기
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
