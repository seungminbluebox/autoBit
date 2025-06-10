# 설정값 및 API 키를 관리하는 모듈
import os
from dotenv import load_dotenv

# .env 불러오기
load_dotenv()

# Upbit API 키
UPBIT_ACCESS_KEY = os.getenv("UPBIT_ACCESS_KEY")
UPBIT_SECRET_KEY = os.getenv("UPBIT_SECRET_KEY")

# 매매 설정
TICKER = "KRW-BTC"           # 고정 종목
BUY_AMOUNT_KRW = 10000       # 1회 매수 금액
INTERVAL_SEC = 5            # 매매 판단 주기 (초)

# 파일 경로
LOG_FILE = "log.csv"
