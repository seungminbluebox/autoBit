# strategies/sideways.py (v5 - 균형 잡힌 매도 로직)

def should_buy(data):
    """
    횡보장에서는 신규 매수를 하지 않습니다.
    """
    return False

def should_sell(data, btc_balance, avg_price):
    """
    횡보장에서 '하락 추세 전환'이 합리적으로 의심될 때,
    손실을 방지하기 위해 보유 포지션을 매도합니다.
    """
    if btc_balance <= 0:
        return False, 0.0

    # 현재 수익률 계산 (평단가 대비)
    profit_ratio = (data["current_price"] - avg_price) / avg_price if avg_price > 0 else 0

    # '균형 잡힌' 하락 전환 신호 3가지
    # 1. 단기 EMA가 장기 EMA 아래로 하락 (추세 꺾임)
    # 2. RSI가 43 미만으로 하락 (모멘텀 약화, 기존 45보다 신중)
    # 3. 현재 수익률이 -1.5% 이하일 때 (실제 손실 발생 확인)
    is_becoming_bearish = (
        data["ema9"] < data["ema21"] and
        data["rsi"] < 43 and
        profit_ratio < -0.015  # ✅ -1.5% 손실 발생 시에만 매도 조건 충족
    )

    if is_becoming_bearish:
        # print(f"[⚠️ 횡보장 리스크 관리] 하락 신호 감지, 손실 최소화 매도 실행 (손익: {profit_ratio*100:.2f}%)")
        return True, 1.0

    return False, 0.0