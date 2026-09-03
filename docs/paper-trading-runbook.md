# KRW-BTC 정규화 모의매매 운영 Runbook

이 문서는 `KRW-BTC` 공개 4시간봉만 사용하는 정규화 모의매매의 운영 절차다. 초기 자산은 정확히 `100`이며 실제 KRW가 아니다. 이 저장소에는 개인 거래소 API, 계정 조회, 실제 주문 기능이 없다.

`PASS`는 **모의매매 평가를 계속할 수 있다는 뜻뿐**이다. 어떤 경우에도 실거래를 켜지 않으며, 매수를 권하거나 매수를 추천하지 않는다.

## 시작, 상태 확인, 정상 종료

PowerShell을 저장소 루트에서 열고 공개 캔들 증거와 SQLite 원장 경로를 명시한다. 이 명령의 옵션은 CLI 파서와 일치한다.

```powershell
& ".\.venv\Scripts\python.exe" -m autobit.cli paper-run --db ".\data\paper\paper.sqlite3" --data-dir ".\data\raw\paper"
```

한 번의 성숙한 확정봉만 처리할 때는 다음을 사용한다.

```powershell
& ".\.venv\Scripts\python.exe" -m autobit.cli paper-once --db ".\data\paper\paper.sqlite3" --data-dir ".\data\raw\paper"
```

별도의 PowerShell 창에서 읽기 전용 상태를 확인한다.

```powershell
& ".\.venv\Scripts\python.exe" -m autobit.cli paper-status --db ".\data\paper\paper.sqlite3"
```

`paper-run`을 정상 종료하려면 실행 중인 창에서 `Ctrl+C`를 한 번 누르고 프로세스가 끝나 PowerShell 프롬프트가 돌아올 때까지 기다린다. 강제 종료, 작업 관리자 종료, 컴퓨터 전원 차단으로 대신하지 않는다. 종료 후에도 위 `paper-status`가 읽혀야 백업이나 복원을 진행할 수 있다.

## SQLite 백업: 반드시 중지한 뒤 수행

1. 위 절차로 `paper-run`을 종료하고 프롬프트 복귀를 확인한다. 원장을 여는 다른 `paper-once`/Python 프로세스도 모두 끝나 있어야 한다.
2. `paper.sqlite3`와, 존재한다면 `paper.sqlite3-wal`를 **같은 시점의 한 묶음**으로 복사한다. WAL이 있을 때 DB 파일만 복사하면 일관된 원장이 아닐 수 있다. `-shm`는 SQLite의 일시적 공유 메모리 파일이므로 durable backup/restore 상태로 복사하지 않는다.
3. 해시와 읽기 전용 `paper-status`로 백업본을 확인한다.

```powershell
$ledger = (Resolve-Path ".\data\paper\paper.sqlite3").Path
$backupDir = Join-Path ".\backups" ("paper-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $backupDir | Out-Null
$backupLedger = Join-Path $backupDir "paper.sqlite3"
Copy-Item -LiteralPath $ledger -Destination $backupLedger
foreach ($suffix in "-wal") {
    $companion = "$ledger$suffix"
    if (Test-Path -LiteralPath $companion) {
        Copy-Item -LiteralPath $companion -Destination "$backupLedger$suffix"
    }
}
Get-ChildItem -LiteralPath $backupDir | Get-FileHash -Algorithm SHA256
& ".\.venv\Scripts\python.exe" -m autobit.cli paper-status --db $backupLedger
```

백업 목록에는 DB와 존재했던 `-wal` 파일이 모두 있어야 한다. `paper-status`가 non-zero이면 그 백업은 사용하지 말고 원본과 함께 보존한 뒤 손상 대응 절차를 따른다.

## 복원 전 검증

실사용 원장을 덮어쓰지 말고 먼저 매 시도마다 새로 만든 빈 검증 디렉터리로 선택한 백업 묶음을 복사한다. DB와 당시 존재했던 `-wal`만 복원한다. `-shm`는 durable state가 아니므로 복사하거나 복원하지 않는다.

```powershell
$backupLedger = ".\backups\paper-YYYYMMDD-HHMMSS\paper.sqlite3"
$verifyRoot = ".\restore-verify"
if (-not (Test-Path -LiteralPath $verifyRoot)) {
    New-Item -ItemType Directory -Path $verifyRoot -ErrorAction Stop | Out-Null
}
$verifyDir = Join-Path $verifyRoot ("attempt-" + [guid]::NewGuid().ToString("N"))
if (Test-Path -LiteralPath $verifyDir) {
    throw "restore verification directory already exists: $verifyDir"
}
New-Item -ItemType Directory -Path $verifyDir -ErrorAction Stop | Out-Null
if (@(Get-ChildItem -LiteralPath $verifyDir -Force).Count -ne 0) {
    throw "restore verification directory is not empty: $verifyDir"
}
$verifyLedger = Join-Path $verifyDir "paper.sqlite3"
Copy-Item -LiteralPath $backupLedger -Destination $verifyLedger
foreach ($suffix in "-wal") {
    if (Test-Path -LiteralPath "$backupLedger$suffix") {
        Copy-Item -LiteralPath "$backupLedger$suffix" -Destination "$verifyLedger$suffix"
    }
}
Get-ChildItem -LiteralPath $verifyDir | Get-FileHash -Algorithm SHA256
& ".\.venv\Scripts\python.exe" -m autobit.cli paper-status --db $verifyLedger
```

복원 후보가 읽히고 `market`이 `KRW-BTC`, `mode`가 `normalized-paper`, 상태와 보유 수량이 예상과 일치할 때만 사용 후보가 된다. 실제 경로를 교체하는 일도 모든 paper 프로세스를 멈춘 상태에서 동일한 DB/companion 묶음으로만 수행한다. 검증하지 않은 복원본으로 `paper-once`나 `paper-run`을 실행하지 않는다.

## 손상·불일치 감지

다음 명령이 `paper-status failed: INVALID_OR_MISSING_LEDGER`로 non-zero 종료하면 원장은 손상되었거나 검증할 수 없다. 이 명령은 읽기 전용이며 원장을 고치지 않는다.

```powershell
& ".\.venv\Scripts\python.exe" -m autobit.cli paper-status --db ".\data\paper\paper.sqlite3"
```

즉시 `paper-run`을 멈추고 원본 DB와 companion 파일을 보존한다. 파일을 편집하거나 새 빈 DB로 대체하지 말고, 위의 검증된 백업 묶음을 별도 위치에서 먼저 확인한다. 복원 검증이 실패하면 해당 원장은 사용하지 않고 원인 조사가 끝날 때까지 모의매매도 재개하지 않는다.

## 공개 API 장애와 오래된 캔들

- 공개 캔들 요청이 세 번 연속 실패하면 health stage가 `HALTED`가 된다. 실패한 요청 동안 기존 stop evidence는 SQLite에 그대로 보존되고 안전한 재시도는 계속하지만, 유효한 확정 공개 캔들이 없으므로 캔들 의존적인 보호 stop 평가는 지연되며 체결될 수 없다. 유효한 완결봉을 다시 읽은 뒤에는 `HALTED`가 신규 진입만 막고, 기존 포지션의 보호 처리·stop 평가는 계속한다. `paper-run`은 제한된 지수 백오프로 재시도한다.
- 종료된 4시간봉이 종료 뒤 10분이 지나도 없으면 `STALE_CANDLE`로 처리한다. 새 진입은 하지 않으며, 최신 완결봉이 확인될 때까지 임의의 진행봉을 사용하지 않는다.
- `paper-status`의 `health_stage`, `breaker_health_reasons`, `health_recovery_progress`, `pending_orders`, `active_stop`를 기록한다. API 장애나 stale candle 동안 상태 파일을 수동 수정하거나 수동 재개 명령을 시도하지 않는다.

자동 복구에는 정상 공개 API 응답 3회, 미확인 주문 0건, 원장 잔고 대조 일치, 유효한 최신 완결봉·스키마·타임스탬프, 그리고 configured 5% 이내의 유효한 fill reconciliation이 필요하다. 즉 **모든** health reason이 해소되어야 하며 `FILL_DEVIATION`이 남아 있으면 앞 조건이 충족되어도 복구하지 않는다. 운영자는 `paper-status`의 `breaker_health_reasons`와 `health_recovery_progress`에서 남은 gate와 성공 횟수를 확인한다. 모두 충족되면 사람의 resume gate 없이 `HALTED`에서 `REDUCED`로 간다. 축소 상태의 정상 복구 사이클이 완료되고 다른 health 이유가 없으면 자동으로 `NORMAL`로 승격된다. 축소 상태에서는 정상 크기보다 작은 risk/exposure가 적용되며, ATR 비율이 높으면 position sizer도 수량을 추가로 줄인다.

## 증거와 보고서 위치

- 모의 계좌·주문·fill·health·risk·cycle evidence: `data\paper\paper.sqlite3` (및 같은 시점의 `-wal`; `-shm`는 durable evidence가 아님)
- 공개 캔들 수집 증거: `data\raw\paper\KRW-BTC-240\<collection-id>\`의 raw 페이지, checkpoint, collection snapshot/manifest
- 정제 데이터와 품질 보고: `data\processed\...\processed.csv`, `quality.json`
- 백테스트/Walk-forward 보고: 각 실행의 `reports\...\` 결과 폴더와 manifest
- 결정적 종단간 인수 증거: `tests\integration\test_paper_acceptance.py`와 `tests\fixtures\paper_acceptance.json`

인수 시나리오는 하나의 진입, 같은 SQLite 원장 재시작, 한 번의 API 장애, 자동 `HALTED → REDUCED → NORMAL`, 고변동성 수량 축소, 하나의 hard stop, 최종 `FLAT`, 중복 주문·음수 현금·초과 매도 0건, 재생 상태 동일성을 확인한다. 실행 명령은 다음과 같다.

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests/integration/test_paper_acceptance.py -v
```

## 모의매매 졸업 기준

백테스트/Walk-forward 판정이 `PASS`여도 최소 **2주와 30회 거래를 모두** 충족할 때까지 모의매매를 계속한다. 이 기간의 수수료·슬리피지 포함 성과와 안전 증거가 OOS 기대 범위에서 25%를 넘게 악화하면 원인 분석 전에는 다음 단계로 진행하지 않는다. `PASS`와 이 관찰 기간 완료는 지속적인 paper evaluation의 조건일 뿐이며, 실거래를 활성화하거나 매수를 권하는 근거가 될 수 없다.
