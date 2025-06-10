import csv
from datetime import datetime
from config import LOG_FILE

def log_trade(ticker, side, price, amount):
    with open(LOG_FILE, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ticker,
            side,               # "buy" or "sell"
            round(price),       # 현재가
            round(amount, 8),   # BTC 수량
            round(price * amount, 2)  # 체결 원화
        ])
