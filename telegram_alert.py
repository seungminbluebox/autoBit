# telegram_alert.py
import requests
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
TELEGRAM_TOKEN = TELEGRAM_TOKEN
TELEGRAM_CHAT_ID = TELEGRAM_CHAT_ID

def send_telegram_message(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"[텔레그램 전송 실패] {e}")
