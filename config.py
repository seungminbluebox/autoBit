# 설정값 및 API 키를 관리하는 모듈
import os
from dotenv import load_dotenv

# .env 불러오기
load_dotenv()

# Upbit API 키
UPBIT_ACCESS_KEY = os.getenv("UPBIT_ACCESS_KEY")
UPBIT_SECRET_KEY = os.getenv("UPBIT_SECRET_KEY")

#telegram API 키
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 매매 설정
TICKER = "KRW-BTC"           # 고정 종목
BUY_AMOUNT_KRW = 20000       # 1회 매수 금액
INTERVAL_SEC = 300          # 매매 판단 주기 (초)

# 파일 경로
LOG_FILE = "log.csv"
