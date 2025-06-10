# 실제 매매 함수
def execute_buy(upbit, ticker, amount_krw):
    try:
        # 현재 KRW 잔고 조회
        balances = upbit.get_balances()
        krw = next((float(b["balance"]) for b in balances if b["currency"] == "KRW"), 0.0)
        # 잔고가 충분한지 확인
        if krw < amount_krw:
            print(f"[❌매수 원화 부족] 보유 KRW: {krw:,.0f} < 요청: {amount_krw:,.0f}")
            return 0.0
        # 시장가 매수 주문
        order = upbit.buy_market_order(ticker, amount_krw)
        print(f"[🔴매수] {amount_krw} KRW 매수 요청 완료")
        # 체결된 BTC 수량 반환
        volume = float(order.get('executed_volume'))
        return volume
    except Exception as e:
        # 예외 발생 시 에러 메시지 출력
        print(f"[💥매수 실패] {e}")
        return 0.0


def execute_sell(upbit, ticker, btc_balance, ratio):
    try:
        # 현재 BTC 잔고 조회
        sell_volume = round(btc_balance * ratio, 8)
        # 잔고가 충분한지 확인
        if sell_volume <= 0:
            print("[❌매도 비트 부족] 매도 수량이 0 이하")
            return 0.0
        # 시장가 매도 주문
        order = upbit.sell_market_order(ticker, sell_volume)
        print(f"[🔵매도] {sell_volume} BTC 매도 요청 완료")
        # 체결된 BTC 수량 반환
        return float(order['volume'])
    
    except Exception as e:
        # 예외 발생 시 에러 메시지 출력
        print(f"[💥매도 실패] {e}")
        return 0.0
