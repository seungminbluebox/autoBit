# autoBit: KRW-BTC 연구·모의매매 시스템

이 저장소는 업비트의 공개 `KRW-BTC` 4시간봉을 이용해 전략을 연구하고, 백테스트·Walk-forward 검증·정규화 모의매매를 실행합니다. 초기 모의자산은 정확히 `100`이며 실제 KRW 잔고가 아닙니다. 거래소 계정에 접속하거나 실제 주문을 전송하지 않습니다. 수익을 보장하거나 특정 매수 시점을 추천하는 도구도 아닙니다.

## 설치

Python 3.12가 필요합니다. PowerShell에서 저장소 루트로 이동한 뒤 다음 명령을 실행합니다.

```powershell
py -3.12 -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -e ".[dev]"
```

아래 예시는 모두 같은 PowerShell 창과 저장소 루트에서 실행합니다.

## 7년 공개 데이터와 품질 검사

`--end-utc`에는 시간대가 포함된 UTC 시각을 사용합니다. Walk-forward의 종료 시각은 4시간 경계여야 하며 해당 시각은 범위에 포함되지 않습니다.

```powershell
& ".\.venv\Scripts\python.exe" -m autobit.cli data-download --output ".\data\raw\krw-btc-7y" --end-utc "2026-09-01T00:00:00Z" --years 7
& ".\.venv\Scripts\python.exe" -m autobit.cli data-quality --input ".\data\raw\krw-btc-7y" --output ".\data\processed\krw-btc-7y"
```

수집은 키가 필요 없는 공개 240분봉 API만 사용합니다. 원본 응답과 수집 증거는 `data\raw\krw-btc-7y`에 보존되고, 정제된 `processed.csv`와 출처·품질 정보 `quality.json`은 `data\processed\krw-btc-7y`에 생성됩니다.

## 백테스트와 Walk-forward

출력 폴더는 실행별로 구분합니다. 특히 Walk-forward 출력 경로는 실행 전에 존재하지 않는 새 경로여야 합니다.

```powershell
& ".\.venv\Scripts\python.exe" -m autobit.cli backtest --input ".\data\processed\krw-btc-7y\processed.csv" --output ".\reports\backtest-baseline" --slippage 0.0005
& ".\.venv\Scripts\python.exe" -m autobit.cli walk-forward --input ".\data\processed\krw-btc-7y\processed.csv" --output ".\reports\walk-forward-2026-09-01" --end-utc "2026-09-01T00:00:00Z"
```

백테스트 묶음에는 요약, 거래·주문·자산곡선 CSV, 품질 정보와 manifest가 들어갑니다. Walk-forward 묶음에는 fold·시도·비용·시장국면 결과, OOS 자산곡선, 검증 판정, 사람이 읽는 보고서와 manifest가 들어갑니다.

## 정규화 모의매매 자동화

한 번만 확정봉을 처리하려면 `paper-once`, 계속 실행하려면 `paper-run`, 저장 상태를 읽기만 하려면 `paper-status`를 사용합니다.

```powershell
& ".\.venv\Scripts\python.exe" -m autobit.cli paper-once --db ".\data\paper\paper.sqlite3" --data-dir ".\data\raw\paper"
& ".\.venv\Scripts\python.exe" -m autobit.cli paper-run --db ".\data\paper\paper.sqlite3" --data-dir ".\data\raw\paper"
& ".\.venv\Scripts\python.exe" -m autobit.cli paper-status --db ".\data\paper\paper.sqlite3"
```

`paper-run`은 데이터 지연, API 오류, 원장 불일치 같은 비정상 증거가 있으면 신규 진입만 안전하게 멈춥니다. 상태 확인과 기존 포지션 보호는 계속하며 제한된 지수 백오프로 자동 재시도합니다. 모든 복구 조건이 충족되면 사람의 재개 명령 없이 `REDUCED` 크기로 다시 시작하고, 정상 복구 사이클을 통과하면 `NORMAL`로 돌아갑니다.

`paper-status`는 기존 SQLite 원장을 변경하지 않고 정규화 현금·자산, BTC 모의수량, 포지션, 활성 손절, 미처리 모의주문, 안전 상태와 다음 UTC 실행 시각을 JSON으로 출력합니다. `equity_status=CURRENT`와 `equity_provenance=COMPLETED_CLOSE_MTM`일 때만 `normalized_equity`가 `equity_as_of_utc` 확정봉 종가 기준의 최신 MTM입니다. `STALE` 또는 `UNAVAILABLE`이면 출처가 마지막 체결가 또는 초기 100인 fallback이므로 최신 시장가로 해석하지 않습니다.

시작·상태 확인·정상 종료·SQLite backup/restore 검증·장애 자동 복구·증거 확인·모의매매 졸업 기준은 [정규화 모의매매 운영 Runbook](docs/paper-trading-runbook.md)을 따르십시오. 이 문서는 최소 2주와 30회 거래 관찰 기간을 아직 완료했다고 주장하지 않습니다. `PASS`는 paper evaluation 지속만 허용하며, 실거래를 켜거나 매수를 추천하지 않습니다.

## 선택적 Telegram 알림

환경변수 이름은 사용자가 정합니다. 실제 토큰과 chat 값은 명령줄 인자로 넣지 말고 환경변수에만 저장한 뒤 그 **이름**을 전달합니다.

```powershell
$env:MY_TELEGRAM_TOKEN = Read-Host "Telegram token"
$env:MY_TELEGRAM_CHAT = Read-Host "Telegram chat value"
& ".\.venv\Scripts\python.exe" -m autobit.cli paper-run --db ".\data\paper\paper.sqlite3" --data-dir ".\data\raw\paper" --telegram-token-env MY_TELEGRAM_TOKEN --telegram-chat-env MY_TELEGRAM_CHAT
```

알림 성공이나 실패는 매매 상태를 바꾸지 않습니다. 중복 전송을 피하는 at-most-once 방식이므로, 프로세스가 알림 시도 기록 직후 중단되면 재시작 뒤 같은 알림을 중복 전송하는 대신 한 건을 놓칠 수 있습니다.

## 전략과 위험 규칙 요약

- 확정된 4시간봉만 사용합니다. 지표는 `EMA(200)`, 현재 봉을 제외한 `Donchian(50/20)`, `ATR(14)`입니다.
- 정상 거래당 위험은 현재 정규화 자산의 `2%`, BTC 최대 노출은 `70%`입니다. 고점 대비 `15%` 낙폭에서는 신규 진입을 멈추고 자동 축소·복구 절차를 수행합니다.
- 진입 초기 손절은 실제 모의 체결가에서 `2.5 ATR` 아래입니다. `+2R` 도달 후 다음 봉부터 진입가와 `최고가 - 3 ATR`을 이용한 추적 손절을 적용합니다.
- 20봉 하단 이탈, 60봉 동안 `+1R` 미달인 정체, 또는 1,095봉 최대 보유 규칙으로 청산할 수 있으므로 보유기간은 유동적입니다.
- 시장가 체결을 모사하며 기본 편도 수수료 `0.05%`와 기본 편도 슬리피지 `0.05%`를 각각 반영합니다. 비용 0 및 더 큰 슬리피지 스트레스도 비교합니다.

이 규칙과 어떤 검증 결과도 미래 수익을 보장하지 않습니다.

## 생성 파일과 운영 제한

- 공개 데이터: 변경 불가 원본 페이지, 체크포인트, 수집 스냅샷·manifest
- 정제 데이터: `processed.csv`, `quality.json`
- 백테스트·검증: JSON/CSV/Markdown 보고서와 해시 manifest
- 모의매매: 초기자산 100 기준의 SQLite 이벤트 원장과 공개 캔들 증거 캐시
- 상태: `paper-status`가 표준 출력으로 내보내는 읽기 전용 JSON

저장 시각은 모두 UTC이고 KST는 표시 용도로만 변환합니다. SQLite 원장과 수집·보고 증거는 정기적으로 백업하십시오. 손상되거나 모순된 증거는 안전 우선으로 거부되며, `paper-run`은 매매를 영구 중단하지 않고 제한된 백오프로 자동 상태 확인을 계속합니다.

시스템이 요청하는 업비트 기능은 공개 `KRW-BTC` 캔들뿐입니다. 업비트 access/secret 키, 개인 WebSocket, 계정·잔고 API, 입출금 API, 거래소 주문 endpoint는 구현되어 있지 않습니다. Telegram을 켰을 때만 알림용 `sendMessage` endpoint를 사용합니다.

## 테스트

일반 검증은 무거운 Walk-forward golden 하나를 제외하고 단위·통합·안전·회귀 테스트 전체를 실행합니다. 이 명령에는 core, 지표 안정성, paper golden이 포함됩니다.

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests\unit tests\integration tests\safety tests\regression --ignore=tests\regression\test_validation_golden.py -v
```

시간이 오래 걸리는 Walk-forward golden은 별도로 실행합니다.

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests\regression\test_validation_golden.py -v
```

명령 목록과 각 인자는 다음과 같이 확인할 수 있습니다.

```powershell
& ".\.venv\Scripts\python.exe" -m autobit.cli --help
```
